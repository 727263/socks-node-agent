"""Rebuild panel/API port firewall rules after PANEL_ALLOW_IP changes."""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
from typing import Any

log = logging.getLogger("socks-agent.firewall")

_DENY_COMMENT = "socks-agent-api-deny"
_ACCEPT_COMMENT = "socks-agent-api"
_LOCAL_COMMENT = "socks-agent-api-local"
_UFW_COMMENT = "socks-agent-api"


def parse_ip_list(raw: str) -> list[str]:
    raw = (raw or "").strip()
    if not raw or raw in ("*", "0.0.0.0", "any"):
        return []
    out: list[str] = []
    for part in raw.replace(";", ",").split(","):
        ip = part.strip()
        if ip and ip not in out:
            out.append(ip)
    return out


def _run(cmd: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)


def _iptables_rebuild(port: int, ips: list[str]) -> None:
    for _ in range(32):
        r = _run(["iptables", "-D", "INPUT", "-p", "tcp", "--dport", str(port), "-j", "DROP", "-m", "comment", "--comment", _DENY_COMMENT])
        if r.returncode != 0:
            break
    for _ in range(32):
        r = _run(["iptables", "-D", "INPUT", "-p", "tcp", "--dport", str(port), "-j", "ACCEPT", "-m", "comment", "--comment", _ACCEPT_COMMENT])
        if r.returncode != 0:
            break
    for _ in range(32):
        r = _run(["iptables", "-D", "INPUT", "-p", "tcp", "-s", "127.0.0.1", "--dport", str(port), "-j", "ACCEPT", "-m", "comment", "--comment", _LOCAL_COMMENT])
        if r.returncode != 0:
            break
    if ips:
        for ip in ips:
            for _ in range(8):
                r = _run(["iptables", "-D", "INPUT", "-p", "tcp", "-s", ip, "--dport", str(port), "-j", "ACCEPT", "-m", "comment", "--comment", _ACCEPT_COMMENT])
                if r.returncode != 0:
                    break
        _run(["iptables", "-I", "INPUT", "-p", "tcp", "--dport", str(port), "-j", "DROP", "-m", "comment", "--comment", _DENY_COMMENT])
        _run(["iptables", "-I", "INPUT", "-p", "tcp", "-s", "127.0.0.1", "--dport", str(port), "-j", "ACCEPT", "-m", "comment", "--comment", _LOCAL_COMMENT])
        for ip in ips:
            _run(["iptables", "-I", "INPUT", "-p", "tcp", "-s", ip, "--dport", str(port), "-j", "ACCEPT", "-m", "comment", "--comment", _ACCEPT_COMMENT])
    else:
        _run(["iptables", "-I", "INPUT", "-p", "tcp", "--dport", str(port), "-j", "ACCEPT", "-m", "comment", "--comment", _ACCEPT_COMMENT])


def _ufw_clear_panel(port: int) -> None:
    _run(["ufw", "delete", "allow", f"{port}/tcp"])
    for _ in range(16):
        st = _run(["ufw", "status", "numbered"])
        if st.returncode != 0:
            break
        nums: list[str] = []
        for line in (st.stdout or "").splitlines():
            m = re.match(r"^\[\s*(\d+)\]", line)
            if not m:
                continue
            if _UFW_COMMENT in line or (f"{port}/tcp" in line and "ALLOW" in line.upper()):
                nums.append(m.group(1))
        if not nums:
            break
        for n in sorted((int(x) for x in nums), reverse=True):
            proc = subprocess.Popen(
                ["ufw", "delete", str(n)],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            try:
                proc.communicate("y\n", timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
    _run(["ufw", "delete", "allow", f"{port}/tcp"])


def _ufw_apply(port: int, ips: list[str]) -> None:
    _ufw_clear_panel(port)
    if ips:
        for ip in ips:
            _run(["ufw", "allow", "from", ip, "to", "any", "port", str(port), "proto", "tcp", "comment", _UFW_COMMENT])
    else:
        _run(["ufw", "allow", f"{port}/tcp", "comment", _UFW_COMMENT])


def _firewalld_apply(port: int, ips: list[str]) -> None:
    _run(["firewall-cmd", "--permanent", "--remove-port", f"{port}/tcp"])
    st = _run(["firewall-cmd", "--permanent", "--list-rich-rules"])
    if st.returncode == 0:
        for line in (st.stdout or "").splitlines():
            line = line.strip()
            if f'port="{port}"' in line or f"port port=\"{port}\"" in line:
                _run(["firewall-cmd", "--permanent", "--remove-rich-rule", line])
    if ips:
        for ip in ips:
            rule = f'rule family="ipv4" source address="{ip}" port port="{port}" protocol="tcp" accept'
            _run(["firewall-cmd", "--permanent", "--add-rich-rule", rule])
    else:
        _run(["firewall-cmd", "--permanent", "--add-port", f"{port}/tcp"])
    _run(["firewall-cmd", "--reload"])


def _persist_iptables() -> None:
    if shutil.which("netfilter-persistent"):
        _run(["netfilter-persistent", "save"])
    elif shutil.which("service") and __import__("pathlib").Path("/etc/init.d/iptables").is_file():
        _run(["service", "iptables", "save"])
    else:
        rules = __import__("pathlib").Path("/etc/iptables")
        rules.mkdir(parents=True, exist_ok=True)
        r = _run(["iptables-save"])
        if r.returncode == 0 and r.stdout:
            rules.joinpath("rules.v4").write_text(r.stdout, encoding="utf-8")


def apply_panel_allow(port: int, allow_raw: str) -> dict[str, Any]:
    """Apply panel/API allowlist to host firewall. Returns per-backend status."""
    ips = parse_ip_list(allow_raw)
    out: dict[str, Any] = {"ips": ips, "applied": [], "skipped": [], "errors": []}

    if shutil.which("ufw"):
        try:
            _ufw_apply(port, ips)
            out["applied"].append("ufw")
        except Exception as e:  # noqa: BLE001
            log.warning("ufw panel allow failed: %s", e)
            out["errors"].append(f"ufw: {e}")
    else:
        out["skipped"].append("ufw")

    if shutil.which("firewall-cmd") and _run(["systemctl", "is-active", "--quiet", "firewalld"]).returncode == 0:
        try:
            _firewalld_apply(port, ips)
            out["applied"].append("firewalld")
        except Exception as e:  # noqa: BLE001
            log.warning("firewalld panel allow failed: %s", e)
            out["errors"].append(f"firewalld: {e}")
    else:
        out["skipped"].append("firewalld")

    if shutil.which("iptables"):
        try:
            _iptables_rebuild(port, ips)
            _persist_iptables()
            out["applied"].append("iptables")
        except Exception as e:  # noqa: BLE001
            log.warning("iptables panel allow failed: %s", e)
            out["errors"].append(f"iptables: {e}")
    else:
        out["skipped"].append("iptables")

    if not out["applied"]:
        out["errors"].append("no firewall backend updated; configure cloud security group manually")
    return out
