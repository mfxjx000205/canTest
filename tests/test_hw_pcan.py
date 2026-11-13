r"""真实硬件连通性验证（PEAK PCAN-USB Pro FD，1 Mbps / 80 MHz）。

默认不跑，必须显式开启：

    # 只监听（安全，推荐先跑这个）
    set CANTEST_HW=1 && .venv\Scripts\python -m pytest tests/test_hw_pcan.py -v

    # 允许向总线发帧（会真的往线上打帧，在役总线上慎用）
    set CANTEST_HW=1 && set CANTEST_HW_TX=1 && .venv\Scripts\python -m pytest tests/test_hw_pcan.py -v

可覆盖通道：``CANTEST_HW_CHANNEL=PCAN_USBBUS2``。

为什么默认只监听：现场这条线是活的 J1939 总线（0x18xx0Axx 一族报文，约 500 fps），
往上发帧可能触发执行器动作。发送类用例单独用 ``CANTEST_HW_TX`` 双保险。
"""

from __future__ import annotations

import os
import time

import pytest

from cantest.bus import BusSession
from cantest.manager import BusManager
from cantest.models import BusConfig, FilterSpec, FrameSpec

pytestmark = [
    pytest.mark.hardware,
    pytest.mark.skipif(
        os.environ.get("CANTEST_HW") != "1",
        reason="真实硬件用例，设置 CANTEST_HW=1 才执行",
    ),
]

CHANNEL = os.environ.get("CANTEST_HW_CHANNEL", "PCAN_USBBUS1")
F_CLOCK_MHZ = 80
BITRATE = 1_000_000
ALLOW_TX = os.environ.get("CANTEST_HW_TX") == "1"

needs_tx = pytest.mark.skipif(
    not ALLOW_TX, reason="会向真实总线发帧，需 CANTEST_HW_TX=1"
)


@pytest.fixture(scope="module")
def manager():
    mgr = BusManager()
    yield mgr
    mgr.close_all()


@pytest.fixture(scope="module")
def session(manager) -> BusSession:
    cfg = BusConfig(
        interface="pcan",
        channel=CHANNEL,
        bitrate=BITRATE,
        f_clock_mhz=F_CLOCK_MHZ,
        sample_point=0.8,
        listen_only=True,
        buffer_size=20000,
    )
    return manager.open(cfg)


@pytest.fixture(scope="module")
def observed(session) -> BusSession:
    """先攒 3 秒真实报文，后续用例基于这段流量做断言。"""
    deadline = time.time() + 3.0
    while time.time() < deadline:
        time.sleep(0.1)
    return session


class TestBitTiming:
    def test_channel_opened(self, session) -> None:
        assert session.interface == "pcan"
        assert session.channel == CHANNEL
        assert session.state() in ("active", "error-passive")

    def test_80mhz_1mbps_solution(self, session) -> None:
        timing = session.meta["timing"]
        # 80 MHz / 1 Mbps = 80 TQ：BRP=1, TSEG1=63, TSEG2=16，采样点 80%
        assert timing["brp"] == 1
        assert timing["tseg1"] == 63
        assert timing["tseg2"] == 16
        assert timing["tq_total"] == 80
        assert timing["tq_ns"] == pytest.approx(12.5)
        assert timing["bit_time_ns"] == pytest.approx(1000.0)
        assert timing["sample_point"] == pytest.approx(0.8)
        # 经典帧由 PCAN 驱动按标称波特率查表初始化，这里如实标注来源
        assert timing["source"] == "driver_table"


class TestReceiving:
    def test_receives_traffic(self, observed) -> None:
        stats = observed.snapshot_stats()
        assert stats.rx_frames > 0, "3 秒内没有收到任何帧，检查接线 / 波特率 / 终端电阻"

    def test_frames_are_well_formed(self, observed) -> None:
        frames = observed.receive(50, 2.0)
        assert frames, "history 里应当有刚收到的帧"
        for msg in frames:
            assert 0 <= msg.arbitration_id <= 0x1FFFFFFF
            assert len(msg.data) <= 64
            # 位定时配错时典型症状是帧长异常的垃圾帧
            assert msg.is_error_frame is False

    def test_no_bus_errors(self, observed) -> None:
        stats = observed.snapshot_stats()
        # 位定时错了这里就会涨：错误帧、bus-off、收发错误
        assert stats.error_frames == 0
        assert stats.bus_off_count == 0
        assert stats.last_error is None

    def test_history_records_frames(self, observed) -> None:
        history = observed.history(limit=20)
        assert len(history) == 20
        # 按时间升序返回，时间戳不应倒退
        stamps = [m.timestamp for m in history]
        assert stamps == sorted(stamps)

    def test_timestamps_are_wall_clock(self, observed) -> None:
        # PCAN 驱动给的是**开机相对时间**（实测 7667 s，不是 epoch），
        # 必须归一到墙钟，否则 GET /frames?since=<epoch> 会把接收帧全过滤掉，
        # 表现为「总线上明明有帧，接口说没收到」。
        now = time.time()
        for msg in observed.history(limit=10):
            assert abs(msg.timestamp - now) < 60, (
                f"时间戳 {msg.timestamp} 不在墙钟附近（now={now}），时间基线没归一"
            )

    def test_bus_load_is_plausible(self, observed) -> None:
        stats = observed.snapshot_stats()
        # bus_load 是 0~1 的**比例**（0.076 表示 7.6%），不是百分数
        assert 0.0 < stats.bus_load <= 1.0
        # 与帧率交叉验算：8 字节扩展帧连插位约 150~160 bit。
        # 区间放宽到 120~200，只抓「帧位数估算离谱一个数量级」，不做精确断言。
        if stats.rx_rate_fps:
            bits_per_frame = stats.bus_load * BITRATE / stats.rx_rate_fps
            assert 120 <= bits_per_frame <= 200, (
                f"由 bus_load={stats.bus_load} 与 {stats.rx_rate_fps} fps 反推 "
                f"{bits_per_frame:.0f} bit/帧，位数估算有问题"
            )


class TestFiltering:
    def test_hardware_filter_selects_one_id(self, observed) -> None:
        seen = observed.history(limit=50)
        assert seen
        target = seen[0].arbitration_id

        observed.set_filters([FilterSpec(can_id=target, id_mask=0x1FFFFFFF)])
        try:
            # 过滤只对新到达的帧生效，不追溯缓冲区里已有的帧。
            # 不清缓冲就收，会把「设过滤之前」的帧一起读出来，看着像过滤失效。
            observed.flush()
            for _ in range(3):
                frames = observed.receive(20, 1.0)
                assert frames
                assert {m.arbitration_id for m in frames} == {target}
        finally:
            observed.set_filters([])

    def test_filter_cleared(self, observed) -> None:
        assert observed.get_filters() == []


class TestControl:
    def test_reset_supported(self, session) -> None:
        # PCAN 驱动有 CAN_Reset，删掉后应重新 ACTIVE
        assert session.reset() is True
        assert session.state() == "active"

    def test_capabilities(self, manager, session) -> None:
        caps = manager.capabilities(session.id)
        assert caps["fd"] is True
        assert caps["hw_filter"] is True
        assert caps["listen_only"] is True


@needs_tx
class TestTransmit:
    """只有显式允许时才发帧——这条线上跑的是真实 J1939 报文。"""

    def test_send_one_frame(self, session) -> None:
        before = session.snapshot_stats()
        # 0x1DEFFFFF 是扩展帧里优先级最低的一档，尽量不影响在役报文
        msg = session.send(FrameSpec(id=0x1DEFFFFF, data="00", is_extended_id=True))
        after = session.snapshot_stats()

        assert msg.arbitration_id == 0x1DEFFFFF
        assert after.tx_frames == before.tx_frames + 1
        assert after.tx_errors == before.tx_errors
        assert session.state() in ("active", "error-passive")

    def test_loopback_sees_own_frame(self, manager) -> None:
        # 自收需要驱动回显；PCAN 上对应 receive_own_messages
        cfg = BusConfig(
            interface="pcan",
            channel=CHANNEL,
            bitrate=BITRATE,
            f_clock_mhz=F_CLOCK_MHZ,
            receive_own_messages=True,
            buffer_size=5000,
        )
        probe = manager.open(cfg)
        try:
            from cantest.frame import FrameMatcher

            probe.send(FrameSpec(id=0x1DEFFFFE, data="AA55", is_extended_id=True))
            got = probe.expect(FrameMatcher(can_id=0x1DEFFFFE), timeout_s=1.0, count=1)
            assert got, "开了自收却看不到自己的帧，说明驱动没回显"
        finally:
            manager.close(probe.id)
