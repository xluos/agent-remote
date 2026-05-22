"""Session-name ↔ Claude session UUID 映射的持久化

存储在 ~/.agent-remote/sessions.json，结构：

  {
    "<session_name>": {
      "claude_uuid": "<uuid4>",
      "cli_type":    "claude" | "codex",
      "created_at":  "2026-...",
      "last_used_at":"2026-..."
    },
    ...
  }

只对 claude 注入 --session-id / --resume；codex 暂时没用这个机制（codex
没有等价的 --resume API）。

为什么放 utils/ 不放 lark_client/：这不是飞书的事，是 agents-remote 这个
产品自己的"会话身份"管理，普通 cla 用户重启也要它来恢复对话。
"""

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

from .session import USER_DATA_DIR


def _map_file() -> Path:
    return USER_DATA_DIR / "sessions.json"


def _load() -> dict:
    p = _map_file()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(data: dict) -> None:
    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(
        str(_map_file()),
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def get_or_create(session_name: str, cli_type: str = "claude") -> Tuple[str, bool]:
    """查找或新建一个 claude session UUID

    Returns
    -------
    (uuid, is_new) : tuple
        is_new=True  → 这是第一次为该 session_name 生成 UUID，调用方应该
                       传给 claude 的 --session-id（让 claude 用这个 ID 起新会话）
        is_new=False → 该 session_name 之前用过，UUID 已存在；调用方应该
                       传给 claude 的 --resume（让 claude 续上之前的对话）
    """
    data = _load()
    now = datetime.now(timezone.utc).isoformat()

    entry = data.get(session_name)
    if entry and entry.get("claude_uuid"):
        # 续用
        entry["last_used_at"] = now
        data[session_name] = entry
        _save(data)
        return entry["claude_uuid"], False

    # 新建
    new_uuid = str(uuid.uuid4())
    data[session_name] = {
        "claude_uuid": new_uuid,
        "cli_type":    cli_type,
        "created_at":  now,
        "last_used_at": now,
    }
    _save(data)
    return new_uuid, True


def lookup(session_name: str) -> Optional[str]:
    """只查不创建。返回 uuid 或 None。"""
    data = _load()
    entry = data.get(session_name)
    return entry.get("claude_uuid") if entry else None


def forget(session_name: str) -> None:
    """删除一个映射（用于 kill session 后清理）"""
    data = _load()
    if session_name in data:
        data.pop(session_name)
        _save(data)


def claude_resume_args(session_name: str, cli_type: str = "claude") -> list:
    """返回要追加到 claude 命令行的参数列表

    - codex 暂不支持，返回空（codex 没有等价的 --resume）
    - claude 首次启动 → ['--session-id', '<uuid>']
    - claude 续会话   → ['--resume', '<uuid>']
    """
    if cli_type != "claude":
        return []
    uid, is_new = get_or_create(session_name, cli_type)
    if is_new:
        return ["--session-id", uid]
    return ["--resume", uid]
