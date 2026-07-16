# SOCKS 极简节点 Agent

在代理 VPS 上直接管控 **Xray**，无需安装 3X-UI。供 [socks5-bot](https://github.com/727263/socks5-bot) 的 `panel_type=agent` 使用。

## 一键安装（公开脚本）

```bash
curl -fsSL https://raw.githubusercontent.com/727263/socks-node-agent/main/install.sh | bash
```

自定义端口示例：

```bash
curl -fsSL https://raw.githubusercontent.com/727263/socks-node-agent/main/install.sh \
  | AGENT_PORT=9100 SHARED_PORT=1080 bash
```

安装结束会打印：**Agent 地址、API Token、inbound_id=1、公网 IP、SOCKS 端口**。

## 能力

- 创建 / 删除 SOCKS 入站（专属端口）
- 共享入站多账号同步
- 流量上限、到期自动停用
- 开关入站、重置已用流量

## 在 Bot 后台添加节点

1. 面板类型选 **极简 Agent**
2. 公网 IP = VPS IP
3. SOCKS 端口 = 安装输出的共享端口（默认 1080）
4. 面板地址 = `http://IP:9100`
5. API Token = 安装输出的 Token
6. inbound_id = `1`

用户名/密码可留空。

## 防火墙

- **必开**: Agent 端口（默认 9100），建议仅放行 Bot 服务器 IP
- **专属模式**: 放行 `20000–65000`（或你在 Bot 配置的 port_range）
- 共享模式: 放行共享 SOCKS 端口

## 卸载

```bash
curl -fsSL https://raw.githubusercontent.com/727263/socks-node-agent/main/uninstall.sh | bash
# 同时删数据: curl -fsSL .../uninstall.sh | REMOVE_DATA=1 bash
# 同时卸 Xray: curl -fsSL .../uninstall.sh | REMOVE_XRAY=1 bash
```

或本机已安装时：

```bash
bash /opt/socks-agent/uninstall.sh
```
