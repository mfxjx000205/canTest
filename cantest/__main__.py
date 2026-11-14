"""命令行入口：``cantest serve`` / ``cantest probe``。"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional


def _cmd_probe(as_json: bool) -> int:
    from . import clock
    from .backends import probe

    result = probe()
    result["clock"] = clock.describe()
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    print(f"平台        : {result['platform']}  (Python {result['python']})")
    print(f"python-can  : {result['python_can']}")
    print(f"默认后端    : {result['default_interface']}")
    clk = result["clock"]
    # 帧时间戳走 clock.now()，分辨率来自 perf_counter（100 ns），不受
    # time.time() 的粗细影响，所以这里如实报两条：一条是实际拿到的精度，
    # 一条是系统墙钟本身的精度（后者只是信息，不影响帧间隔）。
    src_ns = clk["effective_resolution_ns"]
    print(f"帧时间戳    : {src_ns} ns  (perf_counter)")
    if clk["coarse"]:
        print(
            f"系统墙钟    : {clk['wall_clock_resolution_ns'] // 1_000_000} ms —— 偏粗"
        )
        print("              帧间隔测量已用高精度时钟兜住；如需墙钟本身也精确，")
        print("              换 Python 3.11+（Windows 上是 100 ns 计时器）。")
    else:
        print(f"系统墙钟    : {clk['wall_clock_resolution_ns']} ns")
    if result.get("dlls"):
        print("驱动 DLL    :")
        for name, present in result["dlls"].items():
            print(f"  {'[OK]  ' if present else '[MISS]'} {name}")
    print("通道        :")
    for backend, channels in result["channels"].items():
        if not channels:
            continue
        for ch in channels:
            if "error" in ch:
                print(f"  {backend}: 枚举失败 - {ch['error']}")
            else:
                mark = "" if ch.get("probed", True) else " (未探测到)"
                print(f"  {backend}/{ch['channel']}{mark}")
    return 0


def _cmd_serve(host: str, port: int, reload: bool, log_level: str) -> int:
    import uvicorn

    if reload:
        uvicorn.run(
            "cantest.api.app:app",
            host=host,
            port=port,
            reload=True,
            log_level=log_level,
        )
    else:
        from .api.app import app

        uvicorn.run(app, host=host, port=port, log_level=log_level)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cantest", description="CAN 总线测试控制面（python-can + FastAPI）"
    )
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="启动 HTTP / WebSocket 服务")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8700)
    serve.add_argument("--reload", action="store_true", help="开发模式热重载")
    serve.add_argument("--log-level", default="info")

    probe = sub.add_parser("probe", help="硬件与后端自检")
    probe.add_argument("--json", action="store_true", help="输出 JSON")

    args = parser.parse_args(argv)

    if args.command == "probe":
        return _cmd_probe(args.json)
    if args.command in (None, "serve"):
        host = getattr(args, "host", "127.0.0.1")
        port = getattr(args, "port", 8700)
        reload = getattr(args, "reload", False)
        log_level = getattr(args, "log_level", "info")
        return _cmd_serve(host, port, reload, log_level)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
