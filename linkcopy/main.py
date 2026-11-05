import asyncio
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from struct import error as StructError
from tempfile import TemporaryDirectory
from time import monotonic
from urllib.parse import parse_qs, urlsplit

from pyrogram import errors, raw, utils
from pyrogram.enums import ParseMode
from pyrogram.file_id import FileId, FileType
from pyrogram.types import (
    InputMediaAudio,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    LinkPreviewOptions,
    ReplyParameters,
)

from pagermaid.enums import Client, Message
from pagermaid.listener import listener

log = logging.getLogger("linkcopy")
HELP = (
    "用法：,linkcopy <起始链接> [结束链接] [--single]\n"
    "两个链接批量复制同一聊天内的闭区间，最多 1000 个消息 ID。\n"
    "也可以回复一条含消息链接的消息发送 ,linkcopy。\n"
    "默认保留相册；--single 逐条复制。批量任务用 ,linkcopy cancel 停止。"
)
_URL = re.compile(
    r"(?<![\w./])(?:https?://(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/"
    r"|(?:t\.me|telegram\.me|telegram\.dog)/|tg://(?:resolve|privatepost)\?)"
    r"[^\s<>\"\u3000]+",
    re.IGNORECASE,
)
_REUPLOAD_ERRORS = tuple(
    cls
    for name in (
        "ChatForwardsRestricted",
        "FileReferenceExpired",
        "FileReferenceEmpty",
        "FileReferenceInvalid",
        "FileIdInvalid",
        "MediaEmpty",
    )
    if (cls := getattr(errors, name, None)) is not None
)
_MEDIA_TYPES = (
    "photo",
    "video",
    "document",
    "audio",
    "animation",
    "voice",
    "sticker",
    "video_note",
)
_ALBUM_TYPES = {
    "photo": InputMediaPhoto,
    "video": InputMediaVideo,
    "audio": InputMediaAudio,
    "document": InputMediaDocument,
}
_active = set()
_batches = {}
BATCH_LIMIT = 1000
BATCH_FETCH_SIZE = 100
BATCH_DELAY = 1
BATCH_STATUS_INTERVAL = 3
DOWNLOAD_CHUNK_SIZE = 1024 * 1024
DOWNLOAD_THRESHOLD = 16 * 1024 * 1024
DOWNLOAD_WORKERS = 4
DOWNLOAD_STATUS_INTERVAL = 3
# 插件内只允许一个并行下载，避免多条命令把分块并发数成倍放大。
_parallel_download_slots = asyncio.Semaphore(1)


class LinkCopyError(Exception):
    """可直接显示给用户的错误。"""


@dataclass(frozen=True)
class MessageLink:
    chat: int | str
    message_id: int
    single: bool = False
    comment_id: int | None = None


@dataclass
class BatchState:
    start: MessageLink
    end: MessageLink
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    handled: set = field(default_factory=set)
    sent: int = 0
    skipped: int = 0
    last_status: float = 0
    command_id: int | None = None

    @property
    def next_id(self):
        return next(
            (
                mid for mid in range(self.start.message_id, self.end.message_id + 1)
                if mid not in self.handled
            ),
            None,
        )

    def summary(self):
        total = self.end.message_id - self.start.message_id + 1
        return (
            f"已处理 {len(self.handled)}/{total}（{len(self.handled) / total:.1%}）\n"
            f"已发送 {self.sent} 条 · 已跳过 {self.skipped} 条"
        )


class BatchStopped(Exception):
    """用户请求在当前发送结束后停止。"""


def _positive(value):
    if not re.fullmatch(r"[0-9]{1,16}", str(value)) or int(value) <= 0:
        raise LinkCopyError("链接中的消息或频道 ID 无效。")
    return int(value)


def parse_link(value):
    value = value.strip().strip("<>")
    if "://" not in value:
        value = "https://" + value
    try:
        url = urlsplit(value)
        query = parse_qs(url.query, keep_blank_values=True)
        if any(len(values) != 1 for values in query.values()):
            raise LinkCopyError("链接包含重复参数。")
        comment = _positive(query["comment"][0]) if "comment" in query else None
        single = "single" in query
        if url.scheme == "tg" and url.netloc in ("resolve", "privatepost"):
            mid = _positive(query.get("post", [""])[0])
            if url.netloc == "privatepost":
                chat = -1000000000000 - _positive(query.get("channel", [""])[0])
            else:
                chat = query.get("domain", [""])[0]
                if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{2,31}", chat):
                    raise LinkCopyError("链接中的频道用户名无效。")
            return MessageLink(chat, mid, single, comment)
        if url.scheme not in ("http", "https") or url.netloc.lower() not in (
            "t.me",
            "telegram.me",
            "telegram.dog",
            "www.t.me",
            "www.telegram.me",
            "www.telegram.dog",
        ):
            raise LinkCopyError("请提供 Telegram 消息链接。")
        parts = url.path.strip("/").split("/")
        if parts[0] == "s":
            parts = parts[1:]
        if parts and parts[0] == "c" and len(parts) in (3, 4):
            chat = -1000000000000 - _positive(parts[1])
            ids = parts[2:]
        elif len(parts) in (2, 3) and re.fullmatch(
            r"[A-Za-z][A-Za-z0-9_]{2,31}", parts[0]
        ):
            chat, ids = parts[0], parts[1:]
        else:
            raise LinkCopyError("这不是消息链接；需要包含频道和消息 ID。")
        for item in ids:
            _positive(item)
        return MessageLink(chat, _positive(ids[-1]), single, comment)
    except ValueError as exc:
        raise LinkCopyError("消息链接格式无效。") from exc


def _reply_link(message):
    text = str(message.text or message.caption or "")
    candidates = [
        match.group().rstrip(".,，。!！)）]】") for match in _URL.finditer(text)
    ]
    for entity in message.entities or message.caption_entities or []:
        if entity.url:
            candidates.append(entity.url)
    links = set()
    for value in candidates:
        try:
            links.add(parse_link(value))
        except LinkCopyError:
            continue
    if len(links) != 1:
        raise LinkCopyError("被回复的消息需要恰好包含一个有效的 Telegram 消息链接。")
    return links.pop()


def _request(message):
    args = (message.arguments or "").split()
    single = "--single" in args
    args = [arg for arg in args if arg != "--single"]
    if len(args) == 3 and args[1] in ("-", "到"):
        args.pop(1)
    end = None
    if len(args) in (1, 2):
        link = parse_link(args[0])
        if len(args) == 2:
            end = parse_link(args[1])
            start_chat = link.chat.casefold() if isinstance(link.chat, str) else link.chat
            end_chat = end.chat.casefold() if isinstance(end.chat, str) else end.chat
            if start_chat != end_chat:
                raise LinkCopyError("批量复制的两个链接必须来自同一频道或群组。")
            if link.comment_id is not None or end.comment_id is not None:
                raise LinkCopyError("评论链接暂不支持区间复制，请使用单条链接。")
            if end.message_id < link.message_id:
                raise LinkCopyError("结束消息 ID 不能小于起始消息 ID。")
            if end.message_id - link.message_id + 1 > BATCH_LIMIT:
                raise LinkCopyError(f"一次最多复制 {BATCH_LIMIT} 个消息 ID，请缩小区间。")
    elif not args and message.reply_to_message:
        link = _reply_link(message.reply_to_message)
    else:
        raise LinkCopyError(HELP)
    return link, single or link.single or bool(end and end.single), end


async def _rpc(func, *args, **kwargs):
    """只重试明确被服务器拒绝的限流请求，超时等未知结果不重发。"""
    for attempt in range(3):
        try:
            return await func(*args, **kwargs)
        except errors.FloodWait as exc:
            if exc.value > 30 or attempt == 2:
                raise
            await asyncio.sleep(max(1, exc.value))


async def _status(message, text):
    await _rpc(
        message._client.edit_message_text,
        message.chat.id,
        message.id,
        text,
        parse_mode=ParseMode.DISABLED,
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


async def _transfer_status(command, text, force=False):
    if command is None:
        return
    state = _batches.get(command.chat.id)
    if state is not None and state.command_id != command.id:
        state = None
    now = monotonic()
    if state is not None:
        if not force and now - state.last_status < BATCH_STATUS_INTERVAL:
            return
        text = "批量复制中\n" + state.summary() + "\n" + text + "\n停止：,linkcopy cancel"
    try:
        await _status(command, text)
    except errors.MessageNotModified:
        pass
    except Exception as exc:  # noqa: BLE001 — 进度通知失败不影响文件传输。
        log.warning("linkcopy transfer progress failed: %s", type(exc).__name__)
    finally:
        if state is not None:
            state.last_status = now


@dataclass
class TransferProgress:
    command: Message | None
    label: str
    started: float = field(default_factory=monotonic)
    last_status: float = 0

    def text(self, current, total, now):
        mib = 1024 * 1024
        speed = current / max(now - self.started, 0.001)
        if total > 0:
            percent = current * 1000 // total / 10
            details = f"{percent:.1f}% · {current / mib:.1f}/{total / mib:.1f} MiB"
        else:
            details = f"已传输 {current / mib:.1f} MiB"
        if speed > 0:
            details += f" · {speed / mib:.2f} MiB/s"
            if total > current:
                remaining = max(1, int((total - current) / speed))
                minutes, seconds = divmod(remaining, 60)
                details += f"\n预计剩余 {minutes} 分 {seconds} 秒"
        return self.label + "\n" + details

    async def update(self, current, total):
        now = monotonic()
        if now - self.last_status < DOWNLOAD_STATUS_INTERVAL:
            return
        self.last_status = now
        await _transfer_status(self.command, self.text(current, total, now))


async def _delete_command(message):
    try:
        await _rpc(message.delete)
    except Exception as exc:  # noqa: BLE001 — 内容已发送，清理异常不改变发送结果。
        log.warning("linkcopy content sent; command deletion failed: %s", type(exc).__name__)
        await _transfer_status(message, "复制完成，进度消息删除失败，请手动删除。", force=True)


def _destination(message, from_reply):
    kwargs = {"chat_id": message.chat.id}
    if message.message_thread_id:
        kwargs["message_thread_id"] = message.message_thread_id
    # 回复取链接仅用于输入；显式给链接时保留命令的回复关系。
    if message.reply_to_message_id and not from_reply:
        kwargs["reply_to_message_id"] = message.reply_to_message_id
    return kwargs


def _media(message):
    for kind in _MEDIA_TYPES:
        media = getattr(message, kind, None)
        if media:
            return kind, media
    raise LinkCopyError("暂不支持这种消息类型（支持文字和常见媒体）。")


def _caption(message):
    return {
        "caption": str(message.caption or ""),
        "caption_entities": message.caption_entities,
        "parse_mode": ParseMode.DISABLED,
    }


def _metadata(message, kind):
    media = getattr(message, kind)
    fields = {
        "video": ("duration", "width", "height", "file_name"),
        "animation": ("duration", "width", "height", "file_name"),
        "audio": ("duration", "performer", "title", "file_name"),
        "document": ("file_name",),
        "voice": ("duration",),
        "video_note": ("duration",),
    }.get(kind, ())
    kwargs = {
        field: getattr(media, field)
        for field in fields
        if getattr(media, field, None) is not None
    }
    if kind in ("photo", "video", "animation"):
        kwargs["has_spoiler"] = bool(message.has_media_spoiler)
    if kind == "video":
        kwargs["supports_streaming"] = bool(media.supports_streaming)
        cover = getattr(media, "video_cover", None)
        if cover is not None:
            kwargs["video_cover"] = cover.file_id
        timestamp = getattr(media, "video_start_timestamp", None)
        if timestamp is not None:
            kwargs["video_start_timestamp"] = timestamp
    if kind == "video_note":
        kwargs["length"] = media.length
    return kwargs


class ParallelDownloadFallback(Exception):
    """需要交回原生下载器处理（例如 CDN 解密及哈希校验）。"""


async def _parallel_download(client, media, target, command=None):
    size = getattr(media, "file_size", None)
    # 只优化大小已知的大文件；旧版 Pyrogram 没有 get_session 时保留原路径。
    if (
        not isinstance(size, int) or size < DOWNLOAD_THRESHOLD
        or not callable(getattr(client, "get_session", None))
        or getattr(client, "get_file_semaphore", None) is None
    ):
        return False
    try:
        file_id = FileId.decode(media.file_id)
    except (ValueError, TypeError, KeyError, StructError):
        return False
    if file_id.file_type == FileType.PHOTO:
        location = raw.types.InputPhotoFileLocation(
            id=file_id.media_id,
            access_hash=file_id.access_hash,
            file_reference=file_id.file_reference,
            thumb_size=file_id.thumbnail_size,
        )
    elif file_id.file_type in (
        FileType.VIDEO, FileType.DOCUMENT, FileType.AUDIO, FileType.VOICE,
        FileType.ANIMATION, FileType.STICKER, FileType.VIDEO_NOTE,
    ):
        location = raw.types.InputDocumentFileLocation(
            id=file_id.media_id,
            access_hash=file_id.access_hash,
            file_reference=file_id.file_reference,
            thumb_size=file_id.thumbnail_size,
        )
    else:
        return False

    chunks = (size + DOWNLOAD_CHUNK_SIZE - 1) // DOWNLOAD_CHUNK_SIZE
    workers = min(DOWNLOAD_WORKERS, chunks)
    async with _parallel_download_slots, client.get_file_semaphore:
        # 使用客户端已有的 DC 认证、代理和媒体连接，不创建第二个账号或改全局设置。
        session = await client.get_session(file_id.dc_id, is_media=True)
        completed = 0
        started = monotonic()
        progress = TransferProgress(command, f"正在下载：{target.name}（{workers} 路并行）", started)
        await progress.update(0, size)
        try:
            with target.open("wb") as output:
                async def worker(index):
                    nonlocal completed
                    for part in range(index, chunks, workers):
                        offset = part * DOWNLOAD_CHUNK_SIZE
                        result = await _rpc(
                            session.invoke,
                            raw.functions.upload.GetFile(
                                location=location, offset=offset, limit=DOWNLOAD_CHUNK_SIZE,
                            ),
                            retries=1,
                            sleep_threshold=0,
                        )
                        if isinstance(result, raw.types.upload.FileCdnRedirect):
                            raise ParallelDownloadFallback()
                        if not isinstance(result, raw.types.upload.File):
                            raise LinkCopyError("媒体下载失败：服务器返回了未知的分块类型。")
                        expected = min(DOWNLOAD_CHUNK_SIZE, size - offset)
                        if len(result.bytes) != expected:
                            raise LinkCopyError("媒体下载失败：分块长度不符，没有发送不完整的文件。")
                        # seek/write 间没有 await，同一事件循环中不会交错写入。
                        # 按文件位置写入，完成顺序不影响文件顺序，内存只保留每路一块。
                        output.seek(offset)
                        output.write(result.bytes)
                        completed += expected
                        await progress.update(completed, size)

                tasks = [asyncio.create_task(worker(index)) for index in range(workers)]
                try:
                    await asyncio.gather(*tasks)
                finally:
                    # gather 遇错不会自动取消其他任务；必须排空后才能关闭或删掉文件。
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
            if completed != size or target.stat().st_size != size:
                raise LinkCopyError("媒体下载失败：文件大小校验不符，没有发送内容。")
        except BaseException as exc:
            target.unlink(missing_ok=True)
            if isinstance(exc, ParallelDownloadFallback):
                await _transfer_status(command, "当前媒体使用 CDN，正在切换原生下载器…")
                return False
            raise
        await progress.update(completed, size)
        return True


async def _download(client, message, directory, index, command=None):
    # 每项使用独立目录，避免相册中的同名文件互相覆盖。
    target = directory / str(index)
    target.mkdir()
    kind, media = _media(message)
    filename = Path(str(getattr(media, "file_name", "") or "").replace("\\", "/")).name
    if filename in ("", ".", ".."):
        extension = {
            "photo": ".jpg",
            "video": ".mp4",
            "document": ".bin",
            "audio": ".mp3",
            "animation": ".mp4",
            "voice": ".ogg",
            "sticker": ".webp",
            "video_note": ".mp4",
        }[kind]
        if kind == "sticker":
            extension = (
                ".tgs" if media.is_animated else ".webm" if media.is_video else ".webp"
            )
        filename = "media" + extension
    target = target / filename
    if await _parallel_download(client, media, target, command):
        return str(target)
    progress = TransferProgress(command, f"正在下载：{filename}")
    await progress.update(0, getattr(media, "file_size", None) or 0)
    path = await _rpc(
        client.download_media, message, file_name=str(target), progress=progress.update,
    )
    if not path or not Path(path).is_file():
        raise LinkCopyError("媒体下载失败；没有发送内容，请稍后重试。")
    # 原生下载同样检查已知大小，防止静默截断的文件被重新上传。
    size = getattr(media, "file_size", None)
    if isinstance(size, int) and size > 0 and Path(path).stat().st_size != size:
        raise LinkCopyError("媒体下载失败：文件大小校验不符，没有发送内容。")
    return path


def _preview_sources(source):
    kind, media = _media(source)
    if kind not in ("video", "animation", "audio", "document", "video_note"):
        return {}
    previews = {}
    thumbs = getattr(media, "thumbs", None) or []
    if thumbs:
        previews["thumb"] = max(thumbs, key=lambda thumb: thumb.width * thumb.height)
    cover = getattr(media, "video_cover", None)
    if kind == "video" and cover is not None:
        previews["video_cover"] = cover
    return previews


async def _download_previews(client, source, directory):
    paths = {}
    for name, preview in _preview_sources(source).items():
        path = await _rpc(client.download_media, preview, file_name=str(directory / (name + ".jpg")))
        if not path or not Path(path).is_file() or Path(path).stat().st_size == 0:
            raise LinkCopyError("媒体封面下载失败。")
        paths[name] = path
    return paths


async def _send_single(client, source, destination, local_path=None, previews=None, command=None):
    kind, media = _media(source)
    kwargs = _metadata(source, kind)
    if local_path:
        kwargs.update(previews or {})
        progress = TransferProgress(command, f"正在上传：{Path(local_path).name}")
        await progress.update(0, Path(local_path).stat().st_size)
        kwargs["progress"] = progress.update
    if kind not in ("sticker", "video_note"):
        kwargs.update(_caption(source))
    if kind in ("photo", "video", "animation"):
        kwargs["show_caption_above_media"] = bool(source.show_caption_above_media)
    # 不复制按钮；按钮的 callback 与原机器人、原消息绑定。
    return await _rpc(
        getattr(client, "send_" + kind),
        **destination,
        **{kind: local_path or media.file_id},
        **kwargs,
    )


def _album_upload(source, file, thumb, file_name):
    kind, attachment = _media(source)
    if kind == "photo":
        return raw.types.InputMediaUploadedPhoto(file=file, spoiler=bool(source.has_media_spoiler))
    attributes = [raw.types.DocumentAttributeFilename(file_name=file_name)]
    if kind == "video":
        attributes.append(raw.types.DocumentAttributeVideo(
            duration=attachment.duration,
            w=attachment.width,
            h=attachment.height,
            supports_streaming=bool(attachment.supports_streaming),
        ))
    elif kind == "audio":
        attributes.append(raw.types.DocumentAttributeAudio(
            duration=attachment.duration, performer=attachment.performer, title=attachment.title,
        ))
    elif kind != "document":
        raise LinkCopyError("相册中包含不支持的媒体。")
    return raw.types.InputMediaUploadedDocument(
        file=file,
        thumb=thumb,
        mime_type=attachment.mime_type or {
            "video": "video/mp4", "audio": "audio/mpeg", "document": "application/octet-stream",
        }[kind],
        attributes=attributes,
        spoiler=bool(source.has_media_spoiler) if kind == "video" else None,
        nosound_video=True if kind == "video" else None,
        force_file=True if kind == "document" else None,
    )


def _video_cover(source):
    video = source.video
    cover = getattr(video, "video_cover", None) if video else None
    return utils.get_input_media_from_file_id(cover.file_id, FileType.PHOTO).id if cover else None


async def _send_album_raw(client, sources, destination, paths, previews, command):
    peer = await _rpc(client.resolve_peer, destination["chat_id"])
    items = []
    for index, source in enumerate(sources):
        kind, attachment = _media(source)
        if kind not in _ALBUM_TYPES:
            raise LinkCopyError("相册中包含不支持的媒体。")
        if paths:
            path = paths[index]
            progress = TransferProgress(
                command, f"正在上传相册 {index + 1}/{len(sources)}：{Path(path).name}",
            )
            await progress.update(0, Path(path).stat().st_size)
            thumb_path = previews[index].get("thumb")
            thumb = await _rpc(client.save_file, thumb_path) if thumb_path else None
            if thumb_path and thumb is None:
                raise LinkCopyError("媒体缩略图上传失败。")
            file = await _rpc(client.save_file, path, progress=progress.update)
            if file is None:
                raise LinkCopyError("相册媒体上传失败。")
            uploaded = await _rpc(
                client.invoke,
                raw.functions.messages.UploadMedia(
                    peer=peer,
                    media=_album_upload(
                        source, file, thumb, getattr(attachment, "file_name", None) or Path(path).name,
                    ),
                ),
                retries=1, sleep_threshold=0,
            )
            if kind == "photo":
                photo = uploaded.photo
                media = raw.types.InputMediaPhoto(id=raw.types.InputPhoto(
                    id=photo.id, access_hash=photo.access_hash, file_reference=photo.file_reference,
                ), spoiler=bool(source.has_media_spoiler))
            else:
                document = uploaded.document
                media = raw.types.InputMediaDocument(id=raw.types.InputDocument(
                    id=document.id, access_hash=document.access_hash,
                    file_reference=document.file_reference,
                ), spoiler=bool(source.has_media_spoiler) if kind == "video" else None)
        else:
            media = utils.get_input_media_from_file_id(
                attachment.file_id, has_spoiler=bool(source.has_media_spoiler),
            )
        if kind == "video":
            cover_path = previews[index].get("video_cover") if previews else None
            if cover_path:
                cover_file = await _rpc(client.save_file, cover_path)
                if cover_file is None:
                    raise LinkCopyError("视频封面上传失败。")
                uploaded = await _rpc(
                    client.invoke,
                    raw.functions.messages.UploadMedia(
                        peer=peer, media=raw.types.InputMediaUploadedPhoto(file=cover_file),
                    ),
                    retries=1, sleep_threshold=0,
                )
                photo = uploaded.photo
                media.video_cover = raw.types.InputPhoto(
                    id=photo.id, access_hash=photo.access_hash, file_reference=photo.file_reference,
                )
            else:
                media.video_cover = _video_cover(source)
            media.video_timestamp = getattr(attachment, "video_start_timestamp", None)
        items.append(raw.types.InputSingleMedia(
            media=media, random_id=client.rnd_id(),
            **await utils.parse_text_entities(
                client, str(source.caption or ""), ParseMode.DISABLED, source.caption_entities,
            ),
        ))
    reply_id = destination.get("reply_to_message_id")
    reply = ReplyParameters(message_id=reply_id) if reply_id else None
    result = await _rpc(
        client.invoke,
        raw.functions.messages.SendMultiMedia(
            peer=peer, multi_media=items,
            reply_to=await utils.get_reply_to(
                client, reply_parameters=reply,
                message_thread_id=destination.get("message_thread_id"),
            ),
            invert_media=bool(sources[0].show_caption_above_media),
        ),
        retries=1, sleep_threshold=0,
    )
    return await utils.parse_messages(client=client, messages=result)


async def _send_album(client, sources, destination, paths=None, previews=None, command=None):
    # 原始相册接口支持逐项上传进度及独立的视频封面。
    if paths or any(
        source.video and (
            _video_cover(source) is not None
            or getattr(source.video, "video_start_timestamp", None) is not None
        )
        for source in sources
    ):
        return await _send_album_raw(client, sources, destination, paths, previews, command)
    media = []
    for index, source in enumerate(sources):
        kind, attachment = _media(source)
        if kind not in _ALBUM_TYPES:
            raise LinkCopyError("相册中包含不支持的媒体。")
        media.append(
            _ALBUM_TYPES[kind](
                media=attachment.file_id,
                **_caption(source),
                **_metadata(source, kind),
            )
        )
    return await _rpc(
        client.send_media_group,
        **destination,
        media=media,
        show_caption_above_media=bool(sources[0].show_caption_above_media),
    )


async def _copy_media(client, sources, destination, command):
    album = len(sources) > 1
    # 内容保护标记不直接决定下载；先尝试复用，只有明确被拒绝才重传。
    try:
        if album:
            result = await _send_album(client, sources, destination)
        else:
            result = await _send_single(client, sources[0], destination)
        if not result:
            raise LinkCopyError("Telegram 没有返回发送结果，请检查目标聊天后再重试。")
        return
    except _REUPLOAD_ERRORS:
        pass
    await _transfer_status(command, "正在下载媒体，下载完成后重新上传…")
    temporary_root = Path(".work/linkcopy")
    temporary_root.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="transfer-", dir=temporary_root) as temporary:
        paths, previews = [], []
        for index, source in enumerate(sources):
            paths.append(await _download(client, source, Path(temporary), index, command))
            preview_dir = Path(temporary) / "previews" / str(index)
            preview_dir.mkdir(parents=True)
            await _transfer_status(command, f"正在下载封面：{index + 1}/{len(sources)}")
            previews.append(await _download_previews(client, source, preview_dir))
        await _transfer_status(command, "下载完成，正在上传媒体…")
        if album:
            result = await _send_album(client, sources, destination, paths, previews, command)
        else:
            result = await _send_single(client, sources[0], destination, paths[0], previews[0], command)
        if not result:
            raise LinkCopyError("Telegram 没有返回发送结果，请检查目标聊天后再重试。")


def _error_text(exc):
    if isinstance(exc, LinkCopyError):
        return str(exc)
    if isinstance(exc, errors.FloodWait):
        return f"Telegram 要求等待 {exc.value} 秒，请稍后再试。"
    name = type(exc).__name__
    if name in (
        "ChannelPrivate",
        "UserNotParticipant",
        "PeerIdInvalid",
        "UsernameNotOccupied",
        "UsernameInvalid",
    ):
        return (
            "无法访问来源：请确认运行 PagerMaid 的账号已加入该频道或群组，且链接有效。"
        )
    if name in ("MessageIdInvalid", "MsgIdInvalid"):
        return "消息不存在、已删除，或消息链接无效。"
    if name in (
        "ChatWriteForbidden",
        "ChatAdminRequired",
        "UserBannedInChannel",
        "ChatSendMediaForbidden",
        "ChatSendPhotosForbidden",
        "ChatSendVideosForbidden",
    ):
        return "目标聊天不允许发送此类消息，请检查账号权限。"
    if name == "ChatForwardsRestricted":
        return "Telegram 拒绝发送该媒体，下载重传也未成功。"
    if isinstance(exc, (TimeoutError, OSError)):
        return (
            "网络或本地文件操作失败；请先检查目标聊天是否已收到内容，再决定是否重试。"
        )
    return f"处理失败（{name}），请检查目标聊天是否已收到内容后再重试。"


async def _sources(client, source, single):
    _media(source)
    if not source.media_group_id or single:
        return [source]
    sources = await _rpc(client.get_media_group, source.chat.id, source.id)
    sources = sorted(sources, key=lambda item: item.id)
    if (
        not 2 <= len(sources) <= 10
        or len({item.id for item in sources}) != len(sources)
        or source.id not in {item.id for item in sources}
        or any(
            item.media_group_id != source.media_group_id or item.empty
            for item in sources
        )
    ):
        raise LinkCopyError("无法完整读取相册，请重试或使用 --single。")
    return sources


def _resume_link(link, message_id):
    if isinstance(link.chat, int):
        return f"https://t.me/c/{-link.chat - 1000000000000}/{message_id}"
    return f"https://t.me/{link.chat}/{message_id}"


def _batch_checkpoint(state):
    if state.stop.is_set():
        raise BatchStopped()


async def _batch_pause(state):
    _batch_checkpoint(state)
    if BATCH_DELAY > 0:
        try:
            await asyncio.wait_for(state.stop.wait(), timeout=BATCH_DELAY)
        except asyncio.TimeoutError:
            return
        raise BatchStopped()


async def _run_batch(client, message, start, end, single):
    chat_id = message.chat.id
    if chat_id in _batches:
        raise LinkCopyError("当前聊天已有批量任务；请等待完成或使用 ,linkcopy cancel。")
    state = BatchState(start, end, command_id=message.id)
    destination = _destination(message, False)
    _batches[chat_id] = state
    try:
        await _transfer_status(message, "正在读取来源消息…")
        for first in range(start.message_id, end.message_id + 1, BATCH_FETCH_SIZE):
            _batch_checkpoint(state)
            ids = list(range(first, min(first + BATCH_FETCH_SIZE, end.message_id + 1)))
            messages = await _rpc(client.get_messages, start.chat, ids)
            sources_by_id = {item.id: item for item in messages if item and not item.empty}
            for mid in ids:
                if mid in state.handled:
                    continue
                _batch_checkpoint(state)
                source = sources_by_id.get(mid)
                if not source or source.service or not (
                    source.text or any(getattr(source, kind, None) for kind in _MEDIA_TYPES)
                ):
                    state.skipped += 1
                    state.handled.add(mid)
                else:
                    if source.text:
                        result = await _rpc(
                            client.send_message,
                            **destination,
                            text=str(source.text),
                            entities=source.entities,
                            parse_mode=ParseMode.DISABLED,
                            link_preview_options=LinkPreviewOptions(is_disabled=True),
                        )
                        if not result:
                            raise LinkCopyError("Telegram 没有返回发送结果，请先检查目标聊天。")
                        copied = {mid}
                    else:
                        sources = await _sources(client, source, single)
                        # 区间边界裁剪相册；已发成员不重复发送，跨读取批次也保持去重。
                        sources = [
                            item for item in sources
                            if start.message_id <= item.id <= end.message_id
                            and item.id not in state.handled
                        ]
                        _batch_checkpoint(state)
                        await _copy_media(client, sources, destination, message)
                        copied = {item.id for item in sources}
                    state.sent += len(copied)
                    state.handled.update(copied)
                    if state.next_id is not None:
                        await _batch_pause(state)
                if state.next_id is not None:
                    await _transfer_status(message, "正在复制消息…")
        await _delete_command(message)
    except BatchStopped:
        text = "批量复制已停止。\n" + state.summary()
        if state.next_id is not None:
            text += "\n下次可从此链接继续：" + _resume_link(start, state.next_id)
        await _status(message, text)
    except Exception as exc:
        text = "批量任务已中止。\n" + state.summary() + "\n" + _error_text(exc)
        if state.next_id is not None:
            text += (
                "\n停止于：" + _resume_link(start, state.next_id)
                + "\n该条或相册可能已送达，请检查目标聊天后再决定从哪里继续；不要直接重跑整个区间。"
            )
        raise LinkCopyError(text) from exc
    finally:
        _batches.pop(chat_id, None)


async def _run(client, message):
    if (message.arguments or "").strip() == "cancel":
        state = _batches.get(message.chat.id)
        if state is None:
            await _status(message, "当前聊天没有正在运行的批量复制任务。")
        else:
            state.stop.set()
            await _status(message, "已请求停止批量复制；当前下载或发送结束后停止。")
        return
    link, single, end = _request(message)
    if end is not None:
        await _run_batch(client, message, link, end, single)
        return
    source = await _rpc(client.get_messages, link.chat, link.message_id)
    if not source or source.empty:
        raise LinkCopyError("消息不存在、已删除，或当前账号无法读取。")
    if link.comment_id:
        discussion = await _rpc(
            client.get_discussion_message, link.chat, link.message_id
        )
        source = await _rpc(client.get_messages, discussion.chat.id, link.comment_id)
        if not source or source.empty:
            raise LinkCopyError("评论不存在、已删除，或当前账号无法读取讨论组。")
    if source.service:
        raise LinkCopyError("服务消息（入群、置顶通知等）无法复制。")
    if source.text:
        await _rpc(
            client.edit_message_text,
            message.chat.id,
            message.id,
            str(source.text),
            entities=source.entities,
            parse_mode=ParseMode.DISABLED,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        return
    sources = await _sources(client, source, single)
    from_reply = not [
        arg for arg in (message.arguments or "").split() if arg != "--single"
    ]
    await _copy_media(client, sources, _destination(message, from_reply), message)
    await _delete_command(message)


@listener(
    command="linkcopy",
    outgoing=True,
    ignore_edited=True,
    description="按消息链接或链接区间复制原文和媒体",
    parameters="<起始链接> [结束链接] [--single] / cancel（或回复含链接的消息）",
)
async def linkcopy(client: Client, message: Message):
    # 来源读取使用当前账号，禁止 sudo 用户通过本命令读取账号的私有频道。
    if not message.outgoing and not getattr(message.from_user, "is_self", False):
        return
    key = (message.chat.id, message.id)
    if key in _active:
        return
    _active.add(key)
    try:
        await _run(client, message)
    except Exception as exc:  # noqa: BLE001 — 在命令边界统一显示错误，避免框架上传诊断数据。
        if not isinstance(exc, (LinkCopyError, errors.RPCError, OSError, TimeoutError)):
            log.error("linkcopy failed: %s", type(exc).__name__)
        await _status(message, "链接复制失败：" + _error_text(exc))
    finally:
        _active.discard(key)
