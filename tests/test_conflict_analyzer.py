import asyncio
import importlib.util
import json
import sys
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path


class FakeSqlite(dict):
    pass


class StubAsyncClient:
    def __init__(self, *args, **kwargs):
        pass

    async def aclose(self):
        pass


def load_plugin():
    sqlite = FakeSqlite()
    httpx = types.ModuleType("httpx")
    httpx.AsyncClient = StubAsyncClient
    httpx.Timeout = lambda *args, **kwargs: object()
    httpx.Limits = lambda *args, **kwargs: object()
    httpx.TimeoutException = type("TimeoutException", (Exception,), {})
    httpx.HTTPStatusError = type("HTTPStatusError", (Exception,), {})
    httpx.RequestError = type("RequestError", (Exception,), {})

    pyrogram = types.ModuleType("pyrogram")
    pyrogram_enums = types.ModuleType("pyrogram.enums")
    pyrogram_enums.ChatType = types.SimpleNamespace(PRIVATE="private", BOT="bot")
    pyrogram_enums.ParseMode = types.SimpleNamespace(HTML="html", DISABLED=None)
    pyrogram.enums = pyrogram_enums

    pagermaid = types.ModuleType("pagermaid")
    services = types.ModuleType("pagermaid.services")
    services.sqlite = sqlite
    enums = types.ModuleType("pagermaid.enums")
    enums.Client = object
    enums.Message = object
    listener_module = types.ModuleType("pagermaid.listener")
    registered_commands = []

    def fake_listener(*args, **kwargs):
        registered_commands.append(kwargs.get("command"))
        return lambda func: func

    listener_module.listener = fake_listener

    modules = {
        "httpx": httpx,
        "pyrogram": pyrogram,
        "pyrogram.enums": pyrogram_enums,
        "pagermaid": pagermaid,
        "pagermaid.services": services,
        "pagermaid.enums": enums,
        "pagermaid.listener": listener_module,
    }
    old = {name: sys.modules.get(name) for name in modules}
    sys.modules.update(modules)
    try:
        path = Path(__file__).parents[1] / "conflict_analyzer" / "main.py"
        spec = importlib.util.spec_from_file_location("conflict_analyzer_under_test", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        module._test_registered_commands = registered_commands
        return module
    finally:
        for name, value in old.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


class FakeMessage:
    def __init__(
        self, message_id, date, text, user_id, name, reply_to=None, topic_id=None
    ):
        self.id = message_id
        self.date = date
        self.text = text
        self.caption = None
        self.from_user = types.SimpleNamespace(
            id=user_id, first_name=name, last_name=None, username=None
        )
        self.sender_chat = None
        self.reply_to_message_id = reply_to
        self.reply_to_top_message_id = topic_id
        self.empty = False


class FakeHistoryClient:
    def __init__(self, messages):
        self.messages = {message.id: message for message in messages}

    async def get_messages(self, chat_id, ids, replies=0):
        if not isinstance(ids, list):
            return self.messages.get(ids)
        return [self.messages[mid] for mid in ids if mid in self.messages]

    async def search_messages(self, chat_id, from_user, limit=0):
        for message in sorted(self.messages.values(), key=lambda item: item.id, reverse=True):
            if message.from_user.id == from_user:
                yield message


class ConflictAnalyzerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_plugin()

    @classmethod
    def tearDownClass(cls):
        asyncio.run(cls.module._http_client.aclose())

    def test_parse_count_duration_and_topic_link(self):
        spec, link, users = self.module._parse_args(
            ["-l", "https://t.me/c/123/7/99", "30m", "-u", "@A,@B"]
        )
        self.assertIsNone(spec.count)
        self.assertEqual(spec.seconds, 1800)
        self.assertEqual(link, "https://t.me/c/123/7/99")
        self.assertEqual(users, ["@A", "@B"])
        self.assertEqual(
            self.module._parse_message_link(link),
            (-100123, 99, 7),
        )

        spec, _, _ = self.module._parse_args(["250"])
        self.assertEqual(spec.count, 250)
        spec, _, _ = self.module._parse_args(["30d"])
        self.assertEqual(spec.seconds, 30 * 86400)
        with self.assertRaises(ValueError):
            self.module._parse_args(["10"])
        with self.assertRaises(ValueError):
            self.module._parse_args(["3x"])
        with self.assertRaises(ValueError):
            self.module._parse_args(["31d"])

    def test_registers_short_and_plugin_name_help_commands(self):
        self.assertIn("conflict", self.module._test_registered_commands)
        self.assertIn("conflict_analyzer", self.module._test_registered_commands)

    def test_selection_supports_multiple_users_without_reply_edges(self):
        now = datetime(2026, 1, 1, 12, 0)
        records = [
            self.module.ChatRecord(10, now, "user:1", "A", "规则没写清楚"),
            self.module.ChatRecord(11, now, "user:3", "C", "有人玩游戏吗"),
            self.module.ChatRecord(12, now, "user:2", "B", "你没看公告", None),
            self.module.ChatRecord(13, now, "user:4", "D", "公告昨天改过", 10),
            self.module.ChatRecord(14, now, "user:5", "E", "我看到昨天版本了", None),
        ]
        selection = {"relevant_message_ids": [10, 12, 13, 14, 999, "bad"]}
        selected = self.module._select_relevant_records(records, selection, 10)
        self.assertEqual([record.message_id for record in selected], [10, 12, 13, 14])
        self.assertEqual({record.sender_name for record in selected}, {"A", "B", "D", "E"})

    def test_invalid_too_narrow_selection_falls_back_to_full_window(self):
        now = datetime(2026, 1, 1)
        records = [
            self.module.ChatRecord(mid, now, f"user:{mid}", str(mid), "text")
            for mid in range(1, 4)
        ]
        selected = self.module._select_relevant_records(
            records, {"relevant_message_ids": [999]}, 2
        )
        self.assertEqual([item.message_id for item in selected], [1, 2, 3])

    def test_json_extraction_accepts_fenced_output(self):
        data = self.module._extract_json_object(
            '```json\n{"is_conflict": true, "relevant_message_ids": [1, 2]}\n```'
        )
        self.assertTrue(data["is_conflict"])
        self.assertEqual(data["relevant_message_ids"], [1, 2])

    def test_long_html_report_stays_within_telegram_limit(self):
        chunks = self.module._split_html_report(
            "<blockquote><b>报告</b></blockquote>\n",
            "**分析**\n" + "<&😀>" * 3000,
        )
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(
                self.module._telegram_length(chunk),
                self.module.TG_MSG_CHAR_LIMIT,
            )
            self.assertEqual(chunk.count("<b>"), chunk.count("</b>"))


class ConflictWindowTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_plugin()

    @classmethod
    def tearDownClass(cls):
        asyncio.run(cls.module._http_client.aclose())

    async def test_fetches_both_sides_and_excludes_command_message(self):
        now = datetime(2026, 1, 1, 12, 0)
        messages = [
            FakeMessage(mid, now + timedelta(seconds=mid - 100), f"m{mid}", mid % 4, f"U{mid % 4}")
            for mid in range(80, 121)
        ]
        client = FakeHistoryClient(messages)
        anchor = client.messages[100]
        records, truncated = await self.module._fetch_candidate_window(
            client,
            -1001,
            anchor,
            self.module.WindowSpec(count=20),
            exclude_ids={105},
        )
        ids = [record.message_id for record in records]
        self.assertEqual(len(ids), 20)
        self.assertIn(100, ids)
        self.assertTrue(any(mid < 100 for mid in ids))
        self.assertTrue(any(mid > 100 for mid in ids))
        self.assertNotIn(105, ids)
        self.assertFalse(truncated)

    async def test_duration_filters_messages_outside_window(self):
        now = datetime(2026, 1, 1, 12, 0)
        messages = [
            FakeMessage(90, now - timedelta(minutes=20), "old", 1, "A"),
            FakeMessage(99, now - timedelta(minutes=3), "before", 1, "A"),
            FakeMessage(100, now, "anchor", 2, "B"),
            FakeMessage(101, now + timedelta(minutes=4), "after", 3, "C"),
            FakeMessage(110, now + timedelta(minutes=20), "future", 3, "C"),
        ]
        client = FakeHistoryClient(messages)
        records, _ = await self.module._fetch_candidate_window(
            client,
            -1001,
            client.messages[100],
            self.module.WindowSpec(count=None, seconds=5 * 60),
        )
        self.assertEqual([record.message_id for record in records], [99, 100, 101])

    async def test_recursively_includes_reply_islands_and_their_neighbors(self):
        now = datetime(2026, 1, 1, 12, 0)
        messages = [
            FakeMessage(5, now - timedelta(days=2), "original claim", 1, "A"),
            FakeMessage(9, now - timedelta(hours=1, minutes=1), "nearby context", 3, "C"),
            FakeMessage(10, now - timedelta(hours=1), "earlier claim", 1, "A", reply_to=5),
            FakeMessage(100, now, "anchor", 2, "B", reply_to=10),
            FakeMessage(101, now + timedelta(seconds=1), "follow-up", 1, "A"),
        ]
        client = FakeHistoryClient(messages)
        records = [
            self.module._message_to_record(client.messages[100]),
            self.module._message_to_record(client.messages[101]),
        ]
        records, truncated = await self.module._expand_reply_context(
            client,
            -1001,
            records,
            now,
            7 * 86400,
        )
        self.assertEqual(
            [record.message_id for record in records], [5, 9, 10, 100, 101]
        )
        self.assertTrue(next(item for item in records if item.message_id == 5).context_only)
        rendered = self.module._format_records(records, 100)
        self.assertIn("[回复链节点]", rendered)
        self.assertIn("[上下文岛]", rendered)
        self.assertFalse(truncated)

    async def test_topic_window_excludes_adjacent_other_topic(self):
        now = datetime(2026, 1, 1, 12, 0)
        messages = [
            FakeMessage(99, now, "same topic", 1, "A", topic_id=50),
            FakeMessage(100, now, "anchor", 2, "B", topic_id=50),
            FakeMessage(101, now, "other topic", 3, "C", topic_id=60),
            FakeMessage(102, now, "same topic later", 1, "A", topic_id=50),
        ]
        client = FakeHistoryClient(messages)
        records, _ = await self.module._fetch_candidate_window(
            client,
            -1001,
            client.messages[100],
            self.module.WindowSpec(count=20),
            topic_id=50,
        )
        self.assertEqual([item.message_id for item in records], [99, 100, 102])

    async def test_participant_search_finds_non_reply_messages_across_days(self):
        now = datetime(2026, 1, 10, 12, 0)
        messages = [
            FakeMessage(10, now - timedelta(days=5), "old direct statement", 1, "A"),
            FakeMessage(11, now - timedelta(days=5) + timedelta(seconds=5), "joined in", 3, "C"),
            FakeMessage(100, now, "anchor", 2, "B"),
        ]
        client = FakeHistoryClient(messages)
        records = await self.module._fetch_participant_messages(
            client,
            -1001,
            {1},
            now,
            7 * 86400,
        )
        self.assertEqual([item.message_id for item in records], [10, 11])
        self.assertEqual(records[0].context_kind, "参与者补搜")
        self.assertEqual(records[1].context_kind, "补搜上下文")

    async def test_non_text_media_is_kept_as_missing_content_marker(self):
        now = datetime(2026, 1, 1, 12, 0)
        message = FakeMessage(100, now, None, 1, "A")
        message.voice = object()
        record = self.module._message_to_record(message)
        self.assertEqual(record.text, "[语音消息，未转写]")


if __name__ == "__main__":
    unittest.main()
