"""
通信协议定义

消息格式：JSON + 换行符分隔
二进制数据使用 base64 编码
"""

import json
import base64
from dataclasses import dataclass, asdict
from typing import Optional, List
from enum import Enum


class MessageType(str, Enum):
    """消息类型"""
    INPUT = "input"          # 客户端 -> 服务端：用户输入
    OUTPUT = "output"        # 服务端 -> 客户端：Claude 输出
    HISTORY = "history"      # 历史输出（重连时）
    ERROR = "error"          # 错误消息
    RESIZE = "resize"        # 终端大小变化
    PERMISSION_RESPONSE = "permission_response"  # 客户端 -> 服务端：权限决策
    QUESTION_RESPONSE = "question_response"      # 客户端 -> 服务端：AskUserQuestion 答案


@dataclass
class Message:
    """基础消息"""
    type: str

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, data: str) -> "Message":
        obj = json.loads(data)
        msg_type = obj.get("type")

        if msg_type == MessageType.INPUT:
            return InputMessage.from_dict(obj)
        elif msg_type == MessageType.OUTPUT:
            return OutputMessage.from_dict(obj)
        elif msg_type == MessageType.HISTORY:
            return HistoryMessage.from_dict(obj)
        elif msg_type == MessageType.ERROR:
            return ErrorMessage.from_dict(obj)
        elif msg_type == MessageType.RESIZE:
            return ResizeMessage.from_dict(obj)
        elif msg_type == MessageType.PERMISSION_RESPONSE:
            return PermissionResponseMessage.from_dict(obj)
        elif msg_type == MessageType.QUESTION_RESPONSE:
            return QuestionResponseMessage.from_dict(obj)
        else:
            raise ValueError(f"Unknown message type: {msg_type}")


@dataclass
class InputMessage(Message):
    """用户输入消息"""
    data: str  # base64 编码的输入
    client_id: str

    def __init__(self, data: bytes, client_id: str):
        super().__init__(type=MessageType.INPUT)
        self.data = base64.b64encode(data).decode('ascii')
        self.client_id = client_id

    def get_data(self) -> bytes:
        return base64.b64decode(self.data)

    @classmethod
    def from_dict(cls, obj: dict) -> "InputMessage":
        msg = object.__new__(cls)
        msg.type = obj["type"]
        msg.data = obj["data"]
        msg.client_id = obj["client_id"]
        return msg


@dataclass
class OutputMessage(Message):
    """Claude 输出消息"""
    data: str  # base64 编码的输出

    def __init__(self, data: bytes):
        super().__init__(type=MessageType.OUTPUT)
        self.data = base64.b64encode(data).decode('ascii')

    def get_data(self) -> bytes:
        return base64.b64decode(self.data)

    @classmethod
    def from_dict(cls, obj: dict) -> "OutputMessage":
        msg = object.__new__(cls)
        msg.type = obj["type"]
        msg.data = obj["data"]
        return msg


@dataclass
class HistoryMessage(Message):
    """历史输出消息（重连时发送）"""
    data: str  # base64 编码的历史输出

    def __init__(self, data: bytes):
        super().__init__(type=MessageType.HISTORY)
        self.data = base64.b64encode(data).decode('ascii')

    def get_data(self) -> bytes:
        return base64.b64decode(self.data)

    @classmethod
    def from_dict(cls, obj: dict) -> "HistoryMessage":
        msg = object.__new__(cls)
        msg.type = obj["type"]
        msg.data = obj["data"]
        return msg


@dataclass
class ErrorMessage(Message):
    """错误消息"""
    message: str
    code: Optional[str] = None

    def __init__(self, message: str, code: Optional[str] = None):
        super().__init__(type=MessageType.ERROR)
        self.message = message
        self.code = code

    @classmethod
    def from_dict(cls, obj: dict) -> "ErrorMessage":
        msg = object.__new__(cls)
        msg.type = obj["type"]
        msg.message = obj["message"]
        msg.code = obj.get("code")
        return msg


@dataclass
class ResizeMessage(Message):
    """终端大小变化消息"""
    rows: int
    cols: int
    client_id: str

    def __init__(self, rows: int, cols: int, client_id: str):
        super().__init__(type=MessageType.RESIZE)
        self.rows = rows
        self.cols = cols
        self.client_id = client_id

    @classmethod
    def from_dict(cls, obj: dict) -> "ResizeMessage":
        msg = object.__new__(cls)
        msg.type = obj["type"]
        msg.rows = obj["rows"]
        msg.cols = obj["cols"]
        msg.client_id = obj["client_id"]
        return msg


@dataclass
class PermissionResponseMessage(Message):
    """权限决策消息（客户端 → 服务端）

    消费端读到 hook_state.pending_permission 后，发此消息回复 allow/deny。
    服务端收到后写响应文件，解除 permission.sh 的等待。
    """
    request_id: str
    decision: str  # "allow" | "deny"

    def __init__(self, request_id: str, decision: str):
        super().__init__(type=MessageType.PERMISSION_RESPONSE)
        self.request_id = request_id
        self.decision = decision

    @classmethod
    def from_dict(cls, obj: dict) -> "PermissionResponseMessage":
        msg = object.__new__(cls)
        msg.type = obj["type"]
        msg.request_id = obj["request_id"]
        msg.decision = obj["decision"]
        return msg


@dataclass
class QuestionResponseMessage(Message):
    """AskUserQuestion 答案消息（客户端 → 服务端）

    消费端读到 hook_state.pending_question 后，发此消息回复选择的答案。
    服务端通过 PreToolUse 的 updatedInput.answers 注入答案，跳过交互 UI。
    """
    request_id: str
    answers: dict  # {question_text: selected_option_label}

    def __init__(self, request_id: str, answers: dict):
        super().__init__(type=MessageType.QUESTION_RESPONSE)
        self.request_id = request_id
        self.answers = answers

    def to_json(self) -> str:
        return json.dumps({
            "type": self.type,
            "request_id": self.request_id,
            "answers": self.answers,
        }, ensure_ascii=False)

    @classmethod
    def from_dict(cls, obj: dict) -> "QuestionResponseMessage":
        msg = object.__new__(cls)
        msg.type = obj["type"]
        msg.request_id = obj["request_id"]
        msg.answers = obj["answers"]
        return msg


def encode_message(msg: Message) -> bytes:
    """编码消息为字节流（JSON + 换行符）"""
    return (msg.to_json() + "\n").encode('utf-8')


def decode_message(data: bytes) -> Message:
    """解码消息"""
    return Message.from_json(data.decode('utf-8').strip())
