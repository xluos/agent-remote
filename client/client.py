"""
客户端连接器

- 终端 raw mode 处理
- Socket 连接
- 输入转发
- 输出显示
- Ctrl+Q 退出（detach）：只断开当前 client 的视图，server / claude / 飞书桥都继续活
  · Ctrl+D 不拦截，放行给 claude（其天然语义是退出 inner CLI / EOF）
"""

import asyncio
import os
import re
import sys as _sys
_sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent.parent))  # 根目录 → protocol, utils
import sys
import tty
import termios
import signal
import select
from typing import Optional

from utils.protocol import (
    Message, MessageType, InputMessage, ResizeMessage,
    encode_message, decode_message
)
from utils.session import get_socket_path, generate_client_id, get_terminal_size
from utils.shared_state_reader import SharedStateReader

try:
    from stats import track as _track_stats
except Exception:
    def _track_stats(*args, **kwargs): pass


# 特殊按键 — detach client（server / claude / 飞书桥继续活）
#
# 只用 Ctrl+Q 做 detach。Ctrl+D 不再拦截：它的天然语义是退出 inner CLI（claude
# 的 EOF/exit），client 先于 claude 拦截输入，若把 Ctrl+D 也当 detach 截走，用户就
# 没法用 Ctrl+D 退出 claude 了 —— 故放行给 claude。
#
# 两种编码都要认：
#   1) 传统控制字节：终端未进 kitty keyboard protocol 时，Ctrl+Q=\x11
#   2) kitty CSI-u：server 把 claude 的 PTY 原始流原样透传给终端（server.py
#      _broadcast_output），claude 协商开启 kitty keyboard protocol 的序列会一并
#      透传到本地真实终端，于是终端把 Ctrl+字母 编码成 ESC[<codepoint>;<modifiers>u
#      （实测 Ghostty：Ctrl+Q => \x1b[113;5u）。
#      这种形式下旧的字节相等判断永远不匹配，detach 会静默失效。
CTRL_Q = b'\x11'
DETACH_KEYS = {CTRL_Q}

# kitty CSI-u：ESC [ <codepoint>[:sub] ; <modifiers>[:event] u
# 'q'=113；modifiers 字段 = 1 + 位掩码，Ctrl 位 = 4（纯 Ctrl 即 5）
_DETACH_CODEPOINTS = {113}  # Ctrl+Q
_KITTY_CSI_U_RE = re.compile(rb'^\x1b\[(\d+)(?::\d+)*(?:;(\d+)(?::\d+)*)?u$')


def _is_detach_key(data: bytes) -> bool:
    """判断一次输入是否是 detach 快捷键（兼容传统控制字节与 kitty CSI-u 编码）"""
    if data in DETACH_KEYS:
        return True
    m = _KITTY_CSI_U_RE.match(data)
    if not m:
        return False
    if int(m.group(1)) not in _DETACH_CODEPOINTS:
        return False
    modifiers = int(m.group(2)) if m.group(2) else 1
    # modifiers 字段 = 1 + 位掩码；Ctrl 位 = 4
    return bool((modifiers - 1) & 4)


class RemoteClient:
    """远程客户端"""

    def __init__(self, session_name: str, quiet: bool = False):
        self.session_name = session_name
        self.socket_path = get_socket_path(session_name)
        self.client_id = generate_client_id()
        # quiet=True 时连接成功不打印 ✅ 提示（由上层启动 UI 负责展示），
        # 但连接失败的详细诊断仍照常打印
        self.quiet = quiet

        # 连接
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self.buffer = b""

        # 状态
        self.running = False

        # 终端设置
        self.old_settings = None

        # 终端标题栏：读 .mq 共享内存的 hook_state，把"等权限/等问题/运行中/空闲"
        # 写进窗口标题。重点是区分"真的在跑工具" vs "卡住等飞书回复"，破解
        # 用户把"被 hook 拦截"误判成"工具调用卡死"的盲区。
        self._state_reader = SharedStateReader(session_name)
        self._base_title = os.path.basename(session_name.rstrip("/")) or session_name
        self._last_title: Optional[str] = None

    async def connect(self) -> bool:
        """连接到服务器"""
        if not self.socket_path.exists():
            print(
                f"❌ 错误: Socket 文件不存在\n"
                f"   会话名: {self.session_name}\n"
                f"   Socket 路径: {self.socket_path}\n"
                f"\n"
                f"   请使用 `python3 agent_remote.py list` 查看可用会话"
            )
            return False

        try:
            self.reader, self.writer = await asyncio.open_unix_connection(
                path=str(self.socket_path)
            )
            if not self.quiet:
                print(f"✅ 已连接到会话: {self.session_name}")
            return True
        except ConnectionRefusedError as e:
            # 检查进程状态
            from utils.session import list_active_sessions
            sessions = list_active_sessions()
            session_exists = any(s["name"] == self.session_name for s in sessions)

            print(
                f"❌ 连接失败: Connection refused\n"
                f"   会话名: {self.session_name}\n"
                f"   Socket 路径: {self.socket_path}\n"
                f"   文件存在: {self.socket_path.exists()}\n"
                f"   会话在列表中: {session_exists}\n"
                f"\n"
                f"   当前活跃会话:"
            )
            for s in sessions:
                print(f"     - {s['name']} (PID: {s.get('pid', 'N/A')})")
            print(
                f"\n"
                f"   可能原因:\n"
                f"     1. Server 进程已终止但 Socket 文件残留\n"
                f"     2. Socket 文件权限错误\n"
                f"\n"
                f"   建议操作:\n"
                f"     python3 agent_remote.py kill {self.session_name}\n"
                f"     python3 agent_remote.py start {self.session_name}"
            )
            return False
        except Exception as e:
            print(
                f"❌ 连接失败: {type(e).__name__}: {e}\n"
                f"   会话名: {self.session_name}\n"
                f"   Socket 路径: {self.socket_path}"
            )
            return False

    async def run(self):
        """运行客户端"""
        if not await self.connect():
            raise SystemExit(1)

        self.running = True
        _track_stats('terminal', 'connect', session_name=self.session_name)

        # 设置终端 raw mode
        self._setup_terminal()

        # 设置信号处理
        self._setup_signals()

        # 发送初始终端尺寸，让 server 将 PTY 调整为实际终端大小
        rows, cols = get_terminal_size()
        await self._send_resize(rows, cols)

        try:
            # 并行运行输入、输出处理与标题栏轮询
            await asyncio.gather(
                self._read_server(),
                self._read_stdin(),
                self._update_title_loop(),
                return_exceptions=True
            )
        finally:
            self._cleanup()

    def _setup_terminal(self):
        """设置终端 raw mode"""
        if sys.stdin.isatty():
            self.old_settings = termios.tcgetattr(sys.stdin)
            tty.setraw(sys.stdin.fileno())

    def _restore_terminal(self):
        """恢复终端设置"""
        if self.old_settings:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, self.old_settings)

    def _setup_signals(self):
        """设置信号处理"""
        signal.signal(signal.SIGWINCH, self._handle_resize)

    def _handle_resize(self, signum, frame):
        """处理终端大小变化"""
        if self.running and self.writer:
            rows, cols = get_terminal_size()
            asyncio.create_task(self._send_resize(rows, cols))

    async def _send_resize(self, rows: int, cols: int):
        """发送终端大小"""
        msg = ResizeMessage(rows, cols, self.client_id)
        await self._send_message(msg)

    async def _read_server(self):
        """读取服务器消息"""
        while self.running:
            try:
                msg = await asyncio.wait_for(self._read_message(), timeout=0.5)
                if msg is None:
                    self.running = False
                    break
                await self._handle_server_message(msg)
            except asyncio.TimeoutError:
                continue
            except Exception:
                break

    async def _read_message(self) -> Optional[Message]:
        """读取一条消息"""
        while True:
            if b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                try:
                    return decode_message(line)
                except Exception:
                    continue

            try:
                data = await self.reader.read(4096)
                if not data:
                    return None
                self.buffer += data
            except Exception:
                return None

    async def _handle_server_message(self, msg: Message):
        """处理服务器消息"""
        if msg.type == MessageType.OUTPUT:
            data = msg.get_data()
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()

        elif msg.type == MessageType.HISTORY:
            data = msg.get_data()
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()

    async def _read_stdin(self):
        """读取标准输入"""
        loop = asyncio.get_event_loop()

        while self.running:
            try:
                # 在线程池中读取标准输入（带超时）
                data = await loop.run_in_executor(None, self._read_stdin_sync)
                if data:
                    await self._handle_input(data)
                    if not self.running:
                        break
            except Exception:
                break

    def _read_stdin_sync(self) -> bytes:
        """同步读取标准输入（带超时，便于检查 running 状态）"""
        # 使用 select 等待输入，超时 0.1 秒
        rlist, _, _ = select.select([sys.stdin], [], [], 0.1)
        if rlist:
            return os.read(sys.stdin.fileno(), 1024)
        return b""

    async def _handle_input(self, data: bytes):
        """处理输入"""
        # Ctrl+Q → detach（client 退出，server / claude / 飞书桥继续活；Ctrl+D 放行给 claude）
        if _is_detach_key(data):
            self.running = False
            return

        # 其他按键都发送给 Claude
        _track_stats('terminal', 'input', session_name=self.session_name,
                     value=len(data))
        await self._send_input(data)

    async def _send_input(self, data: bytes):
        """发送输入"""
        msg = InputMessage(data, self.client_id)
        await self._send_message(msg)

    async def _send_message(self, msg: Message):
        """发送消息"""
        if self.writer:
            try:
                data = encode_message(msg)
                self.writer.write(data)
                await self.writer.drain()
            except Exception:
                pass

    async def _update_title_loop(self):
        """周期读共享内存，把会话状态写进终端标题栏。

        OSC 序列只改窗口标题、不占屏幕行，因此与 claude 的全屏 TUI 渲染零冲突
        （区别于"底部状态条"会和 claude 抢屏幕）。读 .mq 是同步文件 I/O，放到
        线程池里执行，避免阻塞 event loop。
        """
        loop = asyncio.get_event_loop()
        while self.running:
            try:
                snapshot = await loop.run_in_executor(None, self._state_reader.read)
                title = self._compute_title(snapshot)
                if title != self._last_title:
                    self._last_title = title
                    self._write_title(title)
            except Exception:
                pass
            await asyncio.sleep(0.5)

    def _compute_title(self, snapshot: dict) -> str:
        """根据共享内存快照算出标题；重点区分"卡住等回复" vs "正在跑工具"。

        仅在 hook 模式（hook_state 存在）下展示运行态——解析模式没有权威的
        "等权限/等问题"信号，强行猜测反而误导，故只显示会话名。
        """
        name = self._base_title
        hs = snapshot.get("hook_state")
        if not hs:
            return name
        if hs.get("waiting_permission") or hs.get("pending_permission"):
            return f"⏳ 等待权限确认 · {name}"
        if hs.get("pending_question"):
            return f"❓ 等待回答 · {name}"
        if not hs.get("turn_complete", True):
            tool = hs.get("active_tool")
            return f"⚙ 运行中 {tool} · {name}" if tool else f"⚙ 运行中 · {name}"
        return f"✓ {name}"

    def _write_title(self, title: str):
        """写 OSC 0 设置窗口标题（同步、单次原子写，不与 OUTPUT 写入交错）"""
        try:
            seq = b'\x1b]0;' + title.encode('utf-8', 'replace') + b'\x07'
            sys.stdout.buffer.write(seq)
            sys.stdout.buffer.flush()
        except Exception:
            pass

    def _cleanup(self):
        """清理"""
        self.running = False
        _track_stats('terminal', 'disconnect', session_name=self.session_name)
        # detach 时把标题恢复成干净的会话名，去掉状态标记
        self._write_title(self._base_title)
        self._restore_terminal()

        if self.writer:
            try:
                self.writer.close()
            except Exception:
                pass

        print("\n已断开连接")


def run_client(session_name: str, quiet: bool = False):
    """运行客户端"""
    client = RemoteClient(session_name, quiet=quiet)

    try:
        asyncio.run(client.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Agent Remote Client")
    parser.add_argument("session_name", help="会话名称")
    args = parser.parse_args()

    run_client(args.session_name)
