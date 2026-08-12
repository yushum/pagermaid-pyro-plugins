"""
PagerMaid-Pyro 自动删除消息插件 (autodel)

功能：
  - 设置当前聊天 / 全局的消息自动删除定时
  - 将特定聊天排除在全局规则之外（解决"全局开、个别关"的需求）
  - 通过 -c 远程管理任意聊天的规则（无需在目标聊天内执行命令）
  - 查看所有设置的统一状态面板

优先级规则：
  聊天专属定时 > 排除标记 > 全局定时

存储设计：
  autodel.{cid}            → int   聊天/全局定时（秒）
  autodel.excl.{cid}       → True  排除标记
  autodel.job.{cid}.{mid}  → dict  单条待删任务（运行期以内存为权威态，
                                    DB 仅用于重启恢复，每条消息只产生一次小键写入）
"""

import asyncio
import contextlib
import heapq
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from pyrogram import errors as pyrogram_errors
from pyrogram.errors import FloodWait

from pagermaid.services import bot, sqlite
from pagermaid.enums import Message
from pagermaid.listener import listener
from pagermaid.utils import alias_command, logs


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  常量定义与数据结构
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_KEY_PREFIX = "autodel"        # 定时器键: autodel.{cid} → int (秒)
_EXCL_PREFIX = "autodel.excl"  # 排除键:   autodel.excl.{cid} → True
_JOB_PREFIX = "autodel.job"    # 任务键:   autodel.job.{cid}.{mid} → dict
_LEGACY_IDX_PREFIX = "autodel.idx"  # 旧版本索引键，迁移时清除
_GLOBAL_CID = 0                # 全局定时使用的虚拟 chat id

# 任务来源（决定 cancel/exclude 时的精确清理范围）
_SOURCE_CHAT = "chat"
_SOURCE_GLOBAL = "global"

_TIME_UNITS = {
    "s": 1, "sec": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
}

_MIN_SECONDS = 1
_MAX_SECONDS = 30 * 86400
_MAX_DISPLAY_ITEMS = 20
_DELETE_BATCH_SIZE = 100       # 单次 delete_messages 的最大消息数
_MAX_RETRIES = 3               # 未知错误的最大重试次数
_MAX_FLOOD_RETRIES = 10        # FloodWait 重排上限，防止无限循环
_NAME_FETCH_TIMEOUT = 1.5      # 获取聊天名称的超时（秒）
_NAME_FETCH_CONCURRENCY = 5    # 并发获取聊天名称的信号量

# 删除失败时应直接放弃任务的 Pyrogram 异常类型（按类型匹配，杜绝字符串嗅探）。
# 使用 getattr 防御性获取，兼容不同 Pyrogram fork 的类名差异。
_DROP_ERRORS = tuple(
    exc for exc in (
        getattr(pyrogram_errors, name, None)
        for name in (
            "MessageDeleteForbidden",  # 无权删除
            "ChatAdminRequired",       # 需要管理员权限
            "ChannelPrivate",          # 已被踢出/频道私有
            "ChannelInvalid",          # 频道无效
            "PeerIdInvalid",           # 聊天 ID 无效
            "MessageIdInvalid",        # 消息已不存在
            "MessageIdsEmpty",         # 消息 ID 列表为空
        )
    ) if exc is not None
)


@dataclass(order=True)
class DeleteJob:
    due_at: int
    chat_id: int = field(compare=False)
    message_id: int = field(compare=False)
    source: str = field(compare=False)  # _SOURCE_CHAT / _SOURCE_GLOBAL
    retry_count: int = field(compare=False, default=0)
    flood_count: int = field(compare=False, default=0)

    @property
    def key(self) -> Tuple[int, int]:
        return (self.chat_id, self.message_id)

    def to_dict(self) -> dict:
        return {
            "due_at": self.due_at,
            "source": self.source,
            "retry_count": self.retry_count,
            "flood_count": self.flood_count,
        }

    @classmethod
    def from_dict(cls, cid: int, mid: int, data: dict) -> "DeleteJob":
        return cls(
            due_at=data.get("due_at", 0),
            chat_id=cid,
            message_id=mid,
            source=data.get("source", _SOURCE_CHAT),
            retry_count=data.get("retry_count", 0),
            flood_count=data.get("flood_count", 0),
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  数据操作层（定时器与排除标记）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _timer_key(cid: int) -> str:
    return f"{_KEY_PREFIX}.{cid}"

def _excl_key(cid: int) -> str:
    return f"{_EXCL_PREFIX}.{cid}"

def _job_key(cid: int, mid: int) -> str:
    return f"{_JOB_PREFIX}.{cid}.{mid}"

def _get_timer(cid: int) -> int:
    value = sqlite.get(_timer_key(cid), 0)
    # 类型防御：外部写入非 int 时视为未设置，避免比较/运算处抛错
    return value if isinstance(value, int) else 0

def _set_timer(cid: int, seconds: int) -> None:
    sqlite[_timer_key(cid)] = seconds

def _del_timer(cid: int) -> bool:
    key = _timer_key(cid)
    if key in sqlite:
        del sqlite[key]
        return True
    return False

def _is_excluded(cid: int) -> bool:
    return bool(sqlite.get(_excl_key(cid), False))

def _set_excluded(cid: int, excluded: bool) -> None:
    key = _excl_key(cid)
    if excluded:
        sqlite[key] = True
    elif key in sqlite:
        del sqlite[key]


def _scan_keys(prefix: str) -> List[Tuple[str, int]]:
    """扫描形如 {prefix}.{cid} 的键。依赖 int() 解析天然过滤掉更深层级的键。"""
    prefix_dot = f"{prefix}."
    results = []
    try:
        for key in sqlite.keys():
            if isinstance(key, str) and key.startswith(prefix_dot):
                try:
                    cid = int(key[len(prefix_dot):])
                    results.append((key, cid))
                except (ValueError, TypeError):
                    continue
    except Exception as e:
        logs.error(f"[autodel] 扫描数据库键 {prefix} 时发生异常: {e}", exc_info=True)
    return results

def _get_all_timers() -> List[Tuple[int, int]]:
    results = []
    for key, cid in _scan_keys(_KEY_PREFIX):
        seconds = sqlite.get(key, 0)
        if isinstance(seconds, int) and seconds > 0:
            results.append((cid, seconds))
    return results

def _get_all_excluded() -> List[int]:
    return [cid for key, cid in _scan_keys(_EXCL_PREFIX)
            if sqlite.get(key, False)]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  参数解析工具
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _extract_chat_target(args: str) -> Tuple[str, List[int]]:
    words = args.split()
    if "-c" not in words:
        return args, []

    if words.count("-c") > 1:
        raise ValueError("只能指定一个 `-c` 标志，多 ID 请用逗号分隔")

    idx = words.index("-c")
    if idx + 1 >= len(words):
        raise ValueError("请在 `-c` 后面指定聊天 ID，如 `-c -100123,-100456`")

    cids_str = words[idx + 1]
    cids = []
    for c in cids_str.split(","):
        if not c.strip():
            continue
        try:
            cids.append(int(c.strip()))
        except ValueError:
            raise ValueError(f"'{c.strip()}' 不是有效的聊天 ID") from None

    if not cids:
        raise ValueError("请指定有效的聊天 ID")

    remaining = words[:idx] + words[idx + 2:]
    return " ".join(remaining), cids


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  时间解析与格式化
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def parse_time(text: str) -> int:
    tokens = text.strip().split()

    if not tokens:
        raise ValueError("请输入时间，如 '30 seconds'")

    if len(tokens) % 2 != 0:
        raise ValueError(
            "格式错误：请使用 '数值 单位' 的配对格式\n"
            "例如: 30 seconds 或 1 hours 30 minutes"
        )

    total_seconds = 0
    seen_units = set()

    for i in range(0, len(tokens), 2):
        value_str, unit_str = tokens[i], tokens[i + 1].lower()
        try:
            value = int(value_str)
        except ValueError:
            raise ValueError(f"'{value_str}' 不是有效的数字") from None

        if value < 0:
            raise ValueError(f"时间值不能为负数: {value}")

        if unit_str not in _TIME_UNITS:
            raise ValueError(
                f"未知的时间单位: '{unit_str}'\n"
                "支持: s/seconds, m/minutes, h/hours, d/days"
            )

        base_seconds = _TIME_UNITS[unit_str]
        if base_seconds in seen_units:
            raise ValueError(f"时间单位重复: '{unit_str}'")
        seen_units.add(base_seconds)

        total_seconds += value * base_seconds

    if total_seconds < _MIN_SECONDS:
        raise ValueError(f"总时间不能小于 {_MIN_SECONDS} 秒")
    if total_seconds > _MAX_SECONDS:
        raise ValueError(f"总时间不能超过 {_MAX_SECONDS // 86400} 天")

    return total_seconds


def format_duration(total_seconds: int) -> str:
    if total_seconds <= 0:
        return "0 秒"
    remainder = total_seconds
    parts = []
    for unit_name, unit_secs in [("天", 86400), ("小时", 3600), ("分钟", 60), ("秒", 1)]:
        if remainder >= unit_secs:
            count, remainder = divmod(remainder, unit_secs)
            parts.append(f"{count} {unit_name}")
    return " ".join(parts)


def _get_client(message: Message):
    """获取 Pyrogram Client。Message 上该属性为私有 `_client`，
    不同版本命名可能有差异，逐级回退到 pagermaid 全局 bot 实例。"""
    return getattr(message, "_client", None) or getattr(message, "client", None) or bot


def _escape_md(text: str) -> str:
    """转义聊天标题中的 Markdown 控制字符，防止状态面板被注入格式。"""
    for ch in ("\\", "`", "*", "_", "["):
        text = text.replace(ch, f"\\{ch}")
    return text


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  调度器模块
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def resolve_timer(cid: int) -> Tuple[int, str]:
    """解析生效定时及来源，返回 (秒数, 规则来源)"""
    chat_seconds = _get_timer(cid)
    if chat_seconds > 0:
        return chat_seconds, _SOURCE_CHAT

    if _is_excluded(cid):
        return 0, "none"

    global_seconds = _get_timer(_GLOBAL_CID)
    if global_seconds > 0:
        return global_seconds, _SOURCE_GLOBAL

    return 0, "none"


class AutoDeleteScheduler:
    """堆调度 + 懒删除。

    运行期以内存 (live / job_heap) 为权威态，SQLite 仅承担重启恢复职责：
    每条消息只产生一次 O(1) 的小键写入，不再有全量索引读写。
    唤醒采用 asyncio.Event，规避 wait_for(Condition.wait()) 的取消竞态。
    """

    def __init__(self):
        self.job_heap: List[DeleteJob] = []
        self.live: Dict[Tuple[int, int], DeleteJob] = {}
        self.chat_counts: Counter = Counter()
        self.wakeup = asyncio.Event()
        self.worker_task: Optional[asyncio.Task] = None

    # ── 生命周期 ──

    def ensure_worker(self, client) -> None:
        """幂等启动 worker；若上一个 worker 已死亡则自动拉起新的。"""
        if self.worker_task is not None and not self.worker_task.done():
            return
        if self.worker_task is not None:
            exc = None
            if not self.worker_task.cancelled():
                exc = self.worker_task.exception()
            logs.warning(f"[autodel] 检测到 worker 已退出 (exc={exc!r})，正在重启")
        self.worker_task = asyncio.create_task(self.worker(client))

    async def init(self) -> None:
        """从 DB 恢复任务。

        扫描期间仍可能有新任务入队或旧任务被取消，因此不能
        直接覆盖内存态。恢复前再确认 DB 键仍存在，并与已入队
        的任务合并，可同时避免丢任务和复活已取消任务。
        """
        jobs = await asyncio.to_thread(self._load_jobs_from_db)
        for job in jobs:
            if not isinstance(sqlite.get(_job_key(job.chat_id, job.message_id)), dict):
                continue
            if job.key in self.live:
                continue
            self.live[job.key] = job
            self.chat_counts[job.chat_id] += 1
            heapq.heappush(self.job_heap, job)

    def _load_jobs_from_db(self) -> List[DeleteJob]:
        """加载全部任务；顺带迁移 v1 (含时间戳键) 与清理 v2 遗留索引键。"""
        jobs: List[DeleteJob] = []
        legacy_keys: List[str] = []
        job_prefix = f"{_JOB_PREFIX}."
        idx_prefix = f"{_LEGACY_IDX_PREFIX}."
        try:
            for key in list(sqlite.keys()):
                if not isinstance(key, str):
                    continue
                if key.startswith(job_prefix):
                    parts = key.split(".")
                    if len(parts) == 4:  # v2/v3: autodel.job.{cid}.{mid}
                        try:
                            cid, mid = int(parts[2]), int(parts[3])
                        except ValueError:
                            continue
                        data = sqlite.get(key)
                        if isinstance(data, dict):
                            jobs.append(DeleteJob.from_dict(cid, mid, data))
                    elif len(parts) == 5:  # v1: autodel.job.{ts}.{cid}.{mid}
                        try:
                            ts, cid, mid = int(parts[2]), int(parts[3]), int(parts[4])
                        except ValueError:
                            continue
                        job = DeleteJob(due_at=ts, chat_id=cid,
                                        message_id=mid, source=_SOURCE_CHAT)
                        jobs.append(job)
                        sqlite[_job_key(cid, mid)] = job.to_dict()
                        legacy_keys.append(key)
                elif key.startswith(idx_prefix):
                    legacy_keys.append(key)  # v2 索引结构已废弃
        except Exception as e:
            logs.error(f"[autodel] 恢复任务时发生异常: {e}", exc_info=True)
        for key in legacy_keys:
            with contextlib.suppress(KeyError):
                del sqlite[key]
        if legacy_keys:
            logs.info(f"[autodel] 已迁移/清理 {len(legacy_keys)} 个旧版本存储键")
        return jobs

    # ── 任务增删 ──

    def add_job(self, job: DeleteJob) -> bool:
        key = job.key
        if key not in self.live:
            self.chat_counts[job.chat_id] += 1
        self.live[key] = job
        heapq.heappush(self.job_heap, job)
        sqlite[_job_key(job.chat_id, job.message_id)] = job.to_dict()
        self.wakeup.set()
        return True

    def _db_del(self, cid: int, mid: int) -> None:
        with contextlib.suppress(KeyError):
            del sqlite[_job_key(cid, mid)]

    def _forget(self, cid: int, mid: int) -> None:
        """从内存与 DB 中彻底移除一个任务。"""
        job = self.live.pop((cid, mid), None)
        if job is not None:
            self.chat_counts[cid] -= 1
            if self.chat_counts[cid] <= 0:
                del self.chat_counts[cid]
        self._db_del(cid, mid)

    def remove_jobs(self, cids: Optional[List[int]] = None,
                    source: Optional[str] = None) -> int:
        """按聊天与来源精确清理排队任务。

        cids=None 表示所有聊天；source=None 表示不限来源。
        堆中残留的条目由懒删除（live 身份校验）自然失效。
        """
        cid_set = set(cids) if cids is not None else None
        victims = [
            key for key, job in self.live.items()
            if (cid_set is None or job.chat_id in cid_set)
            and (source is None or job.source == source)
        ]
        for cid, mid in victims:
            self._forget(cid, mid)
        if victims:
            self.wakeup.set()
        return len(victims)

    def clear_all_jobs(self) -> None:
        """清空内存态并删除 DB 中所有任务键（含孤儿键）。"""
        self.job_heap.clear()
        self.live.clear()
        self.chat_counts.clear()
        job_prefix = f"{_JOB_PREFIX}."
        try:
            for key in list(sqlite.keys()):
                if isinstance(key, str) and key.startswith(job_prefix):
                    with contextlib.suppress(KeyError):
                        del sqlite[key]
        except Exception as e:
            logs.error(f"[autodel] 清空任务键时发生异常: {e}", exc_info=True)
        self.wakeup.set()

    # ── 消费循环 ──

    def _pop_expired(self, now: int) -> List[DeleteJob]:
        """弹出所有到期且仍存活的任务，并将其转为『在途』状态（移出 live）。

        通过 `live.get(key) is job` 的对象身份校验实现懒删除：
        被取消或已重排的任务在堆中的旧条目会被静默丢弃。
        """
        expired: List[DeleteJob] = []
        while self.job_heap and self.job_heap[0].due_at <= now:
            job = heapq.heappop(self.job_heap)
            if self.live.get(job.key) is job:
                del self.live[job.key]
                self.chat_counts[job.chat_id] -= 1
                if self.chat_counts[job.chat_id] <= 0:
                    del self.chat_counts[job.chat_id]
                expired.append(job)
        return expired

    def _next_delay(self) -> Optional[float]:
        if not self.job_heap:
            return None
        return max(0.0, self.job_heap[0].due_at - time.time())

    async def worker(self, client) -> None:
        await self.init()
        while True:
            try:
                self.wakeup.clear()
                expired = self._pop_expired(int(time.time()))
                if expired:
                    await self._dispatch(client, expired)
                    continue

                delay = self._next_delay()
                if delay is None:
                    await self.wakeup.wait()
                elif delay > 0:
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(self.wakeup.wait(), timeout=delay)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logs.error(f"[autodel] 队列消费发生未捕获异常: {e}", exc_info=True)
                await asyncio.sleep(1)

    async def _dispatch(self, client, jobs: List[DeleteJob]) -> None:
        jobs_by_chat: Dict[int, List[DeleteJob]] = {}
        for job in jobs:
            jobs_by_chat.setdefault(job.chat_id, []).append(job)
        for cid, chat_jobs in jobs_by_chat.items():
            for i in range(0, len(chat_jobs), _DELETE_BATCH_SIZE):
                await self._process_batch(client, cid, chat_jobs[i:i + _DELETE_BATCH_SIZE])

    async def _process_batch(self, client, cid: int, jobs: List[DeleteJob]) -> None:
        mids = [j.message_id for j in jobs]
        try:
            await client.delete_messages(cid, mids)
        except FloodWait as e:
            # 重排而非阻塞 worker；设上限防止无限循环
            wait = int(getattr(e, "value", 5) or 5)
            for job in jobs:
                job.flood_count += 1
                if job.flood_count > _MAX_FLOOD_RETRIES:
                    logs.warning(
                        f"[autodel] 任务 {cid}/{job.message_id} FloodWait "
                        f"重排超过 {_MAX_FLOOD_RETRIES} 次，放弃"
                    )
                    self._db_del(cid, job.message_id)
                else:
                    job.due_at = int(time.time()) + wait + 1
                    self.add_job(job)
        except _DROP_ERRORS as e:
            if len(jobs) > 1:
                # 批量中一条已不存在时，不能连带放弃其他有效消息。
                for job in jobs:
                    await self._process_batch(client, cid, [job])
            else:
                # 权限/对象不存在类错误：单条重试无意义。
                job = jobs[0]
                logs.info(
                    f"[autodel] 放弃聊天 {cid} 的任务 "
                    f"{job.message_id}: {type(e).__name__}"
                )
                self._db_del(cid, job.message_id)
        except Exception as e:
            if len(jobs) > 1:
                # 拆批逐条重试，防止单条失败拖垮整批
                for job in jobs:
                    await self._process_batch(client, cid, [job])
            else:
                job = jobs[0]
                job.retry_count += 1
                if job.retry_count > _MAX_RETRIES:
                    logs.warning(
                        f"[autodel] 任务 {cid}/{job.message_id} 重试超过 "
                        f"{_MAX_RETRIES} 次，放弃: {e}"
                    )
                    self._db_del(cid, job.message_id)
                else:
                    backoff = 5 * (3 ** (job.retry_count - 1))
                    job.due_at = int(time.time()) + backoff
                    self.add_job(job)
        else:
            for job in jobs:
                self._db_del(cid, job.message_id)


scheduler = AutoDeleteScheduler()


def _clear_all() -> Tuple[int, int]:
    """清除所有自动删除设置（定时器 + 排除标记 + 排队任务）。"""
    timer_entries = _scan_keys(_KEY_PREFIX)
    excl_entries = _scan_keys(_EXCL_PREFIX)

    for key, _ in timer_entries + excl_entries:
        with contextlib.suppress(KeyError):
            del sqlite[key]

    scheduler.clear_all_jobs()
    return len(timer_entries), len(excl_entries)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  命令处理与 UI
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _build_help_text() -> str:
    cmd = alias_command("autodel")
    return (
        "📌 **自动删除消息插件 (autodel)**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "**⏱ 设置定时删除（当前聊天）：**\n"
        f"  `,{cmd} 30 seconds` — 30 秒后自动删除\n"
        f"  `,{cmd} 5 m` — 5 分钟后自动删除\n"
        f"  `,{cmd} 1 h 30 m` — 1 小时 30 分钟后自动删除\n"
        f"  `,{cmd} 1 days` — 1 天后自动删除\n\n"
        "**🌐 全局设置（对所有未单独配置的聊天生效）：**\n"
        f"  `,{cmd} 30 seconds global` — 全局每条消息 30 秒后删除\n"
        f"  `,{cmd} cancel global` — 取消全局定时\n\n"
        "**🛡 排除管理（让特定聊天不受全局规则影响）：**\n"
        f"  `,{cmd} exclude` — 将当前聊天排除在全局规则之外\n"
        f"  `,{cmd} include` — 取消排除，重新受全局规则影响\n\n"
        "**🔒 远程配置（支持多 ID 批量管理）：**\n"
        f"  `,{cmd} 30 m -c -100123,-100456` — 远程批量设置\n"
        f"  `,{cmd} l -c -100123456` — 远程查看某聊天的状态\n\n"
        "**📊 查看与取消：**\n"
        f"  `,{cmd} l` — 查看所有设置（统一状态面板）\n"
        f"  `,{cmd} cancel` — 取消当前聊天的专属定时\n"
        f"  `,{cmd} reset confirm` — ⚠️ 一键清除所有设置（定时+排除）\n"
        f"  `,{cmd} h` — 显示本帮助信息\n\n"
        "**🔄 优先级规则：**\n"
        "  `聊天专属定时` > `排除标记` > `全局定时`"
    )

async def _fetch_chat_name(client, cid: int) -> str:
    if cid == _GLOBAL_CID:
        return "全局"
    try:
        chat = await asyncio.wait_for(client.get_chat(cid), timeout=_NAME_FETCH_TIMEOUT)
        name = _escape_md(chat.title or chat.first_name or "未知聊天")
        return f"{name} (`{cid}`)"
    except Exception:
        return f"未知聊天 (`{cid}`)"

async def _build_status_text(client, current_cid: int) -> str:
    global_seconds = _get_timer(_GLOBAL_CID)
    chat_seconds = _get_timer(current_cid)
    excluded = _is_excluded(current_cid)
    effective, _ = resolve_timer(current_cid)

    lines = ["📊 **自动删除 — 设置总览**", "━━━━━━━━━━━━━━━━━━━━", ""]

    if global_seconds > 0:
        lines.append(f"🌐 **全局定时:** {format_duration(global_seconds)}")
    else:
        lines.append("🌐 **全局定时:** 未设置")

    lines.append("")
    current_name = await _fetch_chat_name(client, current_cid)
    lines.append(f"💬 **当前聊天** {current_name}:")
    if chat_seconds > 0:
        lines.append(f"  ▸ 专属定时: {format_duration(chat_seconds)}")
    else:
        lines.append("  ▸ 专属定时: 未设置")
    if excluded:
        lines.append("  ▸ 排除状态: ✅ 已排除（不受全局规则影响）")
    if effective > 0:
        lines.append(f"  ▸ ⏱ 实际生效: **{format_duration(effective)}**")
    else:
        lines.append("  ▸ ⏱ 实际生效: **不自动删除**")

    all_timers = _get_all_timers()
    other_timers = [(c, s) for c, s in all_timers if c != _GLOBAL_CID and c != current_cid]

    # ── 并发获取聊天名称 ──
    sem = asyncio.Semaphore(_NAME_FETCH_CONCURRENCY)

    async def get_name_safely(cid):
        async with sem:
            return cid, await _fetch_chat_name(client, cid)

    if other_timers:
        lines.append("")
        lines.append("📋 **其他聊天专属定时：**")

        tasks = [get_name_safely(cid) for cid, _ in other_timers[:_MAX_DISPLAY_ITEMS]]
        names_dict = dict(await asyncio.gather(*tasks))

        for cid, secs in other_timers[:_MAX_DISPLAY_ITEMS]:
            excl_mark = " ✅排除" if _is_excluded(cid) else ""
            c_name = names_dict.get(cid, str(cid))
            lines.append(f"  ▸ {c_name}: {format_duration(secs)}{excl_mark}")
        if len(other_timers) > _MAX_DISPLAY_ITEMS:
            lines.append(f"  ... 及其他 {len(other_timers) - _MAX_DISPLAY_ITEMS} 个聊天")

    all_excluded = _get_all_excluded()
    timer_cids = {cid for cid, _ in all_timers}
    other_excluded = [c for c in all_excluded if c != current_cid and c not in timer_cids]

    if other_excluded:
        lines.append("")
        lines.append("🛡 **其他排除的聊天（仅排除、无专属定时）：**")

        tasks = [get_name_safely(cid) for cid in other_excluded[:_MAX_DISPLAY_ITEMS]]
        names_dict = dict(await asyncio.gather(*tasks))

        for cid in other_excluded[:_MAX_DISPLAY_ITEMS]:
            c_name = names_dict.get(cid, str(cid))
            lines.append(f"  ▸ {c_name}")
        if len(other_excluded) > _MAX_DISPLAY_ITEMS:
            lines.append(f"  ... 及其他 {len(other_excluded) - _MAX_DISPLAY_ITEMS} 个聊天")

    if global_seconds <= 0 and not all_timers and not all_excluded:
        lines.append("")
        lines.append("ℹ️ 当前没有任何自动删除设置。")

    return "\n".join(lines)


def _scope_label(is_remote: bool, is_global: bool, count: int) -> str:
    if is_global:
        return "全局"
    if is_remote:
        return f"{count} 个远程聊天"
    return "当前聊天"

def _handle_set(target_cids: List[int], time_text: str,
                is_global: bool, is_remote: bool) -> str:
    seconds = parse_time(time_text)
    cids = [_GLOBAL_CID] if is_global else target_cids
    scope = _scope_label(is_remote, is_global, len(cids))

    for cid in cids:
        _set_timer(cid, seconds)
    return f"✅ 已设置{scope}自动删除: **{format_duration(seconds)}**"

def _handle_cancel(target_cids: List[int], is_global: bool, is_remote: bool) -> str:
    cids = [_GLOBAL_CID] if is_global else target_cids
    scope = _scope_label(is_remote, is_global, len(cids))

    success = any([_del_timer(cid) for cid in cids])

    if is_global:
        # 仅清理所有聊天中因全局规则产生的任务，不动聊天专属任务
        scheduler.remove_jobs(cids=None, source=_SOURCE_GLOBAL)
    else:
        # 仅清理这些聊天中的专属任务；已入队的全局任务不受影响，
        # 因为取消专属定时后聊天仍应受全局规则约束
        scheduler.remove_jobs(cids=target_cids, source=_SOURCE_CHAT)

    if success:
        return f"✅ 已取消{scope}的自动删除任务（已清理排队队列）。"
    return f"⚠️ {scope}均未设置自动删除任务。"

def _handle_exclude(target_cids: List[int], is_remote: bool) -> str:
    scope = _scope_label(is_remote, False, len(target_cids))
    for cid in target_cids:
        _set_excluded(cid, True)

    # 只清理这些聊天中因全局规则产生的任务（不影响其他聊天、不动专属任务）
    scheduler.remove_jobs(cids=target_cids, source=_SOURCE_GLOBAL)

    return f"✅ {scope}已排除，不再受全局自动删除规则影响（已清理全局排队任务）。"

def _handle_include(target_cids: List[int], is_remote: bool) -> str:
    scope = _scope_label(is_remote, False, len(target_cids))
    for cid in target_cids:
        _set_excluded(cid, False)
    return f"✅ {scope}已取消排除，将重新受全局规则影响。"

async def _handle_reset(args: str) -> str:
    cmd = alias_command("autodel")
    if args.strip().split() != ["reset", "confirm"]:
        return (
            "⚠️ 这将清除所有聊天、全局的自动删除设置及排队任务，且无法恢复。\n"
            f"如需继续，请发送 `,{cmd} reset confirm`。"
        )

    # 全量键扫描在线程池中执行，避免大库阻塞事件循环
    timer_count, excl_count = await asyncio.to_thread(_clear_all)
    if timer_count == 0 and excl_count == 0:
        return "ℹ️ 当前没有任何自动删除设置，无需清除。"

    parts = []
    if timer_count > 0:
        parts.append(f"{timer_count} 个定时器")
    if excl_count > 0:
        parts.append(f"{excl_count} 个排除标记")
    return f"✅ 已清除所有自动删除设置：{'、'.join(parts)}。"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  PagerMaid 监听器
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@listener(
    command="autodel",
    need_admin=True,
    description=f"定时删除消息\n请使用 ,{alias_command('autodel')} h 查看可用命令",
)
async def auto_del(message: Message):
    raw_args = message.arguments.strip() if message.arguments else ""

    if not raw_args or raw_args == "h":
        return await message.edit(_build_help_text())

    try:
        args, remote_cids = _extract_chat_target(raw_args)
    except ValueError as e:
        return await message.edit(f"❌ 参数错误: {e}")

    is_remote = bool(remote_cids)
    target_cids = remote_cids if is_remote else [message.chat.id]

    if args.startswith("reset"):
        return await message.edit(await _handle_reset(args))

    if args == "l":
        panel_cid = target_cids[0]
        return await message.edit(await _build_status_text(_get_client(message), panel_cid))

    if args == "exclude":
        return await message.edit(_handle_exclude(target_cids, is_remote))
    if args == "include":
        return await message.edit(_handle_include(target_cids, is_remote))

    words = args.split()
    is_global = "global" in words
    clean_args = " ".join(w for w in words if w != "global") if is_global else args

    if is_global and is_remote:
        return await message.edit("❌ `global` 和 `-c` 不能同时使用。")

    if clean_args == "cancel":
        return await message.edit(_handle_cancel(target_cids, is_global, is_remote))

    try:
        result = _handle_set(target_cids, clean_args, is_global, is_remote)
        await message.edit(result)
    except ValueError as e:
        await message.edit(f"❌ 设置失败: {e}")


@listener(incoming=False, outgoing=True)
async def auto_del_task(message: Message):
    if not message.chat:
        return

    scheduler.ensure_worker(_get_client(message))

    try:
        seconds, source = resolve_timer(message.chat.id)
        if seconds > 0:
            scheduler.add_job(DeleteJob(
                due_at=int(time.time()) + seconds,
                chat_id=message.chat.id,
                message_id=message.id,
                source=source,
            ))
    except Exception as e:
        # 不能让调度失败影响消息发送流程，但必须留下日志便于排障
        logs.warning(f"[autodel] 调度消息删除任务失败: {e}", exc_info=True)
