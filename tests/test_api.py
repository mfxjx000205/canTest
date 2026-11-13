"""FastAPI 控制面：走真实的 ASGI 请求，覆盖自动化测试平台会调用的接口。

用 ``TestClient`` 而不是直接调函数，是为了把依赖注入、参数校验、异常到 HTTP
状态码的映射一起验掉——这些正是外挂设备对接时最容易踩的地方。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cantest.api.app import API_PREFIX, create_app

P = API_PREFIX


@pytest.fixture
def client():
    app = create_app()
    with TestClient(app) as c:
        yield c


@pytest.fixture
def bus_id(client) -> str:
    resp = client.post(f"{P}/buses", json={"interface": "virtual", "buffer_size": 2000})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    return body["data"]["id"]


def _ok(resp):
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True, body
    return body["data"]


def _sent(resp):
    """发送类接口：数据在 data 里，RX 水位在信封顶层的 seq 字段上。"""
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True, body
    assert "seq" in body, body
    return body["data"], body["seq"]


def _err(resp, status: int, code: str):
    assert resp.status_code == status, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == code
    return body["error"]


# --------------------------------------------------------------------------
# 系统接口
# --------------------------------------------------------------------------
class TestSystem:
    def test_health(self, client) -> None:
        data = _ok(client.get(f"{P}/health"))
        assert data["status"] == "ok"
        assert data["version"]
        assert data["ws_subscribers"] == 0

    def test_info(self, client) -> None:
        data = _ok(client.get(f"{P}/info"))
        assert data["platform"]
        assert data["python_can"]
        assert "pcan" in data["capabilities"]

    def test_interfaces(self, client) -> None:
        data = _ok(client.get(f"{P}/interfaces"))
        names = {item["name"] for item in data}
        assert "virtual" in names
        assert isinstance(data, list) and data

    def test_channels_virtual(self, client) -> None:
        data = _ok(client.get(f"{P}/channels", params={"interface": "virtual"}))
        assert data["interface"] == "virtual"
        assert data["channels"]

    def test_unknown_interface_rejected(self, client) -> None:
        _err(client.get(f"{P}/channels", params={"interface": "no-such-bus"}), 400, "bad_request")

    def test_root(self, client) -> None:
        body = client.get("/").json()
        assert body["name"] == "CANtest"
        assert body["api_prefix"] == P


# --------------------------------------------------------------------------
# 会话生命周期
# --------------------------------------------------------------------------
class TestBusLifecycle:
    def test_open_and_get(self, client, bus_id) -> None:
        data = _ok(client.get(f"{P}/buses/{bus_id}"))
        assert data["id"] == bus_id
        assert data["interface"] == "virtual"
        assert data["timing"]["source"] == "virtual"
        assert data["stats"]["tx_frames"] == 0

    def test_open_with_1mbps_80mhz_config(self, client) -> None:
        data = _ok(
            client.post(
                f"{P}/buses",
                json={
                    "interface": "virtual",
                    "bitrate": 1_000_000,
                    "f_clock_mhz": 80,
                    "sample_point": 0.8,
                },
            )
        )
        assert data["config"]["bitrate"] == 1_000_000
        assert data["config"]["f_clock_mhz"] == 80

    def test_list_buses(self, client) -> None:
        client.post(f"{P}/buses", json={"interface": "virtual", "channel": "l1"})
        client.post(f"{P}/buses", json={"interface": "virtual", "channel": "l2"})
        data = _ok(client.get(f"{P}/buses"))
        assert len(data) == 2

    def test_close(self, client, bus_id) -> None:
        _ok(client.delete(f"{P}/buses/{bus_id}"))
        _err(client.get(f"{P}/buses/{bus_id}"), 404, "bus_not_found")

    def test_unknown_bus_returns_404(self, client) -> None:
        _err(client.get(f"{P}/buses/does-not-exist"), 404, "bus_not_found")

    def test_invalid_config_returns_422(self, client) -> None:
        _err(client.post(f"{P}/buses", json={"bitrate": 0}), 422, "validation_failed")

    def test_data_bitrate_without_fd_returns_422(self, client) -> None:
        _err(
            client.post(f"{P}/buses", json={"data_bitrate": 2_000_000}),
            422,
            "validation_failed",
        )


# --------------------------------------------------------------------------
# 收发与断言
# --------------------------------------------------------------------------
class TestFrames:
    def test_send(self, client, bus_id) -> None:
        data = _ok(
            client.post(f"{P}/buses/{bus_id}/send", json={"id": 0x181, "data": "11223344"})
        )
        assert data["id"] == 0x181
        assert data["id_hex"] == "0x181"
        assert data["data"] == "11223344"
        assert data["is_rx"] is False

    def test_send_invalid_payload(self, client, bus_id) -> None:
        _err(
            client.post(f"{P}/buses/{bus_id}/send", json={"id": 0x181, "data": "123"}),
            422,
            "validation_failed",
        )

    def test_send_classic_frame_too_long(self, client, bus_id) -> None:
        _err(
            client.post(f"{P}/buses/{bus_id}/send", json={"id": 0x181, "data": "00" * 9}),
            422,
            "validation_failed",
        )

    def test_batch_send(self, client, bus_id) -> None:
        data = _ok(
            client.post(
                f"{P}/buses/{bus_id}/send/batch",
                json={
                    "frames": [{"id": 0x181, "data": "01"}, {"id": 0x182, "data": "02"}],
                    "interval_ms": 1,
                },
            )
        )
        assert data["sent"] == 2

    def test_receive(self, client, bus_id) -> None:
        client.post(f"{P}/buses/{bus_id}/send", json={"id": 0x181, "data": "0102"})
        data = _ok(
            client.post(
                f"{P}/buses/{bus_id}/receive", json={"count": 1, "timeout_ms": 1000}
            )
        )
        assert data["count"] == 1
        assert data["complete"] is True
        assert data["frames"][0]["id"] == 0x181

    def test_receive_timeout_is_not_an_error(self, client, bus_id) -> None:
        data = _ok(
            client.post(
                f"{P}/buses/{bus_id}/receive", json={"count": 1, "timeout_ms": 100}
            )
        )
        assert data["count"] == 0
        assert data["complete"] is False

    def test_expect_matched(self, client, bus_id) -> None:
        _, seq = _sent(
            client.post(f"{P}/buses/{bus_id}/send", json={"id": 0x181, "data": "1122"})
        )
        data = _ok(
            client.post(
                f"{P}/buses/{bus_id}/expect",
                json={"can_id": 0x181, "timeout_ms": 1000, "since_seq": seq},
            )
        )
        assert data["matched"] is True
        assert data["count"] == 1

    def test_expect_data_prefix(self, client, bus_id) -> None:
        _, seq = _sent(
            client.post(f"{P}/buses/{bus_id}/send", json={"id": 0x181, "data": "AABBCC"})
        )
        data = _ok(
            client.post(
                f"{P}/buses/{bus_id}/expect",
                json={
                    "can_id": 0x181,
                    "data": "aabb",
                    "timeout_ms": 1000,
                    "since_seq": seq,
                },
            )
        )
        assert data["matched"] is True

    def test_expect_id_mask(self, client, bus_id) -> None:
        _, seq = _sent(
            client.post(f"{P}/buses/{bus_id}/send", json={"id": 0x1A5, "data": "01"})
        )
        data = _ok(
            client.post(
                f"{P}/buses/{bus_id}/expect",
                json={
                    "can_id": 0x100,
                    "id_mask": 0x700,
                    "timeout_ms": 1000,
                    "since_seq": seq,
                },
            )
        )
        assert data["matched"] is True

    def test_expect_timeout_returns_408(self, client, bus_id) -> None:
        error = _err(
            client.post(
                f"{P}/buses/{bus_id}/expect", json={"can_id": 0x7FF, "timeout_ms": 100}
            ),
            408,
            "timeout",
        )
        assert error["detail"]["received"] == []

    def test_expect_fail_fast_false_returns_200(self, client, bus_id) -> None:
        data = _ok(
            client.post(
                f"{P}/buses/{bus_id}/expect",
                json={"can_id": 0x7FF, "timeout_ms": 100, "fail_fast": False},
            )
        )
        assert data["matched"] is False

    def test_expect_data_mask_without_data_rejected(self, client, bus_id) -> None:
        _err(
            client.post(
                f"{P}/buses/{bus_id}/expect", json={"data_mask": "FF", "timeout_ms": 100}
            ),
            400,
            "bad_request",
        )

    def test_transact(self, client, bus_id) -> None:
        data = _ok(
            client.post(
                f"{P}/buses/{bus_id}/transact",
                json={
                    "frame": {"id": 0x100, "data": "0102"},
                    "expect": {"can_id": 0x100, "timeout_ms": 1000},
                },
            )
        )
        assert data["count"] == 1
        assert data["responses"][0]["id"] == 0x100
        assert len(data["latency_ms"]) == 1
        assert data["latency"]["avg"] > 0

    def test_transact_timeout(self, client, bus_id) -> None:
        error = _err(
            client.post(
                f"{P}/buses/{bus_id}/transact",
                json={
                    "frame": {"id": 0x100, "data": "01"},
                    "expect": {"can_id": 0x7FF, "timeout_ms": 100},
                },
            ),
            408,
            "timeout",
        )
        assert error["detail"]["sent"]["id"] == 0x100

    def test_frames_history(self, client, bus_id) -> None:
        client.post(f"{P}/buses/{bus_id}/send", json={"id": 0x181, "data": "01"})
        client.post(f"{P}/buses/{bus_id}/receive", json={"count": 1, "timeout_ms": 500})
        data = _ok(client.get(f"{P}/buses/{bus_id}/frames", params={"limit": 10}))
        assert data["count"] >= 1
        assert all(f["id"] == 0x181 for f in data["frames"])

    def test_frames_history_filter(self, client, bus_id) -> None:
        client.post(f"{P}/buses/{bus_id}/send", json={"id": 0x181, "data": "01"})
        client.post(f"{P}/buses/{bus_id}/send", json={"id": 0x281, "data": "02"})
        client.post(f"{P}/buses/{bus_id}/receive", json={"count": 5, "timeout_ms": 300})
        data = _ok(
            client.get(f"{P}/buses/{bus_id}/frames", params={"can_id": 0x281, "limit": 10})
        )
        assert all(f["id"] == 0x281 for f in data["frames"])

    def test_frames_on_unknown_bus(self, client) -> None:
        _err(client.get(f"{P}/buses/nope/frames"), 404, "bus_not_found")


# --------------------------------------------------------------------------
# 过滤器 / 统计 / 维护
# --------------------------------------------------------------------------
class TestBusControl:
    def test_set_and_get_filters(self, client, bus_id) -> None:
        data = _ok(
            client.put(
                f"{P}/buses/{bus_id}/filters",
                json={"filters": [{"can_id": 0x181, "id_mask": 0x700}]},
            )
        )
        assert data["filters"][0]["can_id"] == 0x181

        current = _ok(client.get(f"{P}/buses/{bus_id}/filters"))
        assert len(current["filters"]) == 1

    def test_filter_actually_drops_frames(self, client, bus_id) -> None:
        client.put(
            f"{P}/buses/{bus_id}/filters",
            json={"filters": [{"can_id": 0x100, "id_mask": 0x700}]},
        )
        client.post(f"{P}/buses/{bus_id}/send", json={"id": 0x281, "data": "01"})
        data = _ok(
            client.post(
                f"{P}/buses/{bus_id}/receive", json={"count": 1, "timeout_ms": 200}
            )
        )
        assert data["count"] == 0

    def test_clear_filters(self, client, bus_id) -> None:
        client.put(
            f"{P}/buses/{bus_id}/filters", json={"filters": [{"can_id": 0x100}]}
        )
        _ok(client.delete(f"{P}/buses/{bus_id}/filters"))
        assert _ok(client.get(f"{P}/buses/{bus_id}/filters"))["filters"] == []

    def test_stats(self, client, bus_id) -> None:
        client.post(f"{P}/buses/{bus_id}/send", json={"id": 0x181, "data": "0102"})
        data = _ok(client.get(f"{P}/buses/{bus_id}/stats"))
        assert data["stats"]["tx_frames"] == 1
        assert data["stats"]["tx_bytes"] == 2
        assert data["cyclic_tasks"] == 0
        assert data["recording"]["active"] is False

    def test_flush(self, client, bus_id) -> None:
        client.post(f"{P}/buses/{bus_id}/send", json={"id": 0x181, "data": "01"})
        data = _ok(client.post(f"{P}/buses/{bus_id}/flush"))
        assert data["dropped"] >= 0

    def test_reset_unsupported_on_virtual(self, client, bus_id) -> None:
        # 虚拟总线没有 bus-off 概念，复位应当明确回 501 而不是 500
        _err(client.post(f"{P}/buses/{bus_id}/reset"), 501, "unsupported_feature")

    def test_capabilities(self, client, bus_id) -> None:
        data = _ok(client.get(f"{P}/buses/{bus_id}/capabilities"))
        assert data["fd"] is True

    def test_all_stats(self, client, bus_id) -> None:
        data = _ok(client.get(f"{P}/stats"))
        assert data["count"] == 1
        assert data["items"][0]["id"] == bus_id


# --------------------------------------------------------------------------
# 后台任务
# --------------------------------------------------------------------------
class TestBackground:
    def test_cyclic_lifecycle(self, client, bus_id) -> None:
        data = _ok(
            client.post(
                f"{P}/buses/{bus_id}/cyclic",
                json={"frames": [{"id": 0x181, "data": "01"}], "period_ms": 20, "count": 3},
            )
        )
        task_id = data["id"]
        assert data["period_ms"] == 20

        listed = _ok(client.get(f"{P}/buses/{bus_id}/cyclic"))
        assert listed["count"] == 1

        _ok(client.delete(f"{P}/buses/{bus_id}/cyclic/{task_id}"))
        assert _ok(client.get(f"{P}/buses/{bus_id}/cyclic"))["count"] == 0

    def test_stop_unknown_cyclic(self, client, bus_id) -> None:
        _err(client.delete(f"{P}/buses/{bus_id}/cyclic/nope"), 400, "bad_request")

    def test_record_start_stop(self, client, bus_id, tmp_path) -> None:
        target = str(tmp_path / "api.jsonl")
        started = _ok(
            client.post(
                f"{P}/buses/{bus_id}/record/start",
                json={"format": "jsonl", "path": target},
            )
        )
        assert started["active"] is True
        assert started["path"].endswith("api.jsonl")

        client.post(f"{P}/buses/{bus_id}/send", json={"id": 0x181, "data": "01"})
        client.post(f"{P}/buses/{bus_id}/receive", json={"count": 1, "timeout_ms": 500})

        stopped = _ok(client.post(f"{P}/buses/{bus_id}/record/stop"))
        assert stopped["active"] is False
        assert stopped["frames"] >= 1
        assert _ok(client.get(f"{P}/buses/{bus_id}/record"))["frames"] >= 1

    def test_record_invalid_format(self, client, bus_id) -> None:
        _err(
            client.post(
                f"{P}/buses/{bus_id}/record/start", json={"format": "xml"}
            ),
            422,
            "validation_failed",
        )

    def test_replay_lifecycle(self, client, bus_id, tmp_path) -> None:
        source = str(tmp_path / "src.jsonl")
        client.post(
            f"{P}/buses/{bus_id}/record/start", json={"format": "jsonl", "path": source}
        )
        client.post(f"{P}/buses/{bus_id}/send", json={"id": 0x190, "data": "01"})
        client.post(f"{P}/buses/{bus_id}/receive", json={"count": 1, "timeout_ms": 500})
        client.post(f"{P}/buses/{bus_id}/record/stop")

        job = _ok(
            client.post(
                f"{P}/buses/{bus_id}/replay",
                json={"path": source, "gap_ms": 5, "count": 1},
            )
        )
        assert job["path"] == source

        jobs = _ok(client.get(f"{P}/buses/{bus_id}/replay"))
        assert jobs["count"] >= 1
        _ok(client.delete(f"{P}/buses/{bus_id}/replay"))


# --------------------------------------------------------------------------
# WebSocket
# --------------------------------------------------------------------------
class TestWebSocket:
    def test_hello(self, client, bus_id) -> None:
        with client.websocket_connect(f"{P}/ws/buses/{bus_id}") as ws:
            hello = ws.receive_json()
            assert hello["ok"] is True
            assert hello["data"]["type"] == "hello"
            assert hello["data"]["bus_id"] == bus_id

    def test_ping_pong(self, client, bus_id) -> None:
        with client.websocket_connect(f"{P}/ws/buses/{bus_id}") as ws:
            ws.receive_json()
            ws.send_text("ping")
            pong = ws.receive_json()
            assert pong["data"]["type"] == "pong"

    def test_frames_streamed(self, client, bus_id) -> None:
        with client.websocket_connect(f"{P}/ws/buses/{bus_id}") as ws:
            ws.receive_json()  # hello
            client.post(f"{P}/buses/{bus_id}/send", json={"id": 0x181, "data": "01"})
            event = ws.receive_json()
            assert event["data"]["type"] == "frame"
            assert event["data"]["frame"]["id"] == 0x181

    def test_unknown_bus_closes(self, client) -> None:
        with client.websocket_connect(f"{P}/ws/buses/nope") as ws:
            payload = ws.receive_json()
            assert payload["ok"] is False
            assert payload["error"]["code"] == "bus_not_found"
