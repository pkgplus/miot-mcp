# -*- coding: utf-8 -*-
"""MIoT 代理层 — 包装 MIoTClient，管理 token 刷新和设备控制。"""
import asyncio
import logging
import time
from typing import Optional
from uuid import uuid4

from miot.client import MIoTClient
from miot.types import (
    MIoTActionParam,
    MIoTDeviceInfo,
    MIoTGetPropertyParam,
    MIoTHomeInfo,
    MIoTManualSceneInfo,
    MIoTOauthInfo,
    MIoTSetPropertyParam,
)

from .auth import MIoTAuth
from .config import OAUTH_REDIRECT_URI, CLOUD_SERVER, CACHE_DIR, get_selected_home_ids

_LOGGER = logging.getLogger(__name__)

# Token 提前刷新阈值（秒）
TOKEN_REFRESH_MARGIN = 1800  # 30 分钟


class MiotProxy:
    """MIoT 客户端代理。"""

    def __init__(self):
        self._client: Optional[MIoTClient] = None
        self._oauth_info: Optional[MIoTOauthInfo] = None
        self._refresh_task: Optional[asyncio.Task] = None
        self._ready = False

    # ── 生命周期 ───────────────────────────────────

    async def init(self) -> None:
        """初始化并认证。"""
        self._oauth_info = MIoTAuth.load()
        if not self._oauth_info:
            raise RuntimeError(
                "未登录，请先运行: python -m miot_x login"
            )

        self._client = MIoTClient(
            uuid=uuid4().hex,
            redirect_uri=OAUTH_REDIRECT_URI,
            cache_path=str(CACHE_DIR),
            oauth_info=self._oauth_info,
            cloud_server=CLOUD_SERVER,
        )
        await self._client.init_async()

        # 检查 token 有效性并获取 user_info（mips_cloud 需要 uid）
        token_ok = await self._client.check_token_async()
        if not token_ok:
            _LOGGER.info("token 过期，尝试刷新...")
            await self._refresh_token()
        else:
            # check_token_async 只验证 token 有效性，
            # 但不会把 user_info 存到 _oauth_info。
            # V2 mips_cloud 需要 uid，这里显式调用获取并启动 mips_cloud。
            try:
                await self._client.get_user_info_async()
                # init_async 时因 oauth_info 无 user_info 跳过了 mips_cloud，
                # 现在有了 uid，手动触发
                await self._client._setup_mips_async()
                _LOGGER.info("mips_cloud 已启动（MQTT 实时推送）")
            except Exception as e:
                _LOGGER.warning("获取 user_info / 启动 mips_cloud 失败: %s", e)

        # 启动 token 自动刷新
        self._refresh_task = asyncio.create_task(self._auto_refresh_loop())

        self._ready = True
        _LOGGER.info("MiotProxy 初始化完成")

    async def deinit(self) -> None:
        """释放资源。"""
        self._ready = False
        if self._refresh_task:
            self._refresh_task.cancel()
            self._refresh_task = None
        if self._client:
            await self._client.deinit_async()
            self._client = None

    # ── Token 管理 ─────────────────────────────────

    async def _auto_refresh_loop(self) -> None:
        """后台定时刷新 token。"""
        while True:
            try:
                await asyncio.sleep(300)  # 每 5 分钟检查
                await self._refresh_if_needed()
            except asyncio.CancelledError:
                break
            except Exception as e:
                _LOGGER.warning("token 刷新检查异常: %s", e)
                await asyncio.sleep(60)

    async def _refresh_if_needed(self) -> None:
        """如果 token 快过期，提前刷新。"""
        if not self._oauth_info:
            return
        remaining = self._oauth_info.expires_ts - int(time.time())
        if remaining < TOKEN_REFRESH_MARGIN:
            _LOGGER.info("token 将在 %ds 后过期，刷新中...", remaining)
            await self._refresh_token()

    async def _refresh_token(self) -> None:
        """刷新 access token。"""
        if not self._oauth_info or not self._client:
            raise RuntimeError("无法刷新 token：未初始化")

        self._oauth_info = await self._client.refresh_access_token_async(
            refresh_token=self._oauth_info.refresh_token
        )
        MIoTAuth().save(self._oauth_info)
        _LOGGER.info("token 已刷新，新过期时间: %s", self._oauth_info.expires_ts)

    @property
    def is_ready(self) -> bool:
        return self._ready

    # ── 家庭/房间 ──────────────────────────────────

    async def get_homes(self) -> dict[str, MIoTHomeInfo]:
        all_homes = await self._client.get_homes_async()
        selected = get_selected_home_ids()
        if selected is None:
            return all_homes
        return {k: v for k, v in all_homes.items() if v.home_id in selected}

    # ── 设备 ───────────────────────────────────────

    async def get_devices(self) -> dict[str, MIoTDeviceInfo]:
        all_devices = await self._client.get_devices_async()
        selected = get_selected_home_ids()
        if selected is None:
            return all_devices
        # 按家庭过滤：只返回所选家庭中房间里的设备
        homes = await self._client.get_homes_async()
        allowed_dids = set()
        for home in homes.values():
            if home.home_id not in selected:
                continue
            if home.room_list:
                for rinfo in home.room_list.values():
                    allowed_dids.update(rinfo.dids)
        return {k: v for k, v in all_devices.items() if k in allowed_dids}

    async def get_prop(self, did: str, siid: int, piid: int):
        """读取设备属性。"""
        param = MIoTGetPropertyParam(did=did, siid=siid, piid=piid)
        return await self._client.http_client.get_prop_async(param)

    async def set_prop(self, did: str, siid: int, piid: int, value):
        """设置设备属性。"""
        param = MIoTSetPropertyParam(did=did, siid=siid, piid=piid, value=value)
        return await self._client.http_client.set_prop_async(param)

    async def action(self, did: str, siid: int, aiid: int, in_list: list = None):
        """执行设备动作。"""
        param = MIoTActionParam(did=did, siid=siid, aiid=aiid, in_=in_list or [])
        return await self._client.http_client.action_async(param)

    # ── 开关（自动适配 IR 虚拟设备）─────────────────

    @staticmethod
    def _is_ir_device(model: str) -> bool:
        """判断是否为 IR 虚拟设备（需用 action 而非 set_prop 控制开关）。"""
        return model.startswith("miir.")

    async def set_power(self, did: str, model: str, on: bool):
        """开关设备 — IR 虚拟设备用 action，普通设备用 set_prop。"""
        if self._is_ir_device(model):
            aiid = 5 if on else 6  # miot 电视 IR 标准: aiid=5 开机, aiid=6 关机
            return await self.action(did, siid=2, aiid=aiid)
        else:
            return await self.set_prop(did, siid=2, piid=1, value=on)

    # ── 场景 ───────────────────────────────────────

    async def get_scenes(self) -> dict[str, MIoTManualSceneInfo]:
        all_scenes = await self._client.get_manual_scenes_async()
        selected = get_selected_home_ids()
        if selected is None:
            return all_scenes
        return {k: v for k, v in all_scenes.items() if v.home_id in selected}

    async def run_scene(self, scene_info: MIoTManualSceneInfo) -> bool:
        return await self._client.run_manual_scene_async(scene_info=scene_info)


# ── 共享单例 ───────────────────────────────────────

_shared_proxy: MiotProxy | None = None
_proxy_lock: asyncio.Lock | None = None


def _get_lock():
    global _proxy_lock
    if _proxy_lock is None:
        _proxy_lock = asyncio.Lock()
    return _proxy_lock


async def get_shared_proxy() -> MiotProxy:
    """获取全局共享的 MiotProxy 实例（懒初始化，加锁防并发）。"""
    global _shared_proxy
    async with _get_lock():
        if _shared_proxy is None or not _shared_proxy.is_ready:
            _shared_proxy = MiotProxy()
            await _shared_proxy.init()
    return _shared_proxy


async def reset_shared_proxy():
    """登录后刷新 proxy 的 token（不销毁重建，避免 camera dylib segfault）。"""
    global _shared_proxy
    async with _get_lock():
        if _shared_proxy and _shared_proxy._client:
            # 重新加载 token 并刷新 http client header
            oauth_info = MIoTAuth.load()
            if oauth_info and oauth_info.access_token:
                _shared_proxy._oauth_info = oauth_info
                _shared_proxy._client._oauth_info = oauth_info
                _shared_proxy._client._http_client.update_http_header(
                    access_token=oauth_info.access_token)
                _LOGGER.info("proxy token 已刷新（无需重建连接）")
        else:
            # proxy 尚未初始化，标记为 None 让下次请求重建
            _shared_proxy = None
