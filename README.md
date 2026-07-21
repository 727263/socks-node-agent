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

## Web 面板

安装后自带一个轻量 Web 面板，与 API 共用 `9100` 端口：

- 地址：`http://IP:9100/panel`
- 账号密码：安装结束时打印（`PANEL_USER` 默认 `admin`，`PANEL_PASS` 随机），也存于 `/opt/socks-agent/agent.env`

功能：系统状态（CPU/内存/负载）、xray 状态与版本、账号增删改查、流量统计、xray 版本切换（从官方下载并自动校验回滚）、日志查看、重启 xray/agent、防火墙检查。

| 环境变量 | 说明 |
|----------|------|
| `PANEL_ENABLE` | `0` 关闭面板（默认开） |
| `PANEL_USER` / `PANEL_PASS` | 自定义面板账号密码 |
| `PANEL_SECRET` | 会话签名密钥（不设则随机，重启后需重新登录） |

注意：

- 面板复用 `9100`，用浏览器访问需在防火墙/安全组放行你的管理 IP。
- 面板手动增删的账号与 Bot 自动同步共用数据；若 Bot 对本节点做全量同步，手动账号可能被清理，建议仅用于临时/测试。

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

## TCP BBR

安装脚本会**自动启用 BBR**（内核 >= 4.9 且支持 `tcp_bbr` 模块时）：

- 写入 `/etc/sysctl.d/99-socks-agent-bbr.conf`
- 开机加载 `tcp_bbr` 模块

已启用 BBR 的系统会跳过。OpenVZ 等无模块的容器环境会自动跳过并提示。

不需要 BBR：`SKIP_BBR=1 bash install.sh`

## 卸载

```bash
curl -fsSL https://raw.githubusercontent.com/727263/socks-node-agent/main/uninstall.sh | bash
```

或：

```bash
bash /opt/socks-agent/uninstall.sh
```

`REMOVE_XRAY=1` 仅对 `XRAY_KERNEL=official` 有效（调用 xray-install remove）；XUI 内核在 `${AGENT_HOME}/bin/`，设 `REMOVE_DATA=1` 一并删除。
