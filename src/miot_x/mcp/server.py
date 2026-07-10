# -*- coding: utf-8 -*-
"""MCP Server — FastMCP 工具注册 + 多模式启动。"""
import asyncio
import logging
import sys
from typing import Optional

from fastmcp import FastMCP
from miot.types import MIoTDeviceInfo

from ..lib.proxy import MiotProxy

_LOGGER = logging.getLogger(__name__)

_proxy: Optional[MiotProxy] = None
_devices: dict[str, MIoTDeviceInfo] = {}

mcp = FastMCP(
    name="miot-x",
    instructions="Xiaomi Mijia Smart Home MCP Server — 控制米家智能设备",
)


# ── 初始化 ────────────────────────────────────────

async def _ensure_proxy():
    global _proxy, _devices
    if _proxy is not None:
        return
    _proxy = MiotProxy()
    await _proxy.init()
    _devices = await _proxy.get_devices()
    _LOGGER.info("已连接，设备数: %d", len(_devices))


def _find_device(name_or_id: str) -> MIoTDeviceInfo | None:
    if not name_or_id:
        return None
    if name_or_id in _devices:
        return _devices[name_or_id]
    for d in _devices.values():
        if name_or_id.lower() in d.name.lower():
            return d
    for d in _devices.values():
        cls = d.model.split(".")[1] if "." in d.model else ""
        if name_or_id.lower() in cls.lower():
            return d
    return None


# ── 设备列表 ───────────────────────────────────────

@mcp.tool()
async def list_devices(room: str = "", refresh: bool = False) -> dict:
    """获取所有米家设备列表。

    Args:
        room: 可选，按房间名筛选。
        refresh: 强制刷新。

    Returns:
        {"total": N, "devices": [{did, name, model, online, room}, ...]}
    """
    global _devices
    await _ensure_proxy()

    if refresh:
        _devices = await _proxy.get_devices()

    homes = await _proxy.get_homes()
    result = []
    for did, dev in _devices.items():
        dev_room = ""
        for home in homes.values():
            for rid, rinfo in home.room_list.items():
                if did in rinfo.dids:
                    dev_room = rinfo.room_name
                    break
        if room and room not in dev_room:
            continue
        result.append({
            "did": dev.did,
            "name": dev.name,
            "model": dev.model,
            "online": dev.online,
            "room": dev_room,
        })

    return {"total": len(result), "devices": result}


@mcp.tool()
async def get_device(device_name: str) -> dict:
    """获取指定设备的详细信息，含 SPEC 定义。

    Args:
        device_name: 设备名称或 did（支持模糊匹配）。
    """
    await _ensure_proxy()
    dev = _find_device(device_name)
    if not dev:
        return {"error": f"未找到设备: {device_name}"}

    spec = None
    if _proxy._client and _proxy._client.spec_parser:
        try:
            urn = dev.urn if hasattr(dev, 'urn') and dev.urn else dev.model
            spec_raw = await _proxy._client.spec_parser.parse_async(urn)
            if spec_raw:
                spec = {
                    "type": spec_raw.type,
                    "services": [
                        {
                            "iid": s.iid,
                            "name": s.description,
                            "properties": [
                                {"iid": p.iid, "name": p.description, "format": p.format,
                                 "access": p.access}
                                for p in s.properties
                            ],
                            "actions": [
                                {"iid": a.iid, "name": a.description,
                                 "in": [ai.description for ai in a.in_def]}
                                for a in s.actions
                            ],
                        }
                        for s in spec_raw.services
                    ],
                }
        except Exception:
            pass

    return {
        "did": dev.did,
        "name": dev.name,
        "model": dev.model,
        "online": dev.online,
        "lan_online": getattr(dev, "lan_online", None),
        "spec": spec,
        "raw": dev.model_dump() if hasattr(dev, "model_dump") else {},
    }


# ── 设备控制 ───────────────────────────────────────

@mcp.tool()
async def device_on(device_name: str) -> dict:
    """打开指定设备（通用开关 siid=2, piid=1）。

    Args:
        device_name: 设备名称或 did（支持模糊匹配）。
    """
    await _ensure_proxy()
    dev = _find_device(device_name)
    if not dev:
        return {"error": f"未找到设备: {device_name}"}
    result = await _proxy.set_power(dev.did, dev.model, True)
    return {"did": dev.did, "name": dev.name, "result": result}


@mcp.tool()
async def device_off(device_name: str) -> dict:
    """关闭指定设备（通用开关 siid=2, piid=1；IR 虚拟设备用 action）。

    Args:
        device_name: 设备名称或 did（支持模糊匹配）。
    """
    await _ensure_proxy()
    dev = _find_device(device_name)
    if not dev:
        return {"error": f"未找到设备: {device_name}"}
    result = await _proxy.set_power(dev.did, dev.model, False)
    return {"did": dev.did, "name": dev.name, "result": result}


@mcp.tool()
async def device_toggle(device_name: str) -> dict:
    """切换设备开关状态（先读后写）。

    Args:
        device_name: 设备名称或 did（支持模糊匹配）。
    """
    await _ensure_proxy()
    dev = _find_device(device_name)
    if not dev:
        return {"error": f"未找到设备: {device_name}"}
    # IR 虚拟设备：直接发送开机（无法读取状态）
    if dev.model.startswith("miir."):
        result = await _proxy.action(dev.did, siid=2, aiid=5)
        return {"did": dev.did, "name": dev.name, "action": "toggle_ir", "result": result}
    current = await _proxy.get_prop(dev.did, siid=2, piid=1)
    new_val = not current if isinstance(current, bool) else True
    result = await _proxy.set_prop(dev.did, siid=2, piid=1, value=new_val)
    return {"did": dev.did, "name": dev.name, "from": current, "to": new_val, "result": result}


# ── 属性读写 ───────────────────────────────────────

@mcp.tool()
async def get_prop(device_name: str, siid: int, piid: int) -> dict:
    """读取设备属性。

    Args:
        device_name: 设备名称或 did。
        siid: 服务实例 ID。
        piid: 属性实例 ID。
    """
    await _ensure_proxy()
    dev = _find_device(device_name)
    if not dev:
        return {"error": f"未找到设备: {device_name}"}
    value = await _proxy.get_prop(dev.did, siid=siid, piid=piid)
    return {"did": dev.did, "name": dev.name, "siid": siid, "piid": piid, "value": value}


@mcp.tool()
async def set_prop(device_name: str, siid: int, piid: int, value) -> dict:
    """设置设备属性。

    Args:
        device_name: 设备名称或 did。
        siid: 服务实例 ID。
        piid: 属性实例 ID。
        value: 属性值（bool/int/float/str）。
    """
    await _ensure_proxy()
    dev = _find_device(device_name)
    if not dev:
        return {"error": f"未找到设备: {device_name}"}
    result = await _proxy.set_prop(dev.did, siid=siid, piid=piid, value=value)
    return {"did": dev.did, "name": dev.name, "siid": siid, "piid": piid, "value": value, "result": result}


@mcp.tool()
async def device_action(device_name: str, siid: int, aiid: int, in_list: list = None) -> dict:
    """执行设备动作。

    Args:
        device_name: 设备名称或 did。
        siid: 服务实例 ID。
        aiid: 动作实例 ID。
        in_list: 动作输入参数列表。
    """
    await _ensure_proxy()
    dev = _find_device(device_name)
    if not dev:
        return {"error": f"未找到设备: {device_name}"}
    result = await _proxy.action(dev.did, siid=siid, aiid=aiid, in_list=in_list)
    return {"did": dev.did, "name": dev.name, "siid": siid, "aiid": aiid, "result": result}


# ── 场景 ───────────────────────────────────────────

@mcp.tool()
async def list_scenes(refresh: bool = False) -> dict:
    """获取手动场景列表。

    Args:
        refresh: 强制刷新。
    """
    await _ensure_proxy()
    scenes = await _proxy.get_scenes()
    result = [
        {"scene_id": s.scene_id, "name": s.scene_name, "home_id": s.home_id}
        for s in scenes.values()
    ]
    return {"total": len(result), "scenes": result}


@mcp.tool()
async def execute_scene(scene_name: str) -> dict:
    """执行指定场景（按名称模糊匹配）。

    Args:
        scene_name: 场景名称（支持模糊匹配）。
    """
    await _ensure_proxy()
    scenes = await _proxy.get_scenes()
    for s in scenes.values():
        if scene_name.lower() in s.scene_name.lower():
            ok = await _proxy.run_scene(s)
            return {"scene_id": s.scene_id, "name": s.scene_name, "success": ok}
    return {"error": f"未找到场景: {scene_name}"}


# ── 服务状态 ───────────────────────────────────────

@mcp.tool()
async def get_service_status() -> dict:
    """获取 MCP 服务连接状态。"""
    try:
        await _ensure_proxy()
        return {
            "connected": True,
            "device_count": len(_devices),
            "ready": _proxy.is_ready if _proxy else False,
        }
    except Exception as e:
        return {"connected": False, "error": str(e)}


# ── 入口 ───────────────────────────────────────────

async def main(
    http_port: int = 0,
    http_host: str = "127.0.0.1",
    enable_xiaozhi: bool = False,
):
    """启动 MCP server。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        stream=sys.stderr,
    )

    try:
        await _ensure_proxy()
        _LOGGER.info("MCP server 启动中...")
    except Exception as e:
        _LOGGER.warning("设备连接失败（将以离线模式启动）: %s", e)

    tasks = []

    if enable_xiaozhi:
        from .xiaozhi import xiaozhi_bridge
        tasks.append(xiaozhi_bridge(mcp))

    if http_port > 0:
        _LOGGER.info("HTTP MCP server: http://%s:%d", http_host, http_port)
        tasks.append(mcp.run_http_async(
            host=http_host,
            port=http_port,
            transport="http",
            show_banner=False,
        ))

    if tasks:
        await asyncio.gather(*tasks)
    else:
        await mcp.run_stdio_async()


if __name__ == "__main__":
    asyncio.run(main())
