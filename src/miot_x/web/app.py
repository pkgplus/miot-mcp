# -*- coding: utf-8 -*-
"""Starlette 统一 Web 应用工厂 — 组合 Web UI + REST API + MCP。"""
import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from starlette.applications import Starlette
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .routes import all_api_routes

_LOGGER = logging.getLogger(__name__)
_STATIC_DIR = Path(__file__).parent / "static"


_xiaozhi_connected = False

_homekit_bridge = None


def set_xiaozhi_status(connected: bool):
    global _xiaozhi_connected
    _xiaozhi_connected = connected


def set_homekit_bridge(bridge):
    global _homekit_bridge
    _homekit_bridge = bridge


async def _status(request):
    """服务状态。"""
    from ..lib.proxy import _shared_proxy
    connected = _shared_proxy is not None and _shared_proxy.is_ready
    device_count = 0
    if _shared_proxy:
        try:
            devices = await _shared_proxy.get_devices()
            device_count = len(devices)
        except Exception:
            pass
    result = {
        "connected": connected,
        "device_count": device_count,
        "xiaozhi_connected": _xiaozhi_connected,
    }
    if _homekit_bridge:
        result["homekit"] = {
            "paired": _homekit_bridge.is_paired,
            "accessories": _homekit_bridge.get_accessories(),
        }
    return JSONResponse(result)


async def _homekit_status(request):
    """HomeKit 桥接状态。"""
    if not _homekit_bridge:
        return JSONResponse({"enabled": False})
    return JSONResponse({
        "enabled": True,
        "paired": _homekit_bridge.is_paired,
        "pin": _homekit_bridge.pin if not _homekit_bridge.is_paired else None,
        "accessories": _homekit_bridge.get_accessories(),
    })


async def _xiaozhi_with_status(mcp_instance):
    """包装小智桥接，更新连接状态。"""
    from ..mcp.xiaozhi import xiaozhi_bridge
    set_xiaozhi_status(True)
    try:
        await xiaozhi_bridge(mcp_instance)
    finally:
        set_xiaozhi_status(False)


def create_app(enable_xiaozhi: bool = False, enable_mcp: bool = True, enable_homekit: bool = False):
    """创建统一 Starlette 应用。

    Args:
        enable_xiaozhi: 是否启用小智 WebSocket 桥接。
        enable_mcp: 是否挂载 MCP endpoint。
        enable_homekit: 是否启动 HomeKit 桥接。
    """
    mcp_app = None  # 在 lifespan 闭包捕获前初始化，避免 NameError

    @asynccontextmanager
    async def lifespan(app):
        _LOGGER.info("miot-x Web 服务启动中...")

        # 启动 OAuth :443 常驻回调服务
        from .oauth_callback import start_persistent_callback_server, stop_persistent_callback_server
        await start_persistent_callback_server()

        # 尝试预连接 MIoT
        proxy = None
        try:
            from ..lib.proxy import get_shared_proxy
            proxy = await get_shared_proxy()
            _LOGGER.info("MIoT 已连接")
        except Exception as e:
            _LOGGER.warning("MIoT 连接失败（将以离线模式运行）: %s", e)

        # 启动巴法 → Zengge 鱼缸灯桥接（配置 BEMFA_UID 后自动启用）
        bemfa_bridge = None
        try:
            from ..integrations.bemfa_zengge import BemfaZenggeBridge
            bemfa_bridge = BemfaZenggeBridge.from_env()
            if bemfa_bridge is not None:
                await bemfa_bridge.start()
        except Exception as e:
            bemfa_bridge = None
            _LOGGER.error("巴法鱼缸灯桥接启动失败: %s", e)

        # 启动小智桥接
        xiaozhi_task = None
        if enable_xiaozhi:
            xiaozhi_task = asyncio.create_task(_xiaozhi_with_status(mcp_instance))
            _LOGGER.info("小智桥接已启动")

        # 启动 HomeKit 桥接
        homekit_bridge = None
        if enable_homekit and proxy is not None:
            try:
                from ..homekit import MiotHomeKitBridge
                homekit_bridge = MiotHomeKitBridge(proxy)
                await homekit_bridge.start()
                set_homekit_bridge(homekit_bridge)
                _LOGGER.info("HomeKit 桥接已启动")
            except Exception as e:
                _LOGGER.error("HomeKit 桥接启动失败: %s", e)
        elif enable_homekit:
            _LOGGER.warning("HomeKit 桥接需要 MIoT 连接，跳过")

        # 嵌套 MCP app 的 lifespan
        if enable_mcp and mcp_app is not None:
            async with mcp_app.lifespan(mcp_app):
                yield
        else:
            yield

        # 清理
        if homekit_bridge:
            await homekit_bridge.stop()
        if xiaozhi_task:
            xiaozhi_task.cancel()
        if bemfa_bridge:
            await bemfa_bridge.stop()
        await stop_persistent_callback_server()

    # API routes
    api_routes = [
        Route("/status", _status, methods=["GET"]),
        Route("/homekit/status", _homekit_status, methods=["GET"]),
        *all_api_routes(),
    ]

    # 组合所有路由
    routes = [
        Mount("/api", routes=api_routes),
    ]

    # 挂载 MCP (FastMCP streamable-http)
    if enable_mcp:
        try:
            from ..mcp.server import mcp as mcp_instance
            mcp_app = mcp_instance.http_app(path="/", transport="streamable-http")
            routes.append(Mount("/mcp", app=mcp_app))
            _LOGGER.info("MCP endpoint 已挂载 (/mcp)")
        except Exception as e:
            _LOGGER.warning("MCP 挂载失败: %s", e)

    # SPA fallback — 静态文件优先，未匹配路由返回 index.html
    async def _spa(request):
        path = request.path_params.get("path", "").lstrip("/") or "index.html"
        file_path = _STATIC_DIR / path
        if file_path.exists() and file_path.is_file():
            return FileResponse(file_path)
        index_path = _STATIC_DIR / "index.html"
        if index_path.exists():
            return FileResponse(index_path)
        return PlainTextResponse("Not Found", status_code=404)

    routes.append(Route("/{path:path}", _spa, methods=["GET"]))

    app = Starlette(routes=routes, lifespan=lifespan)
    return app
