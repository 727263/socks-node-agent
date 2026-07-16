#!/usr/bin/env bash
# 卸载 SOCKS Agent（默认保留 Xray，避免误伤其他用途）
#
# 网络卸载:
#   curl -fsSL https://raw.githubusercontent.com/727263/socks-node-agent/main/uninstall.sh | bash
set -euo pipefail

AGENT_HOME="${AGENT_HOME:-/opt/socks-agent}"
REMOVE_XRAY="${REMOVE_XRAY:-0}"

systemctl stop socks-agent 2>/dev/null || true
systemctl disable socks-agent 2>/dev/null || true
rm -f /etc/systemd/system/socks-agent.service
systemctl daemon-reload

if [[ "${REMOVE_DATA:-0}" == "1" ]]; then
  rm -rf "${AGENT_HOME}"
  echo "已删除 ${AGENT_HOME}"
else
  echo "已停止 Agent。数据仍在 ${AGENT_HOME}（设 REMOVE_DATA=1 可删除）"
fi

if [[ "${REMOVE_XRAY}" == "1" ]]; then
  bash -c "$(curl -fsSL https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" @ remove || true
  echo "已尝试卸载 Xray"
fi
