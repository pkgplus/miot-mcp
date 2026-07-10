# -*- coding: utf-8 -*-
"""HomeKit Bridge 管理器。

基于 HAP-python AccessoryDriver，将 miot-x 中的所有设备公开为 HomeKit 配件。
管理配对状态持久化和桥接生命周期。
"""
import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Optional

from pyhap.accessory import Bridge
from pyhap.accessory_driver import AccessoryDriver
from pyhap.const import CATEGORY_BRIDGE
from pyhap import SUPPORT_QR_CODE

from ..lib.config import CACHE_DIR, get_selected_home_ids
from .accessory import MiotAccessory, get_mapping

_LOGGER = logging.getLogger(__name__)

# 配对数据持久化路径
_PERSIST_PATH = Path(os.path.expanduser("~/.miot-x/homekit.state"))

# HomeKit 桥接名称
_BRIDGE_NAME = "miot-x Bridge"

# 默认配对 PIN（可配置）
_DEFAULT_PIN = "123-45-678"

# HAP 服务端口（默认 51234，避免与 Web UI 冲突）
_HAP_PORT = 51828


class MiotHomeKitBridge:
    """miot-x HomeKit 桥接。

    将米家设备作为 HAP 配件暴露，让 iPhone/iPad 的家庭 App 可控。
    """

    def __init__(
        self,
        proxy,                  # MiotProxy
        pin: str = _DEFAULT_PIN,
        bridge_name: str = _BRIDGE_NAME,
        persist_path: str = str(_PERSIST_PATH),
        hap_port: int = _HAP_PORT,
    ):
        self._proxy = proxy
        self._pin = pin
        self._bridge_name = bridge_name
        self._persist_path = persist_path
        self._hap_port = hap_port

        self._driver: Optional[AccessoryDriver] = None
        self._bridge: Optional[Bridge] = None
        self._accessories: list[MiotAccessory] = []
        self._paired = False
        # 独立 TV 配件（Television 必须 non-bridged）
        self._tv_drivers: list[AccessoryDriver] = []
        self._qr_url: Optional[str] = None

        # 配对状态回调
        self._on_paired_callback: Optional[callable] = None

    # ── 生命周期 ────────────────────────────────────

    async def start(self) -> None:
        """启动 HomeKit 桥接服务。

        1. 加载或创建配对状态
        2. 获取米家设备列表
        3. 将可映射设备创建为 HAP 配件
        4. 启动 AccessoryDriver
        """
        import logging as _logging
        _logging.getLogger("pyhap").setLevel(_logging.DEBUG)
        _LOGGER.info("🏠 启动 HomeKit 桥接...")

        # 获取当前事件循环（Starlette/uvicorn 的）
        loop = asyncio.get_running_loop()

        # 创建 driver，使用当前事件循环和固定 PIN
        self._driver = AccessoryDriver(
            persist_file=self._persist_path,
            pincode=self._pin.encode(),
            port=self._hap_port,
            loop=loop,
        )

        # 创建桥接
        self._bridge = Bridge(self._driver, self._bridge_name)

        # 设置配对信息
        self._bridge.set_info_service(
            manufacturer="miot-x",
            model="HomeKit Bridge",
            serial_number="miot-x-hk-001",
            firmware_revision="0.1.0",
        )

        # 添加设备为配件
        await self._add_devices()

        self._driver.add_accessory(self._bridge)

        # 检查是否已配对
        self._check_pairing()

        # 生成 QR 码（未配对时）
        if not self._paired:
            self._generate_qr()

        # 启动 HAP driver
        try:
            await self._driver.async_start()
        except Exception as e:
            _LOGGER.error("HomeKit 启动失败: %s", e, exc_info=True)
            raise

        _LOGGER.info("HomeKit 桥接已启动（%d 个配件，端口 %d）",
                     len(self._accessories), self._hap_port)

    async def stop(self) -> None:
        """停止桥接。"""
        _LOGGER.info("🛑 停止 HomeKit 桥接...")
        for acc in self._accessories:
            await acc.stop_polling()
        for tv_drv in self._tv_drivers:
            tv_drv.stop()
        if self._driver:
            self._driver.stop()
        _LOGGER.info("HomeKit 桥接已停止")

    # ── 设备管理 ────────────────────────────────────

    async def _add_devices(self) -> None:
        """获取所有设备并创建 HAP 配件。

        AID 稳定性：按设备绑定时间 (order_time) 排序，确保相同的设备集合
        在每次重启后获得相同的 AID 分配。新增设备自动排末尾。
        """
        devices = await self._proxy.get_devices()

        added = 0
        skipped = 0
        # 按绑定时间排序（同时间按 did 排序），确保 AID 分配稳定
        sorted_devices = sorted(
            devices.items(),
            key=lambda x: (getattr(x[1], 'order_time', 0), x[0])
        )
        tv_port = self._hap_port + 1  # TV 独立端口从 bridge port + 1 开始

        for did, dev in sorted_devices:
            try:
                mapping = get_mapping(dev.model)
                if not mapping:
                    skipped += 1
                    _LOGGER.debug("跳过未映射设备: %s (%s)", dev.name, dev.model)
                    continue

                # Television 必须作为独立配件（Apple 要求）
                if mapping.service_name == "Television":
                    await self._add_standalone_tv(dev, mapping, tv_port)
                    tv_port += 1
                    added += 1
                    continue

                # 多通道设备：每个通道创建独立配件
                if mapping.channels:
                    for ch_name, ch_siid, ch_piid in mapping.channels:
                        try:
                            ch_acc = MiotAccessory(
                                self._driver, self._proxy, dev, mapping=mapping,
                                channel_name=ch_name,
                                channel_siid=ch_siid,
                                channel_piid=ch_piid,
                            )
                            self._bridge.add_accessory(ch_acc)
                            self._accessories.append(ch_acc)
                            if ch_acc._is_sensor():
                                await ch_acc.start_polling()
                            else:
                                await ch_acc.fetch_initial_state()
                            added += 1
                            _LOGGER.debug("已添加通道: %s → %s", ch_name, mapping.service_name)
                        except Exception as e:
                            _LOGGER.warning("添加通道 %s 失败: %s", ch_name, e)
                else:
                    # 单通道设备
                    acc = MiotAccessory(
                        self._driver, self._proxy, dev, mapping=mapping,
                    )
                    self._bridge.add_accessory(acc)
                    self._accessories.append(acc)
                    if acc._is_sensor():
                        await acc.start_polling()
                    else:
                        await acc.fetch_initial_state()
                    added += 1
                    _LOGGER.debug("已添加: %s → %s", dev.name, mapping.service_name)

            except Exception as e:
                _LOGGER.warning("添加设备 %s 失败: %s", dev.name, e)
                skipped += 1

        _LOGGER.info("设备映射: %d 已添加, %d 跳过, %d 独立TV", added, skipped, len(self._tv_drivers))

    async def _add_standalone_tv(self, dev, mapping, port: int):
        """将 Television 设备作为独立 HAP 配件发布（非 Bridge 子配件）。"""
        loop = asyncio.get_running_loop()
        persist = str(Path(self._persist_path).parent / f"homekit_tv_{dev.did}.state")
        pin = "876-54-321"  # TV 独立配对码

        tv_driver = AccessoryDriver(
            persist_file=persist,
            pincode=pin.encode(),
            port=port,
            loop=loop,
        )

        tv_acc = MiotAccessory(tv_driver, self._proxy, dev, mapping=mapping)
        tv_driver.add_accessory(tv_acc)
        self._accessories.append(tv_acc)

        try:
            await tv_driver.async_start()
            self._tv_drivers.append(tv_driver)
            _LOGGER.info("📺 独立 TV 配件已启动: %s (端口 %d, PIN %s)", dev.name, port, pin)
        except Exception as e:
            _LOGGER.error("TV 独立配件启动失败: %s", e)

    # ── 配对状态 ────────────────────────────────────

    def _check_pairing(self):
        """检查是否已有实际配对客户端。"""
        if os.path.exists(self._persist_path):
            try:
                import json
                with open(self._persist_path, "r") as f:
                    data = json.loads(f.read())
                clients = data.get("paired_clients", {})
                if clients and len(clients) > 0:
                    self._paired = True
                    _LOGGER.info("已有配对记录 — %d 个客户端", len(clients))
                    return
                else:
                    _LOGGER.info("状态文件存在但无配对客户端，将重新生成配对码")
            except Exception:
                pass
        self._paired = False

    def _generate_qr(self):
        """生成配对二维码。"""
        if SUPPORT_QR_CODE and self._bridge:
            xhm_uri = self._bridge.xhm_uri()
            self._qr_url = xhm_uri
            _LOGGER.info("配对码: %s", self._pin)
            _LOGGER.info("扫码 URI: %s", xhm_uri)
            # 打印终端的 QR 码 + 提示
            self._bridge.setup_message()
        else:
            _LOGGER.info("配对码: %s（安装 pyqrcode 可显示二维码）", self._pin)

    # ── 查询接口 ────────────────────────────────────

    @property
    def is_paired(self) -> bool:
        return self._paired

    @property
    def pin(self) -> str:
        return self._pin

    @property
    def qr_url(self) -> Optional[str]:
        return self._qr_url

    @property
    def accessory_count(self) -> int:
        return len(self._accessories)

    def get_accessories(self) -> list:
        return [
            {
                "name": a.device_name,
                "did": a.did,
                "service": a._service_name,
                "online": a.is_online,
            }
            for a in self._accessories
        ]

    def set_on_paired_callback(self, callback):
        """设置配对完成回调。"""
        self._on_paired_callback = callback
