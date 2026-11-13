"""后端探测与总线打开。

python-can 已经把各家驱动抽象成统一的 ``BusABC``，这一层额外负责三件事：

1. 决定默认用哪个后端（Windows 上优先 PEAK，Linux 上优先 socketCAN）；
2. 枚举通道（PCAN 走 ``CAN_GetValue(PCAN_ATTACHED_CHANNELS)``，socketCAN 读 sysfs）；
3. 把「波特率 + 时钟 + 采样点」翻译成后端认识的关键字参数。

不同后端支持的参数不一样，直接透传会撞 ``TypeError``，所以这里按后端组装 kwargs。
"""

from __future__ import annotations

import ctypes
import importlib
import os
import platform
from typing import Any, Dict, List, Optional, Tuple

import can

from .errors import BadRequest, HardwareUnavailable
from .models import BusConfig
from .timing import BitTiming, solve

__all__ = [
    "BACKEND_CAPS",
    "available_backends",
    "default_interface",
    "resolve_interface",
    "list_channels",
    "open_bus",
    "probe",
]

# 各后端能力矩阵。测试平台可据此决定某个用例能不能跑。
BACKEND_CAPS: Dict[str, Dict[str, Any]] = {
    "pcan": {
        "fd": True,
        "hw_filter": True,
        "listen_only": True,
        "sw_loopback": True,
        "error_frames": True,
        "bit_timing": True,
        "plat": "Windows / Linux",
    },
    "socketcan": {
        "fd": True,
        "hw_filter": True,
        "listen_only": False,  # 需先用 ip link 配置，见文档
        "sw_loopback": True,
        "error_frames": True,
        "bit_timing": True,
        "plat": "Linux",
    },
    "kvaser": {
        "fd": True,
        "hw_filter": True,
        "listen_only": True,
        "sw_loopback": True,
        "error_frames": True,
        "bit_timing": False,
        "plat": "Windows / Linux",
    },
    "vector": {
        "fd": True,
        "hw_filter": True,
        "listen_only": True,
        "sw_loopback": True,
        "error_frames": True,
        "bit_timing": False,
        "plat": "Windows",
    },
    "slcan": {
        "fd": False,
        "hw_filter": False,
        "listen_only": False,
        "sw_loopback": True,
        "error_frames": False,
        "bit_timing": False,
        "plat": "全平台（串口）",
    },
    "virtual": {
        "fd": True,
        "hw_filter": False,
        "listen_only": False,
        "sw_loopback": True,
        "error_frames": False,
        "bit_timing": False,
        "plat": "全平台（无硬件）",
    },
}

_DEFAULT_CAPS = {
    "fd": False,
    "hw_filter": False,
    "listen_only": False,
    "sw_loopback": True,
    "error_frames": False,
    "bit_timing": False,
    "plat": "unknown",
}

_FALLBACK_BACKENDS = [
    "pcan", "socketcan", "kvaser", "vector", "slcan", "usb2can",
    "ixxat", "systec", "etas", "nican", "neovi", "cantact", "robotell", "virtual",
]

_PCAN_CHANNEL_FALLBACK = ["PCAN_USBBUS1", "PCAN_USBBUS2"]


def _backend_names() -> List[str]:
    try:
        from can.interfaces import BACKENDS  # python-can >= 4.0

        names = {name for name in BACKENDS}
    except Exception:  # pragma: no cover - 老版本兜底
        names = set(_FALLBACK_BACKENDS)
    # 过滤已废弃的别名，保留稳定名字
    names.discard("socketcan_ctypes")
    names.discard("socketcan_native")
    return sorted(names)


def _dll_present(name: str) -> bool:
    try:
        ctypes.WinDLL(name)  # type: ignore[attr-defined]
        return True
    except (OSError, AttributeError):
        return False


def default_interface() -> str:
    """按平台与已装驱动推断默认后端。"""
    system = platform.system()
    if system == "Windows":
        if _dll_present("PCANBasic.dll"):
            return "pcan"
        if _dll_present("canlib32.dll"):
            return "kvaser"
        if _dll_present("vxlapi64.dll") or _dll_present("vxlapi.dll"):
            return "vector"
        if _dll_present("zlgcan.dll") or _dll_present("ControlCAN.dll"):
            return "zlgcan"
        return "virtual"
    if system == "Linux":
        return "socketcan"
    return "virtual"


def resolve_interface(requested: Optional[str]) -> str:
    name = (requested or "auto").strip().lower()
    if name in ("", "auto"):
        return default_interface()
    known = set(_backend_names())
    if name not in known and name not in BACKEND_CAPS:
        raise BadRequest(
            f"未知后端 {requested!r}",
            detail={"available": sorted(known | set(BACKEND_CAPS))},
        )
    return name


def available_backends() -> List[Dict[str, Any]]:
    """列出所有可导入的后端及其能力。"""
    out: List[Dict[str, Any]] = []
    for name in _backend_names():
        try:
            importlib.import_module(f"can.interfaces.{name}")
            importable = True
        except Exception:
            importable = False
        caps = dict(_DEFAULT_CAPS)
        caps.update(BACKEND_CAPS.get(name, {}))
        out.append(
            {
                "name": name,
                "importable": importable,
                "capabilities": caps,
                "is_default": name == default_interface(),
            }
        )
    return out


def _socketcan_channels() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    base = "/sys/class/net"
    if not os.path.isdir(base):
        return out
    for name in sorted(os.listdir(base)):
        type_path = os.path.join(base, name, "type")
        try:
            with open(type_path, "r", encoding="ascii") as fh:
                if int(fh.read().strip()) != 280:  # ARPHRD_CAN
                    continue
        except (OSError, ValueError):
            continue
        out.append({"interface": "socketcan", "channel": name, "probed": True})
    return out


def list_channels(interface: Optional[str] = None) -> List[Dict[str, Any]]:
    """枚举指定后端上的可用通道。"""
    name = resolve_interface(interface)

    if name == "virtual":
        return [
            {
                "interface": "virtual",
                "channel": "cantest0",
                "probed": False,
                "note": "虚拟总线，通道名由调用方自定",
            }
        ]

    if name == "socketcan":
        return _socketcan_channels()

    try:
        configs = can.detect_available_configs(interfaces=[name])
    except Exception:
        configs = []

    out: List[Dict[str, Any]] = []
    for cfg in configs:
        channel = cfg.get("channel")
        if channel is None:
            continue
        out.append(
            {
                "interface": name,
                "channel": str(channel),
                "probed": True,
                "note": cfg.get("serial") or cfg.get("device") or None,
            }
        )

    # PEAK 驱动偶尔在设备刚插入时枚举为空，给出标准通道名作为兜底
    if not out and name == "pcan":
        out = [
            {"interface": "pcan", "channel": ch, "probed": False, "note": "未探测到，按标准通道名兜底"}
            for ch in _PCAN_CHANNEL_FALLBACK
        ]
    return out


def _pcan_channel(channel: Any) -> str:
    if channel is None:
        return _PCAN_CHANNEL_FALLBACK[0]
    if isinstance(channel, int):
        # PCAN_USBBUS1 = 0x51，依次递增
        return f"PCAN_USBBUS{channel}"
    return str(channel)


def build_kwargs(cfg: BusConfig, interface: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """组装 ``can.Bus()`` 的关键字参数，同时返回位定时元信息。"""
    caps = BACKEND_CAPS.get(interface, _DEFAULT_CAPS)
    meta: Dict[str, Any] = {"interface": interface, "timing": None, "data_timing": None}

    if interface == "pcan":
        channel = _pcan_channel(cfg.channel)
        kwargs: Dict[str, Any] = {
            "interface": "pcan",
            "channel": channel,
            "bitrate": cfg.bitrate,
        }
        nom: BitTiming = solve(cfg.bitrate, cfg.f_clock_mhz, cfg.sample_point)
        meta["timing"] = nom.to_dict()

        if cfg.fd:
            data_br = cfg.data_bitrate or cfg.bitrate
            data_timing = solve(
                data_br, cfg.f_clock_mhz, cfg.data_sample_point, data_phase=True
            )
            meta["data_timing"] = data_timing.to_dict()
            kwargs.update(
                {
                    "fd": True,
                    "f_clock_mhz": cfg.f_clock_mhz,
                    "data_bitrate": data_br,
                }
            )
            kwargs.update(nom.pcan_kwargs("nom"))
            kwargs.update(data_timing.pcan_kwargs("data"))
        else:
            # 经典模式交给 PCAN 驱动按标称波特率查表初始化（80 MHz 下 1 Mbps
            # 对应驱动的标准 BTR），比手工传 BTR 更稳。
            meta["timing"]["source"] = "driver_table"

        if cfg.listen_only:
            kwargs["listen_only"] = True
        if cfg.receive_own_messages or cfg.loopback:
            kwargs["receive_own_messages"] = True
        return kwargs, meta

    if interface == "socketcan":
        channel = str(cfg.channel or "can0")
        kwargs = {"interface": "socketcan", "channel": channel, "bitrate": cfg.bitrate}
        nom = solve(cfg.bitrate, cfg.f_clock_mhz, cfg.sample_point)
        meta["timing"] = nom.to_dict()
        if cfg.fd:
            data_br = cfg.data_bitrate or cfg.bitrate
            data_timing = solve(
                data_br, cfg.f_clock_mhz, cfg.data_sample_point, data_phase=True
            )
            meta["data_timing"] = data_timing.to_dict()
            kwargs["fd"] = True
            kwargs["data_bitrate"] = data_br
        if cfg.receive_own_messages or cfg.loopback:
            kwargs["receive_own_messages"] = True
        return kwargs, meta

    if interface == "virtual":
        channel = str(cfg.channel or "cantest0")
        kwargs = {
            "interface": "virtual",
            "channel": channel,
            # 虚拟总线必须开启自收，否则单会话看不到任何帧
            "receive_own_messages": True,
        }
        meta["timing"] = {"bitrate": cfg.bitrate, "source": "virtual"}
        if cfg.fd:
            meta["data_timing"] = {"bitrate": cfg.data_bitrate or cfg.bitrate, "source": "virtual"}
        return kwargs, meta

    # 其它后端：只传最小公共集，避免不支持的参数报错
    channel = cfg.channel
    if channel is None:
        raise BadRequest(f"后端 {interface} 需要显式指定 channel")
    kwargs = {"interface": interface, "channel": channel, "bitrate": cfg.bitrate}
    if cfg.fd and caps.get("fd"):
        kwargs["fd"] = True
        if cfg.data_bitrate:
            kwargs["data_bitrate"] = cfg.data_bitrate
    if cfg.receive_own_messages or cfg.loopback:
        kwargs["receive_own_messages"] = True
    if cfg.listen_only and caps.get("listen_only"):
        kwargs["listen_only"] = True
    meta["timing"] = {"bitrate": cfg.bitrate, "source": "driver"}
    return kwargs, meta


def open_bus(cfg: BusConfig) -> Tuple[can.BusABC, Dict[str, Any]]:
    """打开总线，返回 ``(bus, meta)``。"""
    interface = resolve_interface(cfg.interface)
    if interface not in _backend_names():  # pragma: no cover - 已被 resolve 拦住
        raise BadRequest(f"后端 {interface} 不可用")

    kwargs, meta = build_kwargs(cfg, interface)
    meta["channel"] = str(kwargs.get("channel"))

    try:
        bus = can.Bus(**kwargs)
    except can.CanInitializationError as exc:
        raise HardwareUnavailable(
            f"无法初始化 {interface} 通道 {kwargs.get('channel')}: {exc}",
            detail={"kwargs": {k: v for k, v in kwargs.items() if k != "interface"}},
        ) from exc
    except (OSError, ValueError, TypeError) as exc:
        raise HardwareUnavailable(
            f"打开 {interface} 通道失败: {exc}",
            detail={"kwargs": {k: v for k, v in kwargs.items() if k != "interface"}},
        ) from exc

    meta["hw_filters"] = bool(BACKEND_CAPS.get(interface, _DEFAULT_CAPS).get("hw_filter"))
    return bus, meta


def probe() -> Dict[str, Any]:
    """启动前自检：平台、可用后端、默认后端、各后端通道。"""
    result: Dict[str, Any] = {
        "platform": platform.system(),
        "python": platform.python_version(),
        "python_can": can.__version__,
        "default_interface": default_interface(),
        "backends": available_backends(),
        "channels": {},
        "dlls": {},
    }
    if platform.system() == "Windows":
        for dll in ("PCANBasic.dll", "canlib32.dll", "vxlapi64.dll", "zlgcan.dll", "ControlCAN.dll"):
            result["dlls"][dll] = _dll_present(dll)

    for backend in ("pcan", "socketcan", "kvaser", "vector", "slcan"):
        try:
            result["channels"][backend] = list_channels(backend)
        except Exception as exc:  # pragma: no cover
            result["channels"][backend] = [{"error": str(exc)}]
    return result
