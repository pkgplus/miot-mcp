# -*- coding: utf-8 -*-
"""miot-x 配置常量。"""
import json
import os
from pathlib import Path

# 缓存目录 — sudo 运行时使用原始用户的 home 目录
def _get_default_cache_dir() -> str:
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        import pwd
        try:
            return os.path.join(pwd.getpwnam(sudo_user).pw_dir, ".miot-x")
        except KeyError:
            pass
    return os.path.expanduser("~/.miot-x")

CACHE_DIR = Path(os.getenv("MIOT_CACHE_DIR", _get_default_cache_dir()))
# Token + 家庭选择持久化文件
AUTH_FILE = CACHE_DIR / "auth.json"
# 设备/场景缓存
DEVICES_FILE = CACHE_DIR / "devices.json"
SCENES_FILE = CACHE_DIR / "scenes.json"

# OAuth 回调地址 — 支持本地 loopback 和小米官方回调页面
# V2 SDK 新增 mico.api.mijia.tech/login_redirect，授权后显示友好页面
OAUTH_REDIRECT_URI = os.getenv(
    "MIOT_REDIRECT_URI", "https://127.0.0.1"
)
# 小米云区域（cn = 中国大陆）
CLOUD_SERVER = os.getenv("MIOT_CLOUD_SERVER", "cn")


def get_selected_home_ids() -> list[str] | None:
    """获取用户选择的家庭 ID 列表。返回 None 表示全部家庭。"""
    if not AUTH_FILE.exists():
        return None
    try:
        data = json.loads(AUTH_FILE.read_text())
        ids = data.get("home_ids")
        if not ids:
            return None
        return ids
    except Exception:
        return None


def save_selected_home_ids(home_ids: list[str] | None) -> None:
    """保存用户选择的家庭 ID 列表到 auth.json。"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    data = {}
    if AUTH_FILE.exists():
        try:
            data = json.loads(AUTH_FILE.read_text())
        except Exception:
            pass
    data["home_ids"] = home_ids
    AUTH_FILE.write_text(json.dumps(data, indent=2))
