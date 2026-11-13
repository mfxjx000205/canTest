"""帧级工具：数据编解码、python-can 消息转换、帧匹配。

这里刻意不依赖 pydantic，方便被 models / bus / api 任意层复用。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Optional, Sequence, Union

import can

__all__ = [
    "DataLike",
    "parse_data",
    "parse_can_id",
    "format_data",
    "format_id",
    "message_to_dict",
    "FrameMatcher",
    "frame_bits",
]

DataLike = Union[str, bytes, bytearray, Sequence[int], None]

_HEX_CLEANUP = re.compile(r"[\s,;:_\-]|0[xX]")

MAX_CLASSIC_DLC = 8
# CAN FD 允许 0~64 字节，但 DLC 只能取这些长度
FD_DLC_LENGTHS = (0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 16, 20, 24, 32, 48, 64)


def parse_data(value: DataLike) -> bytes:
    """把多种写法统一成 bytes。

    支持：

    - ``"1122334455667788"``      连续十六进制
    - ``"11 22 33 44 55 66 77 88"``
    - ``"0x11,0x22,0x33"``
    - ``[0x11, 0x22, ...]``
    - ``b"\\x11\\x22"``
    - ``None`` / ``""``  ->  ``b""``
    """
    if value is None:
        return b""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, str):
        cleaned = _HEX_CLEANUP.sub("", value)
        if not cleaned:
            return b""
        if len(cleaned) % 2 != 0:
            raise ValueError(f"十六进制字符串长度必须为偶数，收到 {value!r}")
        try:
            return bytes.fromhex(cleaned)
        except ValueError as exc:
            raise ValueError(f"非法的十六进制数据：{value!r}") from exc
    if isinstance(value, Sequence):
        out = bytearray()
        for item in value:
            if not isinstance(item, int) or not 0 <= item <= 0xFF:
                raise ValueError(f"字节值必须是 0~255 的整数，收到 {item!r}")
            out.append(item)
        return bytes(out)
    raise TypeError(f"不支持的数据类型：{type(value)!r}")


def parse_can_id(value: "int | str | None") -> int:
    """把 ID 的各种写法统一成整数。

    CAN 的仲裁 ID 在日常沟通里几乎总是十六进制（``0x18FF50E5``），
    但 JSON 只有数字，让调用方自己算成 419385573 既容易错也不可读。
    这里按 Python 字面量习惯定规则，避免 "181 是十进制还是十六进制" 的歧义：

    - ``419385573``        整数，原样返回
    - ``"0x18FF50E5"``     带 ``0x`` 前缀 → 十六进制
    - ``"18FF50E5"``       含 a~f 字母 → 十六进制（不带前缀也不可能是十进制）
    - ``"181"``            纯数字串 → 十进制（想写十六进制请加 ``0x``）
    """
    if isinstance(value, bool):
        # bool 是 int 的子类，但把 True 当 ID 显然是调用方搞错了
        raise TypeError("ID 不能是布尔值")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip().replace("_", "").replace(" ", "")
        if not text:
            raise ValueError("ID 不能为空字符串")
        try:
            if text[:2].lower() == "0x":
                return int(text, 16)
            if text.isdigit():
                return int(text, 10)
            return int(text, 16)
        except ValueError as exc:
            raise ValueError(
                f"非法的 ID：{value!r}（示例：0x18FF50E5 或 419385573）"
            ) from exc
    raise TypeError(f"不支持的 ID 类型：{type(value)!r}")


def format_data(data: Iterable[int]) -> str:
    """bytes -> 连续小写十六进制字符串（不含 0x）。"""
    return bytes(data).hex()


def format_id(can_id: int, extended: bool = False) -> str:
    width = 8 if extended else 3
    return f"0x{can_id:0{width}X}"


def _dlc_for_length(length: int) -> int:
    for idx, allowed in enumerate(FD_DLC_LENGTHS):
        if length == allowed:
            return idx
        if length < allowed:
            # 长度不合法时向上取整到最近的 DLC
            return idx
    return len(FD_DLC_LENGTHS) - 1


def message_to_dict(msg: can.Message) -> Dict[str, Any]:
    """python-can 消息 -> 可直接序列化成 JSON 的字典。"""
    data = bytes(msg.data) if msg.data else b""
    # Message.channel 可能是 int（驱动通道号）也可能是 str，统一成字符串
    channel = getattr(msg, "channel", None)
    return {
        "id": msg.arbitration_id,
        "id_hex": format_id(msg.arbitration_id, msg.is_extended_id),
        "data": format_data(data),
        "data_bytes": list(data),
        "dlc": msg.dlc if msg.dlc is not None else len(data),
        "length": len(data),
        "is_extended_id": bool(msg.is_extended_id),
        "is_fd": bool(msg.is_fd),
        "bitrate_switch": bool(msg.bitrate_switch),
        "is_remote_frame": bool(msg.is_remote_frame),
        "is_error_frame": bool(msg.is_error_frame),
        "is_rx": bool(msg.is_rx),
        "timestamp": msg.timestamp,
        "channel": str(channel) if channel is not None else None,
    }


def frame_bits(msg: can.Message) -> int:
    """估算一帧占用的总线位数（含位填充余量），用于总线负载计算。

    经典帧：SOF+ID+RTR+IDE+r0+DLC+CRC+ACK+EOF+IFS。
    标准帧约 47 bit 固定开销，扩展帧约 67 bit；FD 帧的 CRC 更长、无 IFS 折算。
    位填充最坏情况会插入约 20% 额外位，这里取 1.15 的工程系数。
    """
    if msg.is_error_frame:
        return 0

    if msg.is_fd:
        fixed = 50 if msg.is_extended_id else 36
        crc_bits = 27 if len(msg.data) <= 16 else 32
        fixed += crc_bits
    else:
        fixed = 67 if msg.is_extended_id else 47
        fixed += 3  # IFS

    payload = 0 if msg.is_remote_frame else len(msg.data) * 8
    return int((fixed + payload) * 1.15)


@dataclass
class FrameMatcher:
    """帧匹配条件，用于 expect / wait_for / 过滤断言。

    ID 用 ``(实际ID & id_mask) == (can_id & id_mask)`` 判定，
    数据用前缀逐字节带掩码比较，未指定的字段不参与匹配。
    """

    can_id: Optional[int] = None
    id_mask: Optional[int] = None
    data: Optional[bytes] = None
    data_mask: Optional[bytes] = None
    is_extended_id: Optional[bool] = None
    is_fd: Optional[bool] = None
    is_remote_frame: Optional[bool] = None
    allow_error_frames: bool = False
    extra: Dict[str, Any] = field(default_factory=dict)

    def matches(self, msg: can.Message) -> bool:
        if msg.is_error_frame and not self.allow_error_frames:
            return False

        if self.can_id is not None:
            mask = self.id_mask
            if mask is None:
                mask = 0x1FFFFFFF if msg.is_extended_id else 0x7FF
            if (msg.arbitration_id & mask) != (self.can_id & mask):
                return False

        if self.is_extended_id is not None and msg.is_extended_id != self.is_extended_id:
            return False
        if self.is_fd is not None and msg.is_fd != self.is_fd:
            return False
        if self.is_remote_frame is not None and msg.is_remote_frame != self.is_remote_frame:
            return False

        if self.data is not None:
            actual = bytes(msg.data)
            if len(actual) < len(self.data):
                return False
            mask = self.data_mask
            for idx, expected in enumerate(self.data):
                m = mask[idx] if mask is not None and idx < len(mask) else 0xFF
                if (actual[idx] & m) != (expected & m):
                    return False

        return True
