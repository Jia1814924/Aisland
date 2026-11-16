"""翻译 Key 的 DPAPI 保护：端点写法（带不带 /v1、带不带末尾斜杠）不该让旧密文失效。"""
from __future__ import annotations

import base64
import sys
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from aisland.core import translation_settings as ts

KEY = "sk-test-0123456789"


class EndpointBindingTests(unittest.TestCase):
    def test_slash_and_v1_spellings_open_each_other(self):
        for made, opened in (("https://api.deepseek.com/v1", "https://api.deepseek.com"),
                             ("https://api.deepseek.com", "https://api.deepseek.com/v1"),
                             ("https://api.deepseek.com/v1/", "https://api.deepseek.com")):
            with self.subTest(made=made, opened=opened):
                blob = ts.protect_key(KEY, made)
                self.assertEqual(ts.unprotect_key(blob, opened), KEY)

    def test_known_provider_endpoint_is_canonicalised(self):
        dashed = ts.protect_key(KEY, "https://dashscope.aliyuncs.com/compatible-mode/v1/")
        self.assertEqual(ts.unprotect_key(dashed, "https://dashscope.aliyuncs.com/compatible-mode/v1"),
                         KEY)
        self.assertEqual(ts._entropy_scope("https://dashscope.aliyuncs.com/compatible-mode/v1/"),
                         ts._legacy_scope("https://dashscope.aliyuncs.com/compatible-mode/v1"))

    def test_blob_written_by_the_older_build_still_opens(self):
        """旧版本用未归一的端点当 entropy，升级后不能把用户的 Key 弄丢。"""
        for made, opened in (("https://api.deepseek.com/v1", "https://api.deepseek.com"),
                             ("https://api.deepseek.com", "https://api.deepseek.com/v1")):
            with self.subTest(made=made, opened=opened):
                legacy = base64.b64encode(
                    ts._dpapi(KEY.encode(), ts._legacy_scope(made))).decode("ascii")
                self.assertEqual(ts.unprotect_key(legacy, opened), KEY)

    def test_another_provider_cannot_open_the_blob(self):
        blob = ts.protect_key(KEY, "https://api.deepseek.com")
        with self.assertRaises(OSError):
            ts.unprotect_key(blob, "https://open.bigmodel.cn/api/paas/v4")

    def test_custom_gateway_key_is_bound_to_its_full_path(self):
        blob = ts.protect_key(KEY, "https://gateway.example/v1")
        self.assertEqual(ts.unprotect_key(blob, "https://gateway.example/v1/"), KEY)
        with self.assertRaises(OSError):
            ts.unprotect_key(blob, "https://gateway.example")

    def test_garbage_input_is_rejected(self):
        with self.assertRaises(ValueError):
            ts.unprotect_key("not base64 !!", "https://api.deepseek.com")


class ProviderTableTests(unittest.TestCase):
    """厂商表既是下拉项，也是地址归一和请求参数的唯一来源。"""

    def test_every_provider_is_usable(self):
        for item in ts.PROVIDERS:
            with self.subTest(provider=item.name):
                self.assertTrue(item.base_url.startswith("https://"))
                self.assertFalse(item.base_url.endswith("/"))
                self.assertTrue(item.model)

    def test_endpoints_are_unique(self):
        seen = [item.base_url for item in ts.PROVIDERS] + [a for item in ts.PROVIDERS for a in item.aliases]
        self.assertEqual(len(seen), len(set(seen)))

    def test_requested_vendors_are_present(self):
        names = " ".join(item.name for item in ts.PROVIDERS)
        for vendor in ("Kimi", "MiniMax", "Claude", "ChatGPT", "Gemini", "Grok", "豆包", "混元", "文心"):
            with self.subTest(vendor=vendor):
                self.assertIn(vendor, names)

    def test_lookup_tolerates_spelling_variants(self):
        for item in ts.PROVIDERS:
            with self.subTest(provider=item.name):
                self.assertIs(ts.provider_for(item.base_url), item)
                self.assertIs(ts.provider_for(item.base_url + "/"), item)
        self.assertIs(ts.provider_for("https://api.openai.com"), ts.PROVIDERS[10])
        self.assertIs(ts.provider_for("https://api.deepseek.com/v1"), ts.PROVIDERS[0])
        self.assertIs(ts.provider_for("https://api.minimax.io/v1"), ts.provider_for("https://api.minimaxi.com/v1"))

    def test_custom_endpoint_is_not_guessed(self):
        connection = ts.resolve_connection({"api_key": KEY, "base_url": "https://example.com/v1"})
        self.assertEqual(connection.model, "")
        self.assertIsNone(connection.provider)
        self.assertEqual(connection.request_options, {})
        self.assertEqual(connection.token_field, "max_tokens")
        # 自定义网关未必认 stream_options，带上它可能直接 400，所以不要 usage
        self.assertFalse(connection.stream_usage)

    def test_provider_shapes_the_request(self):
        openai = ts.resolve_connection({"api_key": KEY, "base_url": "https://api.openai.com/v1",
                                        "model": "gpt-5.1-mini"})
        self.assertEqual(openai.model, "gpt-5.4-mini")
        self.assertEqual(ts.resolve_connection({"base_url": openai.base_url}).model, openai.model)
        self.assertEqual(openai.token_field, "max_completion_tokens")
        self.assertIsNone(openai.temperature)      # 新版推理模型只接受默认温度
        self.assertFalse(openai.stream_usage)
        deepseek = ts.Connection(KEY, "https://api.deepseek.com", "deepseek-flash")
        self.assertEqual(deepseek.token_field, "max_tokens")
        self.assertEqual(deepseek.temperature, 1.0)
        self.assertTrue(deepseek.stream_usage)
        self.assertTrue(deepseek.request_options)  # 关掉思考模式

    def test_aliases_share_one_key_blob(self):
        blob = ts.protect_key(KEY, "https://api.minimax.io/v1")
        self.assertEqual(ts.unprotect_key(blob, "https://api.minimaxi.com/v1"), KEY)


if __name__ == "__main__":
    unittest.main()
