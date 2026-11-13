"""测试公共夹具。

所有不依赖真实硬件的用例都跑 ``virtual`` 后端：python-can 的虚拟总线在同一
channel 上做进程内广播，足以验证收发、断言、统计、周期发送、录制回放这些逻辑。
真实硬件的用例统一带 ``hardware`` 标记，默认跳过。
"""

from __future__ import annotations

import time
import uuid
from typing import Callable, Iterator

import pytest

from cantest.manager import BusManager
from cantest.models import BusConfig


@pytest.fixture
def manager() -> Iterator[BusManager]:
    mgr = BusManager()
    try:
        yield mgr
    finally:
        mgr.close_all()


@pytest.fixture
def channel() -> str:
    """每个用例独立的虚拟通道名，避免用例之间互相收到对方的帧。"""
    return f"vt-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def open_bus(manager: BusManager, channel: str) -> Callable[..., object]:
    """打开虚拟总线；返回可调用的工厂，便于用例覆盖配置项。"""

    def _open(**overrides):
        cfg = BusConfig(interface="virtual", channel=channel, buffer_size=2000)
        for key, value in overrides.items():
            setattr(cfg, key, value)
        return manager.open(cfg)

    return _open


@pytest.fixture
def bus(open_bus: Callable[..., object]):
    return open_bus()


@pytest.fixture
def wait_until() -> Callable[[Callable[[], bool], float], bool]:
    """轮询等待条件成立，避免在测试里写死 sleep 时长。"""

    def _wait(predicate: Callable[[], bool], timeout: float = 2.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return predicate()

    return _wait
