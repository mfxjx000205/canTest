"""高精度墙钟：精度、单调性、与系统时间的对齐。

这些用例的价值在于防退化——如果哪天有人把 ``clock.now()`` 改回 ``time.time()``，
在 Windows + Python 3.10 上会立刻红，而不会等到现场测帧间隔时才发现。
"""

from __future__ import annotations

import time

import pytest

from cantest import clock


class TestPrecision:
    def test_resolution_beats_coarse_wall_clock(self) -> None:
        """``clock.now()`` 的分辨率必须优于 ``time.time()``。

        在 3.11+ 两者本来都细，这条断言等价于"没退化"；在 3.10/Windows 上
        ``time.time()`` 是 15.6 ms，只有走 perf_counter 才能到微秒级。
        """
        described = clock.describe()
        raw_ns = described["wall_clock_resolution_ns"]
        eff_ns = described["effective_resolution_ns"]
        if raw_ns > 1_000_000:  # 系统墙钟偏粗，本模块必须更细
            assert eff_ns < raw_ns / 10, (
                f"系统墙钟 {raw_ns} ns，本模块只有 {eff_ns} ns，没有绕开粗时钟"
            )

    def test_consecutive_calls_are_not_all_identical(self) -> None:
        """连续取 200 次不能只有一个值。``time.time()`` 在 3.10 上会失败。"""
        values = {clock.now() for _ in range(200)}
        assert len(values) > 1, "clock.now() 的粒度和 time.time() 一样粗"

    def test_values_are_epoch_like(self) -> None:
        """返回值必须是 epoch 秒，能直接和 ``time.time()`` 比较。"""
        assert abs(clock.now() - time.time()) < 1.0


class TestMonotonicity:
    def test_strictly_non_decreasing(self) -> None:
        values = [clock.now() for _ in range(500)]
        assert all(b >= a for a, b in zip(values, values[1:]))

    def test_monotonic_across_recalibration(self, monkeypatch) -> None:
        """重校锚点不能造成时间倒退。

        校准是"perf + (wall - perf)"的差值重建。如果实现里先记墙钟后记
        perf，两次读之间的刻度跳变会让重建值跳回一格的起点——顺序敏感的
        排序正好会踩到这个坑。
        """
        monkeypatch.setattr(clock, "_RESYNC_INTERVAL_S", 0.001)
        values = []
        for _ in range(30):
            values.append(clock.now())
            time.sleep(0.001)
        assert all(b >= a for a, b in zip(values, values[1:])), "重校造成时间倒退"

    def test_progress_matches_real_elapsed(self) -> None:
        """时长测量必须贴近真实经过时间，不能因为重校而漂。"""
        start = clock.now()
        time.sleep(0.15)
        elapsed = clock.now() - start
        assert 0.13 <= elapsed <= 0.20, f"测得 {elapsed:.3f}s，与 0.15s 不符"


class TestReset:
    def test_reset_forces_recalibration(self, monkeypatch) -> None:
        clock.now()
        assert clock._ready is True
        before = clock.now()
        clock.reset()
        assert clock._ready is False
        after = clock.now()
        assert clock._ready is True
        assert abs(after - before) < 1.0, "重校后与墙钟偏离过大"


class TestDescribe:
    def test_describe_shape(self) -> None:
        d = clock.describe()
        for key in (
            "wall_clock_impl",
            "wall_clock_resolution_ns",
            "effective_resolution_ns",
            "coarse",
            "source",
        ):
            assert key in d
        assert d["source"] == "perf_counter"
        assert isinstance(d["coarse"], bool)
        assert d["effective_resolution_ns"] > 0

    def test_is_coarse_threshold(self) -> None:
        """阈值判定要能区分"粗"和"细"，且不受平台影响。"""
        # 给一个极小的阈值：任何时钟都会被判为粗
        assert clock.is_coarse(threshold_ns=0) is True
        # 给一个极大的阈值：任何时钟都会被判为细
        assert clock.is_coarse(threshold_ns=10**12) is False
