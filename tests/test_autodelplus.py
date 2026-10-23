import asyncio
import time
import unittest
from unittest.mock import AsyncMock, patch

from pyrogram import ContinuePropagation
from pyrogram.enums import ChatType
from pyrogram.types import Chat, Message, MessageOriginUser, User
from sqlitedict import SqliteDict

from plugin_support import bot, load_plugin, sqlite
from pagermaid.common.ignore import ignore_groups_manager
from pagermaid.static import read_context


module = load_plugin("autodelplus")


class AutoDeleteTests(unittest.TestCase):
    def setUp(self):
        self.assertIsInstance(sqlite, SqliteDict)
        sqlite.clear()
        module.scheduler.clear_all_jobs()
        self.scheduler = module.scheduler

    def add_job(self, mid=1, source="chat"):
        job = module.DeleteJob(1, -1001234567890, mid, source)
        self.scheduler.add_job(job)
        return job

    def test_expired_jobs_remain_cancellable(self):
        jobs = [self.add_job(mid) for mid in range(1, 202)]
        self.assertEqual(len(self.scheduler._pop_expired(2)), 201)
        self.assertEqual(self.scheduler.remove_jobs(source="chat"), 201)
        for job in jobs:
            self.scheduler._retry_job(job, 10)
        self.assertFalse(self.scheduler.live)
        self.assertFalse(self.scheduler.job_heap)
        self.assertFalse(list(sqlite.keys()))

    def test_reset_prevents_inflight_retry(self):
        job = self.add_job()
        self.scheduler._pop_expired(2)
        sqlite[module._timer_key(0)] = 60
        result = asyncio.run(module._handle_reset("reset confirm"))
        self.assertIn("已清除", result)
        self.scheduler._retry_job(job, 10)
        self.assertFalse(self.scheduler.live)
        self.assertNotIn(module._job_key(job.chat_id, job.message_id), sqlite)

    def test_completion_does_not_remove_replacement(self):
        old = self.add_job()
        self.scheduler._pop_expired(2)
        current = self.add_job()
        self.scheduler._finish_job(old)
        self.scheduler._retry_job(old, 10)
        self.assertIs(self.scheduler.live[current.key], current)
        self.assertEqual(sqlite[module._job_key(*current.key)], current.to_dict())
        self.assertEqual(self.scheduler.chat_counts[current.chat_id], 1)

    def test_retry_and_completion_update_persistent_state(self):
        job = self.add_job()
        self.scheduler._pop_expired(2)
        self.scheduler._retry_job(job, 20)
        self.assertGreaterEqual(job.due_at, int(time.time()) + 19)
        self.assertEqual(self.scheduler.chat_counts[job.chat_id], 1)
        self.assertEqual(sqlite[module._job_key(*job.key)], job.to_dict())
        self.scheduler._finish_job(job)
        self.assertFalse(self.scheduler.live)
        self.assertFalse(self.scheduler.chat_counts)
        self.assertNotIn(module._job_key(*job.key), sqlite)

    def test_exclusion_only_cancels_global_jobs(self):
        global_job = self.add_job(1, "global")
        chat_job = self.add_job(2, "chat")
        self.scheduler._pop_expired(2)
        module._handle_exclude([global_job.chat_id], True)
        self.scheduler._retry_job(global_job, 10)
        self.assertNotIn(global_job.key, self.scheduler.live)
        self.assertIs(self.scheduler.live[chat_job.key], chat_job)

    def test_recovery_uses_real_sqlite(self):
        job = self.add_job()
        restored = module.AutoDeleteScheduler()
        asyncio.run(restored.init())
        self.assertEqual(restored.live[job.key].to_dict(), job.to_dict())
        self.assertEqual(restored.chat_counts[job.chat_id], 1)

    def test_invalid_persisted_job_is_removed(self):
        key = module._job_key(-1001234567890, 42)
        sqlite[key] = {"due_at": "bad", "source": "chat"}
        self.assertEqual(self.scheduler._load_jobs_from_db(), [])
        self.assertNotIn(key, sqlite)

    def test_inline_and_forwarded_filters_use_real_pyrogram(self):
        async def check():
            own = User(id=123, first_name="测试", is_self=True)
            message = Message(
                id=1,
                chat=Chat(id=-1001234567890, type=ChatType.SUPERGROUP),
                from_user=own,
                via_bot=User(id=456, first_name="机器人", is_bot=True),
                outgoing=True,
            )
            self.assertTrue(await module._AUTO_DELETE_FILTER(bot, message))
            message.forward_origin = MessageOriginUser(
                sender_user=User(id=789, first_name="转发来源")
            )
            self.assertTrue(await module._AUTO_DELETE_FILTER(bot, message))
            message.from_user = User(id=789, first_name="其他用户", is_self=False)
            message.outgoing = False
            self.assertFalse(await module._AUTO_DELETE_FILTER(bot, message))
        asyncio.run(check())

    def test_time_validation(self):
        self.assertEqual(module.parse_time("1 h 30 m"), 5400)
        for value in ("0 s", "-1 m", "31 d", "1 m 2 minutes"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                module.parse_time(value)

    def test_auto_delete_allows_later_command_handler(self):
        async def check():
            own = User(id=123, first_name="测试", is_self=True)
            message = Message(
                id=99, chat=Chat(id=123, type=ChatType.PRIVATE),
                from_user=own, outgoing=False, text=",help", client=bot,
            )
            read_context.clear()
            sqlite[module._timer_key(123)] = 60
            callback = AsyncMock()
            from pagermaid.listener import listener
            async def handle_command(message: Message):
                await callback(message)
            command = listener(command="autodel_regression_probe")(handle_command)
            registered = next(
                handler for handler in bot.dispatcher.groups[2]
                if getattr(handler, "user_callback", handler.callback) is module._auto_del_handler
            )
            try:
                # 按真实分发顺序先执行自动删除，再执行普通命令包装器。
                with patch.object(module.scheduler, "ensure_worker"):
                    self.assertTrue(await registered.check(bot, message))
                    with self.assertRaises(ContinuePropagation):
                        await registered.callback(bot, message)
                self.assertNotIn((123, 99), read_context)
                self.assertIn((123, 99), module.scheduler.live)
                message.arguments = ""
                with self.assertRaises(ContinuePropagation):
                    await command.get_handler()(bot, message)
                callback.assert_awaited_once_with(message)
                self.assertNotIn((123, 99), read_context)
            finally:
                read_context.clear()
        asyncio.run(check())

    def test_auto_delete_respects_ignored_chats(self):
        async def check():
            message = Message(id=99, chat=Chat(id=123, type=ChatType.PRIVATE), client=bot)
            sqlite[module._timer_key(123)] = 60
            ignore_groups_manager.add_id(123)
            with patch.object(module.scheduler, "ensure_worker") as worker:
                with self.assertRaises(ContinuePropagation):
                    await module._auto_del_handler(bot, message)
            worker.assert_not_called()
            self.assertNotIn((123, 99), module.scheduler.live)
        asyncio.run(check())
