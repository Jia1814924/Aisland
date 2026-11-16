# -*- coding: utf-8 -*-
"""文字翻译和查词句共用的流式接口。参考 译声 jpsub/core/translate.py。

信号
  delta(int, str)     (请求 id, 新增片段)
  finished(int, str)  (请求 id, 完整译文)
  failed(int, str)    (请求 id, 友好错误信息)
新请求会让上一个在途请求作废（流读取循环中协作检查，之后不再发出它的信号）。
"""
from __future__ import annotations

import json
import os
import threading
import time

import httpx
from PySide6 import QtCore

from aisland.core.bg import run_bg, ui
from aisland.core.config import DATA_DIR, cfg
from aisland.core.translation_settings import Connection, provider_for, resolve_connection

HISTORY_PATH = DATA_DIR / "translate_history.json"
HISTORY_MAX = 30
TOTAL_DEADLINE_S = 60.0

SYSTEM_PROMPT = (
    "你是专业的多语种翻译。识别原文语种并翻译成简体中文。"
    "原文可能是日语、英语、韩语、法语、德语、俄语、西班牙语、葡萄牙语或其他语言。\n"
    "要求：\n"
    "1. 只输出译文，不加解释、原文、引号或任何前后缀。\n"
    "2. 译文自然、简洁，符合中文表达习惯；保留原文的语气、情绪、敬语层次、时态、否定和疑问。\n"
    "3. 保留原文的段落与换行结构；专有名词、作品名、人名使用中文圈通行译法，没有通行译法时音译或保留原文。\n"
    "4. 网络用语与俚语按语境意译（如 草→笑死，エモい→戳心，推し→本命，やばい 按褒贬译为「绝了/不妙」）。\n"
    "5. 原文省略主语时不擅自补出；句子没说完时保留未完成的语气。\n"
    "6. 如果输入只是一个日语单词或短词组：第一行给出中文释义；第二行写「读音：假名」并附一句简短的补充释义（词性或用法）。"
)

LOOKUP_PROMPT = (
    "你是简明的多语种词句助手。用户消息是要查询的原文，只解释它的语言含义。"
    "识别日语、英语、韩语、法语、德语、俄语、西班牙语、葡萄牙语或其他语言，以简体中文回答。"
    "单词或短语先给常用中文释义，再按需要给读音、词性和简短用法。"
    "句子先给自然中文译文，再用一两句话解释难点、习语或语气。"
    "语种或释义有歧义时简要列出常见解释。用纯文本，最多五行，不用 Markdown。"
)


class _Cancelled(Exception):
    pass


def friendly_error(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        hints = {400: "请求格式错误", 401: "API Key 无效", 402: "模型服务余额不足", 403: "无权限",
                 404: "模型名不对", 422: "请求参数错误", 429: "请求过于频繁", 500: "服务端出错", 503: "服务繁忙"}
        return hints.get(code, f"HTTP {code}")
    if isinstance(e, httpx.TimeoutException):
        return "请求超时"
    if isinstance(e, httpx.ConnectError):
        return "无法连接所选模型服务"
    if isinstance(e, TimeoutError):
        return "翻译超时"
    return str(e)[:160] if isinstance(e, RuntimeError) else "模型服务请求失败"


class TranslateService(QtCore.QObject):
    delta = QtCore.Signal(int, str)
    finished = QtCore.Signal(int, str)
    failed = QtCore.Signal(int, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        ui(lambda: None)  # 确保 bg 回调 invoker 在主线程创建
        self._client: httpx.Client | None = None
        self._client_lock = threading.Lock()
        self._seq = 0
        self._current: dict[str, int] = {}
        self.history: list[dict] = self._load_history()

    def start(self):
        pass

    def stop(self):
        self._current.clear()
        with self._client_lock:
            if self._client is not None:
                try:
                    self._client.close()
                except Exception:  # noqa: BLE001
                    pass
                self._client = None

    # ---------- settings ----------
    def connection(self) -> Connection:
        return resolve_connection(cfg.get("translate"))

    def has_key(self) -> bool:
        return bool(self.connection().api_key)

    def _http(self) -> httpx.Client:
        with self._client_lock:
            if self._client is None or self._client.is_closed:
                self._client = httpx.Client(
                    timeout=httpx.Timeout(30.0, connect=8.0, read=20.0), follow_redirects=True,
                    limits=httpx.Limits(max_connections=4, max_keepalive_connections=4, keepalive_expiry=120))
            return self._client

    # ---------- translate ----------
    def translate(self, text: str) -> int:
        return self._begin(text, "translate")

    def lookup(self, text: str) -> int:
        return self._begin(text, "lookup")

    def _begin(self, text: str, mode: str) -> int:
        self._seq += 1
        rid = self._seq
        self._current[mode] = rid
        text = (text or "").strip()
        if not text:
            QtCore.QTimer.singleShot(0, lambda: self.failed.emit(rid, "没有要翻译的文字"))
            return rid
        run_bg(self._work, rid, text, mode)
        return rid

    def cancel(self, mode: str = "translate"):
        self._current.pop(mode, None)

    def _alive(self, rid: int, mode: str) -> bool:
        return self._current.get(mode) == rid

    def _work(self, rid: int, text: str, mode: str):
        if not self._alive(rid, mode):
            return  # 还没开始就已被更新的请求取代
        try:
            connection = self.connection()
            if not connection.api_key:
                raise RuntimeError("请先设置翻译连接")
            if not connection.model:
                raise RuntimeError("请先在「翻译连接」里填写模型名")
            lookup = mode == "lookup"
            payload = {
                "model": connection.model,
                "messages": [{"role": "system", "content": LOOKUP_PROMPT if lookup else SYSTEM_PROMPT},
                             {"role": "user", "content": text}],
                **connection.request_options,
                "stream": True,
            }
            # 各家对 temperature / 输出上限字段的要求不同，交给 Connection 决定。
            payload[connection.token_field] = 512 if lookup else max(256, min(4000, len(text) * 3 + 200))
            if connection.temperature is not None:
                payload["temperature"] = connection.temperature
            deadline = time.monotonic() + TOTAL_DEADLINE_S
            parts: list[str] = []
            with self._http().stream("POST", connection.base_url + "/chat/completions", json=payload,
                                     headers={"Authorization": f"Bearer {connection.api_key}"},
                                     follow_redirects=provider_for(connection.base_url) is None) as r:
                if not r.is_success:
                    r.read()
                    r.raise_for_status()
                for line in r.iter_lines():
                    if not self._alive(rid, mode):
                        raise _Cancelled()
                    if time.monotonic() > deadline:
                        raise TimeoutError()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except ValueError:
                        continue
                    choices = obj.get("choices") or []
                    piece = str((choices[0].get("delta") or {}).get("content") or "") if choices else ""
                    if piece:
                        parts.append(piece)
                        self.delta.emit(rid, piece)
            out = "".join(parts).strip()
            if not self._alive(rid, mode):
                return
            if not out:
                raise RuntimeError("模型未返回结果")
            self.finished.emit(rid, out)
            if not lookup:
                ui(lambda: self._add_history(text, out))
        except _Cancelled:
            return
        except Exception as e:  # noqa: BLE001
            if self._alive(rid, mode):
                self.failed.emit(rid, friendly_error(e))

    # ---------- history ----------
    def _load_history(self) -> list[dict]:
        try:
            h = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
            return [x for x in h if isinstance(x, dict)][:HISTORY_MAX]
        except Exception:  # noqa: BLE001
            return []

    def _add_history(self, src: str, dst: str):
        self.history = [h for h in self.history if h.get("src") != src]
        self.history.insert(0, {"src": src[:2000], "dst": dst[:4000], "time": time.time()})
        del self.history[HISTORY_MAX:]
        try:
            tmp = HISTORY_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.history, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, HISTORY_PATH)
        except Exception:  # noqa: BLE001
            pass

    def clear_history(self):
        self.history = []
        try:
            HISTORY_PATH.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
