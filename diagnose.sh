#!/usr/bin/env bash
# SOCKS Agent 诊断：一行跑完，把关键信息打印出来
#   curl -fsSL https://raw.githubusercontent.com/727263/socks-node-agent/main/diagnose.sh | bash
#   或指定要测的端口/账号:
#   PORT=21511 USER=u331259b2 PASS=s0KVrdjTugMr bash diagnose.sh
set -uo pipefail

AGENT_HOME="${AGENT_HOME:-/opt/socks-agent}"
ENV_FILE="${AGENT_HOME}/agent.env"
XRAY_CONFIG="${XRAY_CONFIG:-/usr/local/etc/xray/config.json}"

line() { echo "======== $* ========"; }

line "1. 服务状态"
systemctl is-active socks-agent >/dev/null 2>&1 && echo "socks-agent: active" || echo "socks-agent: NOT active"
systemctl is-active xray >/dev/null 2>&1 && echo "xray: active" || echo "xray: NOT active"

line "2. socks-agent 最近日志"
journalctl -u socks-agent -n 25 --no-pager 2>/dev/null || echo "(无 journal)"

line "3. xray 最近日志"
journalctl -u xray -n 25 --no-pager 2>/dev/null || echo "(无 journal)"

line "4. xray 配置里的入站端口"
if [[ -f "${XRAY_CONFIG}" ]]; then
  grep -E '"port"|"protocol"|"tag"' "${XRAY_CONFIG}" | sed 's/^[[:space:]]*//'
else
  echo "找不到 ${XRAY_CONFIG}"
fi

line "5. xray 配置自检"
if command -v xray >/dev/null 2>&1; then
  xray run -test -config "${XRAY_CONFIG}" 2>&1 | tail -5
else
  echo "xray 未安装?"
fi

line "6. 正在监听的端口"
ss -lntp 2>/dev/null | grep -E 'xray|LISTEN' | grep -E ':(1080|2[0-9]{4}|3[0-9]{4}|4[0-9]{4}|5[0-9]{4}|6[0-4][0-9]{3})\b' || ss -lntp 2>/dev/null | grep xray || echo "(未发现 xray 监听)"

line "7. Agent 健康检查"
if [[ -f "${ENV_FILE}" ]]; then
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  curl -fsS --max-time 5 "http://127.0.0.1:${AGENT_LISTEN_PORT:-9100}/api/health" 2>&1 && echo
  echo "--- Agent 里的入站列表 ---"
  curl -fsS --max-time 5 -H "Authorization: Bearer ${AGENT_API_TOKEN:-}" \
    "http://127.0.0.1:${AGENT_LISTEN_PORT:-9100}/api/inbounds/list" 2>&1 \
    | python3 -c 'import sys,json;
d=json.load(sys.stdin);
[print(i.get("id"),i.get("port"),i.get("protocol"),"enable="+str(i.get("enable")),i.get("remark")) for i in (d.get("obj") or [])]' 2>/dev/null \
    || echo "(解析失败，可能 Token 不对或 Agent 未起)"
else
  echo "找不到 ${ENV_FILE}"
fi

line "8. 本机 SOCKS 连通性测试"
PORT="${PORT:-}"
USER="${USER_NAME:-${USER:-}}"
PASS="${PASS:-}"
if [[ -n "${PORT}" ]]; then
  if [[ -n "${USER}" && -n "${PASS}" ]]; then
    echo "测试 127.0.0.1:${PORT} (user=${USER})"
    curl -x "socks5h://${USER}:${PASS}@127.0.0.1:${PORT}" --max-time 12 -fsS https://ifconfig.me 2>&1 && echo " <- 本机直连 SOCKS 成功" || echo " <- 本机 SOCKS 失败"
  else
    echo "测试 127.0.0.1:${PORT} (无认证探测端口是否开放)"
    timeout 5 bash -c "echo > /dev/tcp/127.0.0.1/${PORT}" 2>/dev/null && echo "端口 ${PORT} 本机可连" || echo "端口 ${PORT} 本机不可连"
  fi
else
  echo "未指定 PORT，跳过。可执行: PORT=21511 USER=u331259b2 PASS=xxxx bash diagnose.sh"
fi

line "9. 防火墙"
if command -v ufw >/dev/null 2>&1; then ufw status | head -20; fi
if command -v firewall-cmd >/dev/null 2>&1; then firewall-cmd --list-ports 2>/dev/null; fi
iptables -S INPUT 2>/dev/null | grep -E 'socks|20000|1080|9100' || echo "(iptables 无相关规则)"

echo
echo "==================================================="
echo "关注："
echo "  第6步没有你的端口 → xray 没监听（看第3/5步报错）"
echo "  第8步本机成功、外网不通 → 云安全组没放行该端口段"
echo "==================================================="
