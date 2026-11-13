"""位定时（bit timing）计算。

CAN 控制器的位时间由若干时间份额（TQ, time quantum）组成：

    一个位 = SYNC_SEG(1 TQ) + TSEG1 + TSEG2

    TQ       = BRP / f_clock
    bitrate  = f_clock / (BRP * (1 + TSEG1 + TSEG2))
    采样点   = (1 + TSEG1) / (1 + TSEG1 + TSEG2)

本项目硬件时钟为 80 MHz（PCAN-USB Pro FD / 多数 USB-CAN 分析仪的典型配置）。
1 Mbps 时 80 MHz / 1 Mbps = 80 TQ，取 BRP=1, TSEG1=63, TSEG2=16, SJW=16，
采样点 80%，与 CiA 推荐值一致。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, Optional

__all__ = [
    "BitTiming",
    "solve",
    "preset",
    "PRESETS_80MHZ",
    "DEFAULT_CLOCK_MHZ",
]

DEFAULT_CLOCK_MHZ = 80

# CiA 301 / CiA 1301 对位时间的约束：
# 仲裁段一个位 8~385 TQ，数据段（>1 Mbps）8~49 TQ。
_ARB_MIN_TQ, _ARB_MAX_TQ = 8, 385
_DATA_MIN_TQ, _DATA_MAX_TQ = 8, 49

# TSEG1 / TSEG2 寄存器位宽上限（宽松取值，覆盖 PEAK / SJA1000 / MCP251x）
_TSEG1_MAX = 256
_TSEG2_MAX = 128
_BRP_MAX = 1024

# tie-break 时偏好的 TQ 总数：太小的总位数采样点调节粒度粗，太大的总位数易顶到
# TSEG 寄存器上限。100~200 是工程上最常用的区间，取中值 160。
_PREFERRED_TQ = 160


@dataclass(frozen=True)
class BitTiming:
    """一组完整的位定时参数。"""

    f_clock_mhz: float
    brp: int
    tseg1: int
    tseg2: int
    sjw: int

    # ---- 派生量 -------------------------------------------------------
    @property
    def tq_total(self) -> int:
        return 1 + self.tseg1 + self.tseg2

    @property
    def bitrate(self) -> float:
        return self.f_clock_mhz * 1_000_000 / (self.brp * self.tq_total)

    @property
    def sample_point(self) -> float:
        return (1 + self.tseg1) / self.tq_total

    @property
    def tq_ns(self) -> float:
        return self.brp * 1000.0 / self.f_clock_mhz

    @property
    def bit_time_ns(self) -> float:
        return self.tq_total * self.tq_ns

    # ---- 输出 ---------------------------------------------------------
    def pcan_kwargs(self, prefix: str = "nom") -> Dict[str, float]:
        """转换成 python-can ``pcan`` 后端认识的关键字参数。

        ``prefix="nom"`` 用于仲裁段（nominal），``prefix="data"`` 用于 FD 数据段。
        """
        return {
            f"{prefix}_brp": self.brp,
            f"{prefix}_tseg1": self.tseg1,
            f"{prefix}_tseg2": self.tseg2,
            f"{prefix}_sjw": self.sjw,
        }

    def to_dict(self) -> Dict[str, float]:
        return {
            "f_clock_mhz": self.f_clock_mhz,
            "brp": self.brp,
            "tseg1": self.tseg1,
            "tseg2": self.tseg2,
            "sjw": self.sjw,
            "tq_total": self.tq_total,
            "tq_ns": round(self.tq_ns, 4),
            "bit_time_ns": round(self.bit_time_ns, 4),
            "bitrate": round(self.bitrate, 3),
            "sample_point": round(self.sample_point, 4),
        }

    def __str__(self) -> str:  # pragma: no cover - 便于日志阅读
        return (
            f"{self.bitrate / 1000:g} kbit/s @ {self.f_clock_mhz:g} MHz "
            f"(brp={self.brp}, tseg1={self.tseg1}, tseg2={self.tseg2}, "
            f"sjw={self.sjw}, sp={self.sample_point * 100:.1f}%)"
        )


def _iter_candidates(
    f_clock_hz: float,
    bitrate: float,
    sp_target: float,
    data_phase: bool,
) -> Iterable[BitTiming]:
    lo, hi = (_DATA_MIN_TQ, _DATA_MAX_TQ) if data_phase else (_ARB_MIN_TQ, _ARB_MAX_TQ)
    target_sp_milli = round(sp_target * 1000)

    for brp in range(1, _BRP_MAX + 1):
        exact = f_clock_hz / (bitrate * brp)
        tq_total = round(exact)
        if tq_total < lo or tq_total > hi:
            continue
        # 必须能整除，否则实际波特率与标称值不符
        if abs(f_clock_hz - bitrate * brp * tq_total) > 1e-6 * f_clock_hz:
            continue

        tseg1 = round(tq_total * sp_target) - 1
        tseg1 = max(1, min(tseg1, tq_total - 3))
        tseg2 = tq_total - 1 - tseg1
        if tseg1 > _TSEG1_MAX or tseg2 < 2 or tseg2 > _TSEG2_MAX:
            continue

        yield BitTiming(
            f_clock_mhz=f_clock_hz / 1_000_000,
            brp=brp,
            tseg1=tseg1,
            tseg2=tseg2,
            # SJW 不允许超过 TSEG2；同时按惯例不超过 16 个 TQ
            sjw=max(1, min(tseg2, 16)),
        )


def solve(
    bitrate: int,
    f_clock_mhz: float = DEFAULT_CLOCK_MHZ,
    sample_point: float = 0.8,
    *,
    data_phase: bool = False,
) -> BitTiming:
    """按目标波特率反解位定时参数。

    选择策略：先让采样点误差最小，误差相同时选 TQ 总数最接近 160 的解
    （既保证调节粒度，又不会顶到 TSEG 寄存器上限）。

    >>> solve(1_000_000).tq_total
    80
    >>> solve(1_000_000).tseg1
    63
    """
    if bitrate <= 0:
        raise ValueError("bitrate 必须为正数")
    if not 0.5 <= sample_point <= 0.95:
        raise ValueError("sample_point 应落在 0.5 ~ 0.95 之间")

    f_clock_hz = f_clock_mhz * 1_000_000
    target_sp_milli = round(sample_point * 1000)

    best: Optional[BitTiming] = None
    best_key = None
    for cand in _iter_candidates(f_clock_hz, bitrate, sample_point, data_phase):
        sp_err = abs(round(cand.sample_point * 1000) - target_sp_milli)
        key = (sp_err, abs(cand.tq_total - _PREFERRED_TQ), cand.brp)
        if best_key is None or key < best_key:
            best, best_key = cand, key

    if best is None:
        raise ValueError(
            f"无法用 {f_clock_mhz:g} MHz 时钟生成 {bitrate} bit/s 的位定时"
            f"（{'数据段' if data_phase else '仲裁段'}）"
        )
    return best


def _build_bucket(
    data_phase: bool, spec: Iterable[tuple]
) -> Dict[str, BitTiming]:
    """按 80 MHz 时钟批量预解位定时；解不出来的组合直接跳过，不影响模块导入。"""
    bucket: Dict[str, BitTiming] = {}
    for bitrate, sp in spec:
        try:
            bucket[str(int(bitrate))] = solve(
                int(bitrate), DEFAULT_CLOCK_MHZ, sp, data_phase=data_phase
            )
        except ValueError:  # pragma: no cover - 时钟受限时才会发生
            continue
    return bucket


# 80 MHz 时钟下常用的位定时组合，开箱即用，避免每次现算。
# 仲裁段：常用标准波特率；数据段：FD 常用 1/2/4/5 Mbps。
PRESETS_80MHZ: Dict[str, Dict[str, BitTiming]] = {
    "nominal": _build_bucket(
        False,
        [
            (1_000_000, 0.800),
            (800_000, 0.800),
            (500_000, 0.800),
            (250_000, 0.875),
            (125_000, 0.875),
            (100_000, 0.875),
            (50_000, 0.875),
        ],
    ),
    "data": _build_bucket(
        True,
        [
            (5_000_000, 0.750),
            (4_000_000, 0.750),
            (2_000_000, 0.800),
            (1_000_000, 0.800),
        ],
    ),
}


def preset(bitrate: int, f_clock_mhz: float = DEFAULT_CLOCK_MHZ, sample_point: float = 0.8) -> BitTiming:
    """取预设，没有预置值时现算。"""
    key = str(int(bitrate))
    bucket = PRESETS_80MHZ.get("nominal", {})
    if f_clock_mhz == DEFAULT_CLOCK_MHZ and key in bucket:
        p = bucket[key]
        if abs(p.sample_point - sample_point) < 0.02:
            return p
    return solve(bitrate, f_clock_mhz, sample_point)
