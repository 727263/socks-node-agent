"""Per-TCP-port bandwidth limits via Linux tc+IFB (user upload/download)."""
from __future__ import annotations

import logging
import os
import subprocess
import threading
from typing import Optional

log = logging.getLogger("socks-agent.ratelimit")

IFB_DEV = "ifb0"


class RateLimiter:
    def __init__(self, iface: str = "") -> None:
        self._iface = (iface or "").strip()
        self._lock = threading.RLock()
        self._ready = False
        self._ports: dict[int, tuple[int, int]] = {}

    def apply(self, port: int, uplink_mbps: int, downlink_mbps: int) -> None:
        if port <= 0 or port > 65535:
            return
        uplink_mbps = max(0, int(uplink_mbps or 0))
        downlink_mbps = max(0, int(downlink_mbps or 0))
        with self._lock:
            if not self._ensure_ready():
                return
            prev = self._ports.get(port, (-1, -1))
            if prev == (uplink_mbps, downlink_mbps):
                return
            self._clear_port(port)
            if uplink_mbps == 0 and downlink_mbps == 0:
                self._ports.pop(port, None)
                return
            minor = port & 0xFFFF
            prio = (port % 32000) + 1
            try:
                if downlink_mbps > 0:
                    self._run(
                        "tc", "class", "add", "dev", self._iface, "parent", "1:",
                        "classid", f"1:{minor}", "htb", "rate", f"{downlink_mbps}mbit",
                        "ceil", f"{downlink_mbps}mbit",
                    )
                    self._run(
                        "tc", "filter", "add", "dev", self._iface, "parent", "1:",
                        "protocol", "ip", "prio", str(prio), "u32",
                        "match", "ip", "sport", str(port), "0xffff",
                        "flowid", f"1:{minor}",
                    )
                if uplink_mbps > 0:
                    self._run(
                        "tc", "filter", "add", "dev", self._iface, "parent", "ffff:",
                        "protocol", "ip", "prio", str(prio), "u32",
                        "match", "ip", "dport", str(port), "0xffff",
                        "action", "mirred", "egress", "redirect", "dev", IFB_DEV,
                    )
                    self._run(
                        "tc", "class", "add", "dev", IFB_DEV, "parent", "1:",
                        "classid", f"1:{minor}", "htb", "rate", f"{uplink_mbps}mbit",
                        "ceil", f"{uplink_mbps}mbit",
                    )
                    self._run(
                        "tc", "filter", "add", "dev", IFB_DEV, "parent", "1:",
                        "protocol", "ip", "prio", str(prio), "u32",
                        "match", "ip", "dport", str(port), "0xffff",
                        "flowid", f"1:{minor}",
                    )
            except Exception as e:  # noqa: BLE001
                self._clear_port(port)
                log.warning("ratelimit apply port=%s failed: %s", port, e)
                return
            self._ports[port] = (uplink_mbps, downlink_mbps)
            log.info(
                "ratelimit port=%s up=%sMbps down=%sMbps iface=%s",
                port, uplink_mbps, downlink_mbps, self._iface,
            )

    def remove(self, port: int) -> None:
        if port <= 0:
            return
        with self._lock:
            if not self._ensure_ready():
                return
            self._clear_port(port)
            self._ports.pop(port, None)

    def reconcile_all(self, items: list[dict]) -> None:
        for it in items:
            self.apply(
                int(it.get("port") or 0),
                int(it.get("uplinkLimitMbps") or it.get("uplink_limit_mbps") or 0),
                int(it.get("downlinkLimitMbps") or it.get("downlink_limit_mbps") or 0),
            )

    def _ensure_ready(self) -> bool:
        if self._ready:
            return True
        if os.name != "posix" or not os.path.exists("/proc/net/route"):
            log.debug("ratelimit skipped: not linux")
            return False
        iface = self._iface or _default_route_iface()
        if not iface:
            log.warning("ratelimit: no default route interface")
            return False
        self._iface = iface
        self._run_ignore("modprobe", "ifb", "numifbs=1")
        self._run_ignore("ip", "link", "set", "dev", IFB_DEV, "up")
        self._run_ignore_exists(
            "tc", "qdisc", "add", "dev", iface, "root", "handle", "1:", "htb", "default", "9999",
        )
        self._run_ignore_exists(
            "tc", "class", "add", "dev", iface, "parent", "1:", "classid", "1:9999",
            "htb", "rate", "10gbit", "ceil", "10gbit",
        )
        self._run_ignore_exists("tc", "qdisc", "add", "dev", iface, "handle", "ffff:", "ingress")
        self._run_ignore_exists(
            "tc", "qdisc", "add", "dev", IFB_DEV, "root", "handle", "1:", "htb", "default", "9999",
        )
        self._run_ignore_exists(
            "tc", "class", "add", "dev", IFB_DEV, "parent", "1:", "classid", "1:9999",
            "htb", "rate", "10gbit", "ceil", "10gbit",
        )
        self._ready = True
        return True

    def _clear_port(self, port: int) -> None:
        minor = port & 0xFFFF
        prio = (port % 32000) + 1
        self._run_ignore_missing(
            "tc", "filter", "del", "dev", self._iface, "parent", "1:",
            "protocol", "ip", "prio", str(prio),
        )
        self._run_ignore_missing(
            "tc", "class", "del", "dev", self._iface, "classid", f"1:{minor}",
        )
        self._run_ignore_missing(
            "tc", "filter", "del", "dev", self._iface, "parent", "ffff:",
            "protocol", "ip", "prio", str(prio),
        )
        self._run_ignore_missing(
            "tc", "filter", "del", "dev", IFB_DEV, "parent", "1:",
            "protocol", "ip", "prio", str(prio),
        )
        self._run_ignore_missing(
            "tc", "class", "del", "dev", IFB_DEV, "classid", f"1:{minor}",
        )

    def _run(self, *args: str) -> None:
        r = subprocess.run(args, capture_output=True, text=True, check=False)
        if r.returncode == 0:
            return
        msg = (r.stderr or r.stdout or "").strip() or f"exit {r.returncode}"
        raise RuntimeError(f"{' '.join(args)}: {msg}")

    def _run_ignore(self, *args: str) -> None:
        try:
            self._run(*args)
        except Exception:  # noqa: BLE001
            pass

    def _run_ignore_exists(self, *args: str) -> None:
        try:
            self._run(*args)
        except Exception as e:  # noqa: BLE001
            if "File exists" in str(e) or "EEXIST" in str(e):
                return
            raise

    def _run_ignore_missing(self, *args: str) -> None:
        try:
            self._run(*args)
        except Exception as e:  # noqa: BLE001
            if "No such file" in str(e) or "Cannot find" in str(e):
                return


def _default_route_iface() -> str:
    try:
        with open("/proc/net/route", encoding="utf-8") as f:
            for line in f.read().splitlines()[1:]:
                parts = line.split()
                if len(parts) >= 11 and parts[1] == "00000000":
                    return parts[0]
    except OSError:
        return ""
    return ""


def limits_from_inbound(inb: dict) -> tuple[int, int]:
    up = int(inb.get("uplinkLimitMbps") or inb.get("uplink_limit_mbps") or 0)
    down = int(inb.get("downlinkLimitMbps") or inb.get("downlink_limit_mbps") or 0)
    return max(0, up), max(0, down)
