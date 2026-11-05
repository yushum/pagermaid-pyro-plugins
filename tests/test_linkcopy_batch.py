import asyncio
import unittest
from pathlib import Path
from unittest.mock import patch

import test_linkcopy
from pyrogram import errors
from pyrogram.enums import MessageEntityType, ParseMode
from pyrogram.types import Message, MessageEntity, Video

module = test_linkcopy.module


class LinkCopyBatchTests(unittest.IsolatedAsyncioTestCase):
    photo = test_linkcopy.LinkCopyTests.photo
    downloader = test_linkcopy.LinkCopyTests.downloader
    reject_first_send = test_linkcopy.LinkCopyTests.reject_first_send

    def setUp(self):
        test_linkcopy.LinkCopyTests.setUp(self)
        delay = patch.object(module, "BATCH_DELAY", 0)
        delay.start()
        self.addCleanup(delay.stop)
        self.assertFalse(module._batches)

    def batch(self, sources, start=42, end=44):
        self.command.arguments = f"https://t.me/example/{start} https://t.me/example/{end}"
        by_id = {source.id: source for source in sources}

        async def fetch(chat, ids):
            self.assertEqual(chat.casefold(), "example")
            return [by_id[mid] for mid in reversed(ids) if mid in by_id]

        self.client.get_messages.side_effect = fetch

    def status(self):
        return self.client.edit_message_text.call_args.args[2]

    def test_range_request_accepts_inclusive_end_and_single(self):
        for separator in (" ", " - ", " 到 "):
            with self.subTest(separator=separator):
                self.command.arguments = (
                    "https://t.me/HGLW889/45916" + separator
                    + "https://t.me/hglw889/45994?single"
                )
                start, single, end = module._request(self.command)
                self.assertEqual((start.message_id, end.message_id), (45916, 45994))
                self.assertTrue(single)

    def test_range_request_rejects_invalid_ranges(self):
        for args, reason in (
            ("https://t.me/example/42 https://t.me/another/44", "同一"),
            ("https://t.me/example/44 https://t.me/example/42", "不能小于"),
            ("https://t.me/example/1 https://t.me/example/1001", "1000"),
            ("https://t.me/example/42?comment=9 https://t.me/example/44", "评论"),
            ("https://t.me/example/42 https://t.me/example/44?comment=9", "评论"),
        ):
            with self.subTest(args=args):
                self.command.arguments = args
                with self.assertRaisesRegex(module.LinkCopyError, reason):
                    module._request(self.command)

    async def test_example_copies_all_79_videos_in_order(self):
        videos = [
            Message(
                id=mid,
                video=Video(file_id=f"video-{mid}", file_unique_id=str(mid),
                            width=10, height=10, duration=2, codec="h264"),
            )
            for mid in range(45916, 45995)
        ]
        self.batch(videos, 45916, 45994)
        await module.linkcopy(self.client, self.command)
        self.assertEqual(
            [call.kwargs["video"] for call in self.client.send_video.call_args_list],
            [f"video-{mid}" for mid in range(45916, 45995)],
        )
        self.client.get_messages.assert_awaited_once_with("example", list(range(45916, 45995)))
        self.assertIn("79/79", self.status())
        self.assertIn("已发送 79 条", self.status())
        self.client.delete_messages.assert_not_called()
        self.assertFalse(module._batches)

    async def test_skips_missing_deleted_and_unsupported_messages(self):
        self.batch([
            self.photo(42),
            Message(id=43, empty=True),
            Message(id=45, service=True),
            Message(id=46),
            self.photo(47),
        ], 42, 47)
        await module.linkcopy(self.client, self.command)
        self.assertEqual(self.client.send_photo.await_count, 2)
        self.assertIn("6/6", self.status())
        self.assertIn("已发送 2 条，跳过 4 条", self.status())

    async def test_text_is_sent_not_edited_and_keeps_reply_and_topic(self):
        entity = MessageEntity(type=MessageEntityType.BOLD, offset=0, length=2)
        self.batch([Message(id=42, text="原文", entities=[entity]), self.photo(43)], 42, 43)
        self.command.message_thread_id = 8
        self.command.reply_to_message_id = 6
        await module.linkcopy(self.client, self.command)
        kwargs = self.client.send_message.call_args.kwargs
        self.assertEqual(kwargs["text"], "原文")
        self.assertEqual(kwargs["entities"], [entity])
        self.assertEqual(kwargs["parse_mode"], ParseMode.DISABLED)
        for method in (self.client.send_message, self.client.send_photo):
            self.assertEqual(method.call_args.kwargs["message_thread_id"], 8)
            self.assertEqual(method.call_args.kwargs["reply_to_message_id"], 6)
        self.assertTrue(all(call.args[2] != "原文" for call in self.client.edit_message_text.call_args_list))

    async def test_albums_are_sorted_and_deduplicated_across_fetch_pages(self):
        sources = [self.photo(mid, album="group") for mid in (42, 43, 44)]
        self.batch(sources, 42, 44)
        self.client.get_media_group.return_value = list(reversed(sources))
        self.client.get_media_group.side_effect = None
        with patch.object(module, "BATCH_FETCH_SIZE", 2):
            await module.linkcopy(self.client, self.command)
        self.client.get_media_group.assert_awaited_once()
        self.client.send_media_group.assert_awaited_once()
        self.assertEqual(
            [item.media for item in self.client.send_media_group.call_args.kwargs["media"]],
            ["photo-42", "photo-43", "photo-44"],
        )
        self.client.send_photo.assert_not_called()
        self.assertIn("已发送 3 条", self.status())

    async def test_album_boundaries_do_not_copy_outside_range(self):
        sources = [self.photo(mid, album="group") for mid in (41, 42, 43, 44, 45)]
        self.batch(sources, 42, 44)
        self.client.get_media_group.side_effect = None
        self.client.get_media_group.return_value = sources
        await module.linkcopy(self.client, self.command)
        media = self.client.send_media_group.call_args.kwargs["media"]
        self.assertEqual([item.media for item in media], ["photo-42", "photo-43", "photo-44"])
        self.assertIn("3/3", self.status())

    async def test_single_member_at_album_boundary_uses_single_send(self):
        sources = [self.photo(mid, album="group") for mid in (41, 42, 43)]
        self.batch(sources, 42, 42)
        self.client.get_media_group.side_effect = None
        self.client.get_media_group.return_value = sources
        await module.linkcopy(self.client, self.command)
        self.client.send_media_group.assert_not_called()
        self.client.send_photo.assert_awaited_once()
        self.assertEqual(self.client.send_photo.call_args.kwargs["photo"], "photo-42")

    async def test_single_flag_sends_album_members_individually(self):
        self.batch([self.photo(mid, album="group") for mid in range(42, 45)])
        self.command.arguments += " --single"
        await module.linkcopy(self.client, self.command)
        self.client.get_media_group.assert_not_called()
        self.client.send_media_group.assert_not_called()
        self.assertEqual(self.client.send_photo.await_count, 3)

    async def test_rejected_protected_media_reuploads_then_continues(self):
        self.batch([self.photo(mid, protected=True) for mid in range(42, 45)])
        self.reject_first_send()
        self.client.download_media.side_effect = self.downloader
        await module.linkcopy(self.client, self.command)
        self.assertEqual(self.client.send_photo.await_count, 4)
        self.assertIn("已发送 3 条", self.status())
        path = self.client.download_media.call_args.kwargs["file_name"]
        self.assertFalse(Path(path).exists())

    async def test_timeout_stops_at_failed_id_without_resending(self):
        self.batch([self.photo(mid) for mid in range(42, 45)])
        self.client.send_photo.side_effect = [Message(id=100), TimeoutError()]
        await module.linkcopy(self.client, self.command)
        self.assertEqual(self.client.send_photo.await_count, 2)
        self.client.download_media.assert_not_called()
        self.assertIn("已发送 1 条", self.status())
        self.assertIn("停止于：https://t.me/example/43", self.status())
        self.assertIn("不要直接重跑", self.status())
        self.assertFalse(module._batches)

    async def test_permission_failure_stops_remainder(self):
        self.batch([self.photo(mid) for mid in range(42, 45)])
        self.client.send_photo.side_effect = errors.ChatWriteForbidden()
        await module.linkcopy(self.client, self.command)
        self.client.send_photo.assert_awaited_once()
        self.assertIn("已发送 0 条", self.status())
        self.assertIn("目标聊天", self.status())
        self.assertFalse(module._batches)

    async def test_missing_text_send_result_stops_remainder(self):
        self.batch([Message(id=42, text="原文"), self.photo(43)], 42, 43)
        self.client.send_message.side_effect = None
        self.client.send_message.return_value = None
        await module.linkcopy(self.client, self.command)
        self.client.send_photo.assert_not_called()
        self.assertIn("没有返回发送结果", self.status())
        self.assertIn("已发送 0 条", self.status())

    async def test_read_failure_preserves_progress(self):
        self.batch([self.photo(mid) for mid in range(42, 45)])
        fetch = self.client.get_messages.side_effect

        async def fail_second_page(chat, ids):
            if ids[0] > 42:
                raise OSError()
            return await fetch(chat, ids)

        self.client.get_messages.side_effect = fail_second_page
        with patch.object(module, "BATCH_FETCH_SIZE", 1):
            await module.linkcopy(self.client, self.command)
        self.client.send_photo.assert_awaited_once()
        self.assertIn("已发送 1 条", self.status())
        self.assertIn("停止于：https://t.me/example/43", self.status())

    async def test_cancel_waits_for_inflight_send_and_offers_next_unsent_id(self):
        self.batch([self.photo(mid) for mid in range(42, 45)])
        original_send = self.client.send_photo.side_effect

        async def send_then_cancel(**kwargs):
            result = await original_send(**kwargs)
            cancel = Message(id=8, chat=self.command.chat, outgoing=True, client=self.client)
            cancel.arguments = "cancel"
            await module.linkcopy(self.client, cancel)
            return result

        self.client.send_photo.side_effect = send_then_cancel
        await module.linkcopy(self.client, self.command)
        self.client.send_photo.assert_awaited_once()
        self.assertIn("批量复制已停止", self.status())
        self.assertIn("已发送 1 条", self.status())
        self.assertIn("https://t.me/example/43", self.status())
        self.assertFalse(module._batches)

    async def test_cancel_after_album_does_not_offer_already_sent_member(self):
        sources = [self.photo(mid, album="group") for mid in (42, 43)]
        self.batch(sources + [self.photo(44)])
        self.client.get_media_group.side_effect = None
        self.client.get_media_group.return_value = sources
        original_send = self.client.send_media_group.side_effect

        async def send_then_stop(**kwargs):
            result = await original_send(**kwargs)
            module._batches[self.command.chat.id].stop.set()
            return result

        self.client.send_media_group.side_effect = send_then_stop
        await module.linkcopy(self.client, self.command)
        self.client.send_photo.assert_not_called()
        self.assertIn("已发送 2 条", self.status())
        self.assertIn("https://t.me/example/44", self.status())

    async def test_cancel_wakes_pacing_delay(self):
        state = module.BatchState(module.MessageLink("example", 1), module.MessageLink("example", 3))
        with patch.object(module, "BATCH_DELAY", 60):
            pause = asyncio.create_task(module._batch_pause(state))
            await asyncio.sleep(0)
            state.stop.set()
            with self.assertRaises(module.BatchStopped):
                await asyncio.wait_for(pause, timeout=1)

    async def test_cancel_without_active_batch_reports_status(self):
        self.command.arguments = "cancel"
        await module.linkcopy(self.client, self.command)
        self.assertIn("没有正在运行", self.status())
        self.client.get_messages.assert_not_called()

    async def test_second_batch_in_same_chat_is_rejected(self):
        self.batch([self.photo(42)])
        state = module.BatchState(module.MessageLink("example", 1), module.MessageLink("example", 3))
        module._batches[self.command.chat.id] = state
        try:
            await module.linkcopy(self.client, self.command)
            self.client.get_messages.assert_not_called()
            self.assertIn("已有批量任务", self.status())
            self.assertIs(module._batches[self.command.chat.id], state)
        finally:
            module._batches.clear()

    async def test_native_task_cancellation_releases_batch_slot(self):
        self.batch([self.photo(42)])
        self.client.send_photo.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await module.linkcopy(self.client, self.command)
        self.assertFalse(module._batches)
        self.assertFalse(module._active)

    async def test_progress_is_periodically_updated(self):
        self.batch([self.photo(mid) for mid in range(42, 45)])
        with patch.object(module, "BATCH_STATUS_INTERVAL", 0):
            await module.linkcopy(self.client, self.command)
        self.assertTrue(any("正在批量复制" in call.args[2]
                            for call in self.client.edit_message_text.call_args_list))
        self.assertIn("批量复制完成", self.status())

    def test_private_resume_link(self):
        link = module.parse_link("https://t.me/c/1234567890/42")
        self.assertEqual(module._resume_link(link, 43), "https://t.me/c/1234567890/43")
