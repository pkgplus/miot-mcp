# -*- coding: utf-8 -*-
"""Registry for devices provided by non-MIoT integrations."""

from __future__ import annotations

from typing import Any, Protocol


class ThirdPartyDeviceProvider(Protocol):
    provider_id: str

    async def list_web_devices(self) -> list[dict[str, Any]]: ...

    async def get_web_device(self, did: str) -> dict[str, Any] | None: ...

    async def set_web_power(self, did: str, power: bool) -> Any: ...

    async def get_web_prop(self, did: str, siid: int, piid: int) -> Any: ...

    async def set_web_prop(
        self, did: str, siid: int, piid: int, value: Any
    ) -> Any: ...


class ThirdPartyDeviceRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, ThirdPartyDeviceProvider] = {}

    def register(self, provider: ThirdPartyDeviceProvider) -> None:
        self._providers[provider.provider_id] = provider

    def unregister(self, provider_id: str) -> None:
        self._providers.pop(provider_id, None)

    async def list_devices(self) -> list[dict[str, Any]]:
        devices: list[dict[str, Any]] = []
        for provider in self._providers.values():
            devices.extend(await provider.list_web_devices())
        return devices

    async def _provider_for(
        self, did: str
    ) -> tuple[ThirdPartyDeviceProvider, dict[str, Any]] | None:
        for provider in self._providers.values():
            device = await provider.get_web_device(did)
            if device is not None:
                return provider, device
        return None

    async def has_device(self, did: str) -> bool:
        return await self._provider_for(did) is not None

    async def get_device(self, did: str) -> dict[str, Any] | None:
        match = await self._provider_for(did)
        return match[1] if match else None

    async def set_power(self, did: str, power: bool) -> Any:
        match = await self._provider_for(did)
        if match is None:
            raise KeyError(did)
        return await match[0].set_web_power(did, power)

    async def get_prop(self, did: str, siid: int, piid: int) -> Any:
        match = await self._provider_for(did)
        if match is None:
            raise KeyError(did)
        return await match[0].get_web_prop(did, siid, piid)

    async def set_prop(self, did: str, siid: int, piid: int, value: Any) -> Any:
        match = await self._provider_for(did)
        if match is None:
            raise KeyError(did)
        return await match[0].set_web_prop(did, siid, piid, value)


third_party_registry = ThirdPartyDeviceRegistry()
