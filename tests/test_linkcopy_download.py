import asyncio
import inspect
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_linkcopy
from pyrogram import Client, errors, raw
from pyrogram.file_id import FileId, FileType
from pyrogram.session import Session
from pyrogram.types import Message, Video

module = test_linkcopy.module


class LinkCopyDownloadTests(unittest.IsolatedAsyncioTestCase):
    reject_first_send = test_linkcopy.LinkCopyTests.reject_first_send
    set_source = test_linkcopy.LinkCopyTests.set_source

    def setUp(self):
        test_linkcopy.LinkCopyTests.setUp(self)
        self.directory = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(patch.object(module, "DOWNLOAD_CHUNK_SIZE", 4096))
        self.enterContext(patch.object(module, "DOWNLOAD_THRESHOLD", 8192))
        self.enterContext(patch.object(module, "_parallel_download_slots", asyncio.Semaphore(1)))
        self.data = b"".join(bytes([index]) * 4096 for index in range(9)) + b"last-part"
        self.inflight = 0
        self.peak = 0
        self.finished = []
        self.invoke_signature = inspect.signature(Session.invoke)
        self.session = SimpleNamespace(invoke=AsyncMock(side_effect=self.invoke), stop=AsyncMock())
        self.client.get_session = AsyncMock(side_effect=self.get_session)
        self.client.get_file_semaphore = asyncio.Semaphore(1)
        self.native_signature = inspect.signature(Client.download_media)
        self.client.download_media.side_effect = self.native_download

    def video(self, size=None):
        file_id = FileId(
            file_type=FileType.VIDEO, dc_id=4, media_id=123, access_hash=456,
            file_reference=b"reference",
        ).encode()
        return Message(
            id=42, chat=self.command.chat,
            video=Video(
                file_id=file_id, file_unique_id="unique", codec="h264",
                width=1280, height=720, duration=10, file_size=size or len(self.data),
                file_name="original.mp4", supports_streaming=True,
            ),
            caption="原视频配文", client=self.client,
        )

    async def get_session(self, *args, **kwargs):
        inspect.signature(Client.get_session).bind(self.client, *args, **kwargs)
        return self.session

    async def invoke(self, query, **kwargs):
        self.invoke_signature.bind(self.session, query, **kwargs)
        self.assertIsInstance(query, raw.functions.upload.GetFile)
        query.write()  # 同时验证请求和 file location 可以由真实 Pyrogram 序列化。
        self.assertEqual(query.limit, module.DOWNLOAD_CHUNK_SIZE)
        self.assertEqual(query.offset % query.limit, 0)
        self.assertEqual(kwargs, {"retries": 1, "sleep_threshold": 0})
        self.inflight += 1
        self.peak = max(self.peak, self.inflight)
        try:
            # 让低偏移分块反而较晚返回，检验乱序写入不会损坏文件。
            for _ in range(4 - (query.offset // query.limit) % 4):
                await asyncio.sleep(0)
            self.finished.append(query.offset)
            return raw.types.upload.File(
                type=raw.types.storage.FileUnknown(), mtime=0,
                bytes=self.data[query.offset:query.offset + query.limit],
            )
        finally:
            self.inflight -= 1

    async def native_download(self, message, file_name):
        self.native_signature.bind(self.client, message, file_name=file_name)
        target = Path(file_name)
        self.assertFalse(target.exists(), "原生回退前必须删除并行下载留下的残片")
        target.write_bytes(self.data)
        return str(target)

    async def test_parallel_chunks_finish_out_of_order_without_corrupting_file(self):
        source = self.video()
        path = await module._download(self.client, source, self.directory, 0)
        self.assertEqual(Path(path).read_bytes(), self.data)
        self.assertEqual(Path(path).name, "original.mp4")
        self.assertEqual(self.peak, 4)
        self.assertNotEqual(self.finished, sorted(self.finished))
        self.assertEqual(sorted(self.finished), list(range(0, len(self.data), 4096)))
        self.client.get_session.assert_awaited_once_with(4, is_media=True)
        self.client.download_media.assert_not_called()
        self.session.stop.assert_not_called()  # 不能关闭其他插件共享的连接。
        self.assertEqual(self.client.get_file_semaphore._value, 1)

    async def test_exact_multiple_has_no_extra_eof_request(self):
        self.data = b"a" * 8192
        path = await module._download(self.client, self.video(), self.directory, 0)
        self.assertEqual(Path(path).read_bytes(), self.data)
        self.assertEqual(self.session.invoke.await_count, 2)
        self.assertEqual(self.peak, 2)

    async def test_small_file_uses_native_download(self):
        self.data = b"small"
        path = await module._download(self.client, self.video(), self.directory, 0)
        self.assertEqual(Path(path).read_bytes(), self.data)
        self.client.download_media.assert_awaited_once()
        self.client.get_session.assert_not_called()

    async def test_unknown_size_uses_native_download(self):
        source = self.video()
        source.video.file_size = None
        await module._download(self.client, source, self.directory, 0)
        self.client.download_media.assert_awaited_once()
        self.client.get_session.assert_not_called()

    async def test_legacy_client_without_get_session_uses_native_download(self):
        del self.client.get_session
        path = await module._download(self.client, self.video(), self.directory, 0)
        self.assertEqual(Path(path).read_bytes(), self.data)
        self.client.download_media.assert_awaited_once()
        self.session.invoke.assert_not_called()

    async def test_invalid_file_id_leaves_decoding_to_native_download(self):
        source = self.video()
        source.video.file_id = "invalid"
        await module._download(self.client, source, self.directory, 0)
        self.client.download_media.assert_awaited_once()
        self.client.get_session.assert_not_called()

    async def test_cdn_redirect_removes_partial_file_and_uses_native_download(self):
        async def redirect(query, **kwargs):
            if query.offset == 0:
                return raw.types.upload.File(
                    type=raw.types.storage.FileUnknown(), mtime=0, bytes=self.data[:4096],
                )
            return raw.types.upload.FileCdnRedirect(
                dc_id=5, file_token=b"token", encryption_key=b"k" * 32,
                encryption_iv=b"i" * 16, file_hashes=[],
            )

        self.session.invoke.side_effect = redirect
        path = await module._download(self.client, self.video(), self.directory, 0)
        self.assertEqual(Path(path).read_bytes(), self.data)
        self.client.download_media.assert_awaited_once()
        self.session.stop.assert_not_called()

    async def test_short_chunk_aborts_without_native_retry_or_incomplete_upload(self):
        self.set_source(self.video())
        self.reject_first_send("send_video")
        self.session.invoke.side_effect = lambda *a, **kw: raw.types.upload.File(
            type=raw.types.storage.FileUnknown(), mtime=0, bytes=b"short",
        )
        await module.linkcopy(self.client, self.command)
        self.client.send_video.assert_awaited_once()  # 只有被拒绝的文件引用，没有上传残片。
        self.client.download_media.assert_not_called()
        self.client.delete_messages.assert_not_called()
        self.assertIn("分块长度不符", self.client.edit_message_text.call_args.args[2])
        self.assertFalse(module._active)

    async def test_long_floodwait_aborts_and_drains_other_workers(self):
        target = self.directory / "partial.mp4"
        never = asyncio.Event()
        exited = []

        async def limited(query, **kwargs):
            try:
                if query.offset == 0:
                    await asyncio.sleep(0)
                    raise errors.FloodWait(60)
                await never.wait()
            finally:
                exited.append(query.offset)

        self.session.invoke.side_effect = limited
        with self.assertRaises(errors.FloodWait):
            await module._parallel_download(self.client, self.video().video, target)
        self.assertEqual(len(exited), 4)
        self.assertFalse(target.exists())
        self.client.download_media.assert_not_called()
        self.assertEqual(self.client.get_file_semaphore._value, 1)
        self.assertEqual(module._parallel_download_slots._value, 1)

    async def test_short_floodwait_retries_only_rejected_chunk(self):
        self.data = b"a" * 8192
        limited = False

        async def limited_once(query, **kwargs):
            nonlocal limited
            if query.offset == 0 and not limited:
                limited = True
                raise errors.FloodWait(2)
            return await self.invoke(query, **kwargs)

        self.session.invoke.side_effect = limited_once
        with patch.object(module.asyncio, "sleep", new_callable=AsyncMock) as sleep:
            path = await module._download(self.client, self.video(), self.directory, 0)
        self.assertEqual(Path(path).read_bytes(), self.data)
        self.assertTrue(any(call.args == (2,) for call in sleep.call_args_list))
        offsets = [call.args[0].offset for call in self.session.invoke.call_args_list]
        self.assertEqual(offsets.count(0), 2)
        self.assertEqual(offsets.count(4096), 1)

    async def test_network_error_is_not_silently_retried_via_native_download(self):
        self.session.invoke.side_effect = TimeoutError()
        target = self.directory / "partial.mp4"
        with self.assertRaises(TimeoutError):
            await module._parallel_download(self.client, self.video().video, target)
        self.assertFalse(target.exists())
        self.client.download_media.assert_not_called()

    async def test_cancel_drains_all_workers_before_removing_file(self):
        started = asyncio.Event()
        never = asyncio.Event()
        active = 0
        exited = []

        async def blocked(query, **kwargs):
            nonlocal active
            active += 1
            if active == 4:
                started.set()
            try:
                await never.wait()
            finally:
                exited.append(query.offset)

        self.session.invoke.side_effect = blocked
        target = self.directory / "partial.mp4"
        task = asyncio.create_task(module._parallel_download(self.client, self.video().video, target))
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len(exited), 4)
        self.assertFalse(target.exists())
        self.assertEqual(self.client.get_file_semaphore._value, 1)
        self.assertEqual(module._parallel_download_slots._value, 1)
        self.session.stop.assert_not_called()

    async def test_parallel_files_are_serialized_even_across_clients(self):
        started = asyncio.Event()
        release = asyncio.Event()
        original_invoke = self.invoke

        async def blocked_first(query, **kwargs):
            if not release.is_set():
                started.set()
                await release.wait()
            return await original_invoke(query, **kwargs)

        self.session.invoke.side_effect = blocked_first
        other = SimpleNamespace(get_file_semaphore=asyncio.Semaphore(1),
                                get_session=self.client.get_session)
        first = asyncio.create_task(module._parallel_download(
            self.client, self.video().video, self.directory / "one.mp4",
        ))
        await asyncio.wait_for(started.wait(), timeout=1)
        second = asyncio.create_task(module._parallel_download(
            other, self.video().video, self.directory / "two.mp4",
        ))
        await asyncio.sleep(0)
        self.client.get_session.assert_awaited_once()
        release.set()
        self.assertEqual(await asyncio.gather(first, second), [True, True])
        self.assertLessEqual(self.peak, 4)
        for name in ("one.mp4", "two.mp4"):
            self.assertEqual((self.directory / name).read_bytes(), self.data)

    async def test_cached_large_video_still_does_not_download(self):
        self.set_source(self.video())
        await module.linkcopy(self.client, self.command)
        self.client.send_video.assert_awaited_once()
        self.client.download_media.assert_not_called()
        self.client.get_session.assert_not_called()

    async def test_reupload_uses_complete_file_preserves_metadata_and_cleans_up(self):
        source = self.video()
        self.set_source(source)
        self.reject_first_send("send_video")
        send = self.client.send_video.side_effect
        paths = []

        async def inspect_send(**kwargs):
            if kwargs["video"] != source.video.file_id:
                path = Path(kwargs["video"])
                self.assertEqual(path.read_bytes(), self.data)
                paths.append(path)
            return await send(**kwargs)

        self.client.send_video.side_effect = inspect_send
        await module.linkcopy(self.client, self.command)
        self.assertEqual(len(paths), 1)
        self.assertFalse(paths[0].exists())
        self.assertEqual(self.client.send_video.await_count, 2)
        kwargs = self.client.send_video.call_args.kwargs
        self.assertEqual(kwargs["caption"], "原视频配文")
        self.assertEqual(kwargs["file_name"], "original.mp4")
        self.assertTrue(kwargs["supports_streaming"])
        self.client.delete_messages.assert_awaited_once()

    async def test_progress_shows_parallelism_percentage_and_speed(self):
        self.enterContext(patch.object(module, "DOWNLOAD_STATUS_INTERVAL", 0))
        await module._download(self.client, self.video(), self.directory, 0, self.command)
        texts = [call.args[2] for call in self.client.edit_message_text.call_args_list]
        self.assertIn("4 路", texts[0])
        self.assertIn("0.0%", texts[0])
        self.assertIn("100.0%", texts[-1])
        self.assertIn("MiB/s", texts[-1])

    async def test_progress_edit_failure_does_not_abort_download(self):
        self.client.edit_message_text.side_effect = OSError()
        path = await module._download(self.client, self.video(), self.directory, 0, self.command)
        self.assertEqual(Path(path).read_bytes(), self.data)

    async def test_native_truncated_file_is_not_uploaded(self):
        source = self.video()
        source.video.file_size = 7
        self.set_source(source)
        self.reject_first_send("send_video")
        await module.linkcopy(self.client, self.command)
        self.client.send_video.assert_awaited_once()
        self.assertIn("文件大小校验不符", self.client.edit_message_text.call_args.args[2])
        self.client.delete_messages.assert_not_called()
