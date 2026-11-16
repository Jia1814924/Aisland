# -*- coding: utf-8 -*-
"""实时字幕的流式翻译。"""
from __future__ import annotations

import json
import time
from typing import Callable, Optional

import httpx

from ..core.translation_settings import provider_for, resolve_connection
from .http_util import make_client

OnDelta = Optional[Callable[[str], None]]

MODELS = {"deepseek-flash": "Flash", "deepseek-v4-pro": "Pro"}
# 高峰价，美元 / 百万 token：(缓存命中输入, 未命中输入, 输出)。非高峰为半价。
PRICES = {"deepseek-flash": (0.006, 0.30, 1.20), "deepseek-v4-pro": (0.044, 1.32, 3.96)}


def usage_cost(model: str, usage: dict) -> float:
    """按高峰价估算一次请求的费用（美元）。"""
    hit, miss, out = PRICES.get(model, PRICES["deepseek-flash"])
    h = int(usage.get("prompt_cache_hit_tokens", 0) or 0)
    m = int(usage.get("prompt_cache_miss_tokens", 0) or 0)
    if not h and not m:
        m = int(usage.get("prompt_tokens", 0) or 0)
    o = int(usage.get("completion_tokens", 0) or 0)
    return (h * hit + m * miss + o * out) / 1e6


class TranslationCancelled(Exception):
    """过时的草稿被取消。"""


class TranslationDeadlineExceeded(TimeoutError):
    pass


class TranslationStream:
    """协作取消 + 总时限。"""
    def __init__(self, callback, cancelled, seconds=45.0, read_timeout=15.0):
        self.callback = callback
        self.cancelled = cancelled
        self.deadline = time.monotonic() + seconds
        self.read_timeout = read_timeout

    def checkpoint(self):
        if self.cancelled():
            raise TranslationCancelled()
        if time.monotonic() >= self.deadline:
            raise TranslationDeadlineExceeded("翻译超时")

    def __call__(self, piece):
        self.checkpoint()
        self.callback(piece)


def _checkpoint(callback):
    if hasattr(callback, "checkpoint"):
        callback.checkpoint()


def _timeout(callback, default):
    read = min(default, float(getattr(callback, "read_timeout", default)))
    return httpx.Timeout(default, connect=min(8.0, default), read=read)


# 固定的系统提示放在最前面，DeepSeek 会命中前缀缓存（输入价格约为原价 2%）
SYSTEM_PROMPT_JA = (
    "你是日本动画的日译中字幕翻译。把用户给出的一句日语台词译成简体中文字幕。\n"
    "要求：\n"
    "1. 只输出译文，不加解释、注音、原文、引号或说话人。\n"
    "2. 口语化、简洁，符合中文字幕习惯；保留语气、情绪、时态、否定和疑问。\n"
    "3. 保留角色口癖与人设语气（如「〜にゃ」→喵、「〜ですわ」大小姐腔、「〜のじゃ」老派口吻、「〜っス」后辈腔），用自然的中文表达，不要抹平。\n"
    "4. 名台词、作品专有名词、招式名使用中文圈通行译法（如「全集中・水の呼吸」→全集中·水之呼吸，「知らない天井だ」→陌生的天花板）。\n"
    "5. 网络用语与俚语按语境意译：草→笑死，エモい→戳心/好有感觉，推し→本命/推，尊い→太神了，ワンチャン→有戏，陰キャ→阴角，"
    "やばい按褒贬选「绝了/不妙」。\n"
    "6. 谐音梗、双关尽量用中文谐音或保留笑点，实在不行就意译。\n"
    "7. 原文省略主语时不擅自补出，句子没说完时保留未完成的语气。\n"
    "8. 上文仅用于理解指代和语气，不要翻译上文。"
)

SYSTEM_PROMPT_EN = (
    "你是影视字幕的英译中翻译。把用户给出的一句英语台词译成简体中文字幕。\n"
    "要求：\n"
    "1. 只输出译文，不加解释、音标、原文、引号或说话人。\n"
    "2. 口语化、简洁，符合中文字幕习惯；不要逐词直译，语序按中文调整。\n"
    "3. 保留语气、情绪、时态、否定和疑问；骂人话、俚语、脏话按中文习惯保留力度，不要文雅化。\n"
    "4. 人名、作品名、专有名词用中文圈通行译法；没有通行译法的保持原文。\n"
    "5. 术语按语境意译：you guys→你们，I mean→我是说，whatever→随便吧，damn it→该死。\n"
    "6. 省略主语时不擅自补出，句子没说完时保留未完成的语气。\n"
    "7. 上文仅用于理解指代和语气，不要翻译上文。"
)

SYSTEM_PROMPT_KO = (
    "你是影视字幕的韩译中翻译。把用户给出的一句韩语台词译成简体中文字幕。\n"
    "要求：\n"
    "1. 只输出译文，不加解释、罗马音、原文、引号或说话人。\n"
    "2. 口语化、简洁，符合中文字幕习惯；不要逐词直译，语序按中文调整。\n"
    "3. 韩语的敬语阶（해요体 / 합니다体）与半语（반말）要用中文语气体现：\n"
    "   对长辈、上司的恭敬用「您」「请」，平辈或亲近关系的随意用日常口语，不要把敬语翻成生硬的书面语。\n"
    "4. 保留语气、情绪、时态、否定和疑问；脏话、叹词（아이씨、진짜、헐）按中文习惯译出力度。\n"
    "5. 称呼（오빠 / 누나 / 형 / 언니）按关系译作「哥」「姐」或直接保留称呼，不要一律译成「哥哥」。\n"
    "6. 人名、作品名、专有名词用中文圈通行译法；没有通行译法的保持原文。\n"
    "7. 省略主语时不擅自补出，句子没说完时保留未完成的语气。\n"
    "8. 上文仅用于理解指代和语气，不要翻译上文。"
)

SYSTEM_PROMPTS = {"ja": SYSTEM_PROMPT_JA, "en": SYSTEM_PROMPT_EN, "ko": SYSTEM_PROMPT_KO}
SOURCE_LANGS = ("ja", "en", "ko")


def prompt_for(language: str) -> str:
    return SYSTEM_PROMPTS.get(str(language or "ja"), SYSTEM_PROMPT_JA)


def build_messages(text: str, context: list[str], prompt: str = SYSTEM_PROMPT_JA) -> list[dict]:
    parts = []
    ctx = [c for c in context if c]
    if ctx:
        parts.append("【上文】\n" + "\n".join(ctx[-3:]))
    parts.append("【台词】\n" + text)
    return [{"role": "system", "content": prompt},
            {"role": "user", "content": "\n\n".join(parts)}]


def friendly_error(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        hints = {401: "API Key 无效", 402: "余额不足", 403: "无权限", 404: "模型名不对",
                 422: "请求参数错误", 429: "请求过于频繁", 500: "服务端出错", 503: "服务繁忙"}
        return hints.get(code, f"HTTP {code}")
    if isinstance(e, httpx.TimeoutException):
        return "请求超时"
    if isinstance(e, httpx.ConnectError):
        return "无法连接所选模型服务"
    return "模型服务请求失败"


_CLIENTS: dict[str, httpx.Client] = {}
_CLIENTS_LOCK = __import__("threading").Lock()


def _client(base: str) -> httpx.Client:
    """复用长连接：省掉每句一次的 TLS 握手。"""
    with _CLIENTS_LOCK:
        c = _CLIENTS.get(base)
        if c is None or c.is_closed:
            c = make_client(base, timeout=30.0,
                            limits=httpx.Limits(max_connections=8, max_keepalive_connections=8,
                                                keepalive_expiry=120))
            _CLIENTS[base] = c
        return c


class Translator:
    def __init__(self, config):
        self.config = config
        self.last_provider = "deepseek"
        self.last_error = ""
        self.last_usage: dict = {}

    @property
    def model(self) -> str:
        return self._connection().model

    def _connection(self):
        connection = self.config.get("translate.connection")
        if connection is not None:
            return connection
        return resolve_connection({key: self.config.get("translate." + key)
                                   for key in ("api_key", "base_url", "model")})

    @property
    def source_lang(self) -> str:
        value = str(self.config.get("translate.source_lang", "") or "ja").lower()
        return value if value in SOURCE_LANGS else "ja"

    def translate(self, text: str, context: list[str], on_delta: OnDelta = None,
                  language: str | None = None, **_ignored) -> str:
        text = (text or "").strip()
        if not text:
            return ""
        connection = self._connection()
        if not connection.api_key:
            raise RuntimeError("请先设置翻译连接")
        if not connection.model:
            raise RuntimeError("请先在「翻译连接」里填写模型名")
        provider = provider_for(connection.base_url)
        payload = {
            "model": connection.model,
            "messages": build_messages(text, context, prompt_for(language or self.source_lang)),
            **connection.request_options,
            "stream": True,
        }
        payload[connection.token_field] = 200
        if connection.temperature is not None:
            payload["temperature"] = connection.temperature
        if connection.stream_usage:
            payload["stream_options"] = {"include_usage": True}
        headers = {"Authorization": f"Bearer {connection.api_key}"}
        parts: list[str] = []
        self.last_usage = {}
        try:
            client = _client(connection.base_url)
            with client.stream("POST", connection.base_url + "/chat/completions", json=payload, headers=headers,
                               follow_redirects=provider is None, timeout=_timeout(on_delta, 30)) as r:
                if not r.is_success:
                    r.read()
                    r.raise_for_status()
                for line in r.iter_lines():
                    _checkpoint(on_delta)
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    obj = json.loads(data)
                    if obj.get("usage"):
                        self.last_usage = obj["usage"]
                    choices = obj.get("choices") or []
                    piece = str((choices[0].get("delta") or {}).get("content") or "") if choices else ""
                    if piece:
                        parts.append(piece)
                        if on_delta:
                            on_delta(piece)
        except (TranslationCancelled, TranslationDeadlineExceeded):
            raise
        except Exception as e:  # noqa: BLE001
            self.last_error = friendly_error(e)
            raise RuntimeError(self.last_error) from e
        out = "".join(parts).strip()
        if not out:
            raise RuntimeError("模型未返回译文")
        self.last_error = ""
        return out


def probe_latency(connection, text: str = "ちょっと待って、そんなの聞いてない", language: str = "ja") -> dict:
    """真发一次请求，量首字与全文耗时（毫秒）。设置里比较厂商快慢用。

    实时字幕的「慢」绝大部分是这里的首字延迟，跟本机识别没关系，
    所以换厂商时先量这一项，比看模型参数有用。
    """
    if not connection.api_key:
        raise RuntimeError("请先填写 API Key")
    if not connection.model:
        raise RuntimeError("请先填写模型名")
    provider = provider_for(connection.base_url)
    payload = {
        "model": connection.model,
        "messages": build_messages(text, [], prompt_for(language)),
        **connection.request_options,
        "stream": True,
    }
    payload[connection.token_field] = 200
    if connection.temperature is not None:
        payload["temperature"] = connection.temperature
    if connection.stream_usage:
        payload["stream_options"] = {"include_usage": True}
    headers = {"Authorization": f"Bearer {connection.api_key}"}
    started = time.monotonic()
    first = None
    parts: list[str] = []
    try:
        client = _client(connection.base_url)
        with client.stream("POST", connection.base_url + "/chat/completions", json=payload, headers=headers,
                           follow_redirects=provider is None, timeout=_timeout(None, 20)) as r:
            if not r.is_success:
                r.read()
                r.raise_for_status()
            for line in r.iter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                obj = json.loads(data)
                choices = obj.get("choices") or []
                piece = str((choices[0].get("delta") or {}).get("content") or "") if choices else ""
                if piece:
                    if first is None:
                        first = time.monotonic() - started
                    parts.append(piece)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(friendly_error(e)) from e
    if first is None:
        raise RuntimeError("模型没有返回内容")
    return {"ttft_ms": int(first * 1000), "total_ms": int((time.monotonic() - started) * 1000),
            "text": "".join(parts).strip()}
