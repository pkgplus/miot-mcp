# miot-x

> 米家智能家居控制工具 — 内置小米官方 [miot_kit](https://github.com/XiaoMi/xiaomi-miloco) SDK (Miloco 2.0)，零外部 SDK 依赖，开箱即用。提供 MCP Server、CLI 命令、HomeKit 桥接、Agent Skill 及小智 WebSocket 桥接，**单进程搞定全部**，纯 Python，ARM64 可用，零 GPU 依赖。

## 特性

- 🏠 **官方 SDK 直连** — 基于小米官方 miot_kit (Miloco 2.0)，复用 OAuth + AES 加密协议，稳定可靠
- ⚡ **MQTT 实时推送** — 通过 mips_cloud MQTT 订阅设备上下线、绑定/解绑事件，无需轮询
- 📱 **终端扫码登录** — 运行即显示二维码，手机米家一扫授权，token 自动刷新
- 🔧 **多种接入方式** — 默认 stdio、`--http-port` HTTP MCP、`--xiaozhi` WebSocket 桥接，可任意组合
- 🍎 **HomeKit 桥接** — 米家设备一键导入 Apple 家庭 App，Siri 语音控制，控制中心遥控器
- 🔍 **模糊匹配** — 设备/场景名称智能搜索，说「台灯」就能找到
- 🌐 **小智内置桥接** — `--xiaozhi` 参数直接启用，无需额外进程，断线自动重连
- 🤖 **Agent Skill** — 内置 MCP 和 CLI 两种 Skill，AI 开箱即用
- 🐍 **全异步** — aiohttp 驱动，无同步阻塞，ARM64 / x64 通吃
- ⚡ **单进程架构** — HTTP MCP + 小智桥接 + HomeKit + 可选巴法/Zengge 桥接合为一体，不再需要额外桥接服务
- 🐟 **DIY 鱼缸灯** — 巴法 MQTT 接入小爱，BlueZ 缓存失效时自动真实扫描并恢复连接

## 快速开始

### 1. 安装

```bash
git clone https://github.com/pkgplus/miot-x.git
cd miot-x
python3 -m venv venv && source venv/bin/activate
pip install -e .
# 内置 miot_kit SDK (基于 XiaoMi/xiaomi-miloco Miloco 2.0)，无需额外安装
```

### 2. 扫码登录

```bash
python -m miot_x login
```

在浏览器中打开授权链接完成小米账号登录。默认回调地址为 `https://127.0.0.1`（页面显示"无法连接"是正常的），将地址栏完整 URL 粘贴回终端即可。

也可通过环境变量 `MIOT_REDIRECT_URI=https://mico.api.mijia.tech/login_redirect` 使用小米官方回调页面，授权后显示友好的"授权成功"页面，支持一键复制授权码。

登录成功后会自动提示选择家庭（支持多选或全部）。选择后只会操作对应家庭的设备和场景。随时可通过以下命令重新选择：

```bash
python -m miot_x homes
```

### 3. 使用

```bash
# 测试连接
python -m miot_x test

# 默认 stdio 模式（兼容旧版）
python -m miot_x

# 一体化模式：HTTP MCP（给 Hermes/Claude Code）+ 小智桥接 + HomeKit
python -m miot_x serve --xiaozhi --homekit --http-port 8300 --http-host 0.0.0.0

# 仅 HTTP MCP
python -m miot_x --http-port 8300

# 仅小智桥接
python -m miot_x --xiaozhi
```

## 运行模式

| 参数 | 说明 | 适用场景 |
|------|------|----------|
| (无参数) | stdio MCP Server | 兼容旧版，Claude Code 直连 |
| `--http-port PORT` | HTTP MCP Server (streamable-http) | Hermes Agent 等 HTTP 客户端 |
| `--xiaozhi` | 小智 WebSocket 桥接 | 小智平台远程控制 |
| `--homekit` | HomeKit 桥接（端口 51828） | Apple 家庭 App / Siri 控制 |
| `--http-port PORT --xiaozhi --homekit` | **一体化模式（推荐）** | **单进程同时服务全部** |

## 巴法 / Zengge 鱼缸灯桥接（可选）

`serve` 模式检测到 `BEMFA_UID` 后自动启用，无需独立 systemd 服务。依赖已构建的 `zengge-cli`，配置写入 miot-x 的 `.env`：

```dotenv
BEMFA_UID=<巴法私钥>
BEMFA_TOPIC=<主题名002>
BEMFA_HOST=bemfa.com
BEMFA_PORT=9501
ZENGGE_CLI_PATH=/path/to/zengge-cli
ZENGGE_MESH_NAME=<mesh name>
ZENGGE_MESH_PASS=<mesh password>
ZENGGE_MESH_LTK=<16-byte LTK>
ZENGGE_DEVICE_MAC=<device MAC>
```

支持 `on`、`off` 和 `on#30`。遇到 `le-connection-abort-by-local` 时会重启 BlueZ，保持一次真实扫描会话确认目标 MAC 的新广告，然后带退避重试；不会对协议或凭据错误重启蓝牙。运行 miot-x 的用户需有免密执行 `systemctl restart bluetooth.service` 的权限。

## MCP 工具

| 工具 | 说明 |
|------|------|
| `list_devices` | 设备列表，支持按房间筛选 |
| `get_device` | 设备详情 + SPEC 定义（siid/piid） |
| `device_on` | 打开设备 |
| `device_off` | 关闭设备 |
| `device_toggle` | 切换开关（先读后写） |
| `get_prop` | 读取属性（siid/piid） |
| `set_prop` | 设置属性（siid/piid/value） |
| `device_action` | 执行动作（siid/aiid） |
| `list_scenes` | 场景列表 |
| `execute_scene` | 执行场景（名称模糊匹配） |
| `get_service_status` | 服务连接状态 |

## HomeKit 桥接

基于 [HAP-python](https://github.com/ikalchev/HAP-python) v5，将米家设备暴露为 HomeKit 配件。

### 启动与配对

```bash
python -m miot_x serve --homekit --http-host 0.0.0.0
```

启动后终端显示二维码，iPhone 打开「家庭」App → 右上角 + → 添加配件 → 扫描二维码。

- **配对 PIN**: `123-45-678`
- **桥接端口**: `51828`
- **状态文件**: `~/.miot-x/homekit.state`

### 设备映射

| 米家设备类型 | HomeKit 服务 | 能力 |
|---|---|---|
| light (yeelink/philips/xiaomi/shhf/pmfbj) | Lightbulb | On/Off + Brightness + ColorTemperature |
| switch / outlet | Switch / Outlet | On/Off |
| fan (dmaker/zhimi) | Fan | On/Off + Speed |
| curtain | WindowCovering | Position |
| air-conditioner | HeaterCooler | On/Off + Temp + Mode |
| air-purifier | AirPurifier | On/Off + Speed |
| temp-humidity-sensor | TemperatureSensor + HumiditySensor | 只读（30s 轮询） |
| motion-sensor | MotionSensor | 只读（30s 轮询） |
| contact-sensor | ContactSensor | 只读（30s 轮询） |
| lock (lumi/aqara/loock) | LockMechanism | Lock/Unlock |
| vacuum (rockrobo) | Switch | Start/Stop |
| **TV (miir.tv)** | **Television** | **On/Off + 控制中心遥控器部件** |

### AID 稳定性

配件 ID（AID）按设备绑定时间（`order_time`）排序分配，**纯函数计算，零状态文件**：
- 重启服务 AID 不变 → 已配对设备不会「未响应」
- 新增设备自动排末尾 → 不影响现有配件
- 删除设备自动消失 → 空位保留，不重新分配

## CLI 命令

除了 MCP 模式，还提供独立 CLI 命令用于调试或 Agent Skill 调用：

```bash
python -m miot_x homes                      # 选择/切换家庭
python -m miot_x devices [--room 房间名]   # 设备列表
python -m miot_x device <设备名>            # 设备详情
python -m miot_x on <设备名>                # 打开
python -m miot_x off <设备名>               # 关闭
python -m miot_x toggle <设备名>            # 切换
python -m miot_x get <设备名> <siid> <piid> # 读属性
python -m miot_x set <设备名> <siid> <piid> <value>  # 写属性
python -m miot_x action <设备名> <siid> <aiid> [--args ...]  # 执行动作
python -m miot_x scenes                     # 场景列表
python -m miot_x scene <场景名>             # 执行场景
python -m miot_x status                     # 连接状态
```

所有命令输出 JSON 格式，设备名/场景名支持模糊匹配。

## Agent Skills

项目内置两种 Agent Skill，位于 `skills/` 目录：

| Skill | 路径 | 调用方式 | 适用场景 |
|-------|------|----------|----------|
| **miot-mcp** | `skills/miot-mcp/` | MCP 工具调用 | Claude Code、Hermes、小智等 MCP 环境 |
| **miot-cli** | `skills/miot-cli/` | Bash 执行 CLI 命令 | 无 MCP 环境时的回退方案 |

**MCP 版本**（推荐）：Server 长驻，连接复用，响应快。适合已配置 MCP Server 的环境。

**CLI 版本**：每次通过 Bash 执行独立命令，无需 MCP Server 运行。适合调试或无法注册 MCP 的场景。

### 安装 Skill

将 `skills/miot-mcp/` 或 `skills/miot-cli/` 复制到你的 Agent 项目 skills 目录即可。

## 注册到 Claude Code

在项目 `.claude/settings.json` 中添加 MCP Server：

```json
{
  "mcpServers": {
    "miot-x": {
      "type": "stdio",
      "command": "/path/to/miot-x/venv/bin/python",
      "args": ["-m", "miot_x"]
    }
  }
}
```

## 注册到 Hermes Agent

**推荐使用 HTTP 模式**（单进程，无需子进程）：

```yaml
# ~/.hermes/config.yaml
mcp_servers:
  miot:
    transport: streamable-http
    url: http://127.0.0.1:8300/mcp/
    timeout: 30
```

配合 systemd 开机自启：

```bash
sudo cp miot-x.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now miot-x.service
```

> 旧版 stdio 模式仍然支持，但会额外启动一个子进程。推荐升级到 HTTP 模式。

## 小智平台

**内置桥接**（v1.1+），无需 mcp_pipe：

```bash
# 设置小智 MCP 端点
export MCP_ENDPOINT=wss://api.xiaozhi.me/mcp/?token=***

# 启动（单进程：HTTP MCP + 小智桥接 + HomeKit）
python -m miot_x serve --xiaozhi --homekit --http-port 8300 --http-host 0.0.0.0
```

特性：
- WebSocket 断线自动重连（指数退避，最大 10 分钟）
- 与 HTTP MCP、HomeKit 共享同一进程，零额外开销
- 兼容 `MCP_ENDPOINT` 和 `XIAOZHI_MCP_URL` 环境变量

> ⚠️ `mcp_pipe.py` 已废弃，功能已集成到 `--xiaozhi` 参数中。

## 部署（systemd 开机自启）

```bash
# 1. 创建 .env 文件
cat > .env << EOF
MCP_ENDPOINT=wss://api.xiaozhi.me/mcp/?token=***
EOF

# 2. 安装 service
sudo cp miot-x.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now miot-x.service

# 3. 验证
systemctl status miot-x.service
```

## 架构

```
                    ┌──────────────────┐
    Hermes ──HTTP──►│                  │
                    │   miot_x         │──WS──► 小智平台
  Claude Code ──stdio──►  (单进程)     │
                    │                  │──HTTPS──► 小米 IoT 云
                    │                  │──MQTT──► 米家 MQTT Broker
         ┌──HAP──►  │                  │──mDNS──► iPhone 家庭 App
         │          └──────────────────┘
         │                │
    HomeKit         ┌─────┼─────┐
    配件映射         ▼     ▼     ▼
               proxy.py  auth.py  miot_kit
              (设备控制) (OAuth)  (小米SDK v2)
```

单进程架构：HTTP MCP、小智 WebSocket 桥接、HomeKit 桥接、设备控制全部在同一个进程中运行。

## 项目结构

```
miot-x/
├── src/
│   ├── miot_x/               # miot-x 核心
│   │   ├── __main__.py       #   入口分发 + 参数解析
│   │   ├── lib/              #   核心库：设备控制、认证、配置
│   │   │   ├── config.py     #     配置常量 + 家庭选择
│   │   │   ├── auth.py       #     OAuth 认证 + token 管理
│   │   │   └── proxy.py      #     设备/场景控制代理
│   │   ├── mcp/              #   MCP 协议层
│   │   │   ├── server.py     #     FastMCP 工具注册 + stdio/HTTP
│   │   │   └── xiaozhi.py    #     小智 WebSocket 桥接
│   │   ├── homekit/          #   HomeKit 桥接
│   │   │   ├── bridge.py     #     Bridge 生命周期 + AID 管理
│   │   │   ├── mappers.py    #     设备类型 → HomeKit 服务映射
│   │   │   └── accessory.py  #     miot 设备 → HAP 配件包装
│   │   ├── web/              #   Web UI + API
│   │   │   └── app.py        #     Starlette 应用（lifespan 启动 HomeKit）
│   │   └── cli/              #   CLI 命令层
│   │       └── commands.py   #     所有子命令实现
│   └── miot/                 # 小米官方 miot_kit SDK (内置, Miloco 2.0)
│       ├── client.py         #   设备客户端
│       ├── cloud.py          #   小米 IoT 云通信
│       ├── mips_cloud.py     #   MQTT 实时推送 (mips_cloud)
│       ├── oauth2.py         #   OAuth 2.0 认证
│       └── ...
├── skills/                   # Agent Skills
│   ├── miot-mcp/             #   MCP 版 Skill
│   └── miot-cli/             #   CLI 版 Skill
├── miot-x.service            # systemd service 文件
└── pyproject.toml
```

## miot_kit SDK 说明

本项目内置的 `src/miot/` 来源于小米官方 **[miot_kit](https://github.com/XiaoMi/xiaomi-miloco)**（`XiaoMi/xiaomi-miloco` 仓库 `backend/miot/` 目录，Miloco 2.0 版本）。

### V2 升级内容（相对于 v0.1.15）

- **MQTT 实时推送** — 新增 `mips_cloud.py`，通过 MQTT (TLS 8883) 订阅设备属性变化、上下线、绑定/解绑事件，无需轮询 HTTP 接口
- **fastmcp 3.x** — MCP 协议层升级到 fastmcp 3.4.2
- **多语言 MCP 描述** — i18n 改为 YAML 格式，新增德语/法语支持
- **摄像头改进** — G711A/G711U 音频解码、macOS arm64/x86_64 支持、H264 编码器竞态修复
- **CLI doctor 命令** — 分层探测 + 主动连通性检测
- **依赖升级** — aiohttp 3.14+、av 17+、pydantic 2.13+、新增 paho-mqtt 2.1+

### OAuth2 回调地址

V2 支持两个合法回调地址（小米 OAuth2 服务端注册）：

| 回调地址 | 说明 |
|----------|------|
| `https://127.0.0.1` | 本地 loopback（默认），浏览器显示"无法连接"，从地址栏复制 URL |
| `https://mico.api.mijia.tech/login_redirect` | 小米官方回调页面，显示"授权成功"页面，支持一键复制授权码 |

通过环境变量 `MIOT_REDIRECT_URI` 切换回调地址。

## License

MIT
