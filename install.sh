#!/usr/bin/env bash
# SOCKS 极简 Agent 一键安装：Xray + Agent
#
# 网络安装（推荐）:
#   curl -fsSL https://raw.githubusercontent.com/727263/socks-node-agent/main/install.sh | bash
#
# 本地安装:
#   cd socks-node-agent && bash install.sh
#
# 可选环境变量: AGENT_HOME / AGENT_PORT / SHARED_PORT / PORT_RANGE_START / PORT_RANGE_END
#              / AGENT_REPO / AGENT_REF / SKIP_FIREWALL
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
PORT_RANGE_START="${PORT_RANGE_START:-20000}"
PORT_RANGE_END="${PORT_RANGE_END:-65000}"
XRAY_API_PORT="${XRAY_API_PORT:-10085}"
AGENT_REPO="${AGENT_REPO:-727263/socks-node-agent}"
AGENT_REF="${AGENT_REF:-main}"
SKIP_FIREWALL="${SKIP_FIREWALL:-0}"

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
info "Agent 端口: ${AGENT_PORT}  共享 SOCKS: ${SHARED_PORT}  专属端口段: ${PORT_RANGE_START}-${PORT_RANGE_END}"
info "源码目录: ${BUNDLE_DIR}"

have_os_deps() {
  command -v python3 >/dev/null \
    && command -v curl >/dev/null \
    && command -v openssl >/dev/null \
    && command -v tar >/dev/null \
    && python3 -c 'import ensurepip, venv' >/dev/null 2>&1
}

wait_for_dpkg_lock() {
  local max_wait="${1:-300}"
  local waited=0
  while fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1 \
     || fuser /var/lib/dpkg/lock >/dev/null 2>&1; do
    if [[ "${waited}" -eq 0 ]]; then
      warn "dpkg 正被其他进程占用（常见: unattended-upgrades），等待释放..."
    fi
    if [[ "${waited}" -ge "${max_wait}" ]]; then
      warn "等待 dpkg 锁超时（${max_wait}s）"
      return 1
    fi
    sleep 5
    waited=$((waited + 5))
  done
  [[ "${waited}" -gt 0 ]] && info "dpkg 锁已释放（等待 ${waited}s）"
  return 0
}

install_os_deps() {
  if have_os_deps; then
    info "系统依赖已满足，跳过 apt/yum 安装"
    return 0
  fi

  if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    if wait_for_dpkg_lock 300; then
      if apt-get update -y && apt-get install -y \
          curl ca-certificates python3 python3-venv python3-pip unzip openssl tar; then
        return 0
      fi
      warn "apt 安装失败"
    else
      warn "无法获取 dpkg 锁，跳过 apt 安装"
    fi
    if have_os_deps; then
      warn "apt 未执行/失败，但现有依赖可用，继续安装"
      return 0
    fi
    error "缺少依赖且 apt 不可用。请稍后重试，或手动: apt install python3 python3-venv curl openssl tar"
  elif command -v yum >/dev/null 2>&1; then
    yum install -y curl ca-certificates python3 python3-pip unzip openssl tar || warn "yum 安装失败"
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y curl ca-certificates python3 python3-pip unzip openssl tar || warn "dnf 安装失败"
  else
    warn "未识别包管理器，请确保已安装 python3 / curl / openssl / tar"
  fi
}

install_os_deps

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

create_venv() {
  rm -rf "${AGENT_HOME}/.venv"
  python3 -m venv "${AGENT_HOME}/.venv" 2>/dev/null
}

ensure_venv() {
  [[ -x "${AGENT_HOME}/.venv/bin/python" ]] && return 0
  if create_venv; then
    return 0
  fi
  warn "venv 创建失败，尝试安装 python3-venv ..."
  if command -v apt-get >/dev/null 2>&1; then
    local pyver
    pyver="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || true)"
    export DEBIAN_FRONTEND=noninteractive
    if wait_for_dpkg_lock 300; then
      apt-get update -y || true
      # 装通用包与版本专用包（如 python3.12-venv），并补 pip
      apt-get install -y python3-venv python3-pip ${pyver:+python${pyver}-venv} || true
    else
      warn "无法获取 dpkg 锁，稍后请手动: apt install python3-venv"
    fi
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y python3-virtualenv python3-pip || true
  elif command -v yum >/dev/null 2>&1; then
    yum install -y python3-virtualenv python3-pip || true
  fi
  if create_venv; then
    return 0
  fi
  # 最后回退：无 ensurepip 时用 --without-pip + get-pip.py
  warn "仍失败，尝试 venv(--without-pip) + get-pip ..."
  if python3 -m venv --without-pip "${AGENT_HOME}/.venv" 2>/dev/null; then
    curl -fsSL https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py \
      && "${AGENT_HOME}/.venv/bin/python" /tmp/get-pip.py \
      && rm -f /tmp/get-pip.py
  fi
  [[ -x "${AGENT_HOME}/.venv/bin/python" ]] || error "无法创建 Python venv，请手动安装 python3-venv 后重试"
}

ensure_venv
# shellcheck disable=SC1091
source "${AGENT_HOME}/.venv/bin/activate"
python -m pip install -U pip wheel >/dev/null 2>&1 || warn "pip 升级失败，继续尝试安装依赖"
python -m pip install -r "${AGENT_HOME}/requirements.txt"

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

# ---------- 防火墙：Agent API + 共享 SOCKS + 专属端口段 ----------
open_firewall() {
  if [[ "${SKIP_FIREWALL}" == "1" ]]; then
    warn "已跳过防火墙配置（SKIP_FIREWALL=1）"
    return 0
  fi
  if [[ "${PORT_RANGE_START}" -gt "${PORT_RANGE_END}" ]]; then
    warn "端口段无效: ${PORT_RANGE_START}-${PORT_RANGE_END}，跳过防火墙"
    return 0
  fi

  local opened=0

  if command -v ufw >/dev/null 2>&1; then
    info "配置 UFW：${AGENT_PORT}/tcp, ${SHARED_PORT}/tcp, ${PORT_RANGE_START}:${PORT_RANGE_END}/tcp"
    ufw allow "${AGENT_PORT}/tcp" comment 'socks-agent-api' >/dev/null 2>&1 || true
    ufw allow "${SHARED_PORT}/tcp" comment 'socks-shared' >/dev/null 2>&1 || true
    ufw allow "${PORT_RANGE_START}:${PORT_RANGE_END}/tcp" comment 'socks-dedicated' >/dev/null 2>&1 || true
    # 不主动 enable，避免未放行 SSH 时把自己锁死；已启用则规则立即生效
    if ufw status 2>/dev/null | grep -qi "Status: active"; then
      info "UFW 已启用，端口规则已生效"
    else
      warn "UFW 当前未启用。规则已写入；若要用 UFW，请先放行 SSH 再执行: ufw enable"
    fi
    opened=1
  fi

  if command -v firewall-cmd >/dev/null 2>&1 && systemctl is-active --quiet firewalld 2>/dev/null; then
    info "配置 firewalld：${AGENT_PORT}/tcp, ${SHARED_PORT}/tcp, ${PORT_RANGE_START}-${PORT_RANGE_END}/tcp"
    firewall-cmd --permanent --add-port="${AGENT_PORT}/tcp" >/dev/null 2>&1 || true
    firewall-cmd --permanent --add-port="${SHARED_PORT}/tcp" >/dev/null 2>&1 || true
    firewall-cmd --permanent --add-port="${PORT_RANGE_START}-${PORT_RANGE_END}/tcp" >/dev/null 2>&1 || true
    firewall-cmd --reload >/dev/null 2>&1 || true
    info "firewalld 端口已放行"
    opened=1
  fi

  if [[ "${opened}" -eq 0 ]] && command -v iptables >/dev/null 2>&1; then
    info "配置 iptables（无 UFW/firewalld 时回退）"
    # 已有同注释规则则跳过，避免重复
    if ! iptables -C INPUT -p tcp --dport "${AGENT_PORT}" -j ACCEPT -m comment --comment "socks-agent-api" 2>/dev/null; then
      iptables -I INPUT -p tcp --dport "${AGENT_PORT}" -j ACCEPT -m comment --comment "socks-agent-api" || true
    fi
    if ! iptables -C INPUT -p tcp --dport "${SHARED_PORT}" -j ACCEPT -m comment --comment "socks-shared" 2>/dev/null; then
      iptables -I INPUT -p tcp --dport "${SHARED_PORT}" -j ACCEPT -m comment --comment "socks-shared" || true
    fi
    if ! iptables -C INPUT -p tcp --dport "${PORT_RANGE_START}:${PORT_RANGE_END}" -j ACCEPT -m comment --comment "socks-dedicated" 2>/dev/null; then
      iptables -I INPUT -p tcp --dport "${PORT_RANGE_START}:${PORT_RANGE_END}" -j ACCEPT -m comment --comment "socks-dedicated" || true
    fi
    if command -v netfilter-persistent >/dev/null 2>&1; then
      netfilter-persistent save >/dev/null 2>&1 || true
    elif command -v service >/dev/null 2>&1 && [[ -f /etc/init.d/iptables ]]; then
      service iptables save >/dev/null 2>&1 || true
    else
      warn "iptables 规则已添加，重启后可能丢失；建议安装 iptables-persistent 或改用 UFW"
    fi
    opened=1
  fi

  if [[ "${opened}" -eq 0 ]]; then
    warn "未检测到 UFW / firewalld / iptables，请在云厂商安全组手动放行："
    warn "  ${AGENT_PORT}/tcp  ${SHARED_PORT}/tcp  ${PORT_RANGE_START}-${PORT_RANGE_END}/tcp"
  else
    warn "若使用云厂商安全组/面板防火墙，仍需在控制台放行相同端口"
  fi
}

open_firewall

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
echo "  已尝试放行:   ${AGENT_PORT}(API) / ${SHARED_PORT}(共享) / ${PORT_RANGE_START}-${PORT_RANGE_END}(专属)"
echo "------------------------------------------------------------"
echo "后台「添加节点」时选「极简 Agent」，把上面几项填进去即可。"
echo "更安全做法：云安全组里把 ${AGENT_PORT} 只放行 Bot 服务器 IP。"
echo "跳过防火墙: SKIP_FIREWALL=1 bash install.sh"
echo "============================================================"
