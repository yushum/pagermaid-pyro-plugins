import asyncio
import unittest

import httpx
from pyrogram.parser import Parser
from pyrogram.enums import ParseMode

from plugin_support import bot, load_plugin


module = load_plugin("summarize_user")


class SummarizeUserTests(unittest.TestCase):
    def test_endpoint_and_link_parsing(self):
        self.assertEqual(
            module._build_chat_completions_url("https://example.com/v1/"),
            "https://example.com/v1/chat/completions",
        )
        self.assertEqual(
            module._parse_message_link("https://t.me/c/1234567890/25/42"),
            (-1001234567890, 42),
        )
        self.assertEqual(module._normalize_peer_identifier("12345"), 12345)

    def test_response_extraction_and_redaction(self):
        self.assertEqual(
            module._extract_content({"choices": [{"message": {"content": "报告"}}]}),
            "报告",
        )
        self.assertIsNone(module._extract_content({"choices": []}))
        self.assertNotIn(
            "secret-value", module._response_preview({"error": "secret-value"}, "secret-value")
        )

    def test_url_validation(self):
        self.assertTrue(module._valid_api_url("https://example.com/v1"))
        self.assertTrue(module._valid_api_url("http://127.0.0.1:1/v1"))
        self.assertFalse(module._valid_api_url("http://example.com/v1"))

    def test_long_report_with_real_telegram_parser(self):
        async def check():
            parts = module._build_result_messages(
                "<blockquote>报告</blockquote>\n",
                "💬 测试内容",
                ("**内容** `代码` & <文字> 😀\n" * 1000),
            )
            self.assertGreater(len(parts), 1)
            parser = Parser(bot)
            for part in parts:
                parsed = await parser.parse(part, ParseMode.HTML)
                self.assertLessEqual(module._telegram_length(part), 4096)
                self.assertTrue(parsed["message"])
            local = module._build_local_result_message("报告\n", "", "内容" * 5000)
            self.assertIn("已截断", local)
            self.assertLessEqual(module._telegram_length(local), 4096)
        asyncio.run(check())

    def test_map_failure_stops_later_batches_with_real_httpx(self):
        async def check():
            self.assertIsInstance(module._http_client, httpx.AsyncClient)
            requests = []

            async def record_request(request):
                requests.append(request.url)

            module._http_client.event_hooks["request"].append(record_request)
            try:
                # 独立断网容器的端口 1 没有监听服务，触发真实连接失败。
                with self.assertRaisesRegex(Exception, "API 请求失败"):
                    await module._map_reduce_summary(
                        ["文字" * 10000] * 8,
                        "",
                        "http://127.0.0.1:1/v1",
                        "test",
                    )
                self.assertEqual(len(requests), module.MAX_CONCURRENT_REQUESTS)
            finally:
                module._http_client.event_hooks["request"].remove(record_request)
                await module._http_client.aclose()
        asyncio.run(check())

    def test_analysis_chunk_limit(self):
        async def check():
            with self.assertRaisesRegex(Exception, "单次最多允许"):
                await module._map_reduce_summary(
                    ["文字" * 10000] * 21, "", "http://127.0.0.1:1/v1", "test"
                )
        asyncio.run(check())
