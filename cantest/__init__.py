"""CANtest —— CAN 总线上位机内核 + FastAPI 测试控制面。

最小用法::

    from cantest import BusManager, BusConfig, FrameSpec

    manager = BusManager()
    bus = manager.open(BusConfig(bitrate=1_000_000, f_clock_mhz=80))
    bus.send(FrameSpec(id=0x181, data="1122334455667788"))
    frames = bus.expect(matcher, timeout_s=0.5)
"""

from __future__ import annotations

from .bus import BusSession
from .errors import (
    BadRequest,
    BusNotFound,
    BusOffError,
    CanTestError,
    ChannelInUse,
    HardwareUnavailable,
    OperationTimeout,
    SendFailed,
    UnsupportedFeature,
)
from .frame import FrameMatcher, format_data, message_to_dict, parse_data
from .manager import BusManager
from .models import (
    BatchSendRequest,
    BusConfig,
    BusInfo,
    BusStats,
    CyclicInfo,
    CyclicRequest,
    ExpectRequest,
    FilterSpec,
    Frame,
    FrameSpec,
    RecordInfo,
    RecordRequest,
    ReplayRequest,
    TransactRequest,
)
from .timing import BitTiming, preset, solve

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "BusManager",
    "BusSession",
    "BusConfig",
    "BusInfo",
    "BusStats",
    "Frame",
    "FrameSpec",
    "FrameMatcher",
    "FilterSpec",
    "CyclicRequest",
    "CyclicInfo",
    "ExpectRequest",
    "TransactRequest",
    "RecordRequest",
    "RecordInfo",
    "ReplayRequest",
    "BatchSendRequest",
    "BitTiming",
    "solve",
    "preset",
    "parse_data",
    "format_data",
    "message_to_dict",
    "CanTestError",
    "BadRequest",
    "BusNotFound",
    "ChannelInUse",
    "HardwareUnavailable",
    "OperationTimeout",
    "BusOffError",
    "SendFailed",
    "UnsupportedFeature",
]
