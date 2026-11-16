"""Shared settings for text translation, subtitle translation and lookups."""
from __future__ import annotations

import base64
import ctypes
import json
import os
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID


@dataclass(frozen=True)
class Provider:
    """一家 OpenAI 兼容的模型服务。

    ``temperature`` 为 None 表示不发这个字段（部分新模型只接受默认值）；
    ``token_field`` 让少数服务改用 max_completion_tokens；
    ``aliases`` 收同一家的其他域名写法，配置过的 Key 不会因为换写法而失效。
    """

    name: str
    base_url: str
    model: str
    options: dict = field(default_factory=dict)
    temperature: float | None = 1.0
    token_field: str = "max_tokens"
    stream_usage: bool = False
    aliases: tuple[str, ...] = ()


# 默认模型取各家「快而便宜」的档位：翻译是高频短请求，旗舰模型又慢又贵。
PROVIDERS = (
    Provider("DeepSeek", "https://api.deepseek.com", "deepseek-flash",
             {"thinking": {"type": "disabled"}}, stream_usage=True),
    Provider("Kimi（月之暗面）", "https://api.moonshot.cn/v1", "kimi-k2.5"),
    Provider("通义千问（阿里云百炼）", "https://dashscope.aliyuncs.com/compatible-mode/v1",
             "qwen3.6-plus", {"enable_thinking": False}),
    Provider("智谱 GLM", "https://open.bigmodel.cn/api/paas/v4", "glm-4.7-flash",
             {"thinking": {"type": "disabled"}}),
    Provider("MiniMax", "https://api.minimaxi.com/v1", "MiniMax-M2.5",
             aliases=("https://api.minimax.io/v1",)),
    Provider("豆包（火山方舟）", "https://ark.cn-beijing.volces.com/api/v3", "doubao-seed-2.0-lite"),
    Provider("混元（腾讯云）", "https://api.hunyuan.cloud.tencent.com/v1", "hunyuan-turbo-s"),
    Provider("文心一言（百度千帆）", "https://qianfan.baidubce.com/v2", "ernie-4.5-turbo-128k"),
    Provider("讯飞星火", "https://spark-api-open.xf-yun.com/v1", "spark-v4.0-ultra"),
    Provider("阶跃星辰 StepFun", "https://api.stepfun.com/v1", "step-3.5-flash"),
    Provider("OpenAI（ChatGPT）", "https://api.openai.com/v1", "gpt-5.4-mini",
             temperature=None, token_field="max_completion_tokens"),
    Provider("Anthropic Claude", "https://api.anthropic.com/v1", "claude-haiku-5-5", temperature=None),
    Provider("Google Gemini", "https://generativelanguage.googleapis.com/v1beta/openai", "gemini-3.8-flash"),
    Provider("xAI Grok", "https://api.x.ai/v1", "grok-4.1-fast"),
    Provider("Mistral", "https://api.mistral.ai/v1", "mistral-small-latest"),
    Provider("OpenRouter（聚合）", "https://openrouter.ai/api/v1", "google/gemini-3.8-flash"),
)
DEFAULT_PROVIDER = PROVIDERS[0]
# 列表末尾的兜底项：任何 OpenAI 兼容服务都能手填地址与模型名。
CUSTOM_PROVIDER = Provider("自定义（OpenAI 兼容）", "", "")
INSTALLATION_MARKER = "translation-install.id"
PROTECTED_KEY = "api_key_protected"


def provider_for(base_url: str) -> Provider | None:
    """按地址找回厂商；末尾斜杠、少写或多写一层 /v1 都算同一家。"""
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return None
    for item in PROVIDERS:
        if base == item.base_url or base + "/v1" == item.base_url or item.base_url + "/v1" == base:
            return item
        if base in item.aliases or base + "/v1" in item.aliases:
            return item
    return None


@dataclass(frozen=True)
class Connection:
    api_key: str = field(repr=False)
    base_url: str
    model: str

    @property
    def provider(self) -> Provider | None:
        return provider_for(self.base_url)

    @property
    def request_options(self) -> dict:
        provider = self.provider
        if provider and (provider is DEFAULT_PROVIDER or self.model == provider.model):
            return provider.options
        return {}

    @property
    def temperature(self) -> float | None:
        provider = self.provider
        return 1.0 if provider is None else provider.temperature

    @property
    def token_field(self) -> str:
        provider = self.provider
        return "max_tokens" if provider is None else provider.token_field

    @property
    def stream_usage(self) -> bool:
        """只有确认支持 stream_options 的厂商才要 usage。

        自定义地址背后可能是任意网关，带上它不认识的字段会直接 400，而 usage
        只用来估算费用，不值得为此赌上能不能翻译。
        """
        provider = self.provider
        return bool(provider and provider.stream_usage)


def resolve_connection(settings: object) -> Connection:
    """Read a complete settings snapshot, honoring the existing optional import."""
    values = settings if isinstance(settings, dict) else {}
    key = str(values.get("api_key") or "").strip()
    base = str(values.get("base_url") or "").strip()
    model = str(values.get("model") or "").strip()
    source = str(values.get("import_from") or "").strip()
    if not key and source:
        try:
            path = Path(os.path.expandvars(os.path.expanduser(source)))
            content = json.loads(path.read_text(encoding="utf-8-sig"))
            imported = content.get("translate", {})
            imported_key = str(imported.get("api_key") or "").strip()
            if imported_key:
                key = imported_key
                base = str(imported.get("base_url") or "").strip() or base
                model = str(imported.get("model") or "").strip() or model
        except (OSError, ValueError, AttributeError):
            pass
    base = (base or DEFAULT_PROVIDER.base_url).rstrip("/")
    provider = provider_for(base)
    if provider is not None and provider.base_url == "https://api.openai.com/v1" and model == "gpt-5.1-mini":
        model = provider.model
    # 自定义地址不猜模型：填空让上层提示用户补上，比静默用别家的模型名好。
    return Connection(key, base, model or (provider.model if provider else ""))


@dataclass(frozen=True)
class Installation:
    id: str
    upgrade: bool


def read_installation(root: Path) -> Installation | None:
    try:
        mode, identity = (root / INSTALLATION_MARKER).read_text(encoding="ascii").strip().split(":", 1)
        if mode in ("fresh", "upgrade"):
            return Installation(str(UUID(identity)), mode == "upgrade")
    except (OSError, ValueError):
        pass
    return None


def bind_installation(settings: dict, install: Installation) -> bool:
    """Tie credentials to this installation while leaving unrelated profile data alone."""
    previous = settings.get("install_id")
    if previous == install.id:
        return False
    if previous or not install.upgrade:
        settings.update(api_key="", import_from="", use_bundled_defaults=False)
        settings.pop(PROTECTED_KEY, None)
    settings["install_id"] = install.id
    return True


class _DataBlob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.c_void_p)]


ENTROPY_PREFIX = "Aisland translation\0"


def _entropy_scope(base_url: str) -> bytes:
    """Canonical entropy so `…/v1` and `…` decrypt the same stored key."""
    base = (base_url or "").strip().rstrip("/")
    provider = provider_for(base)
    return (ENTROPY_PREFIX + (provider.base_url if provider else base)).encode("utf-8")


def _legacy_scope(base_url: str) -> bytes:
    """Entropy used before the endpoint was normalized; needed to read old blobs."""
    return (ENTROPY_PREFIX + (base_url or "").strip().rstrip("/")).encode("utf-8")


def _scopes(base_url: str) -> list[bytes]:
    """Current scope first, then the variants older builds could have written."""
    base = (base_url or "").strip().rstrip("/")
    provider = provider_for(base)
    candidates = [_entropy_scope(base)]
    if provider is None:
        return candidates  # A custom gateway may serve a different service on each path.
    for text in (base, base + "/v1", provider.base_url):
        if text:
            candidates.append(_legacy_scope(text))
    unique: list[bytes] = []
    for item in candidates:
        if item not in unique:
            unique.append(item)
    return unique


def _dpapi(value: bytes, entropy_bytes: bytes, *, unprotect: bool = False) -> bytes:
    # Windows binds the encrypted value to this user. Never allow a DPAPI prompt.
    system32 = 0x00000800
    crypt = ctypes.WinDLL("crypt32", winmode=system32, use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", winmode=system32)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    transform = crypt.CryptUnprotectData if unprotect else crypt.CryptProtectData
    transform.argtypes = [ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.POINTER(_DataBlob),
                          ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob)]
    transform.restype = wintypes.BOOL
    buffer = ctypes.create_string_buffer(value)
    scope = ctypes.create_string_buffer(entropy_bytes)
    entropy = _DataBlob(len(entropy_bytes), ctypes.addressof(scope))
    src, dst = _DataBlob(len(value), ctypes.addressof(buffer)), _DataBlob()
    try:
        if not transform(ctypes.byref(src), None, ctypes.byref(entropy), None, None, 1, ctypes.byref(dst)):
            raise ctypes.WinError(ctypes.get_last_error())
        return ctypes.string_at(dst.data, dst.size)
    finally:
        ctypes.memset(ctypes.addressof(buffer), 0, len(buffer))
        if dst.data:
            ctypes.memset(dst.data, 0, dst.size)
            kernel.LocalFree(dst.data)


def protect_key(key: str, base_url: str) -> str:
    return base64.b64encode(_dpapi(key.encode("utf-8"), _entropy_scope(base_url))).decode("ascii")


def unprotect_key(value: str, base_url: str) -> str:
    """Decrypt with the current scope, then with the variants older builds used."""
    blob = base64.b64decode(value, validate=True)
    failure: OSError | None = None
    for scope in _scopes(base_url):
        try:
            return _dpapi(blob, scope, unprotect=True).decode("utf-8")
        except OSError as error:
            failure = error
    raise failure if failure is not None else ValueError("empty scope")
