# -*- coding: utf-8 -*-
"""miot 设备 → HAP 配件包装器。

将 MiotProxy 中的设备封装为 HAP-python Accessory，桥接 miot siid/piid
与 HomeKit characteristic 之间的读写操作。
"""
import asyncio
import logging
from typing import Any, Optional

from pyhap.accessory import Accessory
from pyhap.const import CATEGORY_LIGHTBULB, CATEGORY_SWITCH, CATEGORY_OUTLET, \
    CATEGORY_FAN, CATEGORY_WINDOW_COVERING, CATEGORY_AIR_PURIFIER, \
    CATEGORY_THERMOSTAT, CATEGORY_SENSOR, CATEGORY_DOOR_LOCK, \
    CATEGORY_TELEVISION, CATEGORY_SPEAKER
from pyhap.loader import Loader

from .mappers import DeviceMapping, get_mapping

_LOGGER = logging.getLogger(__name__)
_loader = Loader()

# ── 类别映射 ─────────────────────────────────────────

_CATEGORY_MAP: dict[str, int] = {
    "Lightbulb": CATEGORY_LIGHTBULB,
    "Switch": CATEGORY_SWITCH,
    "Outlet": CATEGORY_OUTLET,
    "Fan": CATEGORY_FAN,
    "WindowCovering": CATEGORY_WINDOW_COVERING,
    "AirPurifier": CATEGORY_AIR_PURIFIER,
    "HeaterCooler": CATEGORY_THERMOSTAT,
    "TemperatureSensor": CATEGORY_SENSOR,
    "HumiditySensor": CATEGORY_SENSOR,
    "MotionSensor": CATEGORY_SENSOR,
    "ContactSensor": CATEGORY_SENSOR,
    "LockMechanism": CATEGORY_DOOR_LOCK,
    "Television": CATEGORY_TELEVISION,
    "TelevisionSpeaker": CATEGORY_SPEAKER,
    "Speaker": CATEGORY_SPEAKER,
}


class MiotAccessory(Accessory):
    """单个米家设备的 HomeKit 配件。"""

    # 传感器默认轮询间隔（秒）
    SENSOR_POLL_INTERVAL = 30
    # 控制类设备轮询间隔（秒）— 较低频率，仅用于同步外部状态变化
    CONTROL_POLL_INTERVAL = 60

    def __init__(
        self,
        driver,
        proxy,          # MiotProxy
        device_info,     # MIoTDeviceInfo
        mapping: Optional[DeviceMapping] = None,
        channel_siid: int | None = None,
        channel_piid: int | None = None,
        channel_name: str | None = None,
        **kwargs,
    ):
        self._proxy = proxy
        self._dev = device_info

        # 自动匹配映射
        self._map = mapping or get_mapping(device_info.model)
        if not self._map:
            raise ValueError(f"无法映射设备到 HomeKit: {device_info.model}")

        self._service_name = self._map.service_name

        # 通道覆写（多通道设备用）
        if channel_siid is not None:
            self._map.on_siid = channel_siid
        if channel_piid is not None:
            self._map.on_piid = channel_piid

        display_name = channel_name or device_info.name or f"Miot {device_info.model}"
        super().__init__(driver, display_name)

        # 设置类别（HAP-python 5.x 通过属性设置）
        cat = _CATEGORY_MAP.get(self._service_name, CATEGORY_SWITCH)
        self.category = cat

        # 标记是否在线
        self._online = getattr(device_info, "online", True)

        # 构建服务
        self._primary_service = self._build_primary_service()
        self.set_primary_service(self._primary_service)

        # 额外服务（如温湿度传感器同时有温度和湿度）
        self._extra_services: list = []
        if self._map.extra_services:
            for es_name in self._map.extra_services:
                extra = self._build_sensor_service(es_name)
                if extra:
                    self._extra_services.append(extra)

        # 传感器轮询任务
        self._poll_task: Optional[asyncio.Task] = None
        # 初始状态读取任务
        self._init_task: Optional[asyncio.Task] = None

    # ── 主服务构建 ──────────────────────────────────

    def _build_primary_service(self):
        """根据服务类型构建主 HAP Service 及可选的附加 characteristic。"""
        svc = self.add_preload_service(self._service_name)

        # 亮度（可选）
        if self._map.brightness_siid is not None:
            char = self._add_opt_char(svc, "Brightness")
            if char:
                self._bind_char(char, "brightness")

        # 色温（可选）
        if self._map.color_temp_siid is not None:
            char = self._add_opt_char(svc, "ColorTemperature")
            if char:
                self._bind_char(char, "color_temp")

        # 风扇速度（可选）
        if self._map.speed_siid is not None:
            char = self._add_opt_char(svc, "RotationSpeed")
            if char:
                self._bind_char(char, "speed")

        # 绑定主开关（On / Active / TargetPosition 等）
        self._bind_primary_char(svc)

        # 目标温度（可选）
        if self._map.target_temp_siid is not None:
            char = self._add_opt_char(svc, "CoolingThresholdTemperature")
            if char:
                self._bind_char(char, "target_temp")

        # Television 专属初始化
        if self._service_name == "Television":
            # 设置必须的初始值（iOS 要求非 None，否则报"失去连接"）
            svc.configure_char("Active", value=0)
            svc.configure_char("ActiveIdentifier", value=1)
            svc.configure_char("SleepDiscoveryMode", value=1)  # Always Discoverable
            svc.configure_char("ConfiguredName", value=self.display_name)

            # RemoteKey（遥控器按键）
            if self._map.actions:
                rk = self._add_opt_char(svc, "RemoteKey")
                if rk:
                    self._bind_remote_key(rk)

            # InputSource（iOS 要求至少一个输入源才显示遥控器）
            input_svc = self.add_preload_service("InputSource")
            input_svc.configure_char("ConfiguredName", value="HDMI 1")
            input_svc.configure_char("InputSourceType", value=3)  # HDMI
            input_svc.configure_char("IsConfigured", value=1)  # Configured
            input_svc.configure_char("CurrentVisibilityState", value=0)  # Shown
            ident = self._add_opt_char(input_svc, "Identifier")
            if ident:
                ident.set_value(1, should_notify=False)
            svc.add_linked_service(input_svc)

        # 音箱音量（可选）
        if self._service_name in ("TelevisionSpeaker", "Speaker") and self._map.volume_siid is not None:
            vol = self._add_opt_char(svc, "Volume")
            if vol:
                self._bind_char(vol, "volume")

        return svc

    def _bind_primary_char(self, svc):
        """绑定主控制 characteristic。"""
        service = self._service_name

        if service in ("Lightbulb", "Switch", "Outlet", "Fan"):
            char = svc.get_characteristic("On")
            self._bind_char(char, "on")

        elif service == "WindowCovering":
            char = svc.get_characteristic("TargetPosition")
            self._bind_char(char, "position")

        elif service in ("AirPurifier", "HeaterCooler", "Television"):
            char = svc.get_characteristic("Active")
            if service == "Television" and self._map.actions and "power_on" in self._map.actions:
                # IR 虚拟电视：用 action (aiid=5/6) 而非 set_prop 控制开关
                self._bind_active_via_action(char)
            else:
                self._bind_char(char, "on")

        elif service in ("TelevisionSpeaker", "Speaker"):
            # Speaker / TelevisionSpeaker: Mute(静音) + Active(开关)
            char = svc.get_characteristic("Mute")
            self._bind_char(char, "mute")
            # Active 是 TelevisionSpeaker 原生可选特性，手动添加
            active = self._add_opt_char(svc, "Active")
            if active:
                self._bind_char(active, "on")

    # ── 传感器附加服务 ──────────────────────────────

    def _build_sensor_service(self, service_name: str):
        """为传感器构建额外 Service（如 HumiditySensor）。"""
        try:
            svc = self.add_preload_service(service_name)

            if service_name == "HumiditySensor":
                char = svc.get_characteristic("CurrentRelativeHumidity")
                self._bind_char(char, "humidity")

            elif service_name == "MotionSensor":
                char = svc.get_characteristic("MotionDetected")
                self._bind_char(char, "motion")

            elif service_name == "ContactSensor":
                char = svc.get_characteristic("ContactSensorState")
                self._bind_char(char, "contact")

            elif service_name == "LockMechanism":
                char = svc.get_characteristic("LockCurrentState")
                self._bind_char(char, "lock")

            elif service_name == "TelevisionSpeaker":
                # Volume 是可选 characteristic，需要手动添加
                vol = self._add_opt_char(svc, "Volume")
                if vol:
                    self._bind_char(vol, "volume")

            return svc
        except Exception as e:
            _LOGGER.warning("无法创建额外服务 %s: %s", service_name, e)
            return None

    # ── Characteristic 绑定 ─────────────────────────

    def _add_opt_char(self, svc, char_name: str):
        """添加可选 characteristic 到服务并设置 broker。"""
        try:
            char_obj = _loader.get_char(char_name)
            if char_obj:
                svc.add_characteristic(char_obj)
                new_char = svc.get_characteristic(char_name)
                # 手动设置 broker 并分配 IID
                # （add_service 之后添加的 characteristic 不会自动分配 broker 和 IID）
                new_char.broker = self
                self.iid_manager.assign(new_char)
                return new_char
        except Exception as e:
            _LOGGER.debug("添加可选 characteristic %s 失败: %s", char_name, e)
        return None

    def _bind_char(self, char, prop_kind: str, inverted: bool = False):
        """绑定 characteristic 到 miot 属性读写。

        prop_kind 映射到 DeviceMapping 中的 siid/piid 字段：
        - 'on'      → on_siid / on_piid
        - 'brightness' → brightness_siid / brightness_piid
        - 'color_temp' → color_temp_siid / color_temp_piid
        - 'speed'   → speed_siid / speed_piid
        - 'temperature' → temperature_siid / temperature_piid
        - 'humidity' → humidity_siid / humidity_piid
        - 'position' → position_siid / position_piid
        - 'target_temp' → target_temp_siid / target_temp_piid
        - 'motion'   → motion_siid / motion_piid
        - 'contact'  → contact_siid / contact_piid

        inverted: 反转 HomeKit 值（如 Mute → on: True=关/False=开）
        """
        siid_key = f"{prop_kind}_siid"
        piid_key = f"{prop_kind}_piid"

        siid = getattr(self._map, siid_key, None)
        piid = getattr(self._map, piid_key, None)

        if siid is None or piid is None:
            _LOGGER.debug("设备 %s 未配置 %s 属性", self._dev.name, prop_kind)
            return

        # 写回调：HAP-python 的 setter_callback 是同步的，用 create_task 调度异步写
        char.setter_callback = self._make_setter(siid, piid, prop_kind, inverted)

        # 读回调：HAP-python v5 的 getter_callback 是同步的，不支持 async。
        # 传感器值通过轮询更新，控制设备通过 _get_initial 设置默认值。
        # 不设置 getter_callback，避免 "coroutine is not numeric" 错误。

    def _make_setter(self, siid: int, piid: int, prop_kind: str, inverted: bool = False):
        """创建写回调 — HomeKit 修改值 → miot set_prop（同步调度异步任务）。"""

        def setter(value):
            if not self._dev.online:
                _LOGGER.warning("设备 %s 离线，跳过写入", self._dev.name)
                return

            # 反转值（如 Speaker Mute: True=静音→miot关, False=播放→miot开）
            if inverted:
                value = not value

            miot_value = self._to_miot_value(prop_kind, value)
            if miot_value is None:
                return

            async def _do_set():
                try:
                    result = await self._proxy.set_prop(
                        self._dev.did, siid=siid, piid=piid, value=miot_value
                    )
                    _LOGGER.info(
                        "HomeKit → miot: %s siid=%d piid=%d val=%s → %s",
                        self._dev.name, siid, piid, miot_value, result,
                    )
                except Exception as e:
                    _LOGGER.error("写入 %s 失败: %s", self._dev.name, e)

            asyncio.create_task(_do_set())

        return setter

    def _make_getter(self, siid: int, piid: int, prop_kind: str):
        """创建读回调 — HomeKit 读取值 → miot get_prop。"""

        async def getter():
            try:
                value = await self._proxy.get_prop(
                    self._dev.did, siid=siid, piid=piid
                )
                return self._from_miot_value(prop_kind, value)
            except Exception as e:
                _LOGGER.error("读取 %s siid=%d piid=%d 失败: %s",
                              self._dev.name, siid, piid, e)
                return None

        return getter

    # ── 电视遥控器 (RemoteKey → IR Action) ──────────

    # HomeKit RemoteKey 值 → IR 动作名映射
    _REMOTE_KEY_MAP: dict[int, str] = {
        4: "channel_up",      # ArrowUp
        5: "channel_down",    # ArrowDown
        6: "volume_down",     # ArrowLeft
        7: "volume_up",       # ArrowRight
        8: "input_source",    # Select
        11: "mute_toggle",    # PlayPause
    }

    _mute_state: bool = False  # 追踪静音状态

    def _bind_active_via_action(self, char):
        """绑定 Active → IR 电源 action（IR 虚拟设备用 action 而非 set_prop）。"""
        on_siid, on_aiid = self._map.actions.get("power_on", (None, None))
        off_siid, off_aiid = self._map.actions.get("power_off", (None, None))

        def active_setter(value):
            aiid = on_aiid if value else off_aiid
            siid = on_siid if value else off_siid
            if siid is None or aiid is None:
                return

            async def _do_action():
                try:
                    result = await self._proxy.action(
                        self._dev.did, siid=siid, aiid=aiid, in_list=[]
                    )
                    _LOGGER.info(
                        "HomeKit IR Power → miot: %s %s → %s",
                        self._dev.name, "on" if value else "off", result,
                    )
                except Exception as e:
                    _LOGGER.error("IR power %s 失败: %s",
                                  "on" if value else "off", e)

            asyncio.create_task(_do_action())

        char.setter_callback = active_setter

    def _bind_remote_key(self, char):
        """绑定 RemoteKey → IR 动作（同步调度异步任务）。"""

        def remote_key_setter(value):
            action_name = self._REMOTE_KEY_MAP.get(value)
            if not action_name:
                return

            # 静音是 toggle：根据当前状态选择开/关
            if action_name == "mute_toggle":
                self._mute_state = not self._mute_state
                action_name = "mute_on" if self._mute_state else "mute_off"

            siid, aiid = self._map.actions.get(action_name, (None, None))
            if siid is None:
                return

            async def _do_action():
                try:
                    result = await self._proxy.action(
                        self._dev.did, siid=siid, aiid=aiid, in_list=[]
                    )
                    _LOGGER.info(
                        "HomeKit RemoteKey → IR: %s key=%d action=%s → %s",
                        self._dev.name, value, action_name, result,
                    )
                except Exception as e:
                    _LOGGER.error("IR action %s 失败: %s", action_name, e)

            asyncio.create_task(_do_action())

        char.setter_callback = remote_key_setter

    # ── 值转换 ──────────────────────────────────────

    @staticmethod
    def _to_miot_value(prop_kind: str, value: Any) -> Any:
        """HomeKit 值 → miot 值。"""
        if prop_kind == "brightness":
            # HomeKit: 0–100 → miot: 0–100
            return max(0, min(100, int(value)))
        elif prop_kind == "color_temp":
            # HomeKit: 140–500 mired → miot: 通常 2700–6500K，按需转换
            return max(140, min(500, int(value)))
        elif prop_kind == "speed":
            # HomeKit: 0–100 → miot: 通常 0–100
            return max(0, min(100, int(value)))
        elif prop_kind == "position":
            # HomeKit: 0–100 → miot: 0–100
            return max(0, min(100, int(value)))
        elif prop_kind == "target_temp":
            # HomeKit: 摄氏度 → miot
            return value
        elif prop_kind == "volume":
            # HomeKit: 0–100 → miot: 0–100
            return max(0, min(100, int(value)))
        # on/off bool 直接透传
        return value

    @staticmethod
    def _from_miot_value(prop_kind: str, value: Any) -> Any:
        """miot 值 → HomeKit 值。"""
        if value is None:
            return None
        if prop_kind in ("brightness", "speed", "position", "volume"):
            try:
                return max(0, min(100, int(value)))
            except (TypeError, ValueError):
                return 0
        if prop_kind == "temperature":
            try:
                return float(value)
            except (TypeError, ValueError):
                return 0.0
        if prop_kind == "humidity":
            try:
                return float(value)
            except (TypeError, ValueError):
                return 0.0
        return value

    # ── 轮询 ────────────────────────────────────────

    async def start_polling(self):
        """启动轮询（传感器 + 控制类设备）。"""
        if self._poll_task:
            return
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def fetch_initial_state(self):
        """启动时读取设备初始状态（非传感器设备）。"""
        if self._is_sensor():
            return
        # 启动控制类设备轮询（首次立即读取，之后定期同步）
        await self.start_polling()

    async def _do_fetch_initial(self):
        """读取设备当前状态并更新 HomeKit characteristic。"""
        try:
            svc = self._primary_service

            # 读取主开关状态
            on_siid = self._map.on_siid
            on_piid = self._map.on_piid
            if on_siid and on_piid:
                try:
                    raw = await self._proxy.get_prop(self._dev.did, siid=on_siid, piid=on_piid)
                    if raw is not None:
                        value = bool(raw) if not isinstance(raw, bool) else raw
                        char = svc.get_characteristic("On") or svc.get_characteristic("Active")
                        if char:
                            char.set_value(value, should_notify=False)
                            _LOGGER.debug("初始状态 %s: on=%s", self._dev.name, value)
                except Exception as e:
                    _LOGGER.debug("读取 %s 初始开关状态失败: %s", self._dev.name, e)

            # 读取亮度
            if self._map.brightness_siid and self._map.brightness_piid:
                try:
                    raw = await self._proxy.get_prop(self._dev.did, siid=self._map.brightness_siid, piid=self._map.brightness_piid)
                    if raw is not None:
                        char = svc.get_characteristic("Brightness")
                        if char:
                            char.set_value(int(raw), should_notify=False)
                except Exception:
                    pass

            # 读取色温
            if self._map.color_temp_siid and self._map.color_temp_piid:
                try:
                    raw = await self._proxy.get_prop(self._dev.did, siid=self._map.color_temp_siid, piid=self._map.color_temp_piid)
                    if raw is not None:
                        char = svc.get_characteristic("ColorTemperature")
                        if char:
                            char.set_value(int(raw), should_notify=False)
                except Exception:
                    pass

            # 读取风扇速度
            if self._map.speed_siid and self._map.speed_piid:
                try:
                    raw = await self._proxy.get_prop(self._dev.did, siid=self._map.speed_siid, piid=self._map.speed_piid)
                    if raw is not None:
                        char = svc.get_characteristic("RotationSpeed")
                        if char:
                            char.set_value(int(raw), should_notify=False)
                except Exception:
                    pass

        except asyncio.CancelledError:
            pass
        except Exception as e:
            _LOGGER.debug("初始状态读取 %s 异常: %s", self._dev.name, e)

    async def stop_polling(self):
        """停止轮询。"""
        if self._poll_task:
            self._poll_task.cancel()
            self._poll_task = None

    async def _poll_loop(self):
        """定期读取设备值并更新 HomeKit characteristic。"""
        is_sensor = self._is_sensor()
        interval = self.SENSOR_POLL_INTERVAL if is_sensor else self.CONTROL_POLL_INTERVAL

        while True:
            try:
                if not is_sensor:
                    # 控制类设备：首次立即读取，之后定期同步外部状态变化
                    await self._poll_update()
                    await asyncio.sleep(interval)
                else:
                    # 传感器：先等一个间隔
                    await asyncio.sleep(interval)
                    await self._poll_update()
            except asyncio.CancelledError:
                break
            except Exception as e:
                _LOGGER.warning("轮询 %s 异常: %s", self._dev.name, e)
                await asyncio.sleep(interval)

    async def _poll_update(self):
        """轮询读取设备属性并更新 HomeKit characteristic。"""
        # 控制类设备：读取开关/亮度/色温等状态
        if not self._is_sensor():
            await self._do_fetch_initial()
            return

        svc = self._primary_service

        # 读取主服务属性
        for char in svc.characteristics:
            # 跳过无回调的默认特性（Identify, Name 等）
            if not hasattr(char, "getter_callback") or char.getter_callback is None:
                continue
            try:
                value = char.getter_callback()
                if value is not None:
                    char.set_value(value, should_notify=False)
            except Exception as e:
                _LOGGER.debug("轮询更新 %s.%s 失败: %s",
                              self._dev.name, char.display_name, e)

        # 轮询 miot 传感器属性（温度/湿度/运动/接触等）
        sensor_props = []
        if self._map.temperature_siid is not None and self._map.temperature_piid is not None:
            sensor_props.append(("temperature", self._map.temperature_siid, self._map.temperature_piid))
        if self._map.humidity_siid is not None and self._map.humidity_piid is not None:
            sensor_props.append(("humidity", self._map.humidity_siid, self._map.humidity_piid))
        if self._map.volume_siid is not None and self._map.volume_piid is not None:
            sensor_props.append(("volume", self._map.volume_siid, self._map.volume_piid))
        if self._map.mute_siid is not None and self._map.mute_piid is not None:
            sensor_props.append(("mute", self._map.mute_siid, self._map.mute_piid))

        for prop_kind, siid, piid in sensor_props:
            try:
                raw = await self._proxy.get_prop(self._dev.did, siid=siid, piid=piid)
                value = self._from_miot_value(prop_kind, raw)
                if value is not None:
                    # 找到对应 characteristic 并更新
                    target_char = None
                    if prop_kind == "temperature":
                        target_char = svc.get_characteristic("CurrentTemperature")
                    elif prop_kind == "humidity":
                        # 湿度可能在额外服务中
                        for es in self._extra_services:
                            c = es.get_characteristic("CurrentRelativeHumidity")
                            if c:
                                target_char = c
                                break
                    elif prop_kind == "volume":
                        # 音量在 TelevisionSpeaker 额外服务 或 Speaker 主服务中
                        target_char = svc.get_characteristic("Volume")
                        if not target_char:
                            for es in self._extra_services:
                                c = es.get_characteristic("Volume")
                                if c:
                                    target_char = c
                                    break
                    elif prop_kind == "mute":
                        target_char = svc.get_characteristic("Mute")
                    if target_char:
                        target_char.set_value(value, should_notify=False)
            except Exception as e:
                _LOGGER.debug("轮询 %s.%s 失败: %s", self._dev.name, prop_kind, e)

    def _is_sensor(self) -> bool:
        """判断是否为传感器类型（需要轮询）。"""
        return self._service_name in (
            "TemperatureSensor", "HumiditySensor",
            "MotionSensor", "ContactSensor",
            "TelevisionSpeaker", "Speaker",
        ) or (
            # 电视 + 音响副服务需要轮询音量值
            self._service_name == "Television"
            and self._map.extra_services
            and "TelevisionSpeaker" in self._map.extra_services
        )

    # ── 状态 ────────────────────────────────────────

    @property
    def did(self) -> str:
        return self._dev.did

    @property
    def device_name(self) -> str:
        return self.display_name  # HomeKit 配件名（含通道信息）

    @property
    def is_online(self) -> bool:
        return self._online

    def set_online(self, online: bool):
        self._online = online
