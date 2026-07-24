#!/usr/bin/env bash
# SOCKS 极简 Agent 一键安装：Agent 管控 Xray
#
# 可选 Xray 内核（三选一）:
#   1) 交互菜单（本地 bash install.sh 且有终端）
#   2) 参数/环境变量: --kernel xui|official  或  XRAY_KERNEL=official
#   3) 非交互默认 XTLS 官方最新内核（curl | bash 且未指定时）
#
# 网络安装:
#   curl -fsSL .../install.sh | bash
#   curl -fsSL .../install.sh | XRAY_KERNEL=xui bash
#
# 本地安装:
#   cd node-agent && bash install.sh
#   bash install.sh --kernel xui
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*" >&2; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*" >&2; }
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
SKIP_BBR="${SKIP_BBR:-0}"
XRAY_KERNEL="${XRAY_KERNEL:-}"
VAXILU_XUI_REPO="${VAXILU_XUI_REPO:-vaxilu/x-ui}"
VAXILU_XUI_TAG="${VAXILU_XUI_TAG:-}"

usage() {
  cat <<'EOF'
用法: install.sh [选项]

  --kernel, -k xui|official   指定 Xray 内核（不指定则交互选择或见默认）
  --help, -h                    显示帮助

环境变量:
  XRAY_KERNEL=xui|official      与 --kernel 相同（curl 管道安装时用）
  SKIP_BBR=1                      跳过 TCP BBR 拥塞控制优化

内核:
  official  XTLS 官方最新 Xray（默认，xray-install）
  xui       vaxilu 旧版 XUI 发布包 Xray（较旧）

示例:
  bash install.sh
  bash install.sh --kernel xui
  curl -fsSL .../install.sh | bash
  curl -fsSL .../install.sh | XRAY_KERNEL=xui bash
EOF
}

normalize_xray_kernel() {
  case "$(echo "${1}" | tr '[:upper:]' '[:lower:]')" in
    official|xtls|latest|1) echo official ;;
    xui|vaxilu|x-ui|2) echo xui ;;
    "") echo "" ;;
    *) echo "invalid" ;;
  esac
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -k|--kernel)
        [[ $# -ge 2 ]] || error "--kernel 需要参数: xui 或 official"
        XRAY_KERNEL="$(normalize_xray_kernel "$2")"
        [[ "${XRAY_KERNEL}" != "invalid" ]] || error "无效内核: $2（可用 xui / official）"
        shift 2
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      xui|official|vaxilu|x-ui|xtls|latest)
        XRAY_KERNEL="$(normalize_xray_kernel "$1")"
        shift
        ;;
      *)
        error "未知参数: $1（用 --help 查看）"
        ;;
    esac
  done
}

choose_xray_kernel() {
  if [[ -n "${XRAY_KERNEL}" ]]; then
    local raw="${XRAY_KERNEL}"
    XRAY_KERNEL="$(normalize_xray_kernel "${raw}")"
    [[ "${XRAY_KERNEL}" != "invalid" ]] || error "无效 XRAY_KERNEL=${raw}，可用: xui / official"
    return
  fi

  if [[ -t 0 ]]; then
    echo
    echo "请选择 Xray 内核:"
    echo "  1) XTLS 官方最新    — xray-install 安装的最新版（推荐，热加载更稳）"
    echo "  2) vaxilu XUI 内核  — 旧版 XUI 发布包同款，较旧"
    echo
    local choice=""
    read -rp "请输入 [1/2] (默认 1): " choice
    choice="${choice:-1}"
    # 交互菜单: 1=official, 2=xui（与旧版 1=xui 对调）
    case "${choice}" in
      1|official|xtls|latest) XRAY_KERNEL="official" ;;
      2|xui|vaxilu|x-ui) XRAY_KERNEL="xui" ;;
      *) XRAY_KERNEL="$(normalize_xray_kernel "${choice}")" ;;
    esac
    [[ "${XRAY_KERNEL}" != "invalid" ]] || error "无效选择: ${choice}"
  else
    XRAY_KERNEL="official"
    info "非交互安装，默认 XTLS 官方最新内核（改用旧版: XRAY_KERNEL=xui）"
  fi
}

parse_args "$@"

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

bbr_active() {
  sysctl net.ipv4.tcp_congestion_control 2>/dev/null | grep -q '\bbbr\b'
}

enable_bbr() {
  if [[ "${SKIP_BBR}" == "1" ]]; then
    warn "已跳过 BBR 优化（SKIP_BBR=1）"
    return 0
  fi

  if bbr_active; then
    info "BBR 已启用，跳过"
    return 0
  fi

  local major minor
  major="$(uname -r | cut -d. -f1)"
  minor="$(uname -r | cut -d. -f2)"
  minor="${minor%%-*}"
  if [[ "${major}" -lt 4 ]] || { [[ "${major}" -eq 4 ]] && [[ "${minor}" -lt 9 ]]; }; then
    warn "内核 $(uname -r) 过低（需 >= 4.9），跳过 BBR"
    return 0
  fi

  info "启用 TCP BBR 拥塞控制 ..."
  modprobe tcp_bbr 2>/dev/null || true

  if ! lsmod 2>/dev/null | grep -q tcp_bbr; then
    if ! modinfo tcp_bbr >/dev/null 2>&1; then
      warn "tcp_bbr 模块不可用（常见于 OpenVZ 等容器），跳过 BBR"
      return 0
    fi
  fi

  mkdir -p /etc/modules-load.d
  echo tcp_bbr > /etc/modules-load.d/tcp-bbr.conf

  cat > /etc/sysctl.d/99-socks-agent-bbr.conf <<'EOF'
# TCP BBR (socks-agent install.sh)
net.core.default_qdisc = fq
net.ipv4.tcp_congestion_control = bbr
EOF

  if sysctl -p /etc/sysctl.d/99-socks-agent-bbr.conf >/dev/null 2>&1; then
    :
  elif ! sysctl -w net.core.default_qdisc=fq net.ipv4.tcp_congestion_control=bbr >/dev/null 2>&1; then
    warn "BBR sysctl 应用失败，配置已写入 /etc/sysctl.d/99-socks-agent-bbr.conf，重启后可能生效"
  fi

  if bbr_active; then
    info "BBR 已启用 (qdisc=$(sysctl -n net.core.default_qdisc 2>/dev/null || echo ?))"
  else
    warn "BBR 配置已写入，重启后生效"
  fi
}

enable_bbr

command -v python3 >/dev/null || error "需要 python3"
command -v curl >/dev/null || error "需要 curl"

choose_xray_kernel
info "已选 Xray 内核: ${XRAY_KERNEL} ($([[ ${XRAY_KERNEL} == xui ]] && echo 'vaxilu XUI' || echo 'XTLS 官方'))"

detect_xui_arch() {
  case "$(uname -m)" in
    x86_64|amd64) echo amd64 ;;
    aarch64|arm64) echo arm64 ;;
    armv7l|armv7) echo arm64 ;;  # vaxilu 包无 armv7，回退 arm64 由下载失败提示
    *) echo amd64 ;;
  esac
}

find_existing_vaxilu_xray() {
  local arch
  arch="$(detect_xui_arch)"
  local p
  for p in \
    "/usr/local/x-ui/bin/xray-linux-${arch}" \
    "/usr/local/x-ui/bin/xray-linux-amd64" \
    "/usr/local/x-ui/bin/xray-linux-arm64"; do
    if [[ -x "${p}" ]]; then
      echo "${p}"
      return 0
    fi
  done
  shopt -s nullglob
  local g
  for g in /usr/local/x-ui/bin/xray-linux-*; do
    if [[ -x "${g}" ]]; then
      echo "${g}"
      return 0
    fi
  done
  shopt -u nullglob
  return 1
}

write_xray_systemd() {
  local xray_bin="$1"
  cat > /etc/systemd/system/xray.service <<EOF
[Unit]
Description=Xray Core (socks-agent)
After=network.target

[Service]
Type=simple
ExecStart=${xray_bin} run -config ${XRAY_CONFIG}
Restart=on-failure
RestartSec=3
LimitNOFILE=1048576

[Install]
WantedBy=multi-user.target
EOF
}

install_xray_from_vaxilu_xui() {
  local arch dest_dir dest_name existing tag url tmp extracted ver dest
  arch="$(detect_xui_arch)"
  dest_dir="${AGENT_HOME}/bin"
  dest_name="xray-linux-${arch}"
  dest="${dest_dir}/${dest_name}"
  mkdir -p "${dest_dir}"

  if existing="$(find_existing_vaxilu_xray)"; then
    info "复用本机 vaxilu XUI 自带 Xray: ${existing}"
    cp -f "${existing}" "${dest}"
    chmod +x "${dest}"
    for f in geoip.dat geosite.dat; do
      [[ -f "/usr/local/x-ui/bin/${f}" ]] && cp -f "/usr/local/x-ui/bin/${f}" "${dest_dir}/" || true
    done
  else
    tag="${VAXILU_XUI_TAG}"
    if [[ -z "${tag}" ]]; then
      tag="$(curl -fsSL "https://api.github.com/repos/${VAXILU_XUI_REPO}/releases/latest" \
        | grep '"tag_name"' | head -1 | sed -E 's/.*"([^"]+)".*/\1/')" || true
    fi
    [[ -n "${tag}" ]] || error "无法获取 vaxilu/x-ui 发布版本"
    url="https://github.com/${VAXILU_XUI_REPO}/releases/download/${tag}/x-ui-linux-${arch}.tar.gz"
    info "下载 vaxilu XUI ${tag} 发布包（仅提取 Xray 内核）..."
    tmp="$(mktemp -d /tmp/xui-xray.XXXXXX)"
    if ! curl -fsSL "${url}" | tar -xz -C "${tmp}"; then
      rm -rf "${tmp}"
      error "下载失败: ${url}"
    fi
    extracted="$(find "${tmp}" -type f -name 'xray-linux-*' | head -1)"
    [[ -n "${extracted}" && -f "${extracted}" ]] || {
      rm -rf "${tmp}"
      error "发布包中未找到 xray-linux-* 二进制"
    }
    cp -f "${extracted}" "${dest}"
    chmod +x "${dest}"
    for f in geoip.dat geosite.dat; do
      local geo
      geo="$(find "${tmp}" -name "${f}" | head -1)"
      [[ -n "${geo}" && -f "${geo}" ]] && cp -f "${geo}" "${dest_dir}/"
    done
    rm -rf "${tmp}"
    info "已从 vaxilu/x-ui ${tag} 提取 Xray → ${dest}"
  fi

  ln -sf "${dest_name}" "${dest_dir}/xray"
  ver="$("${dest}" version 2>/dev/null | head -n1 || true)"
  info "Xray 版本: ${ver:-未知}"
  XRAY_BIN="${dest}"
}

install_xray_official() {
  if [[ ! -x /usr/local/bin/xray ]]; then
    info "安装 XTLS 官方 Xray-core ..."
    bash -c "$(curl -fsSL https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" @ install
  else
    info "已存在官方 Xray: $(/usr/local/bin/xray version 2>/dev/null | head -n1 || true)"
  fi
  [[ -x /usr/local/bin/xray ]] || error "Xray 安装失败"
  XRAY_BIN="/usr/local/bin/xray"
}

assert_xray_bin() {
  [[ -n "${XRAY_BIN}" && "${XRAY_BIN}" == /* && -x "${XRAY_BIN}" ]] \
    || error "Xray 可执行文件无效: ${XRAY_BIN:-<空>}"
}

mkdir -p /usr/local/etc/xray
XRAY_CONFIG="/usr/local/etc/xray/config.json"
XRAY_SERVICE="xray"

case "${XRAY_KERNEL}" in
  xui)
    install_xray_from_vaxilu_xui
    assert_xray_bin
    write_xray_systemd "${XRAY_BIN}"
    XRAY_KERNEL_LABEL="vaxilu XUI 内核"
    ;;
  official)
    install_xray_official
    assert_xray_bin
    XRAY_KERNEL_LABEL="XTLS 官方最新"
    ;;
  *)
    error "未知 XRAY_KERNEL=${XRAY_KERNEL}，可用: xui / official"
    ;;
esac

mkdir -p "${AGENT_HOME}/data" "${AGENT_HOME}/agent"
info "部署 Agent 文件 ..."
cp -a "${BUNDLE_DIR}/agent/." "${AGENT_HOME}/agent/"
cp -f "${BUNDLE_DIR}/requirements.txt" "${AGENT_HOME}/requirements.txt"
[[ -f "${BUNDLE_DIR}/uninstall.sh" ]] && cp -f "${BUNDLE_DIR}/uninstall.sh" "${AGENT_HOME}/uninstall.sh"

VENV_DIR="${AGENT_HOME}/.venv"
VENV_PY="${VENV_DIR}/bin/python"

install_venv_pkg() {
  if command -v apt-get >/dev/null 2>&1; then
    local pyver
    pyver="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || true)"
    export DEBIAN_FRONTEND=noninteractive
    if wait_for_dpkg_lock 300; then
      apt-get update -y || true
      apt-get install -y python3-venv python3-pip ${pyver:+python${pyver}-venv} || true
    else
      warn "无法获取 dpkg 锁，稍后请手动: apt install python3-venv"
    fi
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y python3-virtualenv python3-pip || true
  elif command -v yum >/dev/null 2>&1; then
    yum install -y python3-virtualenv python3-pip || true
  fi
}

# 尝试用标准 venv 建环境（3.12 失败会自清目录，需重试）
try_std_venv() {
  rm -rf "${VENV_DIR}"
  python3 -m venv "${VENV_DIR}" 2>/dev/null && [[ -x "${VENV_PY}" ]]
}

# 无 ensurepip 时：--without-pip 建环境，再 get-pip 补 pip
try_venv_without_pip() {
  rm -rf "${VENV_DIR}"
  python3 -m venv --without-pip "${VENV_DIR}" 2>/dev/null || return 1
  [[ -x "${VENV_PY}" ]] || return 1
  if ! "${VENV_PY}" -m pip --version >/dev/null 2>&1; then
    curl -fsSL https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py || return 1
    "${VENV_PY}" /tmp/get-pip.py >/dev/null 2>&1 || { rm -f /tmp/get-pip.py; return 1; }
    rm -f /tmp/get-pip.py
  fi
  return 0
}

ensure_venv() {
  if try_std_venv && "${VENV_PY}" -m pip --version >/dev/null 2>&1; then
    return 0
  fi
  warn "venv 不可用，安装 python3-venv 后重试 ..."
  install_venv_pkg
  if try_std_venv && "${VENV_PY}" -m pip --version >/dev/null 2>&1; then
    return 0
  fi
  warn "标准 venv 仍失败，回退 --without-pip + get-pip ..."
  try_venv_without_pip && return 0
  error "无法创建 Python venv。请手动执行: apt install -y python3-venv python3-pip 后重跑安装脚本"
}

ensure_venv
info "Python venv: $(${VENV_PY} --version 2>&1)"
"${VENV_PY}" -m pip install -U pip wheel >/dev/null 2>&1 || warn "pip 升级失败，继续尝试安装依赖"
"${VENV_PY}" -m pip install -r "${AGENT_HOME}/requirements.txt"

gen_secret() {
  openssl rand -hex "${1:-16}" 2>/dev/null || head -c "${1:-16}" /dev/urandom | od -An -tx1 | tr -d ' \n'
}

ENV_FILE="${AGENT_HOME}/agent.env"
if [[ -f "${ENV_FILE}" ]]; then
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  info "沿用已有配置（${ENV_FILE}）"
fi
# 缺失项才生成，已有值沿用（升级不改 Token / 面板密码）
AGENT_API_TOKEN="${AGENT_API_TOKEN:-$(gen_secret 24)}"
PANEL_USER="${PANEL_USER:-admin}"
PANEL_PASS="${PANEL_PASS:-$(gen_secret 6)}"
PANEL_SECRET="${PANEL_SECRET:-$(gen_secret 16)}"
PUBLIC_IP="$(curl -4 -fsS --max-time 5 ifconfig.me 2>/dev/null || curl -4 -fsS --max-time 5 ip.sb 2>/dev/null || echo '')"
AGENT_PUBLIC_IP="${AGENT_PUBLIC_IP:-${PUBLIC_IP}}"
cat > "${ENV_FILE}" <<EOF
AGENT_LISTEN_HOST=0.0.0.0
AGENT_LISTEN_PORT=${AGENT_PORT}
AGENT_API_TOKEN=${AGENT_API_TOKEN}
AGENT_DATA_DIR=${AGENT_HOME}/data
AGENT_SHARED_PORT=${SHARED_PORT}
AGENT_PUBLIC_IP=${AGENT_PUBLIC_IP}
XRAY_BIN=${XRAY_BIN}
XRAY_CONFIG=${XRAY_CONFIG}
XRAY_API_ADDR=127.0.0.1:${XRAY_API_PORT}
XRAY_SERVICE=${XRAY_SERVICE}
XRAY_KERNEL=${XRAY_KERNEL}
AGENT_SERVICE=socks-agent
PANEL_ENABLE=1
PANEL_USER=${PANEL_USER}
PANEL_PASS=${PANEL_PASS}
PANEL_SECRET=${PANEL_SECRET}
EOF
chmod 600 "${ENV_FILE}"

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
# 等待独立共享账号播种（SHARED_SOCKS.txt）
for _i in 1 2 3 4 5 6 7 8; do
  if [[ -f "${AGENT_HOME}/data/SHARED_SOCKS.txt" ]]; then
    break
  fi
  sleep 1
done
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

PUBLIC_IP="${AGENT_PUBLIC_IP:-$(curl -4 -fsS --max-time 5 ifconfig.me 2>/dev/null || curl -4 -fsS --max-time 5 ip.sb 2>/dev/null || echo 'YOUR_IP')}"
SHARED_SOCKS_FILE="${AGENT_HOME}/data/SHARED_SOCKS.txt"

echo
echo "============================================================"
echo -e "${GREEN}安装完成 — 可单独使用（无需机器人）${NC}"
echo "------------------------------------------------------------"
echo -e "  ${GREEN}Web 面板:     http://${PUBLIC_IP}:${AGENT_PORT}/panel${NC}"
echo "  面板账号:     ${PANEL_USER}"
echo "  面板密码:     ${PANEL_PASS}"
echo "  公网 IP:      ${PUBLIC_IP}"
echo "  共享 SOCKS:   ${AGENT_SHARED_PORT:-$SHARED_PORT}"
if [[ -f "${SHARED_SOCKS_FILE}" ]]; then
  echo "  共享账号:     见 ${SHARED_SOCKS_FILE}"
  # 打印便于立即使用（不含大段噪音）
  grep -E '^(user|pass|link)=' "${SHARED_SOCKS_FILE}" 2>/dev/null | sed 's/^/    /' || true
fi
echo "------------------------------------------------------------"
echo "独立使用：浏览器打开面板 → 入站/账号 → 新增或改共享账号 → 复制 socks5 链接。"
echo "对接机器人（可选）：面板类型选「极简 Agent」，填下面几项即可。"
echo "  Agent 地址:   http://${PUBLIC_IP}:${AGENT_LISTEN_PORT:-$AGENT_PORT}"
echo "  API Token:    ${AGENT_API_TOKEN}"
echo "  inbound_id:   1"
echo "  Xray 内核:    ${XRAY_KERNEL_LABEL} (${XRAY_BIN})"
echo "------------------------------------------------------------"
echo "请放行: ${AGENT_PORT}/tcp(面板) / ${SHARED_PORT}/tcp(共享) / ${PORT_RANGE_START}-${PORT_RANGE_END}/tcp(专属)"
echo "安全建议：云安全组把 ${AGENT_PORT} 只放行你的管理 IP（及 Bot 服务器 IP，若使用）。"
echo "跳过防火墙: SKIP_FIREWALL=1 bash install.sh"
echo "跳过 BBR:   SKIP_BBR=1 bash install.sh"
echo "改用旧版 XUI 内核: XRAY_KERNEL=xui bash install.sh"
echo "============================================================"
