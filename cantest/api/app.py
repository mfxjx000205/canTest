"""FastAPI 应用工厂。

所有响应统一为 ``{"ok": true, "data": ...}`` / ``{"ok": false, "error": {...}}``，
测试平台只需要判断 ``ok`` 字段，不用解析各家异常格式。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .. import __version__
from ..errors import CanTestError
from ..hub import FrameHub
from ..manager import BusManager
from . import routes_async, routes_bus, routes_frame, routes_system, ws

logger = logging.getLogger("cantest")

API_PREFIX = "/api/v1"

DESCRIPTION = """
CAN 总线测试控制面。底层用 python-can 驱动硬件（Windows 上默认 PCAN，
Linux 上默认 socketCAN），对外暴露一套与测试平台解耦的 HTTP / WebSocket 接口。

**典型用法**

1. `POST /api/v1/buses` 打开通道（80 MHz 时钟、1 Mbps 位定时自动计算）
2. `POST /api/v1/buses/{id}/transact` 发一帧并等响应，拿到往返时延
3. `POST /api/v1/buses/{id}/cyclic` 起周期报文，`GET .../status` 看抖动与负载
4. `WS /api/v1/ws/buses/{id}` 实时看帧流

**注意**：`receive` 是消费式读取（推进游标），`expect` 不消费游标；
跨两次 HTTP 调用做「先发后等」存在竞态，建议直接用 `transact`。
"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    hub = FrameHub()
    hub.bind_loop(asyncio.get_running_loop())
    manager = BusManager(on_frame=hub.publish_message)
    app.state.hub = hub
    app.state.manager = manager
    logger.info("CANtest %s 启动，API 前缀 %s", __version__, API_PREFIX)
    try:
        yield
    finally:
        closed = manager.close_all()
        logger.info("关闭 %d 条总线会话", closed)


def create_app() -> FastAPI:
    app = FastAPI(
        title="CANtest",
        version=__version__,
        description=DESCRIPTION,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    for module in (routes_system, routes_bus, routes_frame, routes_async):
        app.include_router(module.router, prefix=API_PREFIX)
    app.include_router(ws.router, prefix=API_PREFIX)

    @app.exception_handler(CanTestError)
    async def handle_cantest_error(request: Request, exc: CanTestError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status,
            content={"ok": False, "error": exc.to_payload(), "path": request.url.path},
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "ok": False,
                "error": {
                    "code": "validation_failed",
                    "message": "请求参数校验失败",
                    "detail": jsonable_encoder(exc.errors(), custom_encoder={ValueError: str}),
                },
                "path": request.url.path,
            },
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("未处理异常：%s", exc)
        return JSONResponse(
            status_code=500,
            content={
                "ok": False,
                "error": {
                    "code": "internal_error",
                    "message": str(exc) or exc.__class__.__name__,
                },
                "path": request.url.path,
            },
        )

    @app.get("/", include_in_schema=False)
    def root() -> Dict[str, Any]:
        return {
            "name": "CANtest",
            "version": __version__,
            "api_prefix": API_PREFIX,
            "docs": "/docs",
            "health": f"{API_PREFIX}/health",
        }

    return app


app = create_app()
