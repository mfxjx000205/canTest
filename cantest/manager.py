"""会话管理：多条总线的登记、通道独占检查、优雅关闭。"""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from .backends import BACKEND_CAPS, resolve_interface
from .bus import BusSession
from .errors import BusNotFound, ChannelInUse
from .models import BusConfig

__all__ = ["BusManager"]

_SLUG = re.compile(r"[^a-zA-Z0-9]+")


def _slug(text: Any) -> str:
    cleaned = _SLUG.sub("-", str(text or "default")).strip("-").lower()
    return cleaned or "default"


class BusManager:
    """进程内所有总线会话的容器。

    一条物理通道同一时刻只允许一个会话占用（CAN 控制器不支持重复打开），
    虚拟总线例外——它本来就用来模拟多节点。
    """

    def __init__(self, on_frame: Optional[Callable[[str, Any], None]] = None) -> None:
        self._sessions: Dict[str, BusSession] = {}
        self._lock = threading.RLock()
        self._on_frame = on_frame
        self._counter = 0

    # ---- 打开 / 关闭 --------------------------------------------------
    def open(self, config: BusConfig) -> BusSession:
        interface = resolve_interface(config.interface)

        with self._lock:
            if interface != "virtual":
                conflict = self._find_conflict(interface, config.channel)
                if conflict is not None:
                    raise ChannelInUse(
                        f"通道 {interface}:{config.channel or 'default'} 已被会话 {conflict.id} 占用",
                        detail={"owner": conflict.id},
                    )

            self._counter += 1
            # ``PCAN_USBBUS1`` 归一成 ``pcan-usbbus1``，已经是「后端-通道」形式的
            # 就不再重复加前缀，避免出现 pcan-pcan-usbbus1 这种怪 id。
            slug = _slug(config.channel)
            prefix = "" if slug == interface or slug.startswith(f"{interface}-") else f"{interface}-"
            session_id = f"{prefix}{slug}-{self._counter}"

        session = BusSession(
            session_id,
            config,
            on_frame=(lambda msg: self._on_frame(session_id, msg)) if self._on_frame else None,
        )

        with self._lock:
            self._sessions[session_id] = session
        return session

    def _find_conflict(self, interface: str, channel: Any) -> Optional[BusSession]:
        target = str(channel) if channel is not None else None
        for session in self._sessions.values():
            if session.interface != interface:
                continue
            if target is None or session.channel == target:
                return session
        return None

    def get(self, session_id: str) -> BusSession:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise BusNotFound(f"总线会话 {session_id!r} 不存在")
        if session.closed:
            raise BusNotFound(f"总线会话 {session_id!r} 已关闭")
        return session

    def list(self) -> List[BusSession]:
        with self._lock:
            return list(self._sessions.values())

    def close(self, session_id: str) -> None:
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is None:
            raise BusNotFound(f"总线会话 {session_id!r} 不存在")
        session.close()

    def close_all(self) -> int:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            try:
                session.close()
            except Exception:  # noqa: BLE001 - 关闭阶段尽力而为
                pass
        return len(sessions)

    # ---- 汇总 ---------------------------------------------------------
    def summary(self) -> Dict[str, Any]:
        sessions = self.list()
        return {
            "count": len(sessions),
            "sessions": [s.info().model_dump() for s in sessions],
            "server_time": time.time(),
        }

    def capabilities(self, session_id: str) -> Dict[str, Any]:
        session = self.get(session_id)
        return dict(BACKEND_CAPS.get(session.interface, {}))
