import ast
import inspect
import unittest
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from time import monotonic

from pyrogram import Client, raw
from pyrogram.enums import ChatType
from pyrogram.file_id import FileId, FileType, ThumbnailSource
from pyrogram.types import Audio, Chat, Document, InputMediaVideo, Message, Photo, Thumbnail, Video

from plugin_support import load_plugin

module = load_plugin("linkcopy")


def file_id(kind, media_id):
    return FileId(
        file_type=kind, dc_id=2, media_id=media_id, access_hash=123,
        file_reference=b"reference", volume_id=0, local_id=0,
        thumbnail_source=ThumbnailSource.THUMBNAIL,
        thumbnail_file_type=kind, thumbnail_size="x",
    ).encode()


def photo():
    return Photo(
        file_id=file_id(FileType.PHOTO, 100), file_unique_id="cover",
        width=640, height=360, file_size=1024, date=datetime.now(timezone.utc),
    )


def video_message(cover=None, thumbs=None, timestamp=None):
    return Message(
        id=10, chat=Chat(id=-1001234567890, type=ChatType.CHANNEL),
        video=Video(
            file_id=file_id(FileType.VIDEO, 200), file_unique_id="video",
            width=1920, height=1080, codec="h264", duration=30,
            file_name="video.mp4", mime_type="video/mp4", file_size=1024,
            supports_streaming=True, thumbs=thumbs, video_cover=cover,
            video_start_timestamp=timestamp,
        ),
        has_media_spoiler=True,
    )


def roundtrip(value):
    return raw.core.TLObject.read(BytesIO(value.write()))


class LinkCopyPayloadTests(unittest.TestCase):
    def test_batch_summary_counts_processed_ids_separately(self):
        state = module.BatchState(module.MessageLink("example", 100), module.MessageLink("example", 178))
        state.handled.update(range(100, 179))
        state.sent, state.skipped = 61, 18
        self.assertEqual(state.summary(), "已处理 79/79（100.0%）\n已发送 61 条 · 已跳过 18 条")
        self.assertIsNone(state.next_id)

    def test_incomplete_batch_never_displays_complete(self):
        state = module.BatchState(module.MessageLink("example", 1), module.MessageLink("example", 1000))
        state.handled.update(range(1, 1000))
        self.assertIn("99.9%", state.summary())
        self.assertEqual(state.next_id, 1000)

    def test_transfer_text_includes_size_speed_and_remaining_time(self):
        progress = module.TransferProgress(None, "正在上传：video.mp4", started=10)
        text = progress.text(5 * 1024 * 1024, 10 * 1024 * 1024, now=15)
        self.assertEqual(
            text,
            "正在上传：video.mp4\n50.0% · 5.0/10.0 MiB · 1.00 MiB/s\n预计剩余 0 分 5 秒",
        )

    def test_transfer_start_has_no_speed_or_time_estimate(self):
        text = module.TransferProgress(None, "正在下载", started=10).text(0, 1024, now=10)
        self.assertIn("0.0%", text)
        self.assertNotIn("MiB/s", text)
        self.assertNotIn("剩余", text)

    def test_transfer_with_unknown_total_has_no_percentage(self):
        text = module.TransferProgress(None, "正在下载", started=10).text(1024 * 1024, 0, now=12)
        self.assertIn("已传输 1.0 MiB", text)
        self.assertNotIn("%", text)
        self.assertNotIn("剩余", text)

    def test_completed_transfer_has_no_remaining_time(self):
        text = module.TransferProgress(None, "正在上传", started=10).text(1024, 1024, now=11)
        self.assertIn("100.0%", text)
        self.assertNotIn("剩余", text)

    def test_incomplete_transfer_never_rounds_up_to_complete(self):
        text = module.TransferProgress(None, "正在上传", started=10).text(99999, 100000, now=11)
        self.assertIn("99.9%", text)
        self.assertNotIn("100.0%", text)

    def test_video_metadata_retains_cover_and_zero_start_timestamp(self):
        source = video_message(cover=photo(), timestamp=0)
        metadata = module._metadata(source, "video")
        self.assertEqual(metadata["video_cover"], source.video.video_cover.file_id)
        self.assertEqual(metadata["video_start_timestamp"], 0)
        self.assertTrue(metadata["supports_streaming"])
        self.assertTrue(metadata["has_spoiler"])
        inspect.signature(Client.send_video).bind(None, chat_id=1, video=source.video.file_id, **metadata)
        inspect.signature(InputMediaVideo).bind(media=source.video.file_id, **metadata)

    def test_video_metadata_without_cover_omits_optional_fields(self):
        metadata = module._metadata(video_message(), "video")
        self.assertNotIn("video_cover", metadata)
        self.assertNotIn("video_start_timestamp", metadata)

    def test_preview_selection_retains_largest_thumbnail_and_cover(self):
        small = Thumbnail(
            file_id=file_id(FileType.PHOTO, 101), file_unique_id="small",
            width=90, height=50, file_size=400,
        )
        large = Thumbnail(
            file_id=file_id(FileType.PHOTO, 102), file_unique_id="large",
            width=320, height=180, file_size=1000,
        )
        cover = photo()
        selected = module._preview_sources(video_message(cover=cover, thumbs=[small, large]))
        self.assertIs(selected["thumb"], large)
        self.assertIs(selected["video_cover"], cover)

    def test_missing_previews_do_not_create_a_cover(self):
        self.assertEqual(module._preview_sources(video_message()), {})
        self.assertIsNone(module._video_cover(video_message()))

    def test_cover_file_id_becomes_serializable_input_photo(self):
        reference = roundtrip(module._video_cover(video_message(cover=photo())))
        self.assertIsInstance(reference, raw.types.InputPhoto)
        self.assertEqual(reference.id, 100)
        self.assertEqual(reference.access_hash, 123)
        self.assertEqual(reference.file_reference, b"reference")

    def test_album_video_upload_retains_thumbnail_and_attributes(self):
        file = raw.types.InputFile(id=1, parts=1, name="video.mp4", md5_checksum="")
        thumb = raw.types.InputFile(id=2, parts=1, name="thumb.jpg", md5_checksum="")
        payload = roundtrip(module._album_upload(video_message(), file, thumb, "video.mp4"))
        self.assertEqual(payload.thumb.id, 2)
        self.assertEqual(payload.file.id, 1)
        self.assertTrue(payload.spoiler)
        video = next(item for item in payload.attributes if isinstance(item, raw.types.DocumentAttributeVideo))
        self.assertEqual((video.duration, video.w, video.h), (30, 1920, 1080))
        self.assertTrue(video.supports_streaming)

    def test_album_photo_upload_needs_no_filename_or_thumbnail(self):
        source = Message(id=10, photo=photo(), has_media_spoiler=True)
        file = raw.types.InputFile(id=1, parts=1, name="photo.jpg", md5_checksum="")
        payload = roundtrip(module._album_upload(source, file, None, "photo.jpg"))
        self.assertIsInstance(payload, raw.types.InputMediaUploadedPhoto)
        self.assertTrue(payload.spoiler)

    def test_album_audio_upload_preserves_performer_and_title(self):
        source = Message(id=10, audio=Audio(
            file_id=file_id(FileType.AUDIO, 300), file_unique_id="audio", duration=42,
            performer="Artist", title="Title", mime_type="audio/mpeg",
        ))
        file = raw.types.InputFile(id=1, parts=1, name="audio.mp3", md5_checksum="")
        payload = roundtrip(module._album_upload(source, file, None, "audio.mp3"))
        audio = next(item for item in payload.attributes if isinstance(item, raw.types.DocumentAttributeAudio))
        self.assertEqual((audio.duration, audio.performer, audio.title), (42, "Artist", "Title"))

    def test_album_document_upload_preserves_document_type(self):
        source = Message(id=10, document=Document(
            file_id=file_id(FileType.DOCUMENT, 400), file_unique_id="document", mime_type="application/pdf",
        ))
        file = raw.types.InputFile(id=1, parts=1, name="document.pdf", md5_checksum="")
        payload = roundtrip(module._album_upload(source, file, None, "document.pdf"))
        self.assertTrue(payload.force_file)
        self.assertEqual(payload.mime_type, "application/pdf")

    def test_album_reference_keeps_custom_cover_after_serialization(self):
        source = video_message(cover=photo(), timestamp=0)
        media = raw.types.InputMediaDocument(
            id=raw.types.InputDocument(id=200, access_hash=123, file_reference=b"video"),
            video_cover=module._video_cover(source), video_timestamp=0, spoiler=True,
        )
        request = roundtrip(raw.functions.messages.SendMultiMedia(
            peer=raw.types.InputPeerSelf(),
            multi_media=[raw.types.InputSingleMedia(media=media, random_id=1, message="caption")],
            reply_to=raw.types.InputReplyToMessage(reply_to_msg_id=10, top_msg_id=8),
            invert_media=True,
        ))
        self.assertEqual(request.multi_media[0].media.video_cover.id, 100)
        self.assertEqual(request.multi_media[0].media.video_timestamp, 0)
        self.assertEqual(request.reply_to.top_msg_id, 8)
        self.assertTrue(request.invert_media)

    def test_batch_success_path_awaits_command_deletion(self):
        tree = ast.parse(inspect.getsource(module._run_batch))
        body = next(node for node in ast.walk(tree) if isinstance(node, ast.Try)).body
        last = body[-1]
        self.assertIsInstance(last.value, ast.Await)
        self.assertEqual(last.value.value.func.id, "_delete_command")

    def test_command_deletion_awaits_real_message_delete_method(self):
        tree = ast.parse(inspect.getsource(module._delete_command))
        body = next(node for node in ast.walk(tree) if isinstance(node, ast.Try)).body
        call = body[0].value.value
        self.assertEqual(call.func.id, "_rpc")
        self.assertEqual(call.args[0].attr, "delete")

    def test_reupload_uses_repository_work_directory(self):
        tree = ast.parse(inspect.getsource(module._copy_media))
        temporary = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "TemporaryDirectory"
        )
        self.assertIn("dir", {keyword.arg for keyword in temporary.keywords})
        self.assertIn('Path(".work/linkcopy")', inspect.getsource(module._copy_media))

    def test_python_sources_compile(self):
        root = Path(__file__).parents[1]
        for directory in ("linkcopy", "autodelplus", "conflict_analyzer", "summarize_user"):
            path = root / directory / "main.py"
            with self.subTest(plugin=directory):
                compile(path.read_text(), str(path), "exec")


class LinkCopyProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_without_command_performs_no_io(self):
        await module._transfer_status(None, "正在下载")
        progress = module.TransferProgress(None, "正在上传")
        await progress.update(0, 1024)
        self.assertGreater(progress.last_status, 0)

    async def test_batch_throttle_does_not_edit_a_message(self):
        command = Message(id=7, chat=Chat(id=-1001234567890, type=ChatType.SUPERGROUP))
        state = module.BatchState(
            module.MessageLink("example", 1), module.MessageLink("example", 79), command_id=command.id,
        )
        state.last_status = monotonic()
        module._batches[command.chat.id] = state
        try:
            previous = state.last_status
            await module._transfer_status(command, "正在上传")
            self.assertEqual(state.last_status, previous)
        finally:
            module._batches.pop(command.chat.id)
