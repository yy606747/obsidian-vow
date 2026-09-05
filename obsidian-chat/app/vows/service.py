"""誓约层服务：标记解析、净化、预算限流、生命周期事务。

事务约定（设计 §3）：`*_in_tx` 方法在调用方持有的事务内执行，
不 BEGIN / COMMIT / ROLLBACK；own-tx 包装方法自己开 BEGIN IMMEDIATE。
"""

import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import config
from database import get_db

from app.chat.control_syntax import contains_control_marker

from . import repository


class VowConflictError(RuntimeError):
    """active 行状态变更 rowcount != 1（并发竞争或状态机被破坏），整事务必须回滚。"""


class VowReadError(RuntimeError):
    """誓约读取失败。"每轮全量在场"是硬不变量（§5.2）：
    抛出此异常时调用管道必须 fail-closed / 跳过 / 降级，绝不带残缺誓约上下文开口。"""


# ── 标记解析（§4.1 / §4.4）──────────────────────────────

# 标记内部按 [ ] 配对计深，不是"删到第一个 ]"：确认语里出现 [TOY:9] 之类
# 成对括号时整个标记仍被完整摘除、交给净化器拒绝（§4.1 惰性文本），
# 否则被拒候选的尾段（"|确认语]"）会泄漏进可见文本，破坏 §4.3 可见性不变量。
VOW_OPEN = "[VOW:"


def find_vow_markers(text: str) -> tuple[list[tuple[int, int, str]], int | None]:
    """扫描全部完整标记，返回 ([(start, end, 内部文本)], 未闭合标记起点或 None)。

    未闭合 = 自某个 `[VOW:` 起括号深度到文本结尾都未归零，
    其后所有内容都算标记内部（§4.4：未闭合从 `[VOW:` 删到结尾）。
    """
    markers: list[tuple[int, int, str]] = []
    pos = 0
    while True:
        start = text.find(VOW_OPEN, pos)
        if start < 0:
            return markers, None
        depth = 1
        i = start + len(VOW_OPEN)
        while i < len(text):
            ch = text[i]
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        if i >= len(text):
            return markers, start
        markers.append((start, i + 1, text[start + len(VOW_OPEN):i]))
        pos = i + 1

# 后台标记黑名单（§4.2 单一清单常量）：净化时任一片段出现即拒绝。
# 誓约内容会进入每一轮 prompt（§5 常驻注入），所以私有推理标签
# （<think> 等）和思考代码块围栏也必须拒绝——否则一次立约 = 永久提示词污染。
FORBIDDEN_SEGMENT_SNIPPETS = (
    "[VOW", "[UPDATE_MODEL", "[WORKING_MODEL_REQUEST", "[/WORKING_MODEL_REQUEST",
    "[REMEMBER", "[TOY", "【TOY", "[RING",
    "[TIDE", "[MUSIC", "[ALARM", "[MONITOR", "[SCHEDULE", "[HEART",
    "[SCREEN_CHECK", "[MOBILE_SCREEN_CHECK", "[POI_SEARCH", "[查看动态",
    "[CAM_CHECK",
    "<meta", "<think", "<thought", "<analysis", "<reasoning",
    "</meta", "</think", "</thought", "</analysis", "</reasoning",
    "```",
)

_WHITESPACE_PATTERN = re.compile(r"\s+")


def strip_vow_markers(text: str) -> str:
    """剥除即丢弃（§4.4）：所有不允许立约的路径共用。

    必须在任何工具 / 指令解析之前调用。完整标记整体删除；
    未闭合标记从 `[VOW:` 删到文本结尾。返回剥除后文本（已 trim），
    为空时调用方不得落库空 assistant 消息。
    """
    if not text:
        return text
    markers, unclosed_start = find_vow_markers(text)
    parts: list[str] = []
    pos = 0
    for start, end, _inner in markers:
        parts.append(text[pos:start])
        pos = end
    parts.append(text[pos:unclosed_start] if unclosed_start is not None else text[pos:])
    return "".join(parts).strip()


@dataclass
class VowExtract:
    """send / regenerate 路径的提取结果。reject_reason 非空表示候选被整体拒绝。"""
    found: bool
    content_raw: str | None = None
    affirmation_raw: str | None = None
    reject_reason: str | None = None


def extract_vow_marker(text: str) -> tuple[str, VowExtract]:
    """从模型原文提取誓约候选，返回 (剥除标记后的文本, 提取结果)。

    只按第一个 `|` 分隔；一条回复多于一个标记 → 整体拒绝；
    未闭合标记 → 拒绝。两种情况下标记都照常剥除。
    """
    if not text:
        return text, VowExtract(found=False)
    markers, unclosed_start = find_vow_markers(text)
    cleaned = strip_vow_markers(text)

    total = len(markers) + (1 if unclosed_start is not None else 0)
    if total == 0:
        return cleaned, VowExtract(found=False)
    if total > 1:
        return cleaned, VowExtract(found=True, reject_reason="一条回复只能包含一个誓约标记")
    if unclosed_start is not None:
        return cleaned, VowExtract(found=True, reject_reason="誓约标记未闭合")

    inner = markers[0][2]
    if "|" not in inner:
        return cleaned, VowExtract(found=True, reject_reason="誓约缺少确认语")
    content_raw, affirmation_raw = inner.split("|", 1)
    return cleaned, VowExtract(found=True, content_raw=content_raw, affirmation_raw=affirmation_raw)


# ── 内容净化（§4.2，UI 与 AI 共用）──────────────────────────────


def sanitize_segment(raw: str | None, *, max_chars: int) -> tuple[str | None, str | None]:
    """返回 (净化后文本, None) 或 (None, 拒绝原因)。超限拒绝，不截断。"""
    if raw is None:
        return None, "内容为空"
    normalized = _WHITESPACE_PATTERN.sub(" ", raw).strip()
    if not normalized:
        return None, "内容为空"
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in normalized):
        return None, "包含控制字符"
    if "|" in normalized:
        return None, "包含分隔符 |"
    if "]" in normalized:
        return None, "包含 ]"
    upper = normalized.upper()
    if contains_control_marker(normalized):
        return None, "包含后台标记"
    for snippet in FORBIDDEN_SEGMENT_SNIPPETS:
        if snippet.upper() in upper:
            return None, "包含后台标记"
    if len(normalized) > max_chars:
        return None, f"超出 {max_chars} 字符上限"
    return normalized, None


def sanitize_vow_content(raw: str | None) -> tuple[str | None, str | None]:
    return sanitize_segment(raw, max_chars=config.VOW_CONTENT_MAX_CHARS)


def sanitize_affirmation(raw: str | None) -> tuple[str | None, str | None]:
    return sanitize_segment(raw, max_chars=config.VOW_AFFIRMATION_MAX_CHARS)


def sanitize_reason(raw: str | None) -> tuple[str | None, str | None]:
    """修订/退役原因净化：空白规范化 + 控制字符拒绝 + 上限拒绝不截断。

    原因只进版本史展示、不进 prompt，所以不套后台标记黑名单；
    但长度必须封顶——修订次数没有上限，否则可以无限膨胀库和列表接口。
    """
    normalized = _WHITESPACE_PATTERN.sub(" ", str(raw or "")).strip()
    if not normalized:
        return None, "必须填写原因"
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in normalized):
        return None, "原因包含控制字符"
    if len(normalized) > config.VOW_REASON_MAX_CHARS:
        return None, f"原因超出 {config.VOW_REASON_MAX_CHARS} 字符上限"
    return normalized, None


# ── 预算与限流（§6）──────────────────────────────


def day_window(now_ts: float, tz_name: str | None = None) -> tuple[float, float]:
    """显式配置时区计日：返回 now_ts 所在自然日的 [开始, 次日开始) 时间戳。"""
    tz = ZoneInfo(tz_name or config.VOW_DAILY_TZ)
    local = datetime.fromtimestamp(now_ts, tz)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


async def check_admission(db, *, content: str, origin_type: str, now_ts: float) -> str | None:
    """准入检查，返回 None（通过）或拒绝原因。须在写入同一事务内调用。"""
    active_count = await repository.count_active(db)
    if active_count >= config.VOW_ACTIVE_MAX:
        return f"已有 {active_count} 条有效誓约，达到上限，需要先退役一条"
    # 防重（兼作 UI 双击/重试的幂等闸）：同内容的 active 誓约只允许存在一条
    if await repository.count_active_with_content(db, content) > 0:
        return "已有一条内容完全相同的有效誓约"
    total_chars = await repository.sum_active_chars(db)
    if total_chars + len(content) > config.VOW_TOTAL_ACTIVE_CHARS:
        return "有效誓约的总字符预算不足"
    if origin_type == "ai_marker":
        start, end = day_window(now_ts)
        created_today = await repository.count_ai_created_between(db, start, end)
        if created_today >= config.VOW_AI_DAILY_LIMIT:
            return "今天的立约额度已经用完"
    return None


async def ai_quota_remaining(db, now_ts: float) -> int:
    """今日 AI 立约剩余额度（供能力纪律 block 展示）。"""
    start, end = day_window(now_ts)
    used = await repository.count_ai_created_between(db, start, end)
    return max(0, config.VOW_AI_DAILY_LIMIT - used)


# ── 生命周期 ──────────────────────────────


def _new_vow_id() -> str:
    return f"vow_{uuid.uuid4().hex}"


class VowService:
    def __init__(self, *, get_db_factory=get_db, now=time.time):
        self._get_db = get_db_factory
        self._now = now

    # —— in-tx 核心（事务由调用方持有）——

    async def create_in_tx(
        self,
        db,
        *,
        content: str,
        origin_type: str,
        origin_conv_id: str | None = None,
        origin_message_id: str | None = None,
        created_at: float | None = None,
    ) -> tuple[dict | None, str | None]:
        """净化 + 准入 + 插入。返回 (vow, None) 或 (None, 拒绝原因)。"""
        cleaned, err = sanitize_vow_content(content)
        if err:
            return None, err
        now_ts = created_at if created_at is not None else self._now()
        err = await check_admission(db, content=cleaned, origin_type=origin_type, now_ts=now_ts)
        if err:
            return None, err
        vow_id = _new_vow_id()
        vow = await repository.insert_vow(
            db,
            vow_id=vow_id,
            root_id=vow_id,
            previous_version_id=None,
            content=cleaned,
            origin_type=origin_type,
            origin_conv_id=origin_conv_id,
            origin_message_id=origin_message_id,
            created_at=now_ts,
        )
        return vow, None

    async def revise_in_tx(
        self, db, *, vow_id: str, new_content: str, reason: str,
    ) -> tuple[dict | None, str | None]:
        """同事务：旧行 active→superseded，插新行继承 root_id。修订不计入每日新建。"""
        cleaned, err = sanitize_vow_content(new_content)
        if err:
            return None, err
        reason, err = sanitize_reason(reason)
        if err:
            return None, f"修订{err}"
        old = await repository.get_vow(db, vow_id)
        if old is None:
            return None, "誓约不存在"
        if old["status"] != "active":
            return None, "只能修订有效誓约"
        total_chars = await repository.sum_active_chars(db)
        if total_chars - len(old["content"]) + len(cleaned) > config.VOW_TOTAL_ACTIVE_CHARS:
            return None, "有效誓约的总字符预算不足"
        now_ts = self._now()
        rowcount = await repository.close_active(
            db, vow_id=vow_id, new_status="superseded", close_action="revised",
            closed_reason=reason, status_changed_at=now_ts,
        )
        if rowcount != 1:
            raise VowConflictError(f"revise {vow_id}: rowcount={rowcount}")
        # 同内容 active 唯一是正式准入规则，修订与新建同样受约束。
        # 检查放在旧行关闭之后：重申自己（同链同内容、只换原因）合法，
        # 修订成另一条 active 的内容则拒绝；失败由 own-tx 包装整体回滚。
        if await repository.count_active_with_content(db, cleaned) > 0:
            return None, "已有一条内容完全相同的有效誓约"
        new_vow = await repository.insert_vow(
            db,
            vow_id=_new_vow_id(),
            root_id=old["root_id"],
            previous_version_id=vow_id,
            content=cleaned,
            origin_type="user_ui",
            origin_conv_id=None,
            origin_message_id=None,
            created_at=now_ts,
        )
        return new_vow, None

    async def close_in_tx(
        self, db, *, vow_id: str, action: str, reason: str | None = None,
    ) -> tuple[bool, str | None]:
        """退役（必填原因）或兑现。action ∈ {'retired','fulfilled'}。"""
        if action not in ("retired", "fulfilled"):
            return False, f"未知操作 {action}"
        if action == "retired":
            reason, err = sanitize_reason(reason)
            if err:
                return False, f"退役{err}"
        old = await repository.get_vow(db, vow_id)
        if old is None:
            return False, "誓约不存在"
        if old["status"] != "active":
            return False, "只能关闭有效誓约"
        rowcount = await repository.close_active(
            db, vow_id=vow_id, new_status=action, close_action=action,
            closed_reason=(reason or "").strip() or None, status_changed_at=self._now(),
        )
        if rowcount != 1:
            raise VowConflictError(f"close {vow_id}: rowcount={rowcount}")
        return True, None

    async def admit_ai_vow_in_tx(
        self,
        db,
        *,
        extract,
        conv_id: str,
        message_id: str,
        created_at: float | None = None,
    ) -> tuple[dict | None, str | None, str | None]:
        """send / regenerate 提交编排（§4.3）：对提取候选做净化 + 准入 + 插入。

        与 assistant 消息落库共用调用方持有的事务。
        返回 (vow, 确认语, None) 或 (None, None, 拒绝原因)。
        """
        if extract.reject_reason:
            return None, None, extract.reject_reason
        content, err = sanitize_vow_content(extract.content_raw)
        if err:
            return None, None, err
        affirmation, err = sanitize_affirmation(extract.affirmation_raw)
        if err:
            return None, None, err
        vow, err = await self.create_in_tx(
            db,
            content=content,
            origin_type="ai_marker",
            origin_conv_id=conv_id,
            origin_message_id=message_id,
            created_at=created_at,
        )
        if err:
            return None, None, err
        return vow, affirmation, None

    async def revoke_for_origin_message_in_tx(
        self, db, *, message_id: str, close_action: str,
    ) -> dict | None:
        """系统撤约（§4.5）：消息被删除 / regenerate 时调用，与删消息同事务。

        按 origin_message_id 找原始行：仍 active → 退役；
        已非 active（如 UI 修订后为 superseded）→ 合法 no-op，返回 None。
        UI 修订产生的 active 后继版本不受波及（其 origin_message_id 为 NULL）。
        """
        if close_action not in ("origin_regenerated", "origin_deleted"):
            raise ValueError(f"非法撤约动作 {close_action}")
        row = await repository.get_by_origin_message(db, message_id)
        if row is None or row["status"] != "active":
            return None
        rowcount = await repository.close_active(
            db, vow_id=row["id"], new_status="retired", close_action=close_action,
            closed_reason=None, status_changed_at=self._now(),
        )
        if rowcount != 1:
            raise VowConflictError(f"revoke {row['id']}: rowcount={rowcount}")
        return await repository.get_vow(db, row["id"])

    async def revoke_for_origin_conversation_in_tx(self, db, *, conv_id: str) -> int:
        """系统撤约的批量形态（§4.5 / §13-1）：会话删除、聊天文件覆盖导入
        会批量删消息，须在删除前同一事务内调用，返回撤约条数。"""
        return await repository.close_active_by_origin_conv(
            db, conv_id=conv_id, status_changed_at=self._now()
        )

    # —— own-tx 包装（UI 路径与独立调用）——

    async def _run_in_own_tx(self, fn):
        async with self._get_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                result = await fn(db)
            except BaseException:
                await db.rollback()
                raise
            ok = result[0] if isinstance(result, tuple) else result
            if ok is None or ok is False:
                await db.rollback()
            else:
                await db.commit()
            return result

    async def create_ui_vow(self, content: str, *, conv_id: str | None = None):
        return await self._run_in_own_tx(
            lambda db: self.create_in_tx(
                db, content=content, origin_type="user_ui", origin_conv_id=conv_id,
            )
        )

    async def revise_vow(self, vow_id: str, new_content: str, reason: str):
        return await self._run_in_own_tx(
            lambda db: self.revise_in_tx(db, vow_id=vow_id, new_content=new_content, reason=reason)
        )

    async def retire_vow(self, vow_id: str, reason: str):
        return await self._run_in_own_tx(
            lambda db: self.close_in_tx(db, vow_id=vow_id, action="retired", reason=reason)
        )

    async def fulfill_vow(self, vow_id: str):
        return await self._run_in_own_tx(
            lambda db: self.close_in_tx(db, vow_id=vow_id, action="fulfilled")
        )

    # —— 读取 ——

    async def load_vow_prompt_context(self) -> tuple[str, str]:
        """常驻注入读取（§5）：返回 (vow block, 能力纪律片段)。

        读取失败抛 VowReadError，由各管道按 §5.2 三分类处置；
        绝不返回残缺结果。
        """
        from .prompt import build_vow_ability_block, build_vow_block

        try:
            async with self._get_db() as db:
                active = await repository.list_active(db)
                remaining = await ai_quota_remaining(db, self._now())
        except Exception as exc:
            raise VowReadError(f"vow read failed: {exc}") from exc
        return (
            build_vow_block(active, now=self._now()),
            build_vow_ability_block(remaining_today=remaining),
        )

    async def list_active(self) -> list[dict]:
        async with self._get_db() as db:
            return await repository.list_active(db)

    async def list_all(self) -> list[dict]:
        async with self._get_db() as db:
            return await repository.list_all(db)

    async def list_tips(
        self,
        *,
        fulfilled_limit: int = 50,
        fulfilled_offset: int = 0,
    ) -> dict:
        async with self._get_db() as db:
            return await repository.list_tips(
                db,
                fulfilled_limit=fulfilled_limit,
                fulfilled_offset=fulfilled_offset,
            )

    async def list_chain(self, root_id: str) -> list[dict]:
        async with self._get_db() as db:
            return await repository.list_chain(db, root_id)

    async def list_chain_page(
        self,
        root_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        async with self._get_db() as db:
            return await repository.list_chain_page(
                db, root_id, limit=limit, offset=offset
            )


vow_service = VowService()
