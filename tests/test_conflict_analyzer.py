import asyncio
import unittest
from datetime import datetime, timedelta, timezone

from pyrogram.enums import ParseMode
from pyrogram.parser import Parser

from plugin_support import bot, load_plugin


module = load_plugin("conflict_analyzer")


class ConflictAnalyzerTests(unittest.TestCase):
    def records(self, count=30, text_size=4000):
        date = datetime(2026, 1, 1, tzinfo=timezone.utc)
        return [
            module.ChatRecord(mid, date + timedelta(seconds=mid), "user:1", "测试", "文" * text_size)
            for mid in range(1, count + 1)
        ]

    def test_input_limit_tracks_actual_records_and_anchor(self):
        records = self.records()
        result = module._format_records(records, 30)
        self.assertTrue(result.truncated)
        self.assertLessEqual(len(result.text), module.MAX_INPUT_CHARS)
        self.assertLess(len(result.records), len(records))
        self.assertIn(30, [item.message_id for item in result.records])
        self.assertEqual(result.text.count("[MID:"), len(result.records))
        omitted = {item.message_id for item in records} - {
            item.message_id for item in result.records
        }
        selected = module._select_relevant_records(
            result.records, {"is_conflict": True, "relevant_message_ids": list(omitted)}, 30
        )
        self.assertFalse(omitted & {item.message_id for item in selected})

    def test_short_input_and_single_message_truncation(self):
        records = self.records(count=2, text_size=100)
        result = module._format_records(records, 1)
        self.assertFalse(result.truncated)
        self.assertEqual(len(result.records), 2)
        result = module._format_records(self.records(count=1, text_size=5000), 1)
        self.assertTrue(result.truncated)
        self.assertIn("单条消息已截断", result.text)

    def test_no_conflict_after_new_context_skips_http(self):
        async def check():
            formatted = module._format_records(self.records(count=2, text_size=100), 1)
            selection = {"is_conflict": False, "relevant_message_ids": [], "selection_note": "属于普通讨论"}
            # 关闭真实 HTTP 客户端，验证无冲突结果无需任何请求。
            await module._http_client.aclose()
            report = await module._generate_report(
                selection, formatted, False, False, "", "http://127.0.0.1:1", "test"
            )
            self.assertIn("未识别到", report)
            self.assertIn("属于普通讨论", report)
        asyncio.run(check())

    def test_invalid_conflict_flag_is_rejected(self):
        async def check():
            formatted = module._format_records([], 1)
            with self.assertRaisesRegex(ValueError, "布尔值"):
                await module._generate_report(
                    {"is_conflict": "false"}, formatted, False, False,
                    "", "http://127.0.0.1:1", "test",
                )
        asyncio.run(check())

    def test_selection_and_window_parameters(self):
        records = self.records(count=3, text_size=100)
        self.assertEqual(
            module._select_relevant_records(
                records, {"is_conflict": False, "relevant_message_ids": []}, 1
            ),
            [],
        )
        spec, link, users = module._parse_args(["7d", "-u", "@alice,@bob"])
        self.assertEqual(spec.seconds, 7 * 86400)
        self.assertEqual(users, ["@alice", "@bob"])
        self.assertIsNone(link)
        self.assertEqual(
            module._parse_message_link("https://t.me/c/1234567890/25/42"),
            (-1001234567890, 42, 25),
        )

    def test_long_report_with_real_telegram_parser(self):
        async def check():
            parts = module._split_html_report(
                "<blockquote>报告</blockquote>\n",
                "**内容** & <文字> 😀\n" * 1000,
            )
            self.assertGreater(len(parts), 1)
            parser = Parser(bot)
            for part in parts:
                self.assertLessEqual(module._telegram_length(part), 4096)
                parsed = await parser.parse(part, ParseMode.HTML)
                self.assertTrue(parsed["message"])
        asyncio.run(check())
