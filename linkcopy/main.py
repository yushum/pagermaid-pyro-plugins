"""按 Telegram 消息链接复制内容：,linkcopy <链接> [--single]。"""

import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlsplit

from pyrogram import errors
from pyrogram.enums import ParseMode
from pyrogram.types import (
    InputMediaAudio,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    LinkPreviewOptions,
)

from pagermaid.enums import Client, Message
from pagermaid.listener import listener

log = logging.getLogger("linkcopy")
HELP = (
    "用法：,linkcopy <消息链接> [--single]\n"
    "也可以回复一条含消息链接的消息发送 ,linkcopy。\n"
    "默认复制整个相册；--single 只复制链接指向的一条。"
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


class LinkCopyError(Exception):
    """可直接显示给用户的错误。"""


@dataclass(frozen=True)
class MessageLink:
    chat: int | str
    message_id: int
    single: bool = False
    comment_id: int | None = None


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
    if len(args) == 1:
        link = parse_link(args[0])
    elif not args and message.reply_to_message:
        link = _reply_link(message.reply_to_message)
    else:
        raise LinkCopyError(HELP)
    return link, single or link.single


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
    if kind == "video_note":
        kwargs["length"] = media.length
    return kwargs


async def _download(client, message, directory, index):
    # 每项独立目录，避免相册同名文件互相覆盖；使用磁盘而不是把大文件放进内存。
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
    path = await _rpc(client.download_media, message, file_name=str(target / filename))
    if not path or not Path(path).is_file():
        raise LinkCopyError("媒体下载失败；没有发送内容，请稍后重试。")
    return path


async def _send_single(client, source, destination, local_path=None):
    kind, media = _media(source)
    kwargs = _metadata(source, kind)
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


async def _send_album(client, sources, destination, paths=None):
    media = []
    for index, source in enumerate(sources):
        kind, attachment = _media(source)
        if kind not in _ALBUM_TYPES:
            raise LinkCopyError("相册中包含不支持的媒体。")
        media.append(
            _ALBUM_TYPES[kind](
                media=paths[index] if paths else attachment.file_id,
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
    await _status(command, "正在下载并重新上传媒体…\n大文件可能需要较长时间。")
    with TemporaryDirectory(prefix="linkcopy-") as temporary:
        paths = []
        for index, source in enumerate(sources):
            paths.append(await _download(client, source, Path(temporary), index))
        if album:
            result = await _send_album(client, sources, destination, paths)
        else:
            result = await _send_single(client, sources[0], destination, paths[0])
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


async def _run(client, message):
    link, single = _request(message)
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
    _media(source)
    sources = [source]
    if source.media_group_id and not single:
        sources = await _rpc(client.get_media_group, source.chat.id, source.id)
        sources = sorted(sources, key=lambda item: item.id)
        if (
            not 2 <= len(sources) <= 10
            or source.id not in {item.id for item in sources}
            or any(
                item.media_group_id != source.media_group_id or item.empty
                for item in sources
            )
        ):
            raise LinkCopyError("无法完整读取相册，请重试或使用 --single。")
    from_reply = not [
        arg for arg in (message.arguments or "").split() if arg != "--single"
    ]
    await _copy_media(client, sources, _destination(message, from_reply), message)
    # 发送确认成功后才删除命令；删除失败不应再次发送内容。
    try:
        await _rpc(message.delete)
    except Exception as exc:  # noqa: BLE001 — 内容已发送，清理异常不得重发或报告发送失败。
        log.warning(
            "linkcopy content sent; command deletion failed: %s", type(exc).__name__
        )
        try:
            await _status(message, "内容已发送，命令消息未能删除。")
        except Exception as status_exc:  # noqa: BLE001 — 状态通知失败也不改变发送成功的结果。
            log.warning(
                "linkcopy content sent; status update failed: %s",
                type(status_exc).__name__,
            )


@listener(
    command="linkcopy",
    outgoing=True,
    ignore_edited=True,
    description="将 Telegram 消息链接替换成原文或媒体",
    parameters="<链接> [--single]（或回复含链接的消息）",
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
