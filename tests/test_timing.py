"""位定时计算：80 MHz 时钟下必须能精确命中目标波特率。"""

from __future__ import annotations

import pytest

from cantest.timing import PRESETS_80MHZ, BitTiming, preset, solve

STANDARD_BITRATES = [1_000_000, 800_000, 500_000, 250_000, 125_000, 100_000, 50_000]


@pytest.mark.parametrize("bitrate", STANDARD_BITRATES)
def test_solve_hits_exact_bitrate(bitrate: int) -> None:
    timing = solve(bitrate)

    # 位时间公式必须严格成立，否则实际波特率与标称值不符
    assert timing.bitrate == pytest.approx(bitrate, rel=1e-9)
    assert 8 <= timing.tq_total <= 385
    assert timing.tseg2 >= 2
    assert timing.sjw <= timing.tseg2


def test_1mbps_at_80mhz_matches_cia_reference() -> None:
    """80 MHz / 1 Mbps = 80 TQ，标准解是 BRP=1 / TSEG1=63 / TSEG2=16。"""
    timing = solve(1_000_000, f_clock_mhz=80, sample_point=0.8)

    assert timing.brp == 1
    assert timing.tseg1 == 63
    assert timing.tseg2 == 16
    assert timing.tq_total == 80
    assert timing.sample_point == pytest.approx(0.8, abs=1e-9)
    assert timing.tq_ns == pytest.approx(12.5)
    assert timing.bit_time_ns == pytest.approx(1000.0)


@pytest.mark.parametrize("bitrate", STANDARD_BITRATES)
def test_brp_times_tq_covers_one_bit(bitrate: int) -> None:
    """不变式：BRP × 单bit TQ 数 == 时钟 / 波特率（一定是整数才不会有误差）。"""
    timing = solve(bitrate)
    assert timing.brp * timing.tq_total == pytest.approx(80_000_000 / bitrate, rel=1e-9)


def test_sample_point_is_honoured() -> None:
    for target in (0.7, 0.75, 0.8, 0.875):
        timing = solve(1_000_000, sample_point=target)
        assert timing.sample_point == pytest.approx(target, abs=0.01)


def test_data_phase_constraints_for_fd() -> None:
    """FD 数据段一位只能 8~49 TQ，5 Mbps 时正好 16 TQ。"""
    timing = solve(5_000_000, sample_point=0.75, data_phase=True)

    assert 8 <= timing.tq_total <= 49
    assert timing.bitrate == pytest.approx(5_000_000, rel=1e-9)
    assert timing.brp == 1
    assert timing.tq_total == 16


def test_unsolvable_bitrate_raises() -> None:
    # 80 MHz 除不出 3 Mbps 的整数 TQ 总数
    with pytest.raises(ValueError):
        solve(3_000_000)


@pytest.mark.parametrize("bad", [0, -1])
def test_non_positive_bitrate_rejected(bad: int) -> None:
    with pytest.raises(ValueError):
        solve(bad)


@pytest.mark.parametrize("sp", [0.3, 0.99])
def test_out_of_range_sample_point_rejected(sp: float) -> None:
    with pytest.raises(ValueError):
        solve(1_000_000, sample_point=sp)


def test_pcan_kwargs_naming() -> None:
    timing = solve(1_000_000)
    nom = timing.pcan_kwargs("nom")
    data = timing.pcan_kwargs("data")

    assert nom == {
        "nom_brp": timing.brp,
        "nom_tseg1": timing.tseg1,
        "nom_tseg2": timing.tseg2,
        "nom_sjw": timing.sjw,
    }
    assert set(data) == {"data_brp", "data_tseg1", "data_tseg2", "data_sjw"}


def test_to_dict_is_json_friendly() -> None:
    d = solve(1_000_000).to_dict()
    for key in ("brp", "tseg1", "tseg2", "sjw", "tq_total", "sample_point", "bitrate"):
        assert key in d
    assert isinstance(d["sample_point"], float)


def test_presets_cover_common_bitrates() -> None:
    nominal = PRESETS_80MHZ["nominal"]
    for bitrate in STANDARD_BITRATES:
        assert str(bitrate) in nominal
        assert isinstance(nominal[str(bitrate)], BitTiming)
    assert set(PRESETS_80MHZ["data"]) >= {"1000000", "2000000", "4000000", "5000000"}


def test_preset_returns_cached_instance() -> None:
    cached = PRESETS_80MHZ["nominal"]["1000000"]
    assert preset(1_000_000) is cached
    # 换了时钟就必须现算，不能拿 80 MHz 的表糊弄
    assert preset(1_000_000, f_clock_mhz=40).f_clock_mhz == 40
