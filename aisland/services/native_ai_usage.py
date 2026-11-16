"""Read the visible billing pages of the stock Windows desktop clients."""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from unicodedata import normalize
from urllib.parse import urlsplit

from .ai_usage import Metric

_EXECUTABLES = {"workbuddy": "workbuddy.exe", "deepseek_harness": "deepseek harness.exe"}
_ACCOUNT_SECTIONS = ("账号与余额", "Account")
_BALANCE_LABELS = (("充值余额", "赠金余额", "更多"),
                   ("Topped-up balance", "Granted balance", "More"))
_NUMBER = r"(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?"
_POINTS = re.compile(rf"-?{_NUMBER}")
_WALLET = re.compile(rf"([<-]?)\s*([¥$])\s*({_NUMBER})")
_RPC_E_CHANGED_MODE = -2147417850


def _points_after(rows: list[str], label: str) -> str | None:
    if rows.count(label) != 1:
        return None
    index = rows.index(label) + 1
    if index < len(rows) and _POINTS.fullmatch(rows[index]):
        return rows[index]
    return None


def workbuddy_metrics(rows: list[str]) -> list[Metric] | None:
    """Read the remaining amounts from the summary, above the used/total rows."""
    try:
        start = rows.index("总剩余积分")
        end = rows.index("积分明细", start + 1)
    except ValueError:
        return None
    if start == 0 or not _POINTS.fullmatch(rows[start - 1]):
        return None
    summary = rows[start + 1:end]
    base = _points_after(summary, "套餐基础积分")
    reward = _points_after(summary, "平台奖励积分")
    if base is None and reward is None:
        return None
    return [Metric(label, value if value is not None else "—", None,
                   "剩余积分" if value is not None else "本页未显示")
            for label, value in (("套餐基础积分", base), ("奖励积分", reward))]


def _wallet_amount(rows: list[str]) -> str | None:
    if not 1 <= len(rows) <= 2:
        return None
    amounts = {}
    for row in rows:
        match = _WALLET.fullmatch(row)
        if not match:
            return None
        sign, currency, amount = match.groups()
        if currency in amounts:
            return None
        amounts[currency] = f"{sign}{currency}{amount}"
    return " · ".join(amounts.values())


def harness_metrics(rows: list[str]) -> list[Metric] | None:
    """Keep the account's displayed currencies and separate promotional wallets."""
    for recharge, gift, more in _BALANCE_LABELS:
        if rows.count(recharge) != 1 or rows.count(more) != 1:
            continue
        start, end = rows.index(recharge) + 1, rows.index(more)
        if start >= end:
            continue
        body = rows[start:end]
        bonus = None
        if gift in body:
            split = body.index(gift)
            bonus = _wallet_amount(body[split + 1:])
            body = body[:split]
        amount = _wallet_amount(body)
        if amount is None and bonus is None:
            continue
        metrics = [Metric("充值余额", amount or "—", None,
                          "DeepSeek 账户" if amount is not None else "本页未显示")]
        if bonus is not None:
            metrics.append(Metric("赠金余额", bonus, None, "DeepSeek 账户"))
        return metrics
    return None


def _usage_document(provider: str, url: object) -> bool:
    if not isinstance(url, str):
        return False
    try:
        page = urlsplit(url)
    except ValueError:
        return False
    if provider == "workbuddy":
        return (page.scheme == "https" and page.netloc.casefold() == "www.workbuddy.cn"
                and page.path.rstrip("/") == "/profile/credits-usage")
    return page.scheme == "dsh-app" and page.netloc == "app"


def _elements(array, limit: int):
    for index in range(min(array.Length, limit)):
        yield array.GetElement(index)


def usage_page_order(provider: str, urls: list[object]) -> tuple[int, ...]:
    """Indices of the documents worth reading, strict URL matches first.

    Newer WorkBuddy builds keep the billing panel inside the local renderer
    (file:// / hash route), so the remote credits URL is no longer present.
    When that happens, fall back to every document of the WorkBuddy window
    and let the summary anchors decide whether it is the credits panel.
    """
    strict = [index for index, url in enumerate(urls) if _usage_document(provider, url)]
    if provider == "workbuddy" and not strict:
        return tuple(range(len(urls)))
    return tuple(strict)


def _scopes(window, provider: str, automation, library):
    descendants = library.TreeScope_Descendants
    document = automation.CreatePropertyCondition(library.UIA_ControlTypePropertyId,
                                                  library.UIA_DocumentControlTypeId)
    pages = list(_elements(window.FindAll(library.TreeScope_Subtree, document), 12))
    urls = [page.GetCurrentPropertyValue(library.UIA_ValueValuePropertyId) for page in pages]
    for index in usage_page_order(provider, urls):
        page = pages[index]
        # A minimized or background WorkBuddy keeps the credit page loaded; read
        # it anyway so the numbers are not lost just because nobody looks at it.
        if provider == "workbuddy":
            yield page
            continue
        if page.CurrentIsOffscreen:
            continue
        chinese, english = (automation.CreatePropertyCondition(library.UIA_NamePropertyId, name)
                            for name in _ACCOUNT_SECTIONS)
        region = automation.CreatePropertyCondition(library.UIA_AriaRolePropertyId, "region")
        account = automation.CreateAndCondition(region, automation.CreateOrCondition(chinese, english))
        for section in _elements(page.FindAll(descendants, account), 2):
            if not section.CurrentIsOffscreen:
                yield section


def _read_windows(provider: str, handles: list[int], client, com_error) -> list[Metric] | None:
    library = client.GetModule(str(Path(os.environ["SystemRoot"]) / "System32/UIAutomationCore.dll"))
    automation = client.CreateObject(library.CUIAutomation8, interface=library.IUIAutomation2)
    automation.AutoSetFocus = False
    automation.ConnectionTimeout = 600
    automation.TransactionTimeout = 600
    text = automation.CreatePropertyCondition(library.UIA_ControlTypePropertyId,
                                              library.UIA_TextControlTypeId)
    request = automation.CreateCacheRequest()
    request.AddProperty(library.UIA_NamePropertyId)
    parse = workbuddy_metrics if provider == "workbuddy" else harness_metrics
    deadline = time.monotonic() + 3
    for hwnd in handles:
        if time.monotonic() >= deadline:
            break
        try:
            window = automation.ElementFromHandle(hwnd)
            for scope in _scopes(window, provider, automation, library):
                nodes = scope.FindAllBuildCache(library.TreeScope_Descendants, text, request)
                rows = [normalize("NFKC", node.CachedName or "").strip()
                        for node in _elements(nodes, 160)]
                metrics = parse([row for row in rows if row])
                if metrics is not None:
                    return metrics
        except com_error:  # A window can close or change provider during a read.
            continue
    return None


def _live_handles(handles: list[int]) -> list[int]:
    """Skip vanished handles; the worker process bounds calls that still hang."""
    import ctypes

    try:
        user32 = ctypes.windll.user32
    except AttributeError:                 # 非 Windows（仅测试环境）不做过滤
        return list(handles)
    from ctypes import wintypes
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL
    return [hwnd for hwnd in handles if hwnd and user32.IsWindow(hwnd)]


def read_native_usage(provider: str) -> list[Metric] | None:
    from .native_ai_worker import read_isolated_usage

    return read_isolated_usage(provider)


def _read_native_usage_local(provider: str) -> list[Metric] | None:
    """Runs only in the disposable worker, never in the application's thread pool."""
    from .system import chromium_renderer_windows, visible_app_windows

    # The billing panel stays loaded while the client is minimized; excluding
    # iconic windows here would leave the last reading frozen for hours.
    handles = visible_app_windows(_EXECUTABLES[provider], include_minimized=True)
    if not handles:
        return None
    if provider == "workbuddy":
        handles = [target for hwnd in handles for target in (*chromium_renderer_windows(hwnd), hwnd)]
    handles = _live_handles(handles)
    if not handles:
        return None

    import comtypes
    import comtypes.client

    try:
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        except OSError as error:
            if error.winerror != _RPC_E_CHANGED_MODE:
                raise
            # The first import of comtypes can already have initialized this worker.
            comtypes.CoInitialize()
        try:
            return _read_windows(provider, handles, comtypes.client, comtypes.COMError)
        finally:
            comtypes.CoUninitialize()
    except (OSError, comtypes.COMError):
        return None
