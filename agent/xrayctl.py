"""生成 Xray 配置，并通过 xray api 热更新入站。"""
from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("socks-agent.xray")


def inbound_to_xray(inb: dict[str, Any]) -> dict[str, Any]:
    settings = inb.get("settings", "{}")
    if isinstance(settings, str):
        try:
            settings_obj = json.loads(settings)
        except json.JSONDecodeError:
            settings_obj = {}
    else:
        settings_obj = settings or {}

    # Xray-core socks/mixed 入站账号字段是 accounts（与 3X-UI 一致）。
    # 若上游误传 users，统一转回 accounts，否则 xray 读不到账号会拒绝所有登录。
    if "users" in settings_obj and "accounts" not in settings_obj:
        settings_obj = dict(settings_obj)
        settings_obj["accounts"] = settings_obj.pop("users")

    sniff = inb.get("sniffing", "{}")
    if isinstance(sniff, str):
        try:
            sniff_obj = json.loads(sniff) if sniff else {"enabled": False}
        except json.JSONDecodeError:
            sniff_obj = {"enabled": False}
    else:
        sniff_obj = sniff or {"enabled": False}

    # listen 必须为 null（与 XUI 一致）：老版 xray(1.4.2) 多个 socks 入站
    # 都显式绑定 "0.0.0.0" 时账号验证会串台，只有最后一个入站生效。
    return {
        "tag": inb.get("tag") or f"in-{inb['id']}",
        "listen": None,
        "port": int(inb["port"]),
        "protocol": inb.get("protocol") or "socks",
        "settings": settings_obj,
        "streamSettings": {},
        "sniffing": sniff_obj,
    }


def build_full_config(enabled_inbounds: list[dict[str, Any]], api_port: int = 10085) -> dict:
    api_inbound = {
        "tag": "api",
        "listen": "127.0.0.1",
        "port": api_port,
        "protocol": "dokodemo-door",
        "settings": {"address": "127.0.0.1"},
    }
    user_inbounds = [inbound_to_xray(i) for i in enabled_inbounds if i.get("enable")]
    return {
        "log": {"loglevel": "warning"},
        "api": {
            "tag": "api",
            "services": ["HandlerService", "StatsService"],
        },
        "stats": {},
        "policy": {
            "levels": {
                "0": {
                    "statsUserUplink": True,
                    "statsUserDownlink": True,
                }
            },
            "system": {
                "statsInboundUplink": True,
                "statsInboundDownlink": True,
            },
        },
        "inbounds": [api_inbound] + user_inbounds,
        "outbounds": [
            {"protocol": "freedom", "tag": "direct"},
            {"protocol": "blackhole", "tag": "block"},
            # API 路由必需，否则带 stats/api 的配置可能异常
            {"protocol": "freedom", "tag": "api"},
        ],
        "routing": {
            "domainStrategy": "AsIs",
            "rules": [
                {"type": "field", "inboundTag": ["api"], "outboundTag": "api"},
            ],
        },
    }


class XrayController:
    def __init__(
        self,
        *,
        xray_bin: str,
        config_path: str,
        api_addr: str,
        service_name: str = "xray",
    ):
        self.xray_bin = xray_bin
        self.config_path = config_path
        self.api_addr = api_addr
        self.service_name = service_name
        self._api_port = int(api_addr.rsplit(":", 1)[-1])

    def write_config(self, enabled_inbounds: list[dict[str, Any]]) -> bool:
        """写入 Xray 配置；内容无变化返回 False（避免无谓重启）。"""
        cfg = build_full_config(enabled_inbounds, api_port=self._api_port)
        new_text = json.dumps(cfg, ensure_ascii=False, indent=2)
        path = Path(self.config_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if path.exists() and path.read_text(encoding="utf-8") == new_text:
                log.info("xray config unchanged, skip rewrite (%d inbounds)", len(enabled_inbounds))
                return False
        except OSError:
            pass
        tmp = path.with_suffix(".tmp")
        tmp.write_text(new_text, encoding="utf-8")
        os.replace(tmp, path)
        log.info("Wrote xray config: %s (%d inbounds)", path, len(enabled_inbounds))
        return True

    def _is_active(self) -> bool:
        try:
            r = subprocess.run(
                ["systemctl", "is-active", self.service_name],
                capture_output=True, text=True, timeout=10, check=False,
            )
            return r.stdout.strip() == "active"
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
        try:
            with socket.create_connection((host, int(port)), timeout=timeout):
                return True
        except OSError:
            return False

    def _wait_ports_ready(self, ports: list[int], timeout: float = 15.0) -> None:
        """重启后等所有入站端口真正 listen，确保账号已加载再返回。"""
        pending = {int(p) for p in ports if p}
        if not pending:
            return
        deadline = time.time() + timeout
        while pending and time.time() < deadline:
            pending = {p for p in pending if not self._port_open(p)}
            if pending:
                time.sleep(0.3)
        if pending:
            log.warning("xray ports not ready %.0fs after restart: %s", timeout, sorted(pending))
        else:
            log.info("xray all ports ready after restart")

    def restart_service(self, expected_ports: Optional[list[int]] = None) -> None:
        try:
            # 先校验配置，避免写入坏配置后 xray 起不来
            test = subprocess.run(
                [self.xray_bin, "run", "-test", "-config", self.config_path],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if test.returncode != 0:
                msg = (test.stderr or test.stdout or "").strip()
                raise RuntimeError(f"xray config test failed: {msg}")
            subprocess.run(
                ["systemctl", "restart", self.service_name],
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
            )
            log.info("Restarted systemd service: %s", self.service_name)
        except Exception as e:  # noqa: BLE001
            log.warning("systemctl restart failed (%s): %s", self.service_name, e)
            raise
        self._wait_ports_ready(expected_ports or [])

    def apply_from_store(self, enabled_inbounds: list[dict[str, Any]]) -> None:
        """写完整配置并按需重启 Xray（配置未变则不重启，避免重启竞争）。"""
        changed = self.write_config(enabled_inbounds)
        ports = [int(i["port"]) for i in enabled_inbounds if i.get("port")]
        if changed:
            self.restart_service(expected_ports=ports)
        elif not self._is_active():
            log.info("config unchanged but xray inactive, restarting")
            self.restart_service(expected_ports=ports)
        else:
            log.info("config unchanged and xray active, skip restart")

    def sync_live_from_store(self, enabled_inbounds: list[dict[str, Any]]) -> None:
        """安装/启动时全量同步。"""
        self.apply_from_store(enabled_inbounds)

    def _run_api(self, args: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
        cmd = [self.xray_bin, "api", *args, f"--server={self.api_addr}"]
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False,
        )

    def add_inbound_live(self, inb: dict[str, Any]) -> None:
        payload = inbound_to_xray(inb)
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            json.dump(payload, f, ensure_ascii=False)
            tmp_path = f.name
        try:
            # 兼容不同 xray 版本的子命令：adi / adu
            for sub in ("adi", "adu"):
                r = self._run_api([sub, tmp_path])
                if r.returncode == 0:
                    log.info("xray api %s ok tag=%s", sub, payload["tag"])
                    return
                last = r
            raise RuntimeError(
                f"xray api add inbound failed: {(last.stderr or last.stdout or '').strip()}"
            )
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    def remove_inbound_live(self, tag: str) -> None:
        r = self._run_api(["rmi", tag])
        if r.returncode != 0:
            # 不存在时忽略
            msg = (r.stderr or r.stdout or "").lower()
            if "not found" in msg or "no such" in msg:
                log.info("xray inbound already gone: %s", tag)
                return
            raise RuntimeError(f"xray api rmi failed: {(r.stderr or r.stdout or '').strip()}")
        log.info("xray api rmi ok tag=%s", tag)

    def query_inbound_traffic(self) -> dict[str, tuple[int, int]]:
        """返回 {tag: (uplink, downlink)} 累计值（自 xray 启动/上次 reset）。"""
        r = self._run_api(["statsquery", f"--pattern=inbound>>>"])
        if r.returncode != 0:
            log.warning("statsquery failed: %s", (r.stderr or r.stdout or "").strip())
            return {}
        try:
            data = json.loads(r.stdout or "{}")
        except json.JSONDecodeError:
            log.warning("statsquery invalid json: %s", r.stdout[:200])
            return {}
        result: dict[str, tuple[int, int]] = {}
        # 兼容 {"stat":[...]} 或直接 list
        stats = data.get("stat") if isinstance(data, dict) else data
        if not isinstance(stats, list):
            return {}
        ups: dict[str, int] = {}
        downs: dict[str, int] = {}
        for item in stats:
            if not isinstance(item, dict):
                continue
            name = item.get("name") or ""
            # inbound>>>in-1>>>traffic>>>uplink
            parts = name.split(">>>")
            if len(parts) != 4 or parts[0] != "inbound" or parts[2] != "traffic":
                continue
            tag, direction = parts[1], parts[3]
            val = int(item.get("value") or 0)
            if direction == "uplink":
                ups[tag] = val
            elif direction == "downlink":
                downs[tag] = val
        tags = set(ups) | set(downs)
        for tag in tags:
            result[tag] = (ups.get(tag, 0), downs.get(tag, 0))
        return result

    def reset_inbound_stats(self, tag: str) -> None:
        for direction in ("uplink", "downlink"):
            name = f"inbound>>>{tag}>>>traffic>>>{direction}"
            r = self._run_api(["stats", f"--name={name}", "-reset"])
            if r.returncode != 0:
                log.debug("reset stats %s: %s", name, (r.stderr or r.stdout or "").strip())
