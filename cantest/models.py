"""对外的数据契约（pydantic 模型）。

这些模型定义了 FastAPI 的请求/响应结构，是自动测试平台唯一需要关心的部分。
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional, Union

import can
from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator, model_validator

from .frame import FD_DLC_LENGTHS, format_data, parse_data

__all__ = [
    "FilterSpec",
    "FrameSpec",
    "Frame",
    "BusConfig",
    "BusInfo",
    "BusStats",
    "CyclicRequest",
    "CyclicInfo",
    "ReceiveRequest",
    "ExpectRequest",
    "TransactRequest",
    "BatchSendRequest",
    "RecordRequest",
    "ReplayRequest",
    "ResetRequest",
]

MAX_CAN_ID_STD = 0x7FF
MAX_CAN_ID_EXT = 0x1FFFFFFF
MAX_FD_LEN = 64


# --------------------------------------------------------------------------
# 过滤器
# --------------------------------------------------------------------------
class FilterSpec(BaseModel):
    """硬件/软件过滤器。

    语义与 socketCAN 一致：``(frame_id & id_mask) == (can_id & id_mask)``。
    ``id_mask`` 中置 1 的位参与比较，置 0 的位被忽略（通配）。
    """

    model_config = ConfigDict(extra="forbid")

    can_id: int = Field(ge=0, description="要匹配的 CAN ID")
    id_mask: int = Field(default=MAX_CAN_ID_STD, ge=0, description="参与比较的位掩码")
    is_extended_id: Optional[bool] = Field(
        default=None, description="None 表示不区分标准/扩展帧"
    )

    @model_validator(mode="after")
    def _check(self) -> "FilterSpec":
        limit = MAX_CAN_ID_EXT if self.is_extended_id else MAX_CAN_ID_STD
        if self.can_id > MAX_CAN_ID_EXT:
            raise ValueError(f"can_id 超出范围：{self.can_id:#x}")
        if self.is_extended_id is False and self.can_id > limit:
            raise ValueError(f"标准帧 ID 不能超过 {limit:#x}")
        return self

    def to_socketcan(self) -> Dict[str, Any]:
        entry: Dict[str, Any] = {"can_id": self.can_id, "can_mask": self.id_mask}
        if self.is_extended_id is not None:
            entry["extended"] = self.is_extended_id
        return entry


# --------------------------------------------------------------------------
# 帧
# --------------------------------------------------------------------------
class FrameSpec(BaseModel):
    """待发送的帧。``data`` 接受 hex 字符串、字节数组或整数列表。"""

    model_config = ConfigDict(extra="forbid")

    id: int = Field(ge=0, description="仲裁 ID")
    data: bytes = Field(default=b"", description="数据域，hex 字符串或字节数组")
    is_extended_id: bool = False
    is_fd: bool = False
    bitrate_switch: bool = Field(
        default=False, description="CAN FD 是否切换数据段波特率（BRS）"
    )
    is_remote_frame: bool = False

    @field_validator("data", mode="before")
    @classmethod
    def _parse_data(cls, value: Any) -> bytes:
        try:
            return parse_data(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(str(exc)) from exc

    @field_serializer("data")
    def _dump_data(self, value: bytes, _info: Any) -> str:
        return value.hex()

    @model_validator(mode="after")
    def _check(self) -> "FrameSpec":
        limit = MAX_CAN_ID_EXT if self.is_extended_id else MAX_CAN_ID_STD
        if self.id > limit:
            raise ValueError(
                f"{'扩展' if self.is_extended_id else '标准'}帧 ID 不能超过 {limit:#x}"
            )
        if len(self.data) > MAX_FD_LEN:
            raise ValueError(f"数据长度不能超过 {MAX_FD_LEN} 字节")
        if not self.is_fd and len(self.data) > 8:
            raise ValueError("经典 CAN 帧数据不能超过 8 字节，请设置 is_fd=true")
        if self.is_remote_frame and self.data:
            raise ValueError("远程帧不能携带数据")
        if self.bitrate_switch and not self.is_fd:
            raise ValueError("bitrate_switch 仅在 CAN FD 帧上有效")
        return self

    def to_message(self) -> can.Message:
        return can.Message(
            arbitration_id=self.id,
            data=self.data,
            is_extended_id=self.is_extended_id,
            is_fd=self.is_fd,
            bitrate_switch=self.bitrate_switch,
            is_remote_frame=self.is_remote_frame,
            # python-can 的 is_rx 默认是 True，但这里是「待发送」的帧，
            # 不显式置 False 的话，发送接口返回体里的方向就是错的。
            is_rx=False,
            check=True,
        )


class Frame(BaseModel):
    """收到的 / 已发送的帧（响应体）。"""

    id: int
    id_hex: str
    data: str
    data_bytes: List[int]
    dlc: int
    length: int
    is_extended_id: bool
    is_fd: bool
    bitrate_switch: bool
    is_remote_frame: bool
    is_error_frame: bool
    is_rx: bool
    timestamp: float
    channel: Optional[str] = None

    @classmethod
    def from_message(cls, msg: can.Message) -> "Frame":
        from .frame import format_id, message_to_dict

        d = message_to_dict(msg)
        d["id_hex"] = format_id(d["id"], d["is_extended_id"])
        return cls(**d)


# --------------------------------------------------------------------------
# 总线配置 / 状态
# --------------------------------------------------------------------------
class BusConfig(BaseModel):
    """打开一条 CAN 通道所需的全部参数。"""

    model_config = ConfigDict(extra="forbid")

    interface: str = Field(
        default="auto",
        description="pcan / socketcan / kvaser / vector / slcan / virtual / auto",
    )
    channel: Union[str, int, None] = Field(
        default=None, description="通道名，如 PCAN_USBBUS1 / can0 / vcan0；None 自动取第一个"
    )
    bitrate: int = Field(default=1_000_000, ge=1000, description="仲裁段波特率")
    fd: bool = Field(default=False, description="是否启用 CAN FD")
    data_bitrate: Optional[int] = Field(
        default=None, ge=1000, description="FD 数据段波特率，仅 fd=true 时生效"
    )
    f_clock_mhz: float = Field(default=80, gt=0, description="CAN 控制器时钟（本机 80 MHz）")
    sample_point: float = Field(default=0.8, gt=0.5, lt=1.0)
    data_sample_point: float = Field(default=0.8, gt=0.5, lt=1.0)
    loopback: bool = Field(default=False, description="自发自收，不依赖总线对端")
    listen_only: bool = Field(default=False, description="只听不发（不产生 ACK）")
    receive_own_messages: bool = Field(default=False, description="是否把本机发出的帧也上报")
    termination: Optional[bool] = Field(
        default=None, description="内部终端电阻开关，None 表示使用驱动默认值"
    )
    filters: List[FilterSpec] = Field(default_factory=list, description="打开时即生效的过滤器")
    buffer_size: int = Field(default=50000, ge=100, le=1_000_000, description="历史帧环形缓冲长度")
    auto_recover: bool = Field(default=True, description="检测到 bus-off 时自动复位恢复")

    @model_validator(mode="after")
    def _check(self) -> "BusConfig":
        if self.data_bitrate and not self.fd:
            raise ValueError("data_bitrate 仅在 fd=true 时有效")
        if self.fd and self.data_bitrate and self.data_bitrate < self.bitrate:
            raise ValueError("FD 数据段波特率通常不低于仲裁段波特率")
        return self


class BusStats(BaseModel):
    tx_frames: int = 0
    rx_frames: int = 0
    tx_bytes: int = 0
    rx_bytes: int = 0
    error_frames: int = 0
    bus_off_count: int = 0
    tx_errors: int = 0
    rx_errors: int = 0
    tx_rate_fps: float = 0.0
    rx_rate_fps: float = 0.0
    bus_load: float = Field(default=0.0, description="估算总线负载 0~1")
    last_error: Optional[str] = None


class BusInfo(BaseModel):
    id: str
    interface: str
    channel: str
    state: str
    opened_at: float
    uptime_s: float
    config: BusConfig
    timing: Dict[str, Any]
    data_timing: Optional[Dict[str, Any]] = None
    filters: List[FilterSpec] = Field(default_factory=list)
    stats: BusStats
    hw_filters_active: bool = False


# --------------------------------------------------------------------------
# 操作请求
# --------------------------------------------------------------------------
class BatchSendRequest(BaseModel):
    frames: List[FrameSpec] = Field(min_length=1, max_length=10000)
    interval_ms: float = Field(default=0.0, ge=0, description="帧间间隔")


class ReceiveRequest(BaseModel):
    count: int = Field(default=1, ge=1, le=10000)
    timeout_ms: float = Field(default=1000, ge=0, le=600000)
    filter: Optional[FilterSpec] = None


class ExpectRequest(BaseModel):
    """断言式等待：在超时内等一帧满足条件的报文。"""

    can_id: Optional[int] = Field(default=None, description="期望的 ID，None 表示任意")
    id_mask: Optional[int] = Field(default=None, description="ID 掩码，用于位段匹配")
    data: Optional[str] = Field(default=None, description="期望的数据（hex），按前缀比对")
    data_mask: Optional[str] = Field(default=None, description="数据掩码（hex）")
    is_extended_id: Optional[bool] = None
    is_fd: Optional[bool] = None
    timeout_ms: float = Field(default=1000, ge=0, le=600000)
    count: int = Field(default=1, ge=1, le=10000, description="需要凑齐的匹配帧数")
    since_seq: Optional[int] = Field(
        default=None,
        description=(
            "只认这个 RX 序号之后的帧。把 /send 返回的 seq 带上来，"
            "就能消除「先发后等」跨两次 HTTP 调用的竞态"
        ),
    )
    include_buffer: bool = Field(
        default=False,
        description="true 时在已有历史帧中查找；false 时只认调用之后到达的帧",
    )
    fail_fast: bool = Field(
        default=True, description="超时未满足时返回 408，便于测试平台直接判失败"
    )


class TransactRequest(BaseModel):
    """发送一帧并在超时内等待响应，返回往返时延。自动化测试的核心动作。"""

    frame: FrameSpec
    expect: ExpectRequest = Field(default_factory=ExpectRequest)
    repeat: int = Field(default=1, ge=1, le=1000)


class CyclicRequest(BaseModel):
    """周期发送。frames 支持多帧序列，按同一周期依次发出。"""

    frames: List[FrameSpec] = Field(min_length=1, max_length=100)
    period_ms: float = Field(gt=0, le=600000)
    count: int = Field(default=0, ge=0, description="0 表示无限循环")
    name: Optional[str] = None
    start_delay_ms: float = Field(default=0, ge=0)


class CyclicInfo(BaseModel):
    id: str
    name: Optional[str]
    period_ms: float
    count: int
    sent_frames: int
    sent_cycles: int
    running: bool
    started_at: float
    elapsed_s: float
    actual_period_ms: float
    jitter_ms: float
    last_error: Optional[str] = None


class RecordRequest(BaseModel):
    format: Literal["jsonl", "csv", "asc", "blf"] = "jsonl"
    path: Optional[str] = Field(default=None, description="留空则自动生成到 recordings/")
    append: bool = False


class RecordInfo(BaseModel):
    active: bool
    path: Optional[str] = None
    format: Optional[str] = None
    frames: int = 0
    bytes: int = 0
    started_at: Optional[float] = None


class ReplayRequest(BaseModel):
    path: str
    speed: float = Field(default=1.0, gt=0, le=100)
    gap_ms: Optional[float] = Field(default=None, ge=0, description="固定帧间隔，覆盖原始时间戳")
    count: int = Field(default=1, ge=1)
    loop: bool = False


class ResetRequest(BaseModel):
    reason: str = "manual"
