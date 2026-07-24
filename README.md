# SOCKS 极简节点 Agent

在代理 VPS 上直接管控 **Xray SOCKS5**（无需安装完整 3X-UI 面板）。

**两种用法（可并存）：**

1. **单独使用** — 只装 Agent，用 Web 面板增删账号，复制 `socks5://` 链接即可  
2. **对接机器人** — 给 [socks5-bot](https://github.com/727263/socks5-bot) 的 `panel_type=agent` 当节点

## 一键安装

```bash
curl -fsSL https://raw.githubusercontent.com/727263/socks-node-agent/main/install.sh | bash
```

建议热加载稳定时用官方内核（现为默认）：

```bash
curl -fsSL https://raw.githubusercontent.com/727263/socks-node-agent/main/install.sh | bash
```

若需旧版 vaxilu 内核：

```bash
curl -fsSL https://raw.githubusercontent.com/727263/socks-node-agent/main/install.sh | XRAY_KERNEL=xui bash
```

## 单独使用（不装机器人）

1. 安装完成后打开：`http://你的IP:9100/panel`  
2. 用安装脚本打印的 **面板账号/密码** 登录  
3. 「入站 / 账号」里编辑共享端口账号，或点「新增 SOCKS」开专属端口  
4. 「设置」里确认公网 IP，点「复制」拿链接给客户端  

首次启动若共享入站还没有账号，会自动生成一组，并写入：

`/opt/socks-agent/data/SHARED_SOCKS.txt`

不需要 Bot、不需要 API Token 也能完整使用。Token 仅在对接机器人时用。

## Xray 热加载（对齐 3X-UI 体验）

Agent 对 **SOCKS5** 入站采用与 3X-UI 类似的策略：

| 操作 | 行为 |
|------|------|
| 改流量额度 / 到期 / 备注 / 已用流量 | **只写 Agent 数据库**，不重启、不调 xray API |
| 新开 / 删除 / 改账号或端口 / 开关 | 先写 `config.json`，再走 **`xray api` 热加/热删**（`adi`/`rmi`） |
| 热加载失败 | **自动回退**为全量写配置 + `systemctl restart xray` |

**内核建议：** 热加载请用较新 Xray（`official` 或面板内切换）。极老 `1.4.2` 会经常回退重启，功能仍可用但较慢。

## 选择 Xray 内核

| 方式 | 示例 |
|------|------|
| **交互菜单** | 本地执行 `bash install.sh`，按提示选 1 或 2 |
| **命令行参数** | `bash install.sh --kernel official` |
| **环境变量** | `XRAY_KERNEL=xui bash install.sh`（`curl \| bash` 时用） |
| **非交互默认** | `curl ... \| bash` 且未指定 → **XTLS 官方最新** |

| 内核 | 说明 |
|------|------|
| `xui` | [vaxilu/x-ui](https://github.com/vaxilu/x-ui) 发布包里的 Xray，与旧版 XUI 面板同款，**版本较旧** |
| `official` | [XTLS/Xray-install](https://github.com/XTLS/Xray-install) 安装的**最新版** Xray |

内核文件位置：

- `xui` 模式：`/opt/socks-agent/bin/xray-linux-<arch>`
- `official` 模式：`/usr/local/bin/xray`

## Web 面板（类 3X-UI）

- 地址：`http://IP:9100/panel`
- 账号密码：安装结束时打印，也在 `/opt/socks-agent/agent.env`

| 菜单 | 功能 |
|------|------|
| 仪表盘 | CPU / 内存 / 负载 / Xray 状态 / 入站数 |
| 入站 / 账号 | 增删改、开关、搜索、复制链接、清零流量 |
| Xray 内核 | 版本查看与切换 |
| 日志 | xray / agent |
| 运维 | 重启 / 全量重载 / 防火墙检查 |
| 设置 | 公网 IP（复制链接用） |

| 环境变量 | 说明 |
|----------|------|
| `PANEL_ENABLE` | `0` 关闭面板（默认开） |
| `PANEL_USER` / `PANEL_PASS` | 面板账号密码 |
| `PANEL_SECRET` | 会话密钥 |
| `AGENT_PUBLIC_IP` | 默认公网 IP（可被面板设置覆盖） |

面板端口需在防火墙/安全组放行你的管理 IP。

## 对接机器人（可选）

1. 面板类型选 **极简 Agent**  
2. 公网 IP / SOCKS 端口 / `http://IP:9100` / API Token / inbound_id=`1`  

Bot 与面板共用同一账号库；若 Bot 对该节点做「全量同步」，可能清理非 Bot 账号，独立手工号请知悉。

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
