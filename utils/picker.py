"""极简方向键选择器（零依赖，基于 termios/tty）。

- 交互式 TTY：↑/↓（或 j/k）移动，Enter 确认，q/Esc/Ctrl+C 取消。
- 非交互环境（管道、重定向）：自动降级为编号输入。

供 CLI 交互选择会话 / 菜单使用，避免引入 questionary/prompt_toolkit 等依赖。
"""

import os
import select
import sys
import termios
import tty
from typing import List, Optional, Sequence


def _read_key(fd: int) -> str:
    """读取一次按键，归一化为 'up' / 'down' / 'enter' / 'cancel' / 'other'。"""
    b = os.read(fd, 1)
    if not b:
        return "cancel"
    if b in (b"\r", b"\n"):
        return "enter"
    if b in (b"\x03", b"\x04", b"q", b"Q"):  # Ctrl+C / Ctrl+D / q
        return "cancel"
    if b in (b"k", b"K"):
        return "up"
    if b in (b"j", b"J"):
        return "down"
    if b == b"\x1b":
        # 可能是方向键（ESC [ A / ESC O A），也可能是单独的 Esc
        r, _, _ = select.select([fd], [], [], 0.05)
        if not r:
            return "cancel"
        seq = os.read(fd, 2)
        if seq in (b"[A", b"OA"):
            return "up"
        if seq in (b"[B", b"OB"):
            return "down"
        return "other"
    return "other"


def _render(title: str, options: Sequence[str], idx: int, footer: str, first: bool) -> None:
    lines: List[str] = []
    if title:
        lines.append(f"\x1b[1m{title}\x1b[0m")
    for i, opt in enumerate(options):
        if i == idx:
            lines.append(f"\x1b[7m ❯ {opt} \x1b[0m")
        else:
            lines.append(f"   {opt}")
    if footer:
        lines.append(f"\x1b[2m{footer}\x1b[0m")

    out = sys.stdout
    if not first:
        out.write(f"\x1b[{len(lines) - 1}A")  # 回到首行
    out.write("\r")
    out.write("\r\n".join("\x1b[2K" + ln for ln in lines))  # 逐行清屏后重绘
    out.flush()


def _select_numbered(title: str, options: Sequence[str]) -> Optional[int]:
    if title:
        print(title)
    for i, opt in enumerate(options, 1):
        print(f"  {i}. {opt}")
    try:
        raw = input("选择编号（回车取消）: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if raw.isdigit() and 1 <= int(raw) <= len(options):
        return int(raw) - 1
    return None


def select(
    title: str,
    options: Sequence[str],
    *,
    default: int = 0,
    footer: str = "↑/↓ 选择 · Enter 确认 · q/Esc 取消",
) -> Optional[int]:
    """显示选择器，返回选中项的下标；取消返回 None。"""
    options = list(options)
    if not options:
        return None
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return _select_numbered(title, options)

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    idx = max(0, min(default, len(options) - 1))
    try:
        tty.setraw(fd)
        sys.stdout.write("\x1b[?25l")  # 隐藏光标
        _render(title, options, idx, footer, first=True)
        while True:
            key = _read_key(fd)
            if key == "enter":
                return idx
            if key == "cancel":
                return None
            if key == "up":
                idx = (idx - 1) % len(options)
            elif key == "down":
                idx = (idx + 1) % len(options)
            else:
                continue
            _render(title, options, idx, footer, first=False)
    finally:
        sys.stdout.write("\x1b[?25h")  # 恢复光标
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sys.stdout.write("\r\n")
        sys.stdout.flush()
