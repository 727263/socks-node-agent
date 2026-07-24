"""Agent 运行配置（环境变量 / 安装脚本写入的 env 文件）。"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class AgentConfig:
    listen_host: str = "0.0.0.0"
    listen_port: int = 9100
    api_token: str = ""
    data_dir: str = "/opt/socks-agent/data"
    xray_bin: str = "/usr/local/bin/xray"
    xray_config: str = "/usr/local/etc/xray/config.json"
    xray_api_addr: str = "127.0.0.1:10085"
    xray_service: str = "xray"
    # 共享 SOCKS 端口（独立使用 / Bot 节点「SOCKS 端口」）
    shared_port: int = 1080
    # 公网 IP（复制链接用；也可在面板里改，写入 settings.json）
    public_ip: str = ""
    traffic_sync_seconds: int = 20
    enforce_seconds: int = 30
    # Web 面板
    panel_enable: bool = True
    panel_user: str = ""
    panel_pass: str = ""
    panel_secret: str = ""
    agent_service: str = "socks-agent"


def _env_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def load_config() -> AgentConfig:
    return AgentConfig(
        listen_host=os.getenv("AGENT_LISTEN_HOST", "0.0.0.0"),
        listen_port=int(os.getenv("AGENT_LISTEN_PORT", "9100")),
        api_token=os.getenv("AGENT_API_TOKEN", "").strip(),
        data_dir=os.getenv("AGENT_DATA_DIR", "/opt/socks-agent/data"),
        xray_bin=os.getenv("XRAY_BIN", "/usr/local/bin/xray"),
        xray_config=os.getenv("XRAY_CONFIG", "/usr/local/etc/xray/config.json"),
        xray_api_addr=os.getenv("XRAY_API_ADDR", "127.0.0.1:10085"),
        xray_service=os.getenv("XRAY_SERVICE", "xray"),
        shared_port=int(os.getenv("AGENT_SHARED_PORT", "1080")),
        public_ip=os.getenv("AGENT_PUBLIC_IP", "").strip(),
        traffic_sync_seconds=int(os.getenv("AGENT_TRAFFIC_SYNC_SECONDS", "20")),
        enforce_seconds=int(os.getenv("AGENT_ENFORCE_SECONDS", "30")),
        panel_enable=_env_bool("PANEL_ENABLE", True),
        panel_user=os.getenv("PANEL_USER", "").strip(),
        panel_pass=os.getenv("PANEL_PASS", "").strip(),
        panel_secret=os.getenv("PANEL_SECRET", "").strip(),
        agent_service=os.getenv("AGENT_SERVICE", "socks-agent"),
    )
