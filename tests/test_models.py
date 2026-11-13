"""请求/响应契约的校验规则。这些规则是测试平台能拿到 422 而不是脏数据的前提。"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from cantest.models import (
    BusConfig,
    CyclicRequest,
    ExpectRequest,
    FilterSpec,
    Frame,
    FrameSpec,
    ReplayRequest,
    TransactRequest,
)


class TestFilterSpec:
    def test_defaults(self) -> None:
        spec = FilterSpec(can_id=0x181)
        assert spec.id_mask == 0x7FF
        assert spec.is_extended_id is None

    def test_socketcan_payload(self) -> None:
        assert FilterSpec(can_id=0x181, id_mask=0x700).to_socketcan() == {
            "can_id": 0x181,
            "can_mask": 0x700,
        }
        assert FilterSpec(can_id=0x181, is_extended_id=False).to_socketcan()["extended"] is False

    def test_negative_id_rejected(self) -> None:
        with pytest.raises(ValidationError):
            FilterSpec(can_id=-1)

    def test_standard_id_overflow_rejected(self) -> None:
        with pytest.raises(ValidationError):
            FilterSpec(can_id=0x800, is_extended_id=False)

    def test_extended_id_accepted(self) -> None:
        assert FilterSpec(can_id=0x18FF50E5, is_extended_id=True).can_id == 0x18FF50E5


class TestFrameSpec:
    def test_hex_string_parsed(self) -> None:
        spec = FrameSpec(id=0x181, data="1122334455667788")
        assert spec.data == bytes([0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88])

    def test_serializer_emits_hex_string(self) -> None:
        dumped = FrameSpec(id=0x181, data=b"\x01\x02").model_dump()
        assert dumped["data"] == "0102"
        assert json.loads(json.dumps(dumped))["data"] == "0102"

    def test_to_message_is_flagged_as_tx(self) -> None:
        msg = FrameSpec(id=0x181, data="01").to_message()
        assert msg.is_rx is False
        assert msg.arbitration_id == 0x181
        assert bytes(msg.data) == b"\x01"

    def test_classic_frame_length_limit(self) -> None:
        with pytest.raises(ValidationError):
            FrameSpec(id=0x181, data="00" * 9)

    def test_fd_allows_64_bytes(self) -> None:
        spec = FrameSpec(id=0x181, data="00" * 64, is_fd=True)
        assert len(spec.data) == 64

    def test_fd_rejects_more_than_64_bytes(self) -> None:
        with pytest.raises(ValidationError):
            FrameSpec(id=0x181, data="00" * 65, is_fd=True)

    def test_standard_id_overflow(self) -> None:
        with pytest.raises(ValidationError):
            FrameSpec(id=0x800)

    def test_extended_id_overflow(self) -> None:
        with pytest.raises(ValidationError):
            FrameSpec(id=0x20000000, is_extended_id=True)

    def test_remote_frame_cannot_carry_data(self) -> None:
        with pytest.raises(ValidationError):
            FrameSpec(id=0x181, data="01", is_remote_frame=True)

    def test_remote_frame_without_data_ok(self) -> None:
        assert FrameSpec(id=0x181, is_remote_frame=True).is_remote_frame is True

    def test_brs_requires_fd(self) -> None:
        with pytest.raises(ValidationError):
            FrameSpec(id=0x181, data="01", bitrate_switch=True)

    def test_unknown_field_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            FrameSpec(id=0x181, id_hex="0x181")  # type: ignore[call-arg]


class TestBusConfig:
    def test_defaults_match_bench_hardware(self) -> None:
        cfg = BusConfig()
        assert cfg.interface == "auto"
        assert cfg.bitrate == 1_000_000
        assert cfg.f_clock_mhz == 80
        assert cfg.fd is False
        assert cfg.auto_recover is True

    def test_data_bitrate_requires_fd(self) -> None:
        with pytest.raises(ValidationError):
            BusConfig(data_bitrate=2_000_000)

    def test_data_bitrate_must_not_be_lower(self) -> None:
        with pytest.raises(ValidationError):
            BusConfig(fd=True, bitrate=1_000_000, data_bitrate=500_000)

    def test_fd_config_accepted(self) -> None:
        cfg = BusConfig(fd=True, bitrate=500_000, data_bitrate=2_000_000)
        assert cfg.data_bitrate == 2_000_000

    def test_buffer_size_bounds(self) -> None:
        with pytest.raises(ValidationError):
            BusConfig(buffer_size=10)
        with pytest.raises(ValidationError):
            BusConfig(buffer_size=5_000_000)

    def test_integer_channel_supported(self) -> None:
        assert BusConfig(channel=1).channel == 1
        assert BusConfig(channel="PCAN_USBBUS1").channel == "PCAN_USBBUS1"


class TestRequestModels:
    def test_expect_defaults(self) -> None:
        req = ExpectRequest()
        assert req.timeout_ms == 1000
        assert req.count == 1
        assert req.include_buffer is False
        assert req.fail_fast is True

    def test_expect_count_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            ExpectRequest(count=0)

    def test_transact_needs_frame(self) -> None:
        with pytest.raises(ValidationError):
            TransactRequest()  # type: ignore[call-arg]

    def test_transact_nested_defaults(self) -> None:
        req = TransactRequest(frame=FrameSpec(id=0x181))
        assert req.repeat == 1
        assert req.expect.timeout_ms == 1000

    def test_cyclic_requires_frames_and_period(self) -> None:
        with pytest.raises(ValidationError):
            CyclicRequest(frames=[], period_ms=10)
        with pytest.raises(ValidationError):
            CyclicRequest(frames=[FrameSpec(id=1)], period_ms=0)

    def test_cyclic_count_zero_means_infinite(self) -> None:
        assert CyclicRequest(frames=[FrameSpec(id=1)], period_ms=10).count == 0

    def test_replay_speed_bounds(self) -> None:
        with pytest.raises(ValidationError):
            ReplayRequest(path="x.jsonl", speed=0)


class TestFrameResponse:
    def test_from_message_roundtrip(self) -> None:
        msg = FrameSpec(id=0x18FF50E5, data="0102", is_extended_id=True).to_message()
        msg.timestamp = 123.456
        frame = Frame.from_message(msg)

        assert frame.id == 0x18FF50E5
        assert frame.id_hex == "0x18FF50E5"
        assert frame.data == "0102"
        assert frame.data_bytes == [1, 2]
        assert frame.timestamp == 123.456
        assert frame.is_rx is False
