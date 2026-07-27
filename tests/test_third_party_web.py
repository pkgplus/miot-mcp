import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from starlette.requests import Request

from miot_x.integrations.third_party import ThirdPartyDeviceRegistry
from miot_x.web.routes import devices as device_routes


class FakeProvider:
    provider_id = "fake"

    def __init__(self):
        self.power_calls = []
        self.values = {(2, 1): True, (2, 2): 30}

    async def list_web_devices(self):
        return [{
            "did": "third-party:fake:lamp",
            "name": "第三方灯",
            "model": "fake.light",
            "online": True,
            "room": "第三方设备",
            "source": "third_party",
            "platform": "Fake",
            "power": True,
        }]

    async def get_web_device(self, did):
        if did != "third-party:fake:lamp":
            return None
        return (await self.list_web_devices())[0] | {"spec": {"services": []}}

    async def set_web_power(self, did, power):
        self.power_calls.append((did, power))
        self.values[(2, 1)] = power
        return {"success": True}

    async def get_web_prop(self, did, siid, piid):
        return self.values[(siid, piid)]

    async def set_web_prop(self, did, siid, piid, value):
        if value == "invalid":
            raise ValueError("invalid value")
        self.values[(siid, piid)] = value
        return {"success": True}


class ThirdPartyRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_registers_lists_and_controls_provider_devices(self):
        registry = ThirdPartyDeviceRegistry()
        provider = FakeProvider()
        registry.register(provider)

        devices = await registry.list_devices()
        self.assertEqual(devices[0]["did"], "third-party:fake:lamp")
        self.assertTrue(await registry.has_device("third-party:fake:lamp"))

        await registry.set_power("third-party:fake:lamp", False)
        self.assertEqual(provider.power_calls, [("third-party:fake:lamp", False)])
        self.assertFalse(await registry.get_prop("third-party:fake:lamp", 2, 1))


class ThirdPartyWebRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.registry = ThirdPartyDeviceRegistry()
        self.provider = FakeProvider()
        self.registry.register(self.provider)
        self.proxy = AsyncMock()
        self.proxy.get_devices.return_value = {}
        self.proxy.get_homes.return_value = {}

    @staticmethod
    def request(
        path, *, method="GET", path_params=None, body=None, query_string=b""
    ):
        sent = False

        async def receive():
            nonlocal sent
            if sent:
                return {"type": "http.disconnect"}
            sent = True
            return {"type": "http.request", "body": body or b"", "more_body": False}

        return Request({
            "type": "http",
            "method": method,
            "path": path,
            "query_string": query_string,
            "headers": [(b"content-type", b"application/json")],
            "path_params": path_params or {},
        }, receive)

    async def test_device_list_includes_third_party_devices(self):
        request = self.request("/api/devices")
        with patch.object(device_routes, "third_party_registry", self.registry), patch.object(
            device_routes, "_get_proxy_or_error", AsyncMock(return_value=(self.proxy, None))
        ):
            response = await device_routes.list_devices(request)

        payload = json.loads(response.body)
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["devices"][0]["source"], "third_party")

    async def test_room_filter_excludes_other_third_party_rooms(self):
        request = self.request(
            "/api/devices", query_string="room=客厅".encode()
        )
        with patch.object(device_routes, "third_party_registry", self.registry), patch.object(
            device_routes, "_get_proxy_or_error", AsyncMock(return_value=(self.proxy, None))
        ):
            response = await device_routes.list_devices(request)

        self.assertEqual(json.loads(response.body)["devices"], [])

    async def test_power_route_dispatches_to_third_party_provider(self):
        did = "third-party:fake:lamp"
        request = self.request(f"/api/devices/{did}/off", method="POST", path_params={"did": did})
        with patch.object(device_routes, "third_party_registry", self.registry):
            response = await device_routes.device_off(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.provider.power_calls, [(did, False)])

    async def test_property_routes_dispatch_to_third_party_provider(self):
        did = "third-party:fake:lamp"
        body = json.dumps({"siid": 2, "piid": 2, "value": 45}).encode()
        request = self.request(f"/api/devices/{did}/prop", method="POST", path_params={"did": did}, body=body)
        with patch.object(device_routes, "third_party_registry", self.registry):
            response = await device_routes.device_prop(request)
        self.assertEqual(response.status_code, 200)

        request = self.request(
            f"/api/devices/{did}/prop/2/2",
            path_params={"did": did, "siid": "2", "piid": "2"},
        )
        with patch.object(device_routes, "third_party_registry", self.registry):
            response = await device_routes.get_prop_value(request)
        self.assertEqual(json.loads(response.body)["value"], 45)

        body = json.dumps({"siid": 2, "piid": 2, "value": "invalid"}).encode()
        request = self.request(
            f"/api/devices/{did}/prop",
            method="POST",
            path_params={"did": did},
            body=body,
        )
        with patch.object(device_routes, "third_party_registry", self.registry):
            response = await device_routes.device_prop(request)
        self.assertEqual(response.status_code, 400)


class ThirdPartyWebTemplateTests(unittest.TestCase):
    def test_third_party_detail_does_not_duplicate_generic_spec_controls(self):
        html = (
            Path(__file__).parents[1] / "src/miot_x/web/static/index.html"
        ).read_text()
        self.assertIn(
            "currentDevice?.spec && currentDevice?.source !== 'third_party'",
            html,
        )

    def test_unknown_third_party_power_is_labeled_unknown(self):
        script = (
            Path(__file__).parents[1] / "src/miot_x/web/static/app.js"
        ).read_text()
        self.assertIn("dev.source === 'third_party' && dev.power == null", script)

    def test_frontend_control_updates_power_and_rolls_back_on_failure(self):
        script = (
            Path(__file__).parents[1] / "src/miot_x/web/static/app.js"
        ).read_text()
        self.assertIn("dev.power = nextPower", script)
        self.assertIn("if (!response.ok) throw new Error", script)
        self.assertIn("dev.power = oldPower", script)

    def test_platform_subtitle_keeps_online_status(self):
        html = (
            Path(__file__).parents[1] / "src/miot_x/web/static/index.html"
        ).read_text()
        self.assertIn("dev.platform + ' · '", html)


if __name__ == "__main__":
    unittest.main()
