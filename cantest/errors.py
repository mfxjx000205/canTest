"""统一的错误类型与 HTTP 映射。

FastAPI 层只捕获 :class:`CanTestError`，其它异常交给兜底处理器，
保证测试平台永远拿到 ``{"ok": false, "error": {...}}`` 的结构。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

__all__ = [
    "CanTestError",
    "BadRequest",
    "ValidationFailed",
    "BusNotFound",
    "ChannelInUse",
    "HardwareUnavailable",
    "OperationTimeout",
    "BusOffError",
    "UnsupportedFeature",
    "SendFailed",
]


class CanTestError(Exception):
    code = "internal_error"
    status = 500
    message = "内部错误"

    def __init__(
        self,
        message: Optional[str] = None,
        *,
        code: Optional[str] = None,
        status: Optional[int] = None,
        detail: Optional[Any] = None,
    ) -> None:
        self.message = message or self.message
        self.code = code or self.code
        self.status = status or self.status
        self.detail = detail
        super().__init__(self.message)

    def to_payload(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"code": self.code, "message": self.message}
        if self.detail is not None:
            payload["detail"] = self.detail
        return payload


class BadRequest(CanTestError):
    code = "bad_request"
    status = 400
    message = "请求参数不合法"


class ValidationFailed(CanTestError):
    code = "validation_failed"
    status = 422
    message = "参数校验失败"


class BusNotFound(CanTestError):
    code = "bus_not_found"
    status = 404
    message = "总线会话不存在"


class ChannelInUse(CanTestError):
    code = "channel_in_use"
    status = 409
    message = "该通道已被其它会话占用"


class HardwareUnavailable(CanTestError):
    code = "hardware_unavailable"
    status = 503
    message = "CAN 硬件不可用"


class OperationTimeout(CanTestError):
    code = "timeout"
    status = 408
    message = "操作超时"


class BusOffError(CanTestError):
    code = "bus_off"
    status = 503
    message = "总线处于 Bus-Off 状态"


class UnsupportedFeature(CanTestError):
    code = "unsupported_feature"
    status = 501
    message = "当前后端不支持该功能"


class SendFailed(CanTestError):
    code = "send_failed"
    status = 502
    message = "帧发送失败"
