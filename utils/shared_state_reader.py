"""共享内存读端（从 server/shared_state.py 抽离）

server 端（SharedStateWriter）已迁移到 agents-remote-core。
本文件仅保留读端供 lark_client 和 utils 使用。
"""

import json
import struct
from pathlib import Path

from .session import get_mq_path

# 布局常量（与 agents-remote-core 的 SharedStateWriter 保持一致）
MAGIC = b'RCMQ'
HEADER_SIZE = 64
COMPLETED_OFFSET = HEADER_SIZE


class SharedStateReader:
    """读端：按需调用 read() 获取最新快照。

    使用直接文件 I/O 而非 mmap，避免 macOS 上 mmap ACCESS_READ 不可靠地
    反映跨进程写入更新的问题。
    """

    _EMPTY = {"blocks": [], "status_line": None, "bottom_bar": None, "option_block": None, "cli_type": "claude"}

    def __init__(self, session_name: str):
        self._path = get_mq_path(session_name)

    def read(self) -> dict:
        """读取当前完整快照，返回 dict"""
        try:
            with open(self._path, 'rb') as f:
                header = f.read(16)
                if len(header) < 16 or header[:4] != MAGIC:
                    return self._EMPTY
                version = struct.unpack('>I', header[4:8])[0]
                if version < 2:
                    return self._EMPTY
                snapshot_len, _sequence = struct.unpack('>II', header[8:16])
                if snapshot_len == 0:
                    return self._EMPTY
                f.seek(COMPLETED_OFFSET)
                return json.loads(f.read(snapshot_len).decode('utf-8'))
        except Exception:
            return self._EMPTY

    def close(self):
        pass
