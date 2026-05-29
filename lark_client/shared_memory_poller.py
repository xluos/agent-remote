"""
共享内存轮询器 - 流式滚动卡片模型

核心理念：没有 turn、没有 message。只有一个不断增长的 blocks 流和跟踪它的滚动窗口。

数据流：
  .mq { blocks, status_line, bottom_bar }
              ↓ 每秒轮询
    _poll_once(tracker)
              ↓
    渲染 blocks[start_idx:] → 卡片 elements
              ↓ hash diff
    同一张卡片就地更新 / 超限时冻结+开新卡
"""

import asyncio
import hashlib
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Any

logger = logging.getLogger('SharedMemoryPoller')

_root = Path(__file__).parent.parent
sys.path.insert(0, str(_root))

try:
    from stats import track as _track_stats
except Exception:
    def _track_stats(*args, **kwargs): pass

from utils.session import ensure_user_data_dir, USER_DATA_DIR, get_hook_dir

# ── 常量 ──────────────────────────────────────────────────────────────────────
INITIAL_WINDOW = 30    # 首次 attach 最多显示最近 30 个 blocks
from .config import MAX_CARD_BLOCKS  # 单张卡片最多 N 个 blocks → 超限冻结（可通过 .env 配置）
CARD_SIZE_LIMIT = 25 * 1024  # 25KB，飞书限制 30KB，留 5KB 余量
POLL_INTERVAL = 1.0    # 轮询间隔（秒）
RAPID_INTERVAL = 0.2   # 快速轮询间隔（秒）
RAPID_DURATION = 2.0   # 快速轮询持续时间（秒）


# ── 数据模型 ──────────────────────────────────────────────────────────────────

@dataclass
class CardSlice:
    """一张飞书卡片对应的 blocks 窗口"""
    card_id: str
    sequence: int = 0
    start_idx: int = 0       # blocks[start_idx:] 开始渲染
    frozen: bool = False


@dataclass
class StreamTracker:
    """单个 chat_id 的流式跟踪状态"""
    chat_id: str
    session_name: str
    cards: List[CardSlice] = field(default_factory=list)
    content_hash: str = ""
    reader: Optional[Any] = None  # SharedStateReader，延迟初始化
    is_group: bool = False         # 是否为群聊
    prev_is_ready: bool = True     # 上一帧是否就绪（初始 True 避免首次误触发）
    notify_user_id: Optional[str] = None  # 就绪通知 @ 的用户 open_id
    last_notify_message_id: Optional[str] = None  # 上一条就绪通知的 message_id（用于后续加急复用）
    # 简单模式（每轮一张独立卡片）：本轮起始 block 索引 + 触发本轮的 UserInput block_id
    simple_round_start: int = 0
    simple_last_user_bid: Optional[str] = None
    # 就绪状态去抖：首次检测到就绪的时间戳，连续保持超过阈值才确认
    # 初始设为 0.0（远早于当前时间），使首次 attach 到已就绪会话时立即确认
    ready_since: Optional[float] = 0.0
    # hook 状态（来自 agents-remote-core HookHarness）
    hook_state: Optional[dict] = None
    # hook 多问题作答进度（由 lark_handler 维护）
    hook_progress: Optional[dict] = None
    # hook 模式轮次跟踪：上一帧 turn_complete 值（用于检测 True→False 翻转 = 新轮次开始）
    prev_turn_complete: bool = True


# ── 轮询器 ────────────────────────────────────────────────────────────────────

class SharedMemoryPoller:
    """
    共享内存轮询器（流式滚动卡片模型）

    attach 时启动轮询 Task，detach/断线时停止。
    每秒读取 .mq 文件中的 blocks 流，通过 hash diff 触发飞书卡片创建/更新。
    """

    def __init__(self, card_service: Any):
        self._card_service = card_service
        self._trackers: Dict[str, StreamTracker] = {}  # chat_id → StreamTracker
        self._tasks: Dict[str, asyncio.Task] = {}       # chat_id → Task
        self._kick_events: Dict[str, asyncio.Event] = {}  # chat_id → Event（唤醒轮询）
        self._rapid_until: Dict[str, float] = {}           # chat_id → 快速模式截止时间

    def start(self, chat_id: str, session_name: str, is_group: bool = False,
              notify_user_id: Optional[str] = None) -> None:
        """attach 成功后调用：清空旧状态，启动轮询 Task"""
        self.stop(chat_id)

        tracker = StreamTracker(chat_id=chat_id, session_name=session_name, is_group=is_group,
                                notify_user_id=notify_user_id)
        self._trackers[chat_id] = tracker
        self._kick_events[chat_id] = asyncio.Event()

        # 设置 remote_active 标记，让 core 的 permission.sh 拦截权限/问题交互
        self._set_remote_active(session_name, True)

        task = asyncio.create_task(self._poll_loop(chat_id))
        task.add_done_callback(lambda t: self._on_task_done(t, chat_id))
        self._tasks[chat_id] = task
        logger.info(f"轮询器启动: chat_id={chat_id[:8]}..., session={session_name}")

    def stop(self, chat_id: str) -> None:
        """detach/断线时调用：取消 Task，清空状态，关闭 Reader"""
        task = self._tasks.pop(chat_id, None)
        if task:
            task.cancel()

        self._kick_events.pop(chat_id, None)
        self._rapid_until.pop(chat_id, None)

        tracker = self._trackers.pop(chat_id, None)
        session_name = tracker.session_name if tracker else "N/A"
        if tracker:
            # 检查是否还有其他 chat 连着同一个 session，没有才清除标记
            if not self._has_other_tracker_for_session(session_name, exclude_chat=chat_id):
                self._set_remote_active(session_name, False)
            if tracker.reader:
                try:
                    tracker.reader.close()
                except Exception:
                    pass
        logger.info(f"轮询器停止: chat_id={chat_id[:8]}..., session={session_name}")

    def stop_and_get_active_slice(self, chat_id: str) -> Optional['CardSlice']:
        """停止轮询并返回活跃（未冻结）CardSlice，原子操作。供 detach/disconnect 就地更新卡片使用。"""
        task = self._tasks.pop(chat_id, None)
        if task:
            task.cancel()

        self._kick_events.pop(chat_id, None)
        self._rapid_until.pop(chat_id, None)

        tracker = self._trackers.pop(chat_id, None)
        if not tracker:
            return None

        session_name = tracker.session_name
        # 检查是否还有其他 chat 连着同一个 session
        if not self._has_other_tracker_for_session(session_name, exclude_chat=chat_id):
            self._set_remote_active(session_name, False)

        active = None
        if tracker.cards and not tracker.cards[-1].frozen:
            active = tracker.cards[-1]

        if tracker.reader:
            try:
                tracker.reader.close()
            except Exception:
                pass

        logger.info(f"轮询器停止(含活跃切片): chat_id={chat_id[:8]}..., session={session_name}, active={'有' if active else '无'}")
        return active

    def _set_remote_active(self, session_name: str, active: bool):
        """设置/清除 hook_dir/remote_active 标记，控制 core permission.sh 是否拦截交互"""
        flag = get_hook_dir(session_name) / "remote_active"
        try:
            if active:
                flag.parent.mkdir(parents=True, exist_ok=True)
                flag.touch()
                logger.info(f"remote_active 已设置: session={session_name}")
            else:
                flag.unlink(missing_ok=True)
                logger.info(f"remote_active 已清除: session={session_name}")
        except OSError as e:
            logger.warning(f"remote_active 操作失败: {e}")

    def _has_other_tracker_for_session(self, session_name: str, exclude_chat: str) -> bool:
        """检查是否还有其他 chat 连着同一个 session"""
        return any(
            t.session_name == session_name
            for cid, t in self._trackers.items()
            if cid != exclude_chat
        )

    def _on_task_done(self, task: asyncio.Task, chat_id: str) -> None:
        """Task 完成回调：记录异常"""
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.error(f"轮询 Task 异常: chat_id={chat_id[:8]}..., {exc}", exc_info=exc)

    def read_snapshot(self, chat_id: str) -> Optional[dict]:
        """直接读取指定 chat_id 的当前共享内存快照（供 handle_option_select 等即时查询使用）"""
        tracker = self._trackers.get(chat_id)
        if tracker and tracker.reader:
            try:
                return tracker.reader.read()
            except Exception as e:
                logger.warning(f"read_snapshot 失败: {e}")
        return None

    def kick(self, chat_id: str) -> None:
        """触发立即轮询并进入快速轮询模式"""
        self._rapid_until[chat_id] = time.time() + RAPID_DURATION
        ev = self._kick_events.get(chat_id)
        if ev:
            ev.set()

    async def _poll_loop(self, chat_id: str) -> None:
        """轮询循环：支持 kick 唤醒 + 快速轮询模式"""
        while True:
            try:
                # 动态间隔：快速模式 0.2s，常规 1.0s
                rapid_until = self._rapid_until.get(chat_id, 0)
                interval = RAPID_INTERVAL if time.time() < rapid_until else POLL_INTERVAL

                # 等待 kick 事件或超时
                kick_event = self._kick_events.get(chat_id)
                if kick_event:
                    try:
                        await asyncio.wait_for(kick_event.wait(), timeout=interval)
                        kick_event.clear()
                        # kick 触发时进入快速模式
                        self._rapid_until[chat_id] = time.time() + RAPID_DURATION
                    except asyncio.TimeoutError:
                        pass
                else:
                    await asyncio.sleep(interval)

                tracker = self._trackers.get(chat_id)
                if not tracker:
                    break
                await self._poll_once(tracker)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"_poll_once 异常: {e}", exc_info=True)

    async def _poll_once(self, tracker: StreamTracker) -> None:
        """单次轮询：读取共享内存 → diff → 创建/更新卡片 → 就绪通知"""
        # 步骤 1：延迟初始化 Reader
        if tracker.reader is None:
            try:
                from utils.shared_state_reader import SharedStateReader
                from utils.session import get_mq_path
                mq_path = get_mq_path(tracker.session_name)
                if not mq_path.exists():
                    return
                tracker.reader = SharedStateReader(tracker.session_name)
                logger.info(f"Reader 初始化成功: session={tracker.session_name}")
            except Exception as e:
                logger.warning(f"创建 Reader 失败: {e}")
                return

        # 读取共享内存
        try:
            state = tracker.reader.read()
        except Exception as e:
            logger.error(f"读取共享内存失败: {e}")
            tracker.reader = None
            return

        blocks = state.get("blocks", [])
        status_line = state.get("status_line")
        bottom_bar = state.get("bottom_bar")
        agent_panel = state.get("agent_panel")
        option_block = state.get("option_block")
        cli_type = state.get("cli_type", "claude")
        # hook_state 由 agents-remote-core server 的 HookHarness 写入共享内存
        tracker.hook_state = state.get("hook_state")
        # timestamp 存在说明 server 已写入有效快照（即使内容全空，如 Codex 就绪等待输入）
        has_valid_snapshot = state.get("timestamp") is not None

        # 步骤 2：仅计算就绪状态，不发送通知
        should_notify = self._update_ready_state(tracker, blocks, status_line, option_block, agent_panel)

        # 步骤 3：卡片操作（含创建/更新/拆分）
        await self._do_card_update(tracker, blocks, status_line, bottom_bar, agent_panel, option_block, cli_type, has_valid_snapshot=has_valid_snapshot)

        # 步骤 4：通知在卡片操作之后发送，确保新卡先出现
        if should_notify:
            await self._send_ready_notification(tracker, cli_type)

    async def _do_card_update(
        self, tracker: StreamTracker, blocks: List[dict],
        status_line: Optional[dict], bottom_bar: Optional[dict],
        agent_panel: Optional[dict], option_block: Optional[dict],
        cli_type: str,
        has_valid_snapshot: bool = False,
    ) -> None:
        """卡片操作主体：获取活跃卡片 → 创建/更新/拆分"""
        # 简单模式：走独立的"每轮一张卡片"渲染路径
        if _current_simple_mode():
            await self._do_card_update_simple(
                tracker, blocks, status_line, bottom_bar, agent_panel,
                option_block, cli_type, has_valid_snapshot=has_valid_snapshot,
            )
            return

        # 获取活跃卡片（最后一张且未冻结）
        active = None
        if tracker.cards and not tracker.cards[-1].frozen:
            active = tracker.cards[-1]

        if not blocks and not status_line and not bottom_bar and not agent_panel and not option_block and active is None:
            if not has_valid_snapshot:
                return  # 真的还没有快照，不创建卡片
            # 有效快照但内容全空（如 Codex 就绪等待输入），继续创建就绪卡片

        if active is None:
            # 需要创建新卡片
            await self._create_new_card(tracker, blocks, status_line, bottom_bar, agent_panel, option_block, cli_type=cli_type)
            return

        # 有活跃卡片，检查是否需要更新
        blocks_slice = blocks[active.start_idx:]

        # blocks 骤降检测（compact/重启导致 blocks 从头累积）
        if len(blocks) < active.start_idx:
            logger.warning(
                f"[blocks regression] len(blocks)={len(blocks)} < start_idx={active.start_idx}, "
                f"resetting start_idx to 0 (session={tracker.session_name})"
            )
            active.start_idx = 0
            blocks_slice = blocks[0:]
            tracker.content_hash = ""  # 强制刷新

        # 超限检查
        if len(blocks_slice) > MAX_CARD_BLOCKS:
            await self._freeze_and_split(tracker, blocks, status_line, bottom_bar, agent_panel, option_block, cli_type=cli_type)
            return

        # hash diff
        new_hash = self._compute_hash(blocks_slice, status_line, bottom_bar, agent_panel, option_block, tracker.hook_state, tracker.hook_progress)
        if new_hash == tracker.content_hash:
            return  # 无变化

        # 更新卡片
        from .card_builder import build_stream_card
        card_dict = build_stream_card(blocks_slice, status_line, bottom_bar, agent_panel=agent_panel, option_block=option_block, session_name=tracker.session_name, cli_type=cli_type, hook_state=tracker.hook_state, hook_progress=tracker.hook_progress)

        # 大小超限检查（与 blocks 数量超限同一套逻辑）
        card_size = len(json.dumps(card_dict, ensure_ascii=False).encode('utf-8'))
        if card_size > CARD_SIZE_LIMIT:
            freeze_count = self._find_freeze_count(blocks_slice, tracker.session_name)
            await self._freeze_and_split(
                tracker, blocks, status_line, bottom_bar, agent_panel, option_block,
                cli_type=cli_type, freeze_count=freeze_count,
            )
            return

        active.sequence += 1
        success = await self._card_service.update_card(
            card_id=active.card_id,
            sequence=active.sequence,
            card_content=card_dict,
        )

        if getattr(success, 'is_element_limit', False):
            # 元素超限：冻结旧卡 + 推新流式卡
            await self._handle_element_limit(
                tracker, blocks, status_line, bottom_bar, agent_panel, option_block,
                cli_type=cli_type,
            )
            return
        elif not success:
            # 降级：创建新卡片替代
            logger.warning(
                f"update_card 失败 card_id={active.card_id} seq={active.sequence}，降级为新卡片"
            )
            _track_stats('card', 'fallback', session_name=tracker.session_name,
                         chat_id=tracker.chat_id)
            new_card_id = await self._card_service.create_card(card_dict)
            if new_card_id:
                await self._card_service.send_card(tracker.chat_id, new_card_id)
                active.card_id = new_card_id
                active.sequence = 0
        else:
            _track_stats('card', 'update', session_name=tracker.session_name,
                         chat_id=tracker.chat_id)

        tracker.content_hash = new_hash
        logger.debug(
            f"[UPDATE] session={tracker.session_name} blocks={len(blocks_slice)} "
            f"seq={active.sequence} hash={new_hash[:8]}"
        )

    def _detect_new_round_hook(self, tracker: StreamTracker, blocks: List[dict]) -> tuple:
        """hook 模式轮次检测：turn_complete True→False 翻转 = 新轮次开始

        Returns: (is_new_round, round_start)
        """
        hs = tracker.hook_state or {}
        cur_complete = hs.get("turn_complete", False)
        was_complete = tracker.prev_turn_complete
        tracker.prev_turn_complete = cur_complete

        # True → False 翻转：Claude 从就绪进入新一轮执行
        if was_complete and not cur_complete:
            # 找最后一个 UserInput 作为轮次起点
            for i in range(len(blocks) - 1, -1, -1):
                if blocks[i].get("_type") == "UserInput":
                    return True, i
            return True, max(0, len(blocks) - 1)

        return False, 0

    def _detect_new_round_parse(self, tracker: StreamTracker, blocks: List[dict]) -> tuple:
        """解析模式轮次检测：新 UserInput block_id 出现 = 新轮次

        Returns: (is_new_round, round_start, user_bid)
        """
        user_bid: Optional[str] = None
        round_start = 0
        for i in range(len(blocks) - 1, -1, -1):
            if blocks[i].get("_type") == "UserInput":
                round_start = i
                user_bid = blocks[i].get("block_id") or f"idx{i}"
                break
        is_new = user_bid != tracker.simple_last_user_bid
        return is_new, round_start, user_bid

    async def _do_card_update_simple(
        self, tracker: StreamTracker, blocks: List[dict],
        status_line: Optional[dict], bottom_bar: Optional[dict],
        agent_panel: Optional[dict], option_block: Optional[dict],
        cli_type: str,
        has_valid_snapshot: bool = False,
    ) -> None:
        """简单模式卡片更新：每轮一张独立卡片，工具调用折叠

        轮次切换检测分两种模式：
        - hook 模式（hook_state 存在）：turn_complete True→False 翻转 = 新轮次
        - 解析模式（hook_state 为 None）：新 UserInput block_id 出现 = 新轮次
        """
        from .card_builder import build_stream_card

        if not blocks and not status_line and not option_block and not has_valid_snapshot:
            return

        active = tracker.cards[-1] if tracker.cards else None

        # 新一轮检测：区分 hook 模式和解析模式
        if tracker.hook_state is not None:
            is_new_round, round_start = self._detect_new_round_hook(tracker, blocks)
        else:
            is_new_round, round_start, user_bid = self._detect_new_round_parse(tracker, blocks)
            if is_new_round:
                tracker.simple_last_user_bid = user_bid

        if is_new_round:
            if active is not None and not active.frozen:
                old_blocks = blocks[active.start_idx:round_start]
                frozen_card = build_stream_card(
                    old_blocks, is_frozen=True,
                    session_name=tracker.session_name, cli_type=cli_type,
                )
                active.sequence += 1
                try:
                    await self._card_service.update_card(active.card_id, active.sequence, frozen_card)
                    active.frozen = True
                    _track_stats('card', 'freeze', session_name=tracker.session_name,
                                 chat_id=tracker.chat_id)
                except Exception as e:
                    logger.warning(f"[simple] 冻结旧卡失败: {e}")
            tracker.simple_round_start = round_start
            tracker.content_hash = ""
            active = None

        # blocks 骤降保护（compact/重启导致 blocks 从头累积）
        if tracker.simple_round_start > len(blocks):
            logger.warning(
                f"[simple] blocks regression: round_start={tracker.simple_round_start} "
                f"> len(blocks)={len(blocks)}, reset (session={tracker.session_name})"
            )
            tracker.simple_round_start = round_start
            tracker.content_hash = ""
            active = None

        blocks_slice = blocks[tracker.simple_round_start:]

        card_dict = build_stream_card(
            blocks_slice, status_line, bottom_bar,
            agent_panel=agent_panel, option_block=option_block,
            session_name=tracker.session_name, cli_type=cli_type,
            skip_tools=True,
            hook_state=tracker.hook_state, hook_progress=tracker.hook_progress,
        )

        # 超限裁剪：从头删减本轮 blocks
        start_idx = tracker.simple_round_start
        card_size = len(json.dumps(card_dict, ensure_ascii=False).encode('utf-8'))
        while card_size > CARD_SIZE_LIMIT and len(blocks_slice) > 1:
            blocks_slice = blocks_slice[1:]
            start_idx += 1
            card_dict = build_stream_card(
                blocks_slice, status_line, bottom_bar,
                agent_panel=agent_panel, option_block=option_block,
                session_name=tracker.session_name, cli_type=cli_type,
                skip_tools=True,
                hook_state=tracker.hook_state, hook_progress=tracker.hook_progress,
            )
            card_size = len(json.dumps(card_dict, ensure_ascii=False).encode('utf-8'))

        new_hash = self._compute_hash(blocks_slice, status_line, bottom_bar, agent_panel, option_block, tracker.hook_state, tracker.hook_progress)

        if active is None:
            # 创建本轮新卡
            card_id = await self._card_service.create_card(card_dict)
            if card_id:
                await self._card_service.send_card(tracker.chat_id, card_id)
                tracker.cards.append(CardSlice(card_id=card_id, start_idx=start_idx))
                tracker.content_hash = new_hash
                _track_stats('card', 'create', session_name=tracker.session_name,
                             chat_id=tracker.chat_id)
                logger.info(
                    f"[simple NEW] session={tracker.session_name} start_idx={start_idx} "
                    f"card_id={card_id}"
                )
            else:
                logger.warning(f"[simple] create_card 失败 session={tracker.session_name}")
            return

        # 更新本轮卡片
        if new_hash == tracker.content_hash:
            return

        active.start_idx = start_idx
        active.sequence += 1
        success = await self._card_service.update_card(
            card_id=active.card_id, sequence=active.sequence, card_content=card_dict,
        )
        if not success:
            logger.warning(f"[simple] update_card 失败 card_id={active.card_id}，降级新卡")
            _track_stats('card', 'fallback', session_name=tracker.session_name,
                         chat_id=tracker.chat_id)
            new_card_id = await self._card_service.create_card(card_dict)
            if new_card_id:
                await self._card_service.send_card(tracker.chat_id, new_card_id)
                active.card_id = new_card_id
                active.sequence = 0
        else:
            _track_stats('card', 'update', session_name=tracker.session_name,
                         chat_id=tracker.chat_id)
        tracker.content_hash = new_hash

    async def _create_new_card(
        self, tracker: StreamTracker, blocks: List[dict],
        status_line: Optional[dict], bottom_bar: Optional[dict],
        agent_panel: Optional[dict] = None,
        option_block: Optional[dict] = None,
        cli_type: str = "claude",
    ) -> None:
        """创建新卡片（首次 attach 或冻结后）"""
        if not tracker.cards:
            # 首次 attach：取最近 INITIAL_WINDOW 个 blocks
            start_idx = max(0, len(blocks) - INITIAL_WINDOW)
        else:
            # 冻结后：从上张冻结卡片的结束位置开始
            last_frozen = tracker.cards[-1]
            start_idx = last_frozen.start_idx + MAX_CARD_BLOCKS
            if start_idx >= len(blocks):
                start_idx = 0
                logger.warning(
                    f"[_create_new_card] start_idx overflow, reset to 0 "
                    f"(frozen.start_idx={last_frozen.start_idx}, total blocks={len(blocks)})"
                )

        blocks_slice = blocks[start_idx:]
        # 注意：不在此处提前 return，上层 _do_card_update 已做过滤，
        # 走到这里说明确实需要创建卡片（如 Codex 就绪等待输入的空内容卡片）

        from .card_builder import build_stream_card
        card_dict = build_stream_card(blocks_slice, status_line, bottom_bar, agent_panel=agent_panel, option_block=option_block, session_name=tracker.session_name, cli_type=cli_type, hook_state=tracker.hook_state, hook_progress=tracker.hook_progress)

        # 新卡大小检查：超限则从头部裁剪
        card_size = len(json.dumps(card_dict, ensure_ascii=False).encode('utf-8'))
        while card_size > CARD_SIZE_LIMIT and len(blocks_slice) > 1:
            blocks_slice = blocks_slice[1:]
            start_idx += 1
            card_dict = build_stream_card(blocks_slice, status_line, bottom_bar, agent_panel=agent_panel, option_block=option_block, session_name=tracker.session_name, cli_type=cli_type, hook_state=tracker.hook_state, hook_progress=tracker.hook_progress)
            card_size = len(json.dumps(card_dict, ensure_ascii=False).encode('utf-8'))

        card_id = await self._card_service.create_card(card_dict)

        if card_id:
            await self._card_service.send_card(tracker.chat_id, card_id)
            tracker.cards.append(CardSlice(card_id=card_id, start_idx=start_idx))
            tracker.content_hash = self._compute_hash(blocks_slice, status_line, bottom_bar, agent_panel, option_block, tracker.hook_state, tracker.hook_progress)
            _track_stats('card', 'create', session_name=tracker.session_name,
                         chat_id=tracker.chat_id)
            logger.info(
                f"[NEW] session={tracker.session_name} start_idx={start_idx} "
                f"blocks={len(blocks_slice)} card_id={card_id}"
            )
        else:
            logger.warning(f"create_card 失败 session={tracker.session_name}")

    async def _handle_element_limit(
        self, tracker: StreamTracker, blocks: List[dict],
        status_line: Optional[dict], bottom_bar: Optional[dict],
        agent_panel: Optional[dict] = None,
        option_block: Optional[dict] = None,
        cli_type: str = "claude",
    ) -> None:
        """元素超限：冻结旧卡片 + 推送新流式卡片"""
        active = tracker.cards[-1]
        logger.warning(f"元素超限，冻结卡片 {active.card_id} 并推新卡")

        # 1. 冻结旧卡片（灰色 header，无状态区和按钮）
        from .card_builder import build_stream_card
        blocks_slice = blocks[active.start_idx:]
        frozen_card = build_stream_card(blocks_slice, None, None, is_frozen=True)
        active.sequence += 1
        await self._card_service.update_card(active.card_id, active.sequence, frozen_card)
        active.frozen = True
        _track_stats('card', 'freeze', session_name=tracker.session_name,
                     chat_id=tracker.chat_id)

        # 2. 创建新流式卡片，从最近 INITIAL_WINDOW 个 blocks 开始（重置窗口）
        new_start = max(0, len(blocks) - INITIAL_WINDOW)
        new_blocks = blocks[new_start:]
        if not new_blocks and not status_line and not bottom_bar:
            return
        new_card_dict = build_stream_card(
            new_blocks, status_line, bottom_bar,
            agent_panel=agent_panel, option_block=option_block,
            session_name=tracker.session_name,
            cli_type=cli_type,
            hook_state=tracker.hook_state, hook_progress=tracker.hook_progress,
        )
        new_card_id = await self._card_service.create_card(new_card_dict)
        if new_card_id:
            await self._card_service.send_card(tracker.chat_id, new_card_id)
            tracker.cards.append(CardSlice(card_id=new_card_id, start_idx=new_start))
            tracker.content_hash = self._compute_hash(new_blocks, status_line, bottom_bar, agent_panel, option_block, tracker.hook_state, tracker.hook_progress)
            _track_stats('card', 'create', session_name=tracker.session_name,
                         chat_id=tracker.chat_id)
            logger.info(
                f"[ELEMENT_LIMIT_SPLIT] session={tracker.session_name} "
                f"new_start={new_start} blocks={len(new_blocks)} card_id={new_card_id}"
            )
            tracker.last_notify_message_id = None

    def _find_freeze_count(self, blocks_slice: List[dict], session_name: str) -> int:
        """二分查找冻结卡片能容纳的最大 blocks 数（保证卡片 JSON 大小 ≤ CARD_SIZE_LIMIT）"""
        from .card_builder import build_stream_card
        lo, hi = 1, len(blocks_slice)
        result = 1
        while lo <= hi:
            mid = (lo + hi) // 2
            card = build_stream_card(blocks_slice[:mid], None, None,
                                     is_frozen=True, session_name=session_name)
            size = len(json.dumps(card, ensure_ascii=False).encode('utf-8'))
            if size <= CARD_SIZE_LIMIT:
                result = mid
                lo = mid + 1
            else:
                hi = mid - 1
        return result

    async def _freeze_and_split(
        self, tracker: StreamTracker, blocks: List[dict],
        status_line: Optional[dict], bottom_bar: Optional[dict],
        agent_panel: Optional[dict] = None,
        option_block: Optional[dict] = None,
        cli_type: str = "claude",
        freeze_count: Optional[int] = None,
    ) -> None:
        """冻结当前卡片 + 开新卡"""
        active = tracker.cards[-1]
        count = freeze_count if freeze_count is not None else MAX_CARD_BLOCKS
        reason = 'size' if freeze_count is not None else 'count'

        # 冻结当前卡片（只保留前 count 个 blocks，移除状态区和按钮）
        frozen_blocks = blocks[active.start_idx:active.start_idx + count]
        from .card_builder import build_stream_card
        frozen_card = build_stream_card(frozen_blocks, None, None, is_frozen=True)
        active.sequence += 1
        await self._card_service.update_card(active.card_id, active.sequence, frozen_card)
        active.frozen = True
        _track_stats('card', 'freeze', session_name=tracker.session_name,
                     chat_id=tracker.chat_id)
        logger.info(
            f"[FREEZE] session={tracker.session_name} card_id={active.card_id} "
            f"blocks=[{active.start_idx}:{active.start_idx + count}] reason={reason}"
        )

        # 创建新卡片
        new_start = active.start_idx + count
        new_blocks = blocks[new_start:]
        if not new_blocks:
            return

        new_card_dict = build_stream_card(new_blocks, status_line, bottom_bar, agent_panel=agent_panel, option_block=option_block, session_name=tracker.session_name, cli_type=cli_type, hook_state=tracker.hook_state, hook_progress=tracker.hook_progress)

        # 新卡大小检查：超限则从头部裁剪
        new_card_size = len(json.dumps(new_card_dict, ensure_ascii=False).encode('utf-8'))
        while new_card_size > CARD_SIZE_LIMIT and len(new_blocks) > 1:
            new_blocks = new_blocks[1:]
            new_start += 1
            new_card_dict = build_stream_card(new_blocks, status_line, bottom_bar, agent_panel=agent_panel, option_block=option_block, session_name=tracker.session_name, cli_type=cli_type, hook_state=tracker.hook_state, hook_progress=tracker.hook_progress)
            new_card_size = len(json.dumps(new_card_dict, ensure_ascii=False).encode('utf-8'))

        new_card_id = await self._card_service.create_card(new_card_dict)
        if new_card_id:
            await self._card_service.send_card(tracker.chat_id, new_card_id)
            tracker.cards.append(CardSlice(card_id=new_card_id, start_idx=new_start))
            tracker.content_hash = self._compute_hash(new_blocks, status_line, bottom_bar, agent_panel, option_block, tracker.hook_state, tracker.hook_progress)
            logger.info(
                f"[NEW after FREEZE] session={tracker.session_name} start_idx={new_start} "
                f"blocks={len(new_blocks)} card_id={new_card_id}"
            )
            tracker.last_notify_message_id = None

    READY_DEBOUNCE = 2.0  # 就绪状态去抖：连续 N 秒保持 ready 才确认

    def _update_ready_state(
        self, tracker: StreamTracker,
        blocks: list, status_line: Optional[dict], option_block: Optional[dict],
        agent_panel: Optional[dict] = None,
    ) -> bool:
        """更新就绪状态（含去抖），返回是否需要发送就绪通知（不执行发送）

        两种模式：
        - hook 模式：直接用 turn_complete（权威信号），pending 状态时抑制
        - 解析模式：_is_ready() 启发式 + 去抖
        """
        hs = tracker.hook_state
        now = time.time()

        if hs is not None:
            # hook 模式：turn_complete 是权威就绪信号
            if hs.get("pending_question") or hs.get("pending_permission") or hs.get("waiting_permission"):
                tracker.prev_is_ready = False
                return False
            raw_ready = bool(hs.get("turn_complete", False))
        else:
            # 解析模式：启发式判断
            raw_ready = _is_ready(blocks, status_line, option_block, agent_panel)

        if raw_ready:
            if tracker.ready_since is None:
                tracker.ready_since = now
        else:
            tracker.ready_since = None

        # option_block 在场时跳过去抖（用户需要立即看到选项按钮）
        if option_block is not None:
            confirmed = raw_ready
        elif hs is not None:
            # hook 模式：turn_complete 已经是确定信号，不需要去抖
            confirmed = raw_ready
        else:
            confirmed = (
                raw_ready
                and tracker.ready_since is not None
                and (now - tracker.ready_since) >= self.READY_DEBOUNCE
            )

        prev_ready = tracker.prev_is_ready
        tracker.prev_is_ready = confirmed
        return confirmed and not prev_ready and tracker.is_group and _notify_enabled

    async def _send_ready_notification(
        self, tracker: StreamTracker, cli_type: str = "claude"
    ) -> None:
        """发送就绪通知（加急或新消息），应在卡片操作完成后调用"""
        count = _increment_ready_count()
        uid = tracker.notify_user_id or "all"
        cli_name = "Claude" if cli_type == "claude" else "Codex"
        logger.info(f"就绪提醒: chat_id={tracker.chat_id[:8]}..., count={count}, uid={uid}, "
                    f"last_msg={'有' if tracker.last_notify_message_id else '无'}")

        if tracker.last_notify_message_id and uid != "all" and _urgent_enabled:
            # 已有通知消息 + 加急开关开启 → 尝试加急
            try:
                ok = await self._card_service.send_urgent_app(
                    tracker.last_notify_message_id, [uid]
                )
                if ok:
                    # 加急成功 → 5 秒后自动取消
                    asyncio.create_task(self._cancel_urgent_later(
                        tracker.last_notify_message_id, [uid], delay=5
                    ))
                else:
                    # 加急失败（权限未开通等）→ 降级发新消息
                    label = ""
                    text = f'<at user_id="{uid}">{label}</at> {cli_name} 已就绪，等待您的输入...（这是第{count}次通知）'
                    msg_id = await self._card_service.send_text(tracker.chat_id, text)
                    if msg_id:
                        tracker.last_notify_message_id = msg_id
            except Exception as e:
                logger.warning(f"加急通知失败: {e}")
        else:
            # 首次通知（或无法加急时）→ 发新消息，记录 message_id
            label = "所有人" if uid == "all" else ""
            text = f'<at user_id="{uid}">{label}</at> {cli_name} 已就绪，等待您的输入...（这是第{count}次通知）'
            try:
                msg_id = await self._card_service.send_text(tracker.chat_id, text)
                if msg_id:
                    tracker.last_notify_message_id = msg_id
            except Exception as e:
                logger.warning(f"就绪提醒发送失败: {e}")

    async def _cancel_urgent_later(self, message_id: str, user_ids: list, delay: float = 15) -> None:
        """延迟取消加急通知"""
        await asyncio.sleep(delay)
        try:
            await self._card_service.cancel_urgent_app(message_id, user_ids)
        except Exception as e:
            logger.warning(f"延迟取消加急失败: {e}")

    def get_notify_enabled(self) -> bool:
        """获取就绪通知开关状态"""
        return _notify_enabled

    def set_notify_enabled(self, enabled: bool) -> None:
        """更新就绪通知开关状态并持久化"""
        global _notify_enabled
        _notify_enabled = enabled
        _save_notify_enabled(enabled)
        logger.info(f"就绪通知开关已{'开启' if enabled else '关闭'}")

    def get_urgent_enabled(self) -> bool:
        """获取加急通知开关状态"""
        return _urgent_enabled

    def set_urgent_enabled(self, enabled: bool) -> None:
        """更新加急通知开关状态并持久化"""
        global _urgent_enabled
        _urgent_enabled = enabled
        _save_urgent_enabled(enabled)
        logger.info(f"加急通知开关已{'开启' if enabled else '关闭'}")

    def get_bypass_enabled(self) -> bool:
        """获取新会话 bypass 开关状态"""
        return _bypass_enabled

    def set_bypass_enabled(self, enabled: bool) -> None:
        """更新新会话 bypass 开关状态并持久化"""
        global _bypass_enabled
        _bypass_enabled = enabled
        _save_bypass_enabled(enabled)
        logger.info(f"新会话 bypass 开关已{'开启' if enabled else '关闭'}")

    def get_simple_mode(self) -> bool:
        """获取简单模式开关状态（热加载，文件为准）"""
        return _current_simple_mode()

    def set_simple_mode(self, enabled: bool) -> None:
        """更新简单模式开关状态并持久化"""
        _save_simple_mode(enabled)
        _current_simple_mode()  # 立即刷新缓存（写盘后 mtime 变化触发重读）
        logger.info(f"简单模式开关已{'开启' if enabled else '关闭'}")

    def set_hook_progress(self, chat_id: str, progress: Optional[dict]) -> None:
        """由 lark_handler 调用，更新多问题作答进度"""
        tracker = self._trackers.get(chat_id)
        if tracker:
            tracker.hook_progress = progress

    @staticmethod
    def _compute_hash(
        blocks: list, status_line: Optional[dict],
        bottom_bar: Optional[dict], agent_panel: Optional[dict] = None,
        option_block: Optional[dict] = None,
        hook_state: Optional[dict] = None,
        hook_progress: Optional[dict] = None,
    ) -> str:
        """计算内容 hash（用于 diff）"""
        data = {
            "blocks": blocks,
            "status_line": status_line,
            "bottom_bar": bottom_bar,
            "agent_panel": agent_panel,
            "option_block": option_block,
            "hook_state": hook_state,
            "hook_progress": hook_progress,
        }
        return hashlib.md5(
            json.dumps(data, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()


# ── 模块级辅助函数 ────────────────────────────────────────────────────────────

def _is_ready(blocks: list, status_line: Optional[dict], option_block: Optional[dict], agent_panel: Optional[dict] = None) -> bool:
    """数据层就绪判断：无 streaming block、无 status_line、无后台 agent（option_block 不影响就绪）"""
    has_streaming = any(b.get("is_streaming", False) for b in blocks)
    has_agents = agent_panel is not None
    return not has_streaming and status_line is None and not has_agents


_READY_COUNT_FILE = USER_DATA_DIR / "ready_notify_count"
_NOTIFY_ENABLED_FILE = USER_DATA_DIR / "ready_notify_enabled"
_URGENT_ENABLED_FILE = USER_DATA_DIR / "urgent_notify_enabled"
_BYPASS_ENABLED_FILE = USER_DATA_DIR / "bypass_enabled"
_SIMPLE_MODE_FILE = USER_DATA_DIR / "simple_mode_enabled"


def _load_notify_enabled() -> bool:
    """读取就绪通知开关状态，不存在或解析失败返回 True（默认开启）"""
    try:
        return _NOTIFY_ENABLED_FILE.read_text().strip() == "1"
    except Exception:
        return True


def _save_notify_enabled(enabled: bool) -> None:
    """持久化就绪通知开关状态"""
    try:
        ensure_user_data_dir()
        _NOTIFY_ENABLED_FILE.write_text("1" if enabled else "0")
    except Exception as e:
        logger.warning(f"_save_notify_enabled 失败: {e}")


def _load_urgent_enabled() -> bool:
    """读取加急通知开关状态，不存在或解析失败返回 False（默认关闭）"""
    try:
        return _URGENT_ENABLED_FILE.read_text().strip() == "1"
    except Exception:
        return False


def _save_urgent_enabled(enabled: bool) -> None:
    """持久化加急通知开关状态"""
    try:
        ensure_user_data_dir()
        _URGENT_ENABLED_FILE.write_text("1" if enabled else "0")
    except Exception as e:
        logger.warning(f"_save_urgent_enabled 失败: {e}")


def _load_bypass_enabled() -> bool:
    """读取新会话 bypass 开关状态，不存在或解析失败返回 False（默认关闭）"""
    try:
        return _BYPASS_ENABLED_FILE.read_text().strip() == "1"
    except Exception:
        return False


def _save_bypass_enabled(enabled: bool) -> None:
    """持久化新会话 bypass 开关状态"""
    try:
        ensure_user_data_dir()
        _BYPASS_ENABLED_FILE.write_text("1" if enabled else "0")
    except Exception as e:
        logger.warning(f"_save_bypass_enabled 失败: {e}")


def _load_simple_mode() -> bool:
    """读取简单模式开关状态，不存在或解析失败返回 False（默认关闭）"""
    try:
        return _SIMPLE_MODE_FILE.read_text().strip() == "1"
    except Exception:
        return False


def _save_simple_mode(enabled: bool) -> None:
    """持久化简单模式开关状态"""
    try:
        ensure_user_data_dir()
        _SIMPLE_MODE_FILE.write_text("1" if enabled else "0")
    except Exception as e:
        logger.warning(f"_save_simple_mode 失败: {e}")


def _current_simple_mode() -> bool:
    """读取简单模式开关，文件 mtime 变化时重新加载（热加载）。

    daemon 长驻时直接编辑 simple_mode_enabled 文件即可即时生效，
    无需 lark restart；带 mtime 缓存避免轮询热路径反复读盘。
    """
    global _simple_mode_cache, _simple_mode_mtime
    try:
        mtime = _SIMPLE_MODE_FILE.stat().st_mtime
        if mtime != _simple_mode_mtime:
            _simple_mode_mtime = mtime
            _simple_mode_cache = _SIMPLE_MODE_FILE.read_text().strip() == "1"
    except FileNotFoundError:
        _simple_mode_cache = False
        _simple_mode_mtime = 0.0
    except Exception:
        pass
    return _simple_mode_cache


# 模块级开关状态：启动时加载一次
_notify_enabled: bool = _load_notify_enabled()
_urgent_enabled: bool = _load_urgent_enabled()
_bypass_enabled: bool = _load_bypass_enabled()
# 简单模式：缓存 + mtime，热加载（见 _current_simple_mode）
_simple_mode_cache: bool = _load_simple_mode()
_simple_mode_mtime: float = 0.0


def _increment_ready_count() -> int:
    """原子递增全局就绪提醒计数器，返回新值（持久化到文件）"""
    try:
        ensure_user_data_dir()
        try:
            count = int(_READY_COUNT_FILE.read_text().strip())
        except Exception:
            count = 0
        count += 1
        _READY_COUNT_FILE.write_text(str(count))
        return count
    except Exception as e:
        logger.warning(f"_increment_ready_count 失败: {e}")
        return 1
