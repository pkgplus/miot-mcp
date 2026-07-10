# -*- coding: utf-8 -*-
"""miot-x — 小米米家智能家居控制工具。

用法:
    python -m miot_x                   # 默认 serve 模式 (Web UI + MCP + HomeKit)
    python -m miot_x --http-port 8300  # HTTP MCP server
    python -m miot_x --xiaozhi         # 小智 WebSocket 桥接
    python -m miot_x --http-port 8300 --xiaozhi  # 全部启用

管理命令:
    python -m miot_x login     # 扫码登录
    python -m miot_x homes     # 重新选择家庭
    python -m miot_x test      # 测试连接
"""
import asyncio
import ipaddress
import os
import re
import sys

from .mcp import server_main


async def _start_callback_server():
    """启动本地 HTTPS 服务捕获 OAuth 回调。返回 (code, server_task) 或 None。"""
    import ssl
    import tempfile
    from aiohttp import web

    code_future = asyncio.get_event_loop().create_future()

    async def handle_callback(request):
        code = request.query.get("code")
        if code and not code_future.done():
            code_future.set_result(code)
        html = "<html><body><h2>✅ 登录成功！请返回终端继续。</h2></body></html>"
        return web.Response(text=html, content_type="text/html")

    app = web.Application()
    app.router.add_get("/", handle_callback)

    # 生成临时自签证书
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    import datetime

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
        .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.IPv4Address("127.0.0.1"))]), critical=False)
        .sign(key, hashes.SHA256())
    )

    cert_file = tempfile.NamedTemporaryFile(suffix=".pem", delete=False)
    key_file = tempfile.NamedTemporaryFile(suffix=".pem", delete=False)
    cert_file.write(cert.public_bytes(serialization.Encoding.PEM))
    cert_file.close()
    key_file.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    key_file.close()

    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_ctx.load_cert_chain(cert_file.name, key_file.name)

    runner = web.AppRunner(app)
    await runner.setup()
    try:
        site = web.TCPSite(runner, "127.0.0.1", 443, ssl_context=ssl_ctx)
        await site.start()
    except OSError:
        await runner.cleanup()
        return None, None

    return code_future, runner


async def login():
    """小米账号 OAuth 登录。"""
    from .lib.auth import MIoTAuth

    auth = MIoTAuth()
    auth_url, state = await auth.gen_oauth_url()

    # 尝试启动本地 :443 回调服务
    code_future, runner = await _start_callback_server()
    auto_mode = code_future is not None

    print("""
╔══════════════════════════════════════════════════════════╗
║              🔐 小米账号登录                              ║
╚══════════════════════════════════════════════════════════╝
""")

    if auto_mode:
        print(f"""  请在浏览器中打开以下链接，完成小米账号登录：

  {auth_url}

  ⏳ 等待登录完成...（登录后会自动捕获回调）
  💡 如果超时，请将浏览器地址栏的完整 URL 粘贴到这里。
""")
        try:
            code = await asyncio.wait_for(code_future, timeout=120)
        except asyncio.TimeoutError:
            print("  ⏰ 等待超时，请手动粘贴：")
            callback_url = input("\n  📋 回调 URL: ").strip()
            code_match = re.search(r'[?&]code=([^&]+)', callback_url)
            code = code_match.group(1) if code_match else None
        finally:
            await runner.cleanup()
    else:
        redirect_uri_env = os.getenv("MIOT_REDIRECT_URI", "")
        if redirect_uri_env and "mijia.tech" in redirect_uri_env:
            print(f"""  步骤 1: 在浏览器中打开以下链接
  ─────────────────────────────────
  {auth_url}

  步骤 2: 完成小米账号登录
  ─────────────────────────────────
  登录后浏览器会跳转到小米官方授权完成页面，
  页面会显示「授权成功」并提供一键复制授权码按钮。

  步骤 3: 复制授权码
  ─────────────────────────────────
  点击页面上的「复制授权信息」按钮，
  将授权码粘贴到下方：
""")
        else:
            print(f"""  步骤 1: 在浏览器中打开以下链接
  ─────────────────────────────────
  {auth_url}

  步骤 2: 完成小米账号登录
  ─────────────────────────────────
  登录后浏览器会跳转到 https://127.0.0.1/...
  页面会显示"无法连接"——这是正常的。

  步骤 3: 复制浏览器地址栏的完整 URL
  ─────────────────────────────────
  地址类似: https://127.0.0.1/?code=abc123&state=xyz
  请粘贴到下方：
""")
        callback_url = input("  📋 回调 URL: ").strip()
        code_match = re.search(r'[?&]code=([^&]+)', callback_url)
        code = code_match.group(1) if code_match else None

    if not code:
        print("\n  ❌ 未获取到授权码，请重试")
        return

    # 支持官方回调页面返回的 base64 编码授权码
    import base64
    import json as _json
    if not code.startswith("AKSRV") and not code.startswith("code="):
        try:
            decoded = _json.loads(base64.b64decode(code).decode("utf-8"))
            code = decoded.get("code", code)
        except Exception:
            pass

    try:
        await auth.exchange_code(code)
        print("""
  ✅ 登录成功！Token 已保存至 ~/.miot-x/auth.json
""")
    except Exception as e:
        print(f"\n  ❌ 登录失败: {e}")
        return

    await select_homes()


async def select_homes():
    """选择要控制的家庭。"""
    from .lib.proxy import MiotProxy
    from .lib.config import save_selected_home_ids

    print("🏠 正在获取家庭列表...")
    proxy = MiotProxy()
    await proxy.init()
    homes = await proxy.get_homes()
    await proxy.deinit()

    if not homes:
        print("⚠️  未找到任何家庭")
        return

    home_list = list(homes.values())
    print("\n可用家庭:")
    for i, home in enumerate(home_list, 1):
        room_count = len(home.room_list) if home.room_list else 0
        print(f"  [{i}] {home.home_name}（{room_count} 个房间）")
    print(f"  [0] 全部家庭")

    choice = input("\n请选择家庭编号（多个用逗号分隔，直接回车选全部）: ").strip()

    if not choice or choice == "0":
        save_selected_home_ids(None)
        print("✅ 已选择: 全部家庭")
    else:
        selected_ids = []
        selected_names = []
        for part in choice.split(","):
            try:
                idx = int(part.strip()) - 1
                if 0 <= idx < len(home_list):
                    selected_ids.append(home_list[idx].home_id)
                    selected_names.append(home_list[idx].home_name)
            except ValueError:
                continue
        if selected_ids:
            save_selected_home_ids(selected_ids)
            print(f"✅ 已选择: {', '.join(selected_names)}")
        else:
            save_selected_home_ids(None)
            print("✅ 已选择: 全部家庭")


async def test():
    """测试设备连接。"""
    from .lib.proxy import MiotProxy

    proxy = MiotProxy()
    await proxy.init()
    devices = await proxy.get_devices()
    scenes = await proxy.get_scenes()
    print(f"✅ 连接成功: {len(devices)} 设备, {len(scenes)} 场景")
    await proxy.deinit()


CLI_COMMANDS = {"devices", "device", "on", "off", "toggle", "get", "set", "action", "scenes", "scene", "status"}


def main():
    enable_xiaozhi = False
    enable_homekit = False
    http_port = 0
    http_host = "127.0.0.1"
    positional = []

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--xiaozhi":
            enable_xiaozhi = True
        elif a == "--homekit":
            enable_homekit = True
        elif a == "--http-port":
            i += 1
            if i < len(args):
                http_port = int(args[i])
        elif a == "--http-host":
            i += 1
            if i < len(args):
                http_host = args[i]
        else:
            positional.append(a)
        i += 1

    cmd = positional[0] if positional else "serve"

    if cmd == "login":
        asyncio.run(login())
    elif cmd == "test":
        asyncio.run(test())
    elif cmd == "homes":
        asyncio.run(select_homes())
    elif cmd == "serve":
        import logging
        import uvicorn
        from .web import create_app
        logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
        port = http_port or 8300
        app = create_app(enable_xiaozhi=enable_xiaozhi, enable_homekit=enable_homekit)
        print(f"🚀 miot-x Web 服务启动: http://{http_host}:{port}")
        if enable_homekit:
            print("🏠 HomeKit 桥接已启用 — 请用 iPhone 家庭 App 扫码配对")
        uvicorn.run(app, host=http_host, port=port, log_level="info")
    elif cmd in CLI_COMMANDS:
        from .cli import cli_main
        cli_main(positional)
    else:
        asyncio.run(server_main(
            http_port=http_port,
            http_host=http_host,
            enable_xiaozhi=enable_xiaozhi,
        ))


if __name__ == "__main__":
    main()
