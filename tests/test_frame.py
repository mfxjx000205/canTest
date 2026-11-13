"""帧级工具：数据编解码、消息序列化、匹配器、负载估算。"""

from __future__ import annotations

import can
import pytest

from cantest.frame import (
    FD_DLC_LENGTHS,
    FrameMatcher,
    format_data,
    format_id,
    frame_bits,
    message_to_dict,
    parse_can_id,
    parse_data,
)


class TestParseData:
    # "1122334455667788" 拆成字节是 11/22/33/44/55/66/77/88
    PAYLOAD = bytes([0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88])

    @pytest.mark.parametrize(
        "raw",
        [
            "1122334455667788",
            "11 22 33 44 55 66 77 88",
            "11,22,33,44,55,66,77,88",
            "0x11,0x22,0x33,0x44,0x55,0x66,0x77,0x88",
            "11-22-33-44-55-66-77-88",
        ],
    )
    def test_accepts_common_text_forms(self, raw: str) -> None:
        assert parse_data(raw) == self.PAYLOAD

    def test_accepts_bytes_and_int_sequence(self) -> None:
        assert parse_data(b"\x01\x02") == b"\x01\x02"
        assert parse_data(bytearray([1, 2])) == b"\x01\x02"
        assert parse_data([1, 2, 255]) == b"\x01\x02\xff"

    def test_empty_inputs(self) -> None:
        assert parse_data(None) == b""
        assert parse_data("") == b""
        assert parse_data([]) == b""

    def test_odd_length_rejected(self) -> None:
        with pytest.raises(ValueError):
            parse_data("123")

    def test_non_hex_rejected(self) -> None:
        with pytest.raises(ValueError):
            parse_data("zz")

    def test_out_of_range_byte_rejected(self) -> None:
        with pytest.raises(ValueError):
            parse_data([256])

    def test_unsupported_type_rejected(self) -> None:
        with pytest.raises(TypeError):
            parse_data(3.5)  # type: ignore[arg-type]


class TestParseCanId:
    """ID 的写法必须宽容，但歧义必须有明确规则。

    起因是 README 里的 curl 示例写了 ``"id":"0x18FF50E5"``，而模型只收整数，
    照抄示例直接 422。CAN 的 ID 天生按十六进制沟通，逼调用方心算成 419385573 不合理。
    """

    @pytest.mark.parametrize(
        "raw",
        ["0x18FF50E5", "0X18FF50E5", "18FF50E5", 0x18FF50E5, "419385573", 419385573],
    )
    def test_same_value_many_forms(self, raw) -> None:
        assert parse_can_id(raw) == 0x18FF50E5

    def test_plain_digits_are_decimal(self) -> None:
        # "181" 与 "0x181" 结果不同——纯数字串按十进制，避免「这个 181 是几进制」
        # 的猜测；要写十六进制就加 0x 前缀。
        assert parse_can_id("181") == 181
        assert parse_can_id("0x181") == 0x181

    def test_tolerates_separators(self) -> None:
        assert parse_can_id(" 0x18FF_50E5 ") == 0x18FF50E5

    @pytest.mark.parametrize("raw", ["", "   ", "0xZZ"])
    def test_bad_text_rejected(self, raw: str) -> None:
        with pytest.raises(ValueError):
            parse_can_id(raw)

    def test_bool_rejected(self) -> None:
        # bool 是 int 的子类，True 会被当成 ID=1 悄悄发出去，必须挡掉
        with pytest.raises(TypeError):
            parse_can_id(True)  # type: ignore[arg-type]


def test_format_helpers() -> None:
    assert format_data([0x11, 0x22]) == "1122"
    assert format_id(0x181) == "0x181"
    assert format_id(0x18FF50E5, extended=True) == "0x18FF50E5"


class TestMessageToDict:
    def test_classic_frame_fields(self) -> None:
        # python-can 的 is_extended_id 默认是 True，构造「标准帧」必须显式关掉，
        # 否则 id_hex 会按 8 位补零。
        msg = can.Message(
            arbitration_id=0x181,
            data=b"\x01\x02\x03",
            is_rx=True,
            is_extended_id=False,
        )
        d = message_to_dict(msg)

        assert d["id"] == 0x181
        assert d["id_hex"] == "0x181"
        assert d["is_extended_id"] is False
        assert d["data"] == "010203"
        assert d["data_bytes"] == [1, 2, 3]
        assert d["length"] == 3
        assert d["dlc"] == 3
        assert d["is_rx"] is True
        assert d["is_error_frame"] is False

    def test_extended_and_fd_flags(self) -> None:
        msg = can.Message(
            arbitration_id=0x18FF50E5,
            data=bytes(range(12)),
            is_extended_id=True,
            is_fd=True,
            bitrate_switch=True,
        )
        d = message_to_dict(msg)

        assert d["id_hex"] == "0x18FF50E5"
        assert d["is_extended_id"] is True
        assert d["is_fd"] is True
        assert d["bitrate_switch"] is True
        assert d["length"] == 12

    def test_empty_payload(self) -> None:
        d = message_to_dict(can.Message(arbitration_id=0x100))
        assert d["data"] == ""
        assert d["data_bytes"] == []
        assert d["length"] == 0


class TestFrameMatcher:
    def _msg(self, can_id: int = 0x181, data: bytes = b"\x01\x02", **kw) -> can.Message:
        return can.Message(arbitration_id=can_id, data=data, **kw)

    def test_empty_matcher_matches_everything(self) -> None:
        assert FrameMatcher().matches(self._msg())

    def test_exact_id(self) -> None:
        matcher = FrameMatcher(can_id=0x181)
        assert matcher.matches(self._msg(0x181))
        assert not matcher.matches(self._msg(0x182))

    def test_id_mask_selects_bit_range(self) -> None:
        """掩码 0x700 表示只看高 3 位：0x1xx 都算匹配。"""
        matcher = FrameMatcher(can_id=0x100, id_mask=0x700)
        assert matcher.matches(self._msg(0x1FF))
        assert matcher.matches(self._msg(0x123))
        assert not matcher.matches(self._msg(0x281))

    def test_data_prefix_match(self) -> None:
        matcher = FrameMatcher(data=b"\x01\x02")
        assert matcher.matches(self._msg(data=b"\x01\x02\x03"))
        assert not matcher.matches(self._msg(data=b"\x01\x03"))
        assert not matcher.matches(self._msg(data=b"\x01"))

    def test_data_mask_ignores_masked_bits(self) -> None:
        matcher = FrameMatcher(data=b"\x10", data_mask=b"\xF0")
        assert matcher.matches(self._msg(data=b"\x1F"))
        assert not matcher.matches(self._msg(data=b"\x20"))

    def test_extended_flag_must_match(self) -> None:
        matcher = FrameMatcher(is_extended_id=False)
        assert matcher.matches(self._msg(0x181, is_extended_id=False))
        assert not matcher.matches(self._msg(0x181, is_extended_id=True))

    def test_error_frames_filtered_by_default(self) -> None:
        err = can.Message(arbitration_id=0, is_error_frame=True)
        assert not FrameMatcher().matches(err)
        assert FrameMatcher(allow_error_frames=True).matches(err)

    def test_fd_flag_filter(self) -> None:
        matcher = FrameMatcher(is_fd=True)
        assert matcher.matches(self._msg(is_fd=True))
        assert not matcher.matches(self._msg(is_fd=False))


class TestFrameBits:
    def test_standard_beats_extended(self) -> None:
        # can.Message 的默认 is_extended_id 是 True，标准帧必须显式声明
        std = frame_bits(
            can.Message(arbitration_id=0x100, data=bytes(8), is_extended_id=False)
        )
        ext = frame_bits(
            can.Message(arbitration_id=0x18FF50E5, data=bytes(8), is_extended_id=True)
        )
        assert ext > std

    def test_more_data_costs_more_bits(self) -> None:
        small = frame_bits(can.Message(arbitration_id=0x100, data=bytes(8)))
        large = frame_bits(can.Message(arbitration_id=0x100, data=bytes(64), is_fd=True))
        assert large > small

    def test_error_frames_charge_nothing(self) -> None:
        assert frame_bits(can.Message(is_error_frame=True)) == 0

    def test_remote_frame_has_no_payload(self) -> None:
        data_frame = frame_bits(can.Message(arbitration_id=0x100, data=bytes(8)))
        remote = frame_bits(
            can.Message(arbitration_id=0x100, is_remote_frame=True, dlc=8)
        )
        assert remote < data_frame

    def test_fd_dlc_table_is_monotonic(self) -> None:
        assert list(FD_DLC_LENGTHS) == sorted(FD_DLC_LENGTHS)
        assert FD_DLC_LENGTHS[-1] == 64
