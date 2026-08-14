"""
summarize_user — PagerMaid-Pyro 插件

功能：抓取指定用户在群组/频道的发言，调用 OpenAI 兼容 API 生成用户画像分析报告。
     支持远程群组定向分析（零痕迹）和 Map-Reduce 分块分析（支持 2000+ 条消息）。

用法：
  消息链接分析（最简单，支持私密群组 + 无用户名用户）：
    ,summarize_user -l <消息链接> [数量]

  远程分析（零痕迹）：
    ,summarize_user -g <群组> [数量] <用户>

  群内分析：
    ,summarize_user [数量] [用户名/ID]
    ,summarize_user [数量]               — 回复某人消息时

  工具命令：
    ,summarize_user getid                — 获取当前对话 ID（回复消息可同时获取用户 ID）

  配置命令：
    ,summarize_user setapi <API_KEY>     — 设置 API 密钥
    ,summarize_user seturl <BASE_URL>    — 设置 API 地址
    ,summarize_user setmodel <MODEL>     — 设置模型名称
    ,summarize_user setdisplay <NAME>    — 设置输出显示的模型名称
    ,summarize_user setprompt <PROMPT>   — 自定义 System Prompt
    ,summarize_user resetprompt          — 恢复默认 System Prompt
    ,summarize_user showconfig           — 查看当前配置
"""

import asyncio
import json
import logging
import re
from typing import List, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

import httpx
from pyrogram.enums import ChatType, ParseMode

from pagermaid.listener import listener
from pagermaid.enums import Client, Message
from pagermaid.services import sqlite

# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
log = logging.getLogger("summarize_user")

# ---------------------------------------------------------------------------
# 持久化存储键名
# ---------------------------------------------------------------------------
API_KEY_KEY = "summarize_user_api_key"
BASE_URL_KEY = "summarize_user_base_url"
MODEL_KEY = "summarize_user_model"
PROMPT_KEY = "summarize_user_prompt"
DISPLAY_MODEL_KEY = "summarize_user_display_model"

# ---------------------------------------------------------------------------
# 默认值与限制常量
# ---------------------------------------------------------------------------
DEFAULT_BASE_URL = "https://api.openai.com/v1/"
DEFAULT_MODEL = "gpt-3.5-turbo"
DEFAULT_LIMIT = 100            # 默认抓取消息条数
MAX_LIMIT = 5000               # 群内模式最大允许抓取条数
REMOTE_MAX_LIMIT = 5000        # 远程模式最大允许抓取条数
CHUNK_MAX_CHARS = 30000        # Map-Reduce 每个分块的最大字符数
API_TIMEOUT_SECONDS = 120.0    # API 请求超时（秒）
TG_MSG_CHAR_LIMIT = 4096       # Telegram 单条消息字符上限
MAX_CONCURRENT_REQUESTS = 3    # 并发 API 请求上限

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------
DEFAULT_SYSTEM_PROMPT = (
    "你是一个专业的用户画像与言论分析专家。"
    "你将收到某用户在群组中的发言记录，每条格式为 [时间] 内容。\n"
    "请基于这些发言对该用户进行全面、客观的总结画像。\n\n"
    "**核心原则：**\n"
    "1. **客观真实**：严格基于用户实际发言内容分析，不得凭空推测。\n"
    "2. **零美化零偏见**：严禁任何讨好、夸大、美化或抹黑倾向，保持绝对中立。\n\n"
    "**输出结构（请严格按以下格式和顺序输出）：**\n\n"
    "第一行必须是一句话总结，格式如下（不要加任何标题前缀）：\n"
    "💬 用一句直白、犀利的话概括这个用户的整体特征和印象。\n\n"
    "然后空一行，输出以下各维度的详细分析：\n"
    "- **主要关注话题/领域**：用户经常讨论什么内容，核心兴趣点是什么。\n"
    "- **发言风格与情绪倾向**：语言风格特征（如正式/口语化、简洁/啰嗦），"
    "整体情绪基调（如平和、激动、讽刺、消极等）。\n"
    "- **活跃规律与社交特征**：根据时间戳分析活跃时段、发言频率，"
    "以及与他人的互动模式（如主动发起讨论、回应他人、还是自说自话）。\n"
    "- **性格特征与思维习惯**：体现出的性格特点，表达逻辑是否清晰，"
    "有无特定的口头禅或交流习惯。\n"
    "- **客观综合评价**：一段直白、客观、真实的综合评价。\n\n"
    "**格式要求：**\n"
    "- 第一行的一句话总结必须以 💬 开头，不超过 50 个字。\n"
    "- 详细分析部分使用双星号加粗（即 **文字**）、换行和短横线 - 进行排版。\n"
    "- 绝对不要使用 # 标题语法，因为 Telegram 无法渲染。\n"
    "- 不要使用编号列表（1. 2. 3.），统一使用 - 列表。"
)

MAP_SYSTEM_PROMPT = (
    "你是一个发言分析助手。你将收到某用户的一段发言记录片段，"
    "每条格式为 [时间] 内容。\n"
    "请对这段发言进行客观、精炼的要点提炼，包括：\n"
    "- 主要讨论话题和关键词\n"
    "- 发言风格和情绪特征\n"
    "- 活跃时段和发言频率特征\n"
    "- 值得注意的观点、立场或行为模式\n\n"
    "**要求：**\n"
    "- 保持客观中立，不做价值判断，只提炼事实特征。\n"
    "- 根据实际内容密度灵活调整篇幅，但不超过 600 字。\n"
    "- 不要使用 # 标题语法，仅用双星号加粗和短横线 - 排版。"
)

REDUCE_SYSTEM_PROMPT = (
    "你是一个专业的用户画像与言论分析专家。"
    "以下是对某用户不同时间段发言的多段局部分析结果。\n"
    "请将这些局部分析综合为一份完整、连贯、有深度的用户画像报告。\n\n"
    "**核心要求：**\n"
    "- **综合提炼**：提取各段分析中一致的特征，归纳为统一结论，而非简单罗列或拼接。\n"
    "- **关注变化**：如果不同时间段的分析之间存在矛盾或变化趋势，"
    "请指出并分析可能的原因（如兴趣转移、情绪波动等）。\n"
    "- **客观中立**：保持绝对中立，不美化不抹黑。\n\n"
    "**输出结构（请严格按以下格式和顺序输出）：**\n\n"
    "第一行必须是一句话总结，格式如下（不要加任何标题前缀）：\n"
    "💬 用一句直白、犀利的话概括这个用户的整体特征和印象。\n\n"
    "然后空一行，输出以下各维度的详细分析：\n"
    "- **主要关注话题/领域**\n"
    "- **发言风格与情绪倾向**\n"
    "- **活跃规律与社交特征**\n"
    "- **性格特征与思维习惯**\n"
    "- **客观综合评价**\n\n"
    "**格式要求：**\n"
    "- 第一行的一句话总结必须以 💬 开头，不超过 50 个字。\n"
    "- 详细分析部分使用双星号加粗（即 **文字**）、换行和短横线 - 进行排版。\n"
    "- 绝对不要使用 # 标题语法，因为 Telegram 无法渲染。\n"
    "- 不要使用编号列表（1. 2. 3.），统一使用 - 列表。"
)

# ---------------------------------------------------------------------------
# 模块级资源（连接池复用 & 并发控制）
# ---------------------------------------------------------------------------
_http_client = httpx.AsyncClient(
    timeout=httpx.Timeout(API_TIMEOUT_SECONDS, connect=15.0),
    limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
)
_api_semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

# ---------------------------------------------------------------------------
# 配置子命令映射（可扩展）
# ---------------------------------------------------------------------------
_CONFIG_COMMANDS = {
    "setapi": {
        "key": API_KEY_KEY,
        "label": "API_KEY",
        "sensitive": True,
    },
    "seturl": {
        "key": BASE_URL_KEY,
        "label": "BASE_URL",
        "sensitive": False,
        "validator": lambda v: v.startswith(("http://", "https://")),
        "validator_msg": "URL 必须以 http:// 或 https:// 开头。",
    },
    "setmodel": {
        "key": MODEL_KEY,
        "label": "MODEL",
        "sensitive": False,
    },
    "setdisplay": {
        "key": DISPLAY_MODEL_KEY,
        "label": "DISPLAY_MODEL",
        "sensitive": False,
        "join_args": True,
    },
    "setprompt": {
        "key": PROMPT_KEY,
        "label": "自定义 System Prompt",
        "sensitive": False,
        "join_args": True,
    },
}


# ===================================================================
# 辅助函数 — 文本处理
# ===================================================================


def _safe_truncate(text: str, max_len: int = TG_MSG_CHAR_LIMIT) -> str:
    """在段落或换行边界处智能截断文本，避免破坏格式标签。"""
    if len(text) <= max_len:
        return text

    suffix = "\n\n…(内容过长，已截断)"
    target = max_len - len(suffix)

    cut = text.rfind("\n\n", 0, target)
    if cut == -1:
        cut = text.rfind("\n", 0, target)
    if cut == -1:
        cut = text.rfind(" ", 0, target)
    if cut == -1:
        cut = target

    return text[:cut] + suffix


def _split_message(text: str, max_len: int = TG_MSG_CHAR_LIMIT) -> List[str]:
    """将长文本在段落边界处切分为多条消息。"""
    if len(text) <= max_len:
        return [text]

    chunks = []
    remaining = text

    while remaining:
        if len(remaining) <= max_len:
            chunks.append(remaining)
            break

        # 在 max_len 范围内寻找最佳切分点
        cut = remaining.rfind("\n\n", 0, max_len)
        if cut == -1 or cut < max_len // 2:
            cut = remaining.rfind("\n", 0, max_len)
        if cut == -1 or cut < max_len // 2:
            cut = remaining.rfind(" ", 0, max_len)
        if cut == -1 or cut < max_len // 4:
            cut = max_len

        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()

    return [c for c in chunks if c]


def _mask_sensitive(value: str) -> str:
    """对敏感值做脱敏显示，仅保留首尾各 4 个字符。"""
    if len(value) <= 10:
        return "****"
    return f"{value[:4]}{'*' * (len(value) - 8)}{value[-4:]}"


def _sanitize_error(error: object, api_key: Optional[str] = None) -> str:
    """过滤错误或响应预览中可能包含的敏感内容。"""
    msg = str(error)
    if api_key:
        msg = msg.replace(api_key, "****")
    msg = re.sub(r"(?i)(Bearer\s+)\S+", r"\1****", msg)
    msg = re.sub(r"\bsk-[A-Za-z0-9_-]+", "sk-****", msg)
    return msg


def _md_to_html(text: str) -> str:
    """将 LLM 返回的 Markdown 格式文本转换为 Telegram HTML 格式。

    处理规则：
    1. 转义 HTML 特殊字符（&, <, >）
    2. **加粗** → <b>加粗</b>
    3. `代码` → <code>代码</code>
    """
    # 先转义 HTML 特殊字符
    text = text.replace("&", "&amp;")
    text = text.replace("<", "&lt;")
    text = text.replace(">", "&gt;")
    # **加粗** → <b>加粗</b>
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    # `代码` → <code>代码</code>（单行内联代码）
    text = re.sub(r"`([^`]+?)`", r"<code>\1</code>", text)
    return text


def _split_summary(summary: str) -> Tuple[str, str]:
    """将 LLM 输出分离为一句话总结和详细分析。

    LLM 被要求以 💬 开头输出一句话总结，后面空行再输出详细分析。
    如果格式不符合预期，整体作为详细分析，一句话总结留空。

    Returns:
        (one_liner, detail) — one_liner 可能为空字符串
    """
    text = summary.strip()

    # 查找以 💬 开头的行
    if not text.startswith("💬"):
        # 也可能 LLM 在 💬 前面加了换行
        idx = text.find("💬")
        if idx != -1 and idx < 100:
            text = text[idx:]
        else:
            return "", summary.strip()

    # 以第一个空行为界拆分
    for sep in ("\n\n", "\n"):
        pos = text.find(sep)
        if pos != -1:
            one_liner = text[:pos].strip()
            detail = text[pos:].strip()
            if detail:
                return one_liner, detail
    
    # 没有找到分隔，整段就是一句话（极端情况）
    return text.strip(), ""


# ===================================================================
# 辅助函数 — 参数解析
# ===================================================================


def _parse_message_link(link: str) -> Tuple[Optional[object], Optional[int]]:
    """
    解析 Telegram 消息链接，提取 chat 标识和消息 ID。

    支持的格式：
      - 私密群组: https://t.me/c/1234567890/12345
      - 公开群组: https://t.me/groupname/12345

    返回:
        (chat_identifier, message_id)
        chat_identifier 可能是 int（私密群组 ID）或 str（公开群组用户名）。
        解析失败时返回 (None, None)。
    """
    # 私密群组链接: https://t.me/c/<chat_id>/<msg_id>
    m = re.match(r"https?://t\.me/c/(\d+)/(\d+)", link)
    if m:
        # Telegram 内部 ID 需加 -100 前缀
        chat_id = int(f"-100{m.group(1)}")
        msg_id = int(m.group(2))
        return chat_id, msg_id

    # 公开群组链接: https://t.me/<username>/<msg_id>
    m = re.match(r"https?://t\.me/([a-zA-Z_][a-zA-Z0-9_]{3,})/(\d+)", link)
    if m:
        username = m.group(1)
        msg_id = int(m.group(2))
        return username, msg_id

    return None, None


def _extract_flags(args: list) -> Tuple[list, Optional[str], Optional[str]]:
    """
    从参数列表中提取标志参数。

    支持的标志：
      -g / --group <群组标识>    指定远程群组
      -l / --link  <消息链接>    通过消息链接指定群组和用户

    返回:
        (clean_args, group_identifier, message_link)
    """
    if not args:
        return [], None, None

    clean = []
    group_id = None
    msg_link = None
    i = 0

    while i < len(args):
        if args[i] in ("-g", "--group") and i + 1 < len(args):
            group_id = args[i + 1]
            i += 2
        elif args[i] in ("-l", "--link") and i + 1 < len(args):
            msg_link = args[i + 1]
            i += 2
        else:
            clean.append(args[i])
            i += 1

    return clean, group_id, msg_link


# ===================================================================
# 辅助函数 — 配置管理
# ===================================================================


async def _handle_config(args: list, message: Message) -> Optional[bool]:
    """处理配置和工具子命令。返回 True 表示已处理，None 表示不是子命令。"""
    if not args:
        return None

    sub = args[0].lower()

    # --- getid ---
    if sub == "getid":
        lines = [f"📍 **当前对话 ID**: `{message.chat.id}`"]
        chat_title = getattr(message.chat, "title", None)
        if chat_title:
            lines.append(f"📛 **对话名称**: {chat_title}")
        if message.reply_to_message and message.reply_to_message.from_user:
            u = message.reply_to_message.from_user
            name = u.first_name or ""
            uname = f" (@{u.username})" if u.username else " (无用户名)"
            lines.append(f"👤 **用户 ID**: `{u.id}`（{name}{uname}）")
        lines.append("\n💡 可将以上 ID 用于远程分析：")
        lines.append("`,summarize_user -g <对话ID> [数量] <用户ID>`")
        await message.edit("\n".join(lines))
        return True

    # --- showconfig ---
    if sub == "showconfig":
        api_key = sqlite.get(API_KEY_KEY)
        base_url = sqlite.get(BASE_URL_KEY, DEFAULT_BASE_URL)
        model = sqlite.get(MODEL_KEY, DEFAULT_MODEL)
        has_custom_prompt = PROMPT_KEY in sqlite

        display_model = sqlite.get(DISPLAY_MODEL_KEY)
        lines = [
            "⚙️ **当前配置**\n",
            f"- **API_KEY**: {_mask_sensitive(api_key) if api_key else '❌ 未设置'}",
            f"- **BASE_URL**: `{base_url}`",
            f"- **MODEL**: `{model}`",
            f"- **显示名称**: `{display_model}`" if display_model else "- **显示名称**: 📝 默认（与 MODEL 相同）",
            f"- **System Prompt**: {'✅ 自定义' if has_custom_prompt else '📝 默认'}",
        ]
        await message.edit("\n".join(lines))
        return True

    # --- resetprompt ---
    if sub == "resetprompt":
        if PROMPT_KEY in sqlite:
            del sqlite[PROMPT_KEY]
        await message.edit("✅ 已恢复默认 System Prompt。")
        return True

    # --- 通用 set 命令 ---
    cfg = _CONFIG_COMMANDS.get(sub)
    if cfg is None:
        return None

    if len(args) < 2:
        await message.edit(f"❌ 请提供 {cfg['label']} 的值。")
        return True

    value = " ".join(args[1:]) if cfg.get("join_args") else args[1]

    validator = cfg.get("validator")
    if validator and not validator(value):
        await message.edit(f"❌ {cfg.get('validator_msg', '输入值格式不正确。')}")
        return True

    sqlite[cfg["key"]] = value

    if cfg.get("sensitive"):
        try:
            await message.delete()
        except Exception:
            pass
        try:
            await message.reply(
                f"✅ 已成功设置 {cfg['label']}（出于安全考虑，包含密钥的消息已自动删除）",
            )
        except Exception:
            log.warning("无法发送 setapi 确认消息")
    else:
        await message.edit(f"✅ 已成功设置 {cfg['label']}。")

    return True


# ===================================================================
# 辅助函数 — 用户解析
# ===================================================================


async def _resolve_target(
    client: Client,
    message: Message,
    args: list,
    is_remote: bool,
) -> Tuple[Optional[object], int]:
    """
    解析目标用户和抓取数量。

    Args:
        is_remote: 是否为远程模式（影响 limit 上界和回复消息的支持）

    返回:
        (target_user, limit) — target_user 为 None 时表示解析失败
    """
    limit = DEFAULT_LIMIT
    max_limit = REMOTE_MAX_LIMIT if is_remote else MAX_LIMIT
    target_user = None

    # 远程模式下不支持回复消息（你不在那个群里）
    if not is_remote and message.reply_to_message:
        if message.reply_to_message.from_user:
            target_user = message.reply_to_message.from_user
        else:
            await message.edit("❌ 该消息来自匿名管理员或频道身份，无法识别具体用户。")
            return None, limit

        if args and args[0].isdigit():
            limit = int(args[0])
    else:
        if not args:
            if is_remote:
                await message.edit(
                    "❌ 远程模式下必须指定目标用户。\n\n"
                    "用法：`,summarize_user -g <群组> [数量] <用户>`"
                )
            else:
                await message.edit(
                    "❌ 请回复某位用户的消息，或在命令后加上 用户ID/用户名。\n\n"
                    "用法示例：\n"
                    "• 回复消息后发送 `,summarize_user`\n"
                    "• `,summarize_user @username`\n"
                    "• `,summarize_user -g @group 2000 @username`"
                )
            return None, limit

        user_arg = None

        if len(args) >= 2 and args[0].isdigit():
            limit = int(args[0])
            user_arg = args[1]
        elif len(args) == 1 and args[0].isdigit():
            try:
                target_user = await client.get_users(int(args[0]))
            except Exception:
                await message.edit(
                    f"❌ 无法识别 `{args[0]}` 是用户 ID 还是抓取数量。\n\n"
                    "请使用更明确的格式：\n"
                    "• `,summarize_user 200 @username`\n"
                    "• `,summarize_user -g @group 2000 @username`"
                )
                return None, limit
        else:
            user_arg = args[0]

        if not target_user and user_arg:
            try:
                target_user = await client.get_users(user_arg)
            except Exception:
                await message.edit("❌ 无法识别目标用户，请确保输入的用户名或 ID 正确。")
                return None, limit

    if not target_user:
        await message.edit("❌ 无法识别目标用户，请确保输入的用户名或 ID 正确。")
        return None, limit

    # 限制上界
    if limit > max_limit:
        limit = max_limit
        log.info("limit 被裁剪至 %d", max_limit)
    if limit < 1:
        limit = DEFAULT_LIMIT

    return target_user, limit


# ===================================================================
# 辅助函数 — 消息抓取
# ===================================================================


async def _fetch_messages(
    client: Client,
    chat_id: int,
    user_id: int,
    limit: int,
) -> List[str]:
    """
    抓取目标用户的文本消息，附带时间戳。

    limit 控制的是收集到的 **文本消息** 数量，而非 Telegram 返回的总消息数。
    图片、贴纸、语音等无文本内容的消息会被跳过，不计入 limit。
    """
    texts = []

    # limit=0 表示不限制 search_messages 返回数量，
    # 由我们自己计数文本消息并在达到目标时终止。
    async for msg in client.search_messages(chat_id, from_user=user_id, limit=0):
        content = msg.text or msg.caption
        if not content:
            continue

        date_str = msg.date.strftime("%Y-%m-%d %H:%M") if msg.date else ""
        texts.append(f"[{date_str}] {content}")

        if len(texts) >= limit:
            break

    # 时间倒序翻转：最老的在前面
    texts.reverse()
    return texts


# ===================================================================
# 辅助函数 — 分块
# ===================================================================


def _chunk_texts(texts: List[str], chunk_max_chars: int = CHUNK_MAX_CHARS) -> List[str]:
    """
    将消息文本列表按字符数切分为多个块。
    每个块是一个拼接好的字符串，不超过 chunk_max_chars。
    """
    chunks = []
    current_chunk_lines = []
    current_chars = 0

    for entry in texts:
        entry_len = len(entry) + 1  # +1 for \n
        if current_chars + entry_len > chunk_max_chars and current_chunk_lines:
            chunks.append("\n".join(current_chunk_lines))
            current_chunk_lines = []
            current_chars = 0

        current_chunk_lines.append(entry)
        current_chars += entry_len

    if current_chunk_lines:
        chunks.append("\n".join(current_chunk_lines))

    return chunks


# ===================================================================
# 辅助函数 — LLM 调用
# ===================================================================


def _build_chat_completions_url(base_url: str) -> str:
    """由 API 基础地址或完整端点构建 Chat Completions 地址。"""
    clean_url = base_url.strip()
    parts = urlsplit(clean_url)
    path = parts.path.rstrip("/")
    if not path.endswith("/chat/completions"):
        path += "/chat/completions"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


def _response_preview(
    value: object,
    api_key: Optional[str] = None,
    max_chars: int = 300,
) -> str:
    """生成适合直接展示的、截断且脱敏的响应预览。"""
    if isinstance(value, (dict, list)):
        try:
            text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            text = str(value)
    else:
        text = str(value or "")

    text = _sanitize_error(text, api_key).replace("\x00", "")
    if not text:
        return "<空响应>"
    return text[:max_chars] + ("…" if len(text) > max_chars else "")


def _extract_api_error(data: object) -> Optional[str]:
    """提取 HTTP 200 响应中常见的 OpenAI 风格错误。"""
    if not isinstance(data, dict) or "error" not in data:
        return None

    error = data["error"]
    if isinstance(error, str):
        return error.strip() or None
    if not isinstance(error, dict):
        return None

    message = error.get("message")
    details = [str(error[key]) for key in ("type", "code") if error.get(key)]
    if message:
        suffix = f" ({'/'.join(details)})" if details else ""
        return f"{message}{suffix}"
    return _response_preview(error)


def _extract_content(data: object) -> Optional[str]:
    """从 OpenAI Chat Completions 兼容响应中提取最终文本。"""
    if not isinstance(data, dict):
        return None

    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return None

    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        return None

    message = first_choice.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            return content.strip() or None
        if isinstance(content, list):
            texts = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                text = item.get("text")
                if item.get("type") in ("text", "output_text") and isinstance(
                    text, str
                ):
                    if text.strip():
                        texts.append(text.strip())
            if texts:
                return "\n".join(texts)

    # 少数兼容网关会在 Chat Completions 端点返回 legacy choice.text。
    legacy_text = first_choice.get("text")
    if isinstance(legacy_text, str) and legacy_text.strip():
        return legacy_text.strip()

    return None


async def _call_llm(
    text: str,
    system_prompt: str,
    api_key: str,
    base_url: str,
    model: str,
) -> str:
    """调用 OpenAI 兼容 API，返回模型生成的文本。"""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ],
    }

    endpoint = _build_chat_completions_url(base_url)

    async with _api_semaphore:
        try:
            response = await _http_client.post(endpoint, json=payload, headers=headers)
            response.raise_for_status()
        except httpx.TimeoutException:
            raise Exception("API 请求超时，请检查网络连接或稍后重试。")
        except httpx.HTTPStatusError as e:
            body_preview = _response_preview(e.response.text, api_key)
            raise Exception(
                f"API 返回 HTTP {e.response.status_code}。\n"
                f"实际响应预览：{body_preview}"
            )
        except httpx.RequestError as e:
            raise Exception(f"API 请求失败：{_sanitize_error(e, api_key)}")

    try:
        data = response.json()
    except ValueError:
        preview = _response_preview(response.text, api_key)
        raise Exception(
            "API 返回了无法解析的非 JSON 响应。\n"
            f"实际响应预览：{preview}"
        )

    api_error = _extract_api_error(data)
    if api_error:
        raise Exception(f"API 返回错误：{_sanitize_error(api_error, api_key)}")

    content = _extract_content(data)

    if content is None:
        preview = _response_preview(data, api_key)
        log.error("API 响应结构异常: %s", preview)
        raise Exception(
            "API 返回了无法识别的 OpenAI 兼容响应结构。\n"
            f"实际响应预览：{preview}"
        )

    return content


async def _map_reduce_summary(
    texts: List[str],
    api_key: str,
    base_url: str,
    model: str,
    progress_callback=None,
) -> str:
    """
    Map-Reduce 分块总结。

    - 单块时直接使用用户自定义 / 默认 Prompt 生成总结。
    - 多块时：Map 阶段并发生成局部摘要，Reduce 阶段合并为最终画像。

    Args:
        progress_callback: 可选的 async 回调函数，接收进度文本。
    """
    chunks = _chunk_texts(texts)
    total_chunks = len(chunks)

    if total_chunks == 1:
        # 单块：直接调用完整画像 Prompt
        user_prompt = sqlite.get(PROMPT_KEY, DEFAULT_SYSTEM_PROMPT)
        return await _call_llm(
            f"以下是该用户的近期发言记录：\n\n{chunks[0]}",
            user_prompt,
            api_key,
            base_url,
            model,
        )

    # --- Map 阶段：并发生成各块的局部摘要 ---
    if progress_callback:
        await progress_callback(
            f"⏳ 消息量较大，将分 **{total_chunks}** 块进行分析..."
        )

    async def _map_one(idx: int, chunk: str) -> str:
        if progress_callback:
            await progress_callback(
                f"⏳ 正在分析第 **{idx + 1}/{total_chunks}** 块..."
            )
        return await _call_llm(
            f"以下是该用户的第 {idx + 1}/{total_chunks} 段发言记录：\n\n{chunk}",
            MAP_SYSTEM_PROMPT,
            api_key,
            base_url,
            model,
        )

    # 并发执行 Map（受 _api_semaphore 限制并发数）
    map_tasks = [_map_one(i, chunk) for i, chunk in enumerate(chunks)]
    partial_summaries = await asyncio.gather(*map_tasks, return_exceptions=True)

    # 收集成功的局部摘要，记录失败的
    successful = []
    failures = []
    for i, result in enumerate(partial_summaries):
        if isinstance(result, Exception):
            log.warning("第 %d/%d 块分析失败: %s", i + 1, total_chunks, result)
            failures.append(result)
        else:
            successful.append(f"**第 {i + 1} 段分析：**\n{result}")

    if not successful:
        first_error = _sanitize_error(failures[0]) if failures else "未知错误"
        raise Exception(f"所有分块分析均失败。首个错误：{first_error}")

    # --- Reduce 阶段：合并所有局部摘要 ---
    if progress_callback:
        await progress_callback(
            f"⏳ 已完成 {len(successful)}/{total_chunks} 块分析，正在生成综合画像..."
        )

    combined_partials = "\n\n---\n\n".join(successful)
    return await _call_llm(
        f"以下是对该用户 {len(successful)} 个时间段发言的局部分析结果：\n\n{combined_partials}",
        REDUCE_SYSTEM_PROMPT,
        api_key,
        base_url,
        model,
    )


# ===================================================================
# 主命令
# ===================================================================


@listener(
    is_plugin=True,
    command="summarize_user",
    description=(
        "对用户发言进行总结画像\n\n"
        "消息链接分析（私密群/无用户名皆可）：\n"
        "  ,summarize_user -l <消息链接> [数量]\n\n"
        "远程分析（零痕迹）：\n"
        "  ,summarize_user -g <群组> [数量] <用户>\n\n"
        "群内分析：\n"
        "  ,summarize_user [数量] [用户]\n\n"
        "工具：getid\n"
        "配置：setapi / seturl / setmodel / setdisplay / setprompt / resetprompt / showconfig"
    ),
    parameters="[-l <链接>] [-g <群组>] [数量] [用户] / getid / setapi / setdisplay / showconfig",
)
async def summarize_user(client: Client, message: Message) -> None:
    args = message.parameter or []

    # ---- 1. 配置/工具子命令 ----
    handled = await _handle_config(args, message)
    if handled:
        return

    # ---- 2. 检查 API 配置 ----
    api_key = sqlite.get(API_KEY_KEY)
    base_url = sqlite.get(BASE_URL_KEY, DEFAULT_BASE_URL)
    model = sqlite.get(MODEL_KEY, DEFAULT_MODEL)

    if not api_key:
        return await message.edit(
            "❌ 未设置 API_KEY，请使用 `,summarize_user setapi <API_KEY>` 进行设置。"
        )

    # ---- 3. 提取标志参数 ----
    clean_args, group_identifier, message_link = _extract_flags(args)

    # ---- 4. 根据模式确定 chat_id 和 target_user ----
    target_user = None
    search_chat_id = None
    group_title = None
    is_remote = False  # 是否为远程模式（-g 或 -l）

    if message_link:
        # ---- 模式 A：消息链接模式 (-l) ----
        is_remote = True
        chat_identifier, msg_id = _parse_message_link(message_link)

        if chat_identifier is None or msg_id is None:
            return await message.edit(
                "❌ 无法解析消息链接。\n\n"
                "支持的格式：\n"
                "• `https://t.me/c/1234567890/12345`（私密群组）\n"
                "• `https://t.me/groupname/12345`（公开群组）\n\n"
                "💡 在群聊中长按消息 → 复制消息链接"
            )

        # 获取群组信息
        try:
            target_chat = await client.get_chat(chat_identifier)
            search_chat_id = target_chat.id
            group_title = getattr(target_chat, "title", None) or str(target_chat.id)
        except Exception:
            return await message.edit(
                f"❌ 无法访问消息链接中的群组。\n"
                "请确保你已加入该群组。"
            )

        # 通过消息 ID 获取发送者
        try:
            linked_msg = await client.get_messages(search_chat_id, msg_id)
        except Exception:
            return await message.edit("❌ 无法获取链接指向的消息，请确认链接有效。")

        if not linked_msg or not linked_msg.from_user:
            return await message.edit(
                "❌ 无法从该消息中识别发送者。\n"
                "（可能是匿名管理员或频道身份发送的消息）"
            )

        target_user = linked_msg.from_user

        # 解析数量（clean_args 里只剩数量参数）
        limit = DEFAULT_LIMIT
        if clean_args and clean_args[0].isdigit():
            limit = int(clean_args[0])

    elif group_identifier:
        # ---- 模式 B：远程群组模式 (-g) ----
        is_remote = True
        try:
            target_chat = await client.get_chat(group_identifier)
            search_chat_id = target_chat.id
            group_title = getattr(target_chat, "title", None) or str(target_chat.id)
        except Exception:
            return await message.edit(
                f"❌ 无法访问群组 `{group_identifier}`。\n"
                "请确保群组标识正确且你已加入该群组。"
            )

    else:
        # ---- 模式 C：群内模式 ----
        if message.chat.type in (ChatType.PRIVATE, ChatType.BOT):
            return await message.edit(
                "❌ 在私聊中使用请指定目标群组。\n\n"
                "用法：\n"
                "• `,summarize_user -l <消息链接> [数量]`\n"
                "• `,summarize_user -g <群组> [数量] <用户>`\n\n"
                "💡 私密群组/无用户名？长按群内消息 → 复制链接 → 用 `-l`"
            )
        search_chat_id = message.chat.id
        group_title = getattr(message.chat, "title", None) or str(message.chat.id)

    # ---- 5. 解析目标用户（如果尚未通过 -l 获取） ----
    if not target_user:
        target_user, limit = await _resolve_target(
            client, message, clean_args, is_remote
        )
        if not target_user:
            return
    else:
        # -l 模式下 limit 已在上面解析，但仍需校验上界
        max_limit = REMOTE_MAX_LIMIT if is_remote else MAX_LIMIT
        if limit > max_limit:
            limit = max_limit
        if limit < 1:
            limit = DEFAULT_LIMIT

    display_name = target_user.first_name or str(target_user.id)

    # 进度消息：始终编辑当前对话中的命令消息
    await message.edit(
        f"⏳ 正在从 **{group_title}** 抓取 **{display_name}** 的消息"
        f"（上限 {limit} 条）..."
    )

    # ---- 6. 抓取消息 ----
    try:
        texts = await _fetch_messages(client, search_chat_id, target_user.id, limit)
    except Exception as e:
        return await message.edit(
            f"❌ 抓取消息时出错：{_sanitize_error(e)}\n\n"
            "(提示：可能是因为没有该群组历史消息的权限，或该用户未在此群发送过消息)"
        )

    if not texts:
        return await message.edit(
            f"❌ 未能在 **{group_title}** 中抓取到 **{display_name}** 的任何文本消息。"
        )

    await message.edit(
        f"⏳ 成功提取了 **{len(texts)}** 条消息，正在分析，请稍候..."
    )

    # ---- 7. Map-Reduce 分块分析 ----
    # 进度回调：更新当前对话中的状态消息
    async def _progress(text: str):
        try:
            await message.edit(text)
        except Exception:
            pass  # 编辑频率过高时可能失败，静默忽略

    try:
        summary = await _map_reduce_summary(
            texts, api_key, base_url, model, progress_callback=_progress
        )
    except Exception as e:
        return await message.edit(
            f"❌ {_sanitize_error(e, api_key)}",
            parse_mode=ParseMode.DISABLED,
        )

    # ---- 8. 输出结果 ----
    # 确定输出中显示的模型名称
    display_model = sqlite.get(DISPLAY_MODEL_KEY, model)

    # 分离一句话总结和详细分析
    one_liner, detail = _split_summary(summary)

    # 分别转为 HTML
    one_liner_html = _md_to_html(one_liner) if one_liner else ""
    detail_html = _md_to_html(detail) if detail else _md_to_html(summary)

    # HTML 转义动态内容
    dn_safe = display_name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    gt_safe = group_title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    dm_safe = display_model.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # 组装输出：头部元信息 + 一句话总结 + 可折叠详细分析
    result_header = (
        f"<blockquote>"
        f"👤 <b>{dn_safe}</b> 的用户画像\n"
        f"📍 群组：<b>{gt_safe}</b>\n"
        f"📊 基于最近 <b>{len(texts)}</b> 条发言\n"
        f"🤖 模型：<b>{dm_safe}</b>"
        f"</blockquote>\n"
    )

    # 一句话总结（直接展示）
    summary_line = f"{one_liner_html}\n\n" if one_liner_html else ""

    # 详细分析（折叠）
    detail_block = (
        f"<blockquote expandable>{detail_html}</blockquote>"
        if detail_html else ""
    )

    result_text = result_header + summary_line + detail_block

    if is_remote:
        # 远程模式：分条发送完整结果到当前对话（通常是收藏夹）
        parts = _split_message(result_text)
        await message.edit(parts[0], parse_mode=ParseMode.HTML)
        for part in parts[1:]:
            await client.send_message(
                message.chat.id, part, parse_mode=ParseMode.HTML
            )
    else:
        # 群内模式：截断后编辑消息
        await message.edit(
            _safe_truncate(result_text), parse_mode=ParseMode.HTML
        )
