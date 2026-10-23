import asyncio
import inspect
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from plugin_support import load_plugin
from pyrogram import Client, errors, types
from pyrogram.enums import ChatType, MessageEntityType, ParseMode, StickerType
from pyrogram.types import (
    Chat,
    InputMediaPhoto,
    Message,
    MessageEntity,
    Photo,
    User,
    Video,
)

module = load_plugin("linkcopy")


class LinkCopyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = SimpleNamespace()
        # 使用真实客户端方法签名校验参数，避免 mock 掩盖 API 不兼容。
        for name in (
            "get_messages",
            "get_media_group",
            "get_discussion_message",
            "download_media",
            "edit_message_text",
            "delete_messages",
            "send_photo",
            "send_video",
            "send_document",
            "send_audio",
            "send_animation",
            "send_voice",
            "send_sticker",
            "send_video_note",
            "send_media_group",
        ):
            signature = inspect.signature(getattr(Client, name))

            async def call(*args, _signature=signature, **kwargs):
                _signature.bind(self.client, *args, **kwargs)
                return Message(id=100)

            setattr(self.client, name, AsyncMock(side_effect=call))
        self.command = Message(
            id=7,
            chat=Chat(id=-1009876543210, type=ChatType.SUPERGROUP),
            from_user=User(id=1, first_name="self", is_self=True),
            outgoing=True,
            client=self.client,
        )
        self.command.arguments = "https://t.me/example/42"

    def photo(self, mid=42, protected=False, album=None, **kwargs):
        return Message(
            id=mid,
            chat=Chat(id=-1001234567890, type=ChatType.CHANNEL),
            photo=Photo(
                file_id=f"photo-{mid}",
                file_unique_id=f"unique-{mid}",
                width=10,
                height=10,
                file_size=10,
                date=datetime.now(timezone.utc),
            ),
            has_protected_content=protected,
            media_group_id=album,
            client=self.client,
            **kwargs,
        )

    def set_source(self, source):
        self.client.get_messages.side_effect = None
        self.client.get_messages.return_value = source

    async def downloader(self, message, file_name):
        path = Path(file_name)
        path.write_bytes(b"test-media")
        return str(path)

    def reject_first_send(self, name="send_photo"):
        method = getattr(self.client, name)
        send = method.side_effect

        async def reject_then_send(*args, **kwargs):
            if method.await_count == 1:
                raise errors.ChatForwardsRestricted()
            return await send(*args, **kwargs)

        method.side_effect = reject_then_send

    def test_link_formats(self):
        cases = {
            "https://t.me/example/42": ("example", 42),
            "t.me/c/1234567890/42": (-1001234567890, 42),
            "https://telegram.me/c/1234567890/9/42?thread=9": (-1001234567890, 42),
            "https://t.me/example/9/42": ("example", 42),
            "https://t.me/s/example/42?t=30": ("example", 42),
            "tg://resolve?domain=example&post=42": ("example", 42),
            "tg://privatepost?channel=1234567890&post=42": (-1001234567890, 42),
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                result = module.parse_link(url)
                self.assertEqual((result.chat, result.message_id), expected)

    def test_invalid_links(self):
        for url in (
            "https://evil.example/example/42",
            "https://t.me.evil/example/42",
            "https://t.me@example.com/example/42",
            "https://t.me/example",
            "https://t.me/+invite",
            "https://t.me/example/0",
            "https://t.me/c/no/42",
            "https://t.me/example/9/no",
            "https://t.me/example/42?comment=bad",
            "tg://resolve?domain=example&post=42&post=43",
        ):
            with self.subTest(url=url), self.assertRaises(module.LinkCopyError):
                module.parse_link(url)

    def test_hidden_reply_link_and_ambiguity(self):
        reply = Message(
            id=3,
            text="🔗 点这里",
            entities=[
                MessageEntity(
                    type=MessageEntityType.TEXT_LINK,
                    offset=3,
                    length=3,
                    url="https://t.me/example/42?single",
                )
            ],
        )
        self.command.reply_to_message = reply
        self.command.arguments = ""
        link, single = module._request(self.command)
        self.assertEqual(link.message_id, 42)
        self.assertTrue(single)
        reply.text = "https://t.me/example/43"
        with self.assertRaises(module.LinkCopyError):
            module._request(self.command)

    async def test_text_replaces_command_and_preserves_entities_at_limit(self):
        entity = MessageEntity(type=MessageEntityType.BOLD, offset=0, length=3)
        source = Message(id=42, text="文" * 4096, entities=[entity])
        self.set_source(source)
        await module.linkcopy(self.client, self.command)
        args, kwargs = self.client.edit_message_text.call_args
        self.assertEqual(args, (self.command.chat.id, 7, source.text))
        self.assertEqual(kwargs["entities"], [entity])
        self.assertEqual(kwargs["parse_mode"], ParseMode.DISABLED)
        self.client.delete_messages.assert_not_called()

    async def test_cached_photo_keeps_caption_spoiler_reply_and_topic(self):
        source = self.photo(caption="**原样** <字>", has_media_spoiler=True)
        self.set_source(source)
        self.command.message_thread_id = 8
        self.command.reply_to_message_id = 6
        await module.linkcopy(self.client, self.command)
        kwargs = self.client.send_photo.call_args.kwargs
        self.assertEqual(kwargs["photo"], "photo-42")
        self.assertEqual(kwargs["caption"], source.caption)
        self.assertEqual(kwargs["parse_mode"], ParseMode.DISABLED)
        self.assertTrue(kwargs["has_spoiler"])
        self.assertEqual(kwargs["message_thread_id"], 8)
        self.assertEqual(kwargs["reply_to_message_id"], 6)
        self.client.download_media.assert_not_called()
        self.client.delete_messages.assert_awaited_once()

    async def test_protected_photo_reuploads_and_cleans_files(self):
        self.set_source(self.photo(protected=True))
        self.reject_first_send()
        self.client.download_media.side_effect = self.downloader
        await module.linkcopy(self.client, self.command)
        path = self.client.send_photo.call_args.kwargs["photo"]
        self.assertIn("linkcopy-", path)
        self.assertFalse(Path(path).exists())
        self.assertEqual(self.client.send_photo.await_count, 2)
        self.assertEqual(
            self.client.send_photo.call_args_list[0].kwargs["photo"], "photo-42"
        )
        self.client.delete_messages.assert_awaited_once()

    async def test_protected_photo_reuses_file_without_downloading(self):
        for message_protected, chat_protected in (
            (True, False),
            (False, True),
            (True, True),
        ):
            with self.subTest(
                message_protected=message_protected, chat_protected=chat_protected
            ):
                source = self.photo(protected=message_protected)
                source.chat.has_protected_content = chat_protected
                self.set_source(source)
                self.client.send_photo.reset_mock()
                self.client.delete_messages.reset_mock()
                with patch.object(
                    module, "TemporaryDirectory", side_effect=AssertionError("不应下载")
                ):
                    await module.linkcopy(self.client, self.command)
                self.client.send_photo.assert_awaited_once()
                self.assertEqual(
                    self.client.send_photo.call_args.kwargs["photo"], "photo-42"
                )
                self.client.delete_messages.assert_awaited_once()
        self.client.download_media.assert_not_called()
        self.client.edit_message_text.assert_not_called()

    async def test_cached_rejection_falls_back_to_upload(self):
        self.set_source(self.photo())
        self.client.download_media.side_effect = self.downloader
        paths = []

        async def send(**kwargs):
            paths.append(kwargs["photo"])
            if len(paths) == 1:
                raise errors.ChatForwardsRestricted()
            self.assertTrue(Path(paths[-1]).is_file())
            return Message(id=100)

        self.client.send_photo.side_effect = send
        await module.linkcopy(self.client, self.command)
        self.assertEqual(paths[0], "photo-42")
        self.assertFalse(Path(paths[1]).exists())
        self.client.delete_messages.assert_awaited_once()

    async def test_album_preserves_order_entities_and_same_filenames(self):
        entity = MessageEntity(
            type=MessageEntityType.TEXT_LINK,
            offset=0,
            length=2,
            url="https://example.com",
        )
        first = self.photo(
            41, protected=True, album=1, caption="标题", caption_entities=[entity]
        )
        second = self.photo(42, protected=True, album=1)
        self.set_source(second)
        self.client.get_media_group.side_effect = None
        self.client.get_media_group.return_value = [second, first]
        self.reject_first_send("send_media_group")
        self.client.download_media.side_effect = self.downloader
        await module.linkcopy(self.client, self.command)
        media = self.client.send_media_group.call_args.kwargs["media"]
        self.assertEqual(len(media), 2)
        self.assertIsInstance(media[0], InputMediaPhoto)
        self.assertEqual(media[0].caption, "标题")
        self.assertEqual(media[0].caption_entities, [entity])
        self.assertNotEqual(media[0].media, media[1].media)
        self.assertTrue(all(not Path(item.media).exists() for item in media))
        self.assertEqual(self.client.send_media_group.await_count, 2)
        self.client.send_photo.assert_not_called()

    async def test_protected_album_reuses_files_without_downloading(self):
        first = self.photo(41, protected=True, album=1, caption="标题")
        second = self.photo(42, protected=True, album=1)
        self.set_source(second)
        self.client.get_media_group.side_effect = None
        self.client.get_media_group.return_value = [second, first]
        await module.linkcopy(self.client, self.command)
        self.client.send_media_group.assert_awaited_once()
        media = self.client.send_media_group.call_args.kwargs["media"]
        self.assertEqual([item.media for item in media], ["photo-41", "photo-42"])
        self.assertEqual(media[0].caption, "标题")
        self.client.download_media.assert_not_called()
        self.client.delete_messages.assert_awaited_once()

    async def test_single_flag_skips_album(self):
        self.set_source(self.photo(album=1))
        self.command.arguments += " --single"
        await module.linkcopy(self.client, self.command)
        self.client.get_media_group.assert_not_called()
        self.client.send_photo.assert_awaited_once()

    async def test_reply_input_is_kept_and_not_used_as_reply_target(self):
        self.set_source(self.photo())
        self.command.arguments = ""
        self.command.reply_to_message_id = 3
        self.command.reply_to_message = Message(id=3, text="看 https://t.me/example/42")
        await module.linkcopy(self.client, self.command)
        self.assertNotIn("reply_to_message_id", self.client.send_photo.call_args.kwargs)
        self.assertEqual(self.client.delete_messages.call_args.kwargs["message_ids"], 7)

    async def test_comment_link_resolves_discussion(self):
        self.command.arguments += "?comment=123"
        self.client.get_messages.side_effect = [
            Message(id=42),
            Message(id=123, text="评论内容"),
        ]
        self.client.get_discussion_message.side_effect = None
        self.client.get_discussion_message.return_value = Message(
            id=9,
            chat=Chat(id=-1001111111111, type=ChatType.SUPERGROUP),
        )
        await module.linkcopy(self.client, self.command)
        self.assertEqual(self.client.get_messages.call_args.args, (-1001111111111, 123))
        self.assertEqual(self.client.edit_message_text.call_args.args[2], "评论内容")

    async def test_failed_download_keeps_command_and_sends_nothing(self):
        self.set_source(self.photo(protected=True))
        self.reject_first_send()
        self.client.download_media.side_effect = None
        self.client.download_media.return_value = None
        await module.linkcopy(self.client, self.command)
        self.client.send_photo.assert_awaited_once()
        self.client.delete_messages.assert_not_called()
        self.assertIn("媒体下载失败", self.client.edit_message_text.call_args.args[2])

    async def test_upload_timeout_is_not_retried_and_files_are_cleaned(self):
        self.set_source(self.photo(protected=True))
        self.client.download_media.side_effect = self.downloader
        self.client.send_photo.side_effect = [
            errors.ChatForwardsRestricted(),
            TimeoutError(),
        ]
        await module.linkcopy(self.client, self.command)
        self.assertEqual(self.client.send_photo.await_count, 2)
        self.assertFalse(
            Path(self.client.send_photo.call_args.kwargs["photo"]).exists()
        )
        self.client.delete_messages.assert_not_called()
        self.assertIn("是否已收到", self.client.edit_message_text.call_args.args[2])

    async def test_protected_cached_timeout_does_not_download_or_retry(self):
        self.set_source(self.photo(protected=True))
        self.client.send_photo.side_effect = TimeoutError()
        await module.linkcopy(self.client, self.command)
        self.client.send_photo.assert_awaited_once()
        self.client.download_media.assert_not_called()
        self.client.delete_messages.assert_not_called()
        self.assertIn("是否已收到", self.client.edit_message_text.call_args.args[2])

    async def test_file_reference_rejection_falls_back_to_upload(self):
        self.set_source(self.photo(protected=True))
        self.client.send_photo.side_effect = [
            errors.FileReferenceExpired(),
            Message(id=100),
        ]
        self.client.download_media.side_effect = self.downloader
        await module.linkcopy(self.client, self.command)
        self.assertEqual(self.client.send_photo.await_count, 2)
        self.client.download_media.assert_awaited_once()
        self.client.delete_messages.assert_awaited_once()

    async def test_reupload_rejection_does_not_loop(self):
        self.set_source(self.photo(protected=True))
        self.client.send_photo.side_effect = errors.ChatForwardsRestricted()
        self.client.download_media.side_effect = self.downloader
        await module.linkcopy(self.client, self.command)
        self.assertEqual(self.client.send_photo.await_count, 2)
        self.client.download_media.assert_awaited_once()
        self.client.delete_messages.assert_not_called()
        self.assertFalse(
            Path(self.client.send_photo.call_args.kwargs["photo"]).exists()
        )
        self.assertIn(
            "下载重传也未成功", self.client.edit_message_text.call_args.args[2]
        )

    async def test_album_download_failure_sends_no_partial_album(self):
        sources = [
            self.photo(41, protected=True, album=1),
            self.photo(42, protected=True, album=1),
        ]
        self.set_source(sources[0])
        self.client.get_media_group.side_effect = None
        self.client.get_media_group.return_value = sources
        self.reject_first_send("send_media_group")
        paths = []

        async def download(message, file_name):
            if paths:
                raise OSError("disk full")
            paths.append(await self.downloader(message, file_name))
            return paths[-1]

        self.client.download_media.side_effect = download
        await module.linkcopy(self.client, self.command)
        self.assertFalse(Path(paths[0]).exists())
        self.client.send_media_group.assert_awaited_once()
        self.client.delete_messages.assert_not_called()

    async def test_cancelled_transfer_cleans_files_and_active_marker(self):
        self.set_source(self.photo(protected=True))
        self.client.download_media.side_effect = self.downloader
        self.client.send_photo.side_effect = [
            errors.ChatForwardsRestricted(),
            asyncio.CancelledError(),
        ]
        with self.assertRaises(asyncio.CancelledError):
            await module.linkcopy(self.client, self.command)
        self.assertFalse(
            Path(self.client.send_photo.call_args.kwargs["photo"]).exists()
        )
        self.assertFalse(module._active)

    async def test_unreadable_and_unsupported_messages_keep_command(self):
        for source in (Message(id=42, empty=True), Message(id=42)):
            self.set_source(source)
            await module.linkcopy(self.client, self.command)
        self.client.send_photo.assert_not_called()
        self.client.delete_messages.assert_not_called()

    async def test_other_users_cannot_read_private_sources(self):
        self.command.outgoing = False
        self.command.from_user = User(id=2, first_name="sudo", is_self=False)
        await module.linkcopy(self.client, self.command)
        self.client.get_messages.assert_not_called()

    async def test_delete_failure_does_not_resend_content(self):
        self.set_source(self.photo())
        self.client.delete_messages.side_effect = errors.MessageDeleteForbidden()
        await module.linkcopy(self.client, self.command)
        self.client.send_photo.assert_awaited_once()
        self.assertIn("内容已发送", self.client.edit_message_text.call_args.args[2])

    async def test_video_metadata_uses_supported_arguments(self):
        source = Message(
            id=42,
            chat=Chat(id=-1001234567890, type=ChatType.CHANNEL),
            video=Video(
                file_id="video",
                file_unique_id="unique",
                width=640,
                height=480,
                duration=12,
                codec="h264",
                file_name="a.mp4",
                supports_streaming=True,
            ),
        )
        self.set_source(source)
        await module.linkcopy(self.client, self.command)
        kwargs = self.client.send_video.call_args.kwargs
        self.assertEqual(kwargs["duration"], 12)
        self.assertEqual(kwargs["file_name"], "a.mp4")
        self.assertTrue(kwargs["supports_streaming"])

    async def test_other_media_use_the_correct_send_methods(self):
        common = {"file_id": "attachment", "file_unique_id": "unique"}
        media = {
            "audio": types.Audio(**common, duration=12, performer="歌手", title="标题"),
            "document": types.Document(**common, file_name="report.pdf"),
            "animation": types.Animation(**common, width=10, height=10, duration=2),
            "voice": types.Voice(**common, duration=3),
            "video_note": types.VideoNote(**common, length=100, duration=4),
            "sticker": types.Sticker(
                **common,
                type=StickerType.REGULAR,
                width=10,
                height=10,
                is_animated=False,
                is_video=False,
            ),
        }
        for kind, attachment in media.items():
            with self.subTest(kind=kind):
                self.set_source(
                    Message(
                        id=42,
                        chat=self.photo().chat,
                        **{kind: attachment},
                        caption="配文",
                    )
                )
                await module.linkcopy(self.client, self.command)
                method = getattr(self.client, "send_" + kind)
                method.assert_awaited_once()
                self.assertEqual(method.call_args.kwargs[kind], "attachment")
                if kind not in ("video_note", "sticker"):
                    self.assertEqual(method.call_args.kwargs["caption"], "配文")

    async def test_media_group_builds_audio_document_and_video_items(self):
        common = {"file_id": "attachment", "file_unique_id": "unique"}
        for kind, attachment in (
            ("audio", types.Audio(**common, duration=3, title="音频")),
            ("document", types.Document(**common, file_name="a.pdf")),
            (
                "video",
                types.Video(**common, width=10, height=10, duration=2, codec="h264"),
            ),
        ):
            with self.subTest(kind=kind):
                source = Message(id=42, **{kind: attachment}, caption="原配文")
                await module._send_album(
                    self.client, [source, source], {"chat_id": 123}
                )
                items = self.client.send_media_group.call_args.kwargs["media"]
                self.assertEqual(items[0].caption, "原配文")
                self.assertEqual(items[0].parse_mode, ParseMode.DISABLED)

    async def test_download_uses_safe_basename(self):
        source = Message(
            id=42,
            chat=self.photo().chat,
            has_protected_content=True,
            document=types.Document(
                file_id="file", file_unique_id="unique", file_name="../../a.txt"
            ),
        )
        self.set_source(source)
        self.reject_first_send("send_document")
        self.client.download_media.side_effect = self.downloader
        await module.linkcopy(self.client, self.command)
        path = Path(self.client.download_media.call_args.kwargs["file_name"])
        self.assertEqual(path.name, "a.txt")
        self.assertNotIn("..", path.parts)
        self.assertFalse(path.exists())

    async def test_failed_cleanup_status_does_not_report_send_failure(self):
        self.set_source(self.photo())
        self.client.delete_messages.side_effect = errors.MessageDeleteForbidden()
        self.client.edit_message_text.side_effect = OSError()
        await module.linkcopy(self.client, self.command)
        self.client.send_photo.assert_awaited_once()
        self.client.edit_message_text.assert_awaited_once()

    async def test_send_permission_error_does_not_download_or_retry(self):
        self.set_source(self.photo(protected=True))
        self.client.send_photo.side_effect = errors.ChatWriteForbidden()
        await module.linkcopy(self.client, self.command)
        self.client.send_photo.assert_awaited_once()
        self.client.download_media.assert_not_called()
        self.assertIn("目标聊天", self.client.edit_message_text.call_args.args[2])

    async def test_long_floodwait_is_reported_without_waiting(self):
        self.client.get_messages.side_effect = errors.FloodWait(60)
        await module.linkcopy(self.client, self.command)
        self.client.get_messages.assert_awaited_once()
        self.assertIn("60 秒", self.client.edit_message_text.call_args.args[2])

    async def test_short_floodwait_retries_only_rejected_operation(self):
        operation = AsyncMock(side_effect=[errors.FloodWait(2), "ok"])
        with patch.object(module.asyncio, "sleep", new_callable=AsyncMock) as sleep:
            self.assertEqual(await module._rpc(operation), "ok")
        sleep.assert_awaited_once_with(2)
        self.assertEqual(operation.await_count, 2)
