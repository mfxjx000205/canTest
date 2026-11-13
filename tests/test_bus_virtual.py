"""虚拟总线端到端：收发、断言、过滤、统计、周期发送、录制回放。

这些用例不碰硬件，是回归测试的主力。真实硬件的用例见 ``test_hardware.py``。
"""

from __future__ import annotations

import time

import pytest

from cantest.errors import BusNotFound, OperationTimeout, UnsupportedFeature
from cantest.frame import FrameMatcher
from cantest.models import (
    BusConfig,
    CyclicRequest,
    FilterSpec,
    FrameSpec,
    RecordRequest,
    ReplayRequest,
)
from cantest.recorder import iter_frames


class TestSendReceive:
    def test_send_returns_message(self, bus) -> None:
        msg = bus.send(FrameSpec(id=0x181, data="1122334455667788"))

        assert msg.arbitration_id == 0x181
        assert bytes(msg.data) == bytes([0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88])
        assert msg.is_rx is False, "发送的帧不该被标成接收"
        assert msg.timestamp > 0

    def test_send_rejects_unsupported_by_frame_spec(self) -> None:
        # 9 字节经典帧在模型层就被拦住，不会走到总线上
        with pytest.raises(Exception):
            FrameSpec(id=0x181, data="00" * 9)

    def test_virtual_bus_echoes_own_frame(self, bus) -> None:
        bus.send(FrameSpec(id=0x181, data="0102"))
        frames = bus.receive(count=1, timeout_s=1.0)

        assert len(frames) == 1
        assert frames[0].arbitration_id == 0x181
        assert bytes(frames[0].data) == b"\x01\x02"

    def test_receive_is_consuming(self, bus) -> None:
        bus.send(FrameSpec(id=0x181, data="01"))
        assert len(bus.receive(count=1, timeout_s=1.0)) == 1
        # 同一帧不会被第二次读到
        assert bus.receive(count=1, timeout_s=0.2) == []

    def test_receive_respects_matcher(self, bus) -> None:
        bus.send(FrameSpec(id=0x181, data="01"))
        bus.send(FrameSpec(id=0x281, data="02"))

        frames = bus.receive(count=1, timeout_s=1.0, matcher=FrameMatcher(can_id=0x281))
        assert [f.arbitration_id for f in frames] == [0x281]

    def test_receive_timeout_returns_empty(self, bus) -> None:
        t0 = time.perf_counter()
        assert bus.receive(count=1, timeout_s=0.2) == []
        assert time.perf_counter() - t0 >= 0.15

    def test_batch_send(self, bus) -> None:
        frames = [FrameSpec(id=0x100 + i, data=f"0{i}") for i in range(5)]
        assert bus.send_many(frames, interval_ms=1) == 5
        assert bus.snapshot_stats().tx_frames == 5

    def test_extended_id_roundtrip(self, bus) -> None:
        bus.send(FrameSpec(id=0x18FF50E5, data="AABB", is_extended_id=True))
        frames = bus.receive(count=1, timeout_s=1.0)

        assert frames[0].arbitration_id == 0x18FF50E5
        assert frames[0].is_extended_id is True


class TestExpect:
    def test_expect_sees_new_frame(self, bus) -> None:
        bus.send(FrameSpec(id=0x181, data="1122"))
        frames = bus.expect(FrameMatcher(can_id=0x181), timeout_s=1.0)

        assert len(frames) == 1
        assert bytes(frames[0].data) == b"\x11\x22"

    def test_expect_does_not_consume_receive_cursor(self, bus) -> None:
        bus.send(FrameSpec(id=0x181, data="01"))
        assert len(bus.expect(FrameMatcher(can_id=0x181), timeout_s=1.0)) == 1
        # 游标没被推进，receive 还能读到同一帧
        assert len(bus.receive(count=1, timeout_s=0.5)) == 1

    def test_expect_times_out(self, bus) -> None:
        t0 = time.perf_counter()
        assert bus.expect(FrameMatcher(can_id=0x7FF), timeout_s=0.2) == []
        assert time.perf_counter() - t0 >= 0.15

    def test_expect_count_collects_multiple(self, bus) -> None:
        for _ in range(3):
            bus.send(FrameSpec(id=0x181, data="01"))

        frames = bus.expect(FrameMatcher(can_id=0x181), timeout_s=1.0, count=3)
        assert len(frames) == 3

    def test_expect_include_buffer_reads_history(self, bus) -> None:
        bus.send(FrameSpec(id=0x181, data="01"))
        # 先等它落进缓冲
        assert bus.expect(FrameMatcher(can_id=0x181), timeout_s=1.0)

        # 不消费游标，直接从历史里翻出来
        frames = bus.expect(
            FrameMatcher(can_id=0x181), timeout_s=0.3, include_buffer=True
        )
        assert len(frames) == 1

    def test_expect_without_buffer_ignores_history(self, bus) -> None:
        bus.send(FrameSpec(id=0x181, data="01"))
        assert bus.expect(FrameMatcher(can_id=0x181), timeout_s=1.0)

        # include_buffer=False 只认调用之后到达的帧
        assert bus.expect(FrameMatcher(can_id=0x181), timeout_s=0.2) == []


class TestTransact:
    def test_transact_returns_latency(self, bus) -> None:
        responses, latencies = bus.transact(
            FrameSpec(id=0x100, data="0102"),
            FrameMatcher(can_id=0x100),
            timeout_s=1.0,
        )

        assert len(responses) == 1
        assert len(latencies) == 1
        assert 0 < latencies[0] < 1000

    def test_transact_repeat(self, bus) -> None:
        responses, latencies = bus.transact(
            FrameSpec(id=0x100, data="01"),
            FrameMatcher(can_id=0x100),
            timeout_s=1.0,
            repeat=3,
        )
        assert len(responses) == 3
        assert len(latencies) == 3

    def test_transact_timeout_raises(self, bus) -> None:
        with pytest.raises(OperationTimeout) as info:
            bus.transact(
                FrameSpec(id=0x100, data="01"),
                FrameMatcher(can_id=0x7FF),
                timeout_s=0.2,
            )
        assert info.value.status == 408
        assert info.value.detail["expected"] == 1

    def test_transact_ignores_stale_frames(self, bus) -> None:
        """缓冲里已有的同 ID 帧不能被当成这次请求的响应。"""
        bus.send(FrameSpec(id=0x100, data="01"))
        assert bus.expect(FrameMatcher(can_id=0x100), timeout_s=1.0)

        # 之后不再有响应，expect 必须以序号为界判定超时
        with pytest.raises(OperationTimeout):
            bus.transact(
                FrameSpec(id=0x100, data="02"),
                FrameMatcher(can_id=0x7FF),
                timeout_s=0.2,
            )


class TestFilters:
    def test_id_mask_filter_drops_others(self, bus) -> None:
        hw_ok = bus.set_filters([FilterSpec(can_id=0x100, id_mask=0x700)])
        assert isinstance(hw_ok, bool)
        assert len(bus.get_filters()) == 1

        bus.send(FrameSpec(id=0x123, data="01"))
        bus.send(FrameSpec(id=0x281, data="02"))

        frames = bus.receive(count=5, timeout_s=0.5)
        assert [f.arbitration_id for f in frames] == [0x123]

    def test_clear_filters(self, bus) -> None:
        bus.set_filters([FilterSpec(can_id=0x100, id_mask=0x700)])
        bus.set_filters([])

        bus.send(FrameSpec(id=0x281, data="02"))
        assert len(bus.receive(count=1, timeout_s=1.0)) == 1

    def test_extended_filter_does_not_match_standard(self, bus) -> None:
        bus.set_filters([FilterSpec(can_id=0x100, id_mask=0x700, is_extended_id=True)])

        bus.send(FrameSpec(id=0x123, data="01", is_extended_id=False))
        assert bus.receive(count=1, timeout_s=0.3) == []


class TestStatsAndInfo:
    def test_stats_after_one_frame(self, bus) -> None:
        bus.send(FrameSpec(id=0x181, data="11223344"))
        bus.receive(count=1, timeout_s=1.0)

        stats = bus.snapshot_stats()
        assert stats.tx_frames == 1
        assert stats.tx_bytes == 4
        assert stats.rx_frames >= 1
        assert stats.bus_load > 0
        assert stats.tx_rate_fps >= 1

    def test_info_structure(self, bus) -> None:
        info = bus.info()

        assert info.id == bus.id
        assert info.interface == "virtual"
        assert info.channel
        assert info.state in ("active", "error-passive", "bus-off", "unknown")
        assert info.timing["source"] == "virtual"
        assert info.uptime_s >= 0

    def test_history_merges_tx_and_rx(self, bus) -> None:
        bus.send(FrameSpec(id=0x181, data="01"))
        bus.receive(count=1, timeout_s=1.0)

        history = bus.history(limit=10)
        # 一帧 TX（发送记录）+ 一帧 RX（虚拟总线回环）
        assert len(history) == 2
        assert all(m.arbitration_id == 0x181 for m in history)

    def test_history_matcher(self, bus) -> None:
        bus.send(FrameSpec(id=0x181, data="01"))
        bus.send(FrameSpec(id=0x281, data="02"))
        bus.receive(count=5, timeout_s=0.5)

        history = bus.history(limit=10, matcher=FrameMatcher(can_id=0x281))
        assert history and all(m.arbitration_id == 0x281 for m in history)

    def test_flush_clears_buffers(self, bus) -> None:
        bus.send(FrameSpec(id=0x181, data="01"))
        bus.receive(count=1, timeout_s=1.0)

        assert bus.flush() >= 1
        assert bus.history(limit=10) == []

    def test_reset_unsupported_raises(self, bus) -> None:
        # VirtualBus 连 reset 方法都没有，必须明确报「不支持」，而不是伪造成
        # 复位成功——测试平台会误以为 bus-off 已恢复。
        with pytest.raises(UnsupportedFeature):
            bus.reset()
        assert bus.reset(silent=True) is False

    def test_capabilities(self, manager, bus) -> None:
        caps = manager.capabilities(bus.id)
        assert caps["fd"] is True
        assert caps["hw_filter"] is False


class TestCyclic:
    def test_finite_cyclic_sends_all_cycles(self, bus, wait_until) -> None:
        task = bus.start_cyclic(
            CyclicRequest(
                frames=[FrameSpec(id=0x181, data="01")],
                period_ms=20,
                count=3,
                name="smoke",
            )
        )
        assert wait_until(lambda: not task.running, timeout=3.0)

        info = task.info()
        assert info.sent_cycles == 3
        assert info.sent_frames == 3
        assert info.running is False
        assert info.last_error is None

    def test_cyclic_multi_frame_sequence(self, bus, wait_until) -> None:
        task = bus.start_cyclic(
            CyclicRequest(
                frames=[FrameSpec(id=0x181, data="01"), FrameSpec(id=0x182, data="02")],
                period_ms=20,
                count=2,
            )
        )
        assert wait_until(lambda: not task.running, timeout=3.0)
        assert task.info().sent_frames == 4

    def test_stop_cyclic_manually(self, bus) -> None:
        task = bus.start_cyclic(
            CyclicRequest(frames=[FrameSpec(id=0x181, data="01")], period_ms=10)
        )
        assert len(bus.cyclic_tasks()) == 1
        assert bus.stop_cyclic(task.id) == 1
        assert task.running is False

    def test_stop_all_cyclic(self, bus) -> None:
        bus.start_cyclic(CyclicRequest(frames=[FrameSpec(id=0x181)], period_ms=10))
        bus.start_cyclic(CyclicRequest(frames=[FrameSpec(id=0x182)], period_ms=10))
        assert bus.stop_cyclic(None) == 2

    def test_stop_unknown_task(self, bus) -> None:
        assert bus.stop_cyclic("nope") == 0


class TestRecordReplay:
    def test_record_jsonl(self, bus, tmp_path) -> None:
        target = tmp_path / "rec.jsonl"
        info = bus.start_record(RecordRequest(format="jsonl", path=str(target)))
        assert info.active is True

        for i in range(3):
            bus.send(FrameSpec(id=0x180 + i, data="0102"))
        bus.receive(count=5, timeout_s=0.5)
        stopped = bus.stop_record()

        assert stopped.active is False
        frames = list(iter_frames(str(target)))
        assert len(frames) >= 3
        assert frames[0].arbitration_id == 0x180

    def test_record_csv_has_header(self, bus, tmp_path) -> None:
        target = tmp_path / "rec.csv"
        bus.start_record(RecordRequest(format="csv", path=str(target)))
        bus.send(FrameSpec(id=0x181, data="0102"))
        bus.stop_record()

        text = target.read_text(encoding="utf-8")
        assert "timestamp" in text.splitlines()[0]
        assert len(text.splitlines()) >= 2

    def test_double_record_rejected(self, bus, tmp_path) -> None:
        bus.start_record(RecordRequest(path=str(tmp_path / "a.jsonl")))
        with pytest.raises(Exception):
            bus.start_record(RecordRequest(path=str(tmp_path / "b.jsonl")))
        bus.stop_record()

    def test_replay_sends_frames(self, bus, tmp_path, wait_until) -> None:
        source = tmp_path / "source.jsonl"
        bus.start_record(RecordRequest(path=str(source)))
        for i in range(2):
            bus.send(FrameSpec(id=0x190 + i, data="01"))
        bus.stop_record()

        before = bus.snapshot_stats().tx_frames
        job = bus.start_replay(
            ReplayRequest(path=str(source), gap_ms=5, count=1)
        )
        assert wait_until(lambda: not job.running, timeout=3.0)
        assert job.sent >= 2
        assert bus.snapshot_stats().tx_frames >= before + job.sent

    def test_replay_missing_file_records_error(self, bus, tmp_path, wait_until) -> None:
        job = bus.start_replay(ReplayRequest(path=str(tmp_path / "nope.jsonl")))
        assert wait_until(lambda: job.error is not None, timeout=2.0)
        assert "不存在" in job.error


class TestLifecycle:
    def test_close_removes_session(self, manager, bus) -> None:
        bus_id = bus.id
        manager.close(bus_id)
        with pytest.raises(BusNotFound):
            manager.get(bus_id)

    def test_virtual_allows_same_channel_twice(self, manager, channel) -> None:
        first = manager.open(BusConfig(interface="virtual", channel=channel))
        second = manager.open(BusConfig(interface="virtual", channel=channel))
        assert first.id != second.id
        # 两条虚拟总线共享通道，能互相收到对方发的帧
        first.send(FrameSpec(id=0x181, data="01"))
        frames = second.receive(count=1, timeout_s=1.0)
        assert frames and frames[0].arbitration_id == 0x181

    def test_close_all(self, manager) -> None:
        manager.open(BusConfig(interface="virtual", channel="a"))
        manager.open(BusConfig(interface="virtual", channel="b"))
        assert manager.close_all() == 2
        assert manager.list() == []
