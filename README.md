# SOCKS 极简节点 Agent

在代理 VPS 上直接管控 **Xray**（无需安装 XUI/3X-UI 面板）。供 [socks5-bot](https://github.com/727263/socks5-bot) 的 `panel_type=agent` 使用。

## 一键安装

```bash
curl -fsSL https://raw.githubusercontent.com/727263/socks-node-agent/main/install.sh | bash
```

## 选择 Xray 内核

| 方式 | 示例 |
|------|------|
| **交互菜单** | 本地执行 `bash install.sh`，按提示选 1 或 2 |
| **命令行参数** | `bash install.sh --kernel official` |
| **环境变量** | `XRAY_KERNEL=xui bash install.sh`（`curl \| bash` 时用） |
| **非交互默认** | `curl ... \| bash` 且未指定 → **vaxilu XUI 内核** |

| 内核 | 说明 |
|------|------|
| `xui` | [vaxilu/x-ui](https://github.com/vaxilu/x-ui) 发布包里的 Xray，与旧版 XUI 面板同款，**版本较旧** |
| `official` | [XTLS/Xray-install](https://github.com/XTLS/Xray-install) 安装的**最新版** Xray |

```bash
# 交互选择
bash install.sh

# 指定官方最新
bash install.sh -k official

# 管道安装指定内核
curl -fsSL .../install.sh | XRAY_KERNEL=official bash
```

内核文件位置：

- `xui` 模式：`/opt/socks-agent/bin/xray-linux-<arch>`
- `official` 模式：`/usr/local/bin/xray`

## 在 Bot 后台添加节点

1. 面板类型选 **极简 Agent**
2. 公网 IP = VPS IP
3. SOCKS 端口 = 安装输出的共享端口（默认 1080）
4. 面板地址 = `http://IP:9100`
5. API Token = 安装输出的 Token
6. inbound_id = `1`

用户名/密码可留空。

## 防火墙

| 端口 | 默认 | 用途 |
|------|------|------|
| Agent API | `9100/tcp` | Bot 调管控接口 |
| 共享 SOCKS | `1080/tcp` | 共享入站 |
| 专属端口段 | `20000-65000/tcp` | 每用户独立端口 |

环境变量：`AGENT_PORT` / `SHARED_PORT` / `PORT_RANGE_START` / `PORT_RANGE_END` / `SKIP_FIREWALL=1`

## 卸载

```bash
curl -fsSL https://raw.githubusercontent.com/727263/socks-node-agent/main/uninstall.sh | bash
```

或：

```bash
bash /opt/socks-agent/uninstall.sh
```

`REMOVE_XRAY=1` 仅对 `XRAY_KERNEL=official` 有效（调用 xray-install remove）；XUI 内核在 `${AGENT_HOME}/bin/`，设 `REMOVE_DATA=1` 一并删除。
