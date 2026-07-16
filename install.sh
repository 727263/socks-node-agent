#!/usr/bin/env bash
# SOCKS 极简 Agent 一键安装：Xray + Agent
#
# 网络安装（推荐）:
#   curl -fsSL https://raw.githubusercontent.com/727263/socks-node-agent/main/install.sh | bash
#
# 本地安装:
#   cd socks-node-agent && bash install.sh
#
# 可选环境变量: AGENT_HOME / AGENT_PORT / SHARED_PORT / AGENT_REPO / AGENT_REF
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERR ]${NC} $*" >&2; exit 1; }

[[ "$(id -u)" -eq 0 ]] || error "请用 root 运行"

AGENT_HOME="${AGENT_HOME:-/opt/socks-agent}"
AGENT_PORT="${AGENT_PORT:-9100}"
SHARED_PORT="${SHARED_PORT:-1080}"
XRAY_API_PORT="${XRAY_API_PORT:-10085}"
AGENT_REPO="${AGENT_REPO:-727263/socks-node-agent}"
AGENT_REF="${AGENT_REF:-main}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
BUNDLE_DIR=""
TMP_FETCH=""

cleanup() {
  if [[ -n "${TMP_FETCH}" && -d "${TMP_FETCH}" ]]; then
    rm -rf "${TMP_FETCH}"
  fi
}
trap cleanup EXIT

fetch_bundle() {
  info "从 GitHub 拉取源码: ${AGENT_REPO}@${AGENT_REF}"
  command -v curl >/dev/null || error "需要 curl"
  TMP_FETCH="$(mktemp -d /tmp/socks-agent-src.XXXXXX)"
  local url="https://codeload.github.com/${AGENT_REPO}/tar.gz/${AGENT_REF}"
  if ! curl -fsSL "${url}" | tar -xz -C "${TMP_FETCH}"; then
    error "下载失败: ${url}"
  fi
  local extracted
  extracted="$(find "${TMP_FETCH}" -mindepth 1 -maxdepth 1 -type d | head -n1)"
  [[ -n "${extracted}" && -f "${extracted}/agent/main.py" ]] \
    || error "下载的压缩包里找不到 agent/main.py"
  BUNDLE_DIR="${extracted}"
}

if [[ -n "${SCRIPT_DIR}" && -f "${SCRIPT_DIR}/agent/main.py" ]]; then
  BUNDLE_DIR="${SCRIPT_DIR}"
elif [[ -f "./agent/main.py" ]]; then
  BUNDLE_DIR="$(pwd)"
else
  fetch_bundle
fi

info "安装目录: ${AGENT_HOME}"
info "Agent 端口: ${AGENT_PORT}  共享 SOCKS 端口: ${SHARED_PORT}"
info "源码目录: ${BUNDLE_DIR}"

if command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  apt-get install -y curl ca-certificates python3 python3-venv python3-pip unzip openssl tar
elif command -v yum >/dev/null 2>&1; then
  yum install -y curl ca-certificates python3 python3-pip unzip openssl tar
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y curl ca-certificates python3 python3-pip unzip openssl tar
else
  warn "未识别包管理器，请确保已安装 python3 / curl / unzip / openssl / tar"
fi

command -v python3 >/dev/null || error "需要 python3"
command -v curl >/dev/null || error "需要 curl"

if [[ ! -x /usr/local/bin/xray ]]; then
  info "安装 Xray-core ..."
  bash -c "$(curl -fsSL https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" @ install
else
  info "已存在 Xray: $(/usr/local/bin/xray version 2>/dev/null | head -n1 || true)"
fi
[[ -x /usr/local/bin/xray ]] || error "Xray 安装失败"

mkdir -p /usr/local/etc/xray
XRAY_SERVICE="xray"

mkdir -p "${AGENT_HOME}/data" "${AGENT_HOME}/agent"
info "部署 Agent 文件 ..."
cp -a "${BUNDLE_DIR}/agent/." "${AGENT_HOME}/agent/"
cp -f "${BUNDLE_DIR}/requirements.txt" "${AGENT_HOME}/requirements.txt"
[[ -f "${BUNDLE_DIR}/uninstall.sh" ]] && cp -f "${BUNDLE_DIR}/uninstall.sh" "${AGENT_HOME}/uninstall.sh"

if [[ ! -d "${AGENT_HOME}/.venv" ]]; then
  python3 -m venv "${AGENT_HOME}/.venv"
fi
# shellcheck disable=SC1091
source "${AGENT_HOME}/.venv/bin/activate"
pip install -U pip wheel >/dev/null
pip install -r "${AGENT_HOME}/requirements.txt"

ENV_FILE="${AGENT_HOME}/agent.env"
if [[ -f "${ENV_FILE}" ]]; then
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  info "沿用已有 Token（${ENV_FILE}）"
else
  AGENT_API_TOKEN="$(openssl rand -hex 24 2>/dev/null || head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  cat > "${ENV_FILE}" <<EOF
AGENT_LISTEN_HOST=0.0.0.0
AGENT_LISTEN_PORT=${AGENT_PORT}
AGENT_API_TOKEN=${AGENT_API_TOKEN}
AGENT_DATA_DIR=${AGENT_HOME}/data
AGENT_SHARED_PORT=${SHARED_PORT}
XRAY_BIN=/usr/local/bin/xray
XRAY_CONFIG=/usr/local/etc/xray/config.json
XRAY_API_ADDR=127.0.0.1:${XRAY_API_PORT}
XRAY_SERVICE=${XRAY_SERVICE}
EOF
  chmod 600 "${ENV_FILE}"
  info "已生成新 API Token"
fi

# shellcheck disable=SC1090
source "${ENV_FILE}"

cat > /etc/systemd/system/socks-agent.service <<EOF
[Unit]
Description=SOCKS Node Agent (Xray controller)
After=network.target ${XRAY_SERVICE}.service
Wants=${XRAY_SERVICE}.service

[Service]
Type=simple
WorkingDirectory=${AGENT_HOME}
EnvironmentFile=${ENV_FILE}
Environment=PYTHONPATH=${AGENT_HOME}
ExecStart=${AGENT_HOME}/.venv/bin/python -m agent.main
Restart=on-failure
RestartSec=3
LimitNOFILE=1048576

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "${XRAY_SERVICE}" >/dev/null 2>&1 || true
systemctl enable socks-agent
systemctl restart "${XRAY_SERVICE}" || true
systemctl restart socks-agent

sleep 2
if systemctl is-active --quiet socks-agent; then
  info "socks-agent 已启动"
else
  warn "socks-agent 未处于 active，请检查: journalctl -u socks-agent -n 50"
fi

PUBLIC_IP="$(curl -4 -fsS --max-time 5 ifconfig.me 2>/dev/null || curl -4 -fsS --max-time 5 ip.sb 2>/dev/null || echo 'YOUR_IP')"

echo
echo "============================================================"
echo -e "${GREEN}安装完成${NC}"
echo "------------------------------------------------------------"
echo "  面板类型:     agent"
echo "  Agent 地址:   http://${PUBLIC_IP}:${AGENT_LISTEN_PORT:-$AGENT_PORT}"
echo "  API Token:    ${AGENT_API_TOKEN}"
echo "  inbound_id:   1   (共享占位，专属模式也填 1)"
echo "  公网 IP:      ${PUBLIC_IP}"
echo "  SOCKS 端口:   ${AGENT_SHARED_PORT:-$SHARED_PORT}  (节点「SOCKS 端口」填这个)"
echo "------------------------------------------------------------"
echo "后台「添加节点」时选「极简 Agent」，把上面几项填进去即可。"
echo "建议防火墙只允许 Bot 服务器访问 ${AGENT_LISTEN_PORT:-$AGENT_PORT}。"
echo "专属端口模式请放行 config 里的 port_range（默认 20000-65000）。"
echo "============================================================"
