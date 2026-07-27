"""系统与服务状态采集（Linux，读 /proc + systemctl）。"""
from __future__ import annotations

import os
import socket
import subprocess
import time
from typing import Any, Optional


def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def _cpu_snapshot() -> Optional[tuple[int, int]]:
    line = _read("/proc/stat").splitlines()
    if not line or not line[0].startswith("cpu "):
        return None
    parts = [int(x) for x in line[0].split()[1:]]
    idle = parts[3] + (parts[4] if len(parts) > 4 else 0)  # idle + iowait
    total = sum(parts)
    return total, idle


def cpu_percent_from_snapshots(
    a: Optional[tuple[int, int]], b: Optional[tuple[int, int]],
) -> float:
    if a is None or b is None:
        return 0.0
    dt = b[0] - a[0]
    di = b[1] - a[1]
    if dt <= 0:
        return 0.0
    return round((1 - di / dt) * 100, 2)


def cpu_percent(interval: float = 0.3) -> float:
    a = _cpu_snapshot()
    if a is None:
        return 0.0
    time.sleep(interval)
    return cpu_percent_from_snapshots(a, _cpu_snapshot())


def mem_info() -> dict[str, Any]:
    total = avail = 0
    for ln in _read("/proc/meminfo").splitlines():
        if ln.startswith("MemTotal:"):
            total = int(ln.split()[1]) * 1024
        elif ln.startswith("MemAvailable:"):
            avail = int(ln.split()[1]) * 1024
    used = max(0, total - avail)
    percent = round(used / total * 100, 2) if total else 0.0
    return {"total": total, "used": used, "avail": avail, "percent": percent}


def swap_info() -> dict[str, Any]:
    total = free = 0
    for ln in _read("/proc/meminfo").splitlines():
        if ln.startswith("SwapTotal:"):
            total = int(ln.split()[1]) * 1024
        elif ln.startswith("SwapFree:"):
            free = int(ln.split()[1]) * 1024
    used = max(0, total - free)
    percent = round(used / total * 100, 2) if total else 0.0
    return {"total": total, "used": used, "free": free, "percent": percent}


def disk_info(path: str = "/") -> dict[str, Any]:
    try:
        st = os.statvfs(path)
    except OSError:
        return {"total": 0, "used": 0, "free": 0, "percent": 0.0}
    total = st.f_frsize * st.f_blocks
    free = st.f_frsize * st.f_bavail
    used = max(0, total - free)
    percent = round(used / total * 100, 2) if total else 0.0
    return {"total": total, "used": used, "free": free, "percent": percent}


def load_avg() -> list[float]:
    parts = _read("/proc/loadavg").split()
    try:
        return [float(parts[0]), float(parts[1]), float(parts[2])]
    except (IndexError, ValueError):
        return [0.0, 0.0, 0.0]


def cpu_count() -> int:
    n = 0
    for ln in _read("/proc/cpuinfo").splitlines():
        if ln.startswith("processor"):
            n += 1
    return n or 1


def host_uptime_seconds() -> int:
    parts = _read("/proc/uptime").split()
    try:
        return max(0, int(float(parts[0])))
    except (IndexError, ValueError):
        return 0


def _parse_net_dev() -> dict[str, tuple[int, int]]:
    """iface -> (rx_bytes, tx_bytes)"""
    out: dict[str, tuple[int, int]] = {}
    lines = _read("/proc/net/dev").splitlines()
    for ln in lines[2:]:
        if ":" not in ln:
            continue
        name, rest = ln.split(":", 1)
        name = name.strip()
        cols = rest.split()
        if len(cols) < 9:
            continue
        try:
            out[name] = (int(cols[0]), int(cols[8]))
        except ValueError:
            continue
    return out


def _default_iface() -> str:
    """优先默认路由网卡，否则取非 lo 流量最大的。"""
    try:
        r = subprocess.run(
            ["ip", "route", "show", "default"],
            capture_output=True, text=True, timeout=3, check=False,
        )
        for tok in (r.stdout or "").split():
            # default via x.x.x.x dev eth0 ...
            pass
        parts = (r.stdout or "").split()
        if "dev" in parts:
            i = parts.index("dev")
            if i + 1 < len(parts):
                return parts[i + 1]
    except Exception:  # noqa: BLE001
        pass
    counters = _parse_net_dev()
    best, best_sum = "", -1
    for name, (rx, tx) in counters.items():
        if name == "lo":
            continue
        s = rx + tx
        if s > best_sum:
            best, best_sum = name, s
    return best or "eth0"


def net_snapshot() -> dict[str, Any]:
    iface = _default_iface()
    counters = _parse_net_dev()
    rx, tx = counters.get(iface, (0, 0))
    return {"iface": iface, "rx": rx, "tx": tx}


def net_rates(interval: float = 0.3) -> dict[str, Any]:
    a = net_snapshot()
    time.sleep(interval)
    b = net_snapshot()
    # 若采样间网卡名变化，用 b
    iface = b.get("iface") or a.get("iface") or ""
    if a.get("iface") == b.get("iface") and interval > 0:
        rx_rate = max(0, int((b["rx"] - a["rx"]) / interval))
        tx_rate = max(0, int((b["tx"] - a["tx"]) / interval))
    else:
        rx_rate = tx_rate = 0
    return {
        "iface": iface,
        "rx": b["rx"],
        "tx": b["tx"],
        "rx_rate": rx_rate,
        "tx_rate": tx_rate,
    }


def _count_proc_net(path: str) -> int:
    lines = _read(path).splitlines()
    # 首行表头
    return max(0, len(lines) - 1) if lines else 0


def conn_counts() -> dict[str, int]:
    tcp = _count_proc_net("/proc/net/tcp") + _count_proc_net("/proc/net/tcp6")
    udp = _count_proc_net("/proc/net/udp") + _count_proc_net("/proc/net/udp6")
    return {"tcp": tcp, "udp": udp}


def service_active(name: str) -> str:
    try:
        r = subprocess.run(
            ["systemctl", "is-active", name],
            capture_output=True, text=True, timeout=10, check=False,
        )
        return r.stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def service_uptime_seconds(name: str) -> int:
    """服务本次启动至今的秒数（读 ActiveEnterTimestampMonotonic）。"""
    try:
        r = subprocess.run(
            ["systemctl", "show", name, "--property=ActiveEnterTimestampMonotonic"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        raw = r.stdout.strip().split("=", 1)
        if len(raw) != 2 or not raw[1].isdigit():
            return 0
        start_us = int(raw[1])
        if start_us == 0:
            return 0
        now_us = _monotonic_us()
        return max(0, int((now_us - start_us) / 1_000_000))
    except Exception:  # noqa: BLE001
        return 0


def _monotonic_us() -> int:
    parts = _read("/proc/uptime").split()
    try:
        return int(float(parts[0]) * 1_000_000)
    except (IndexError, ValueError):
        return 0


def xray_version(xray_bin: str) -> str:
    try:
        r = subprocess.run(
            [xray_bin, "version"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        first = (r.stdout or "").strip().splitlines()
        if not first:
            return ""
        toks = first[0].split()
        for t in toks:
            if t and t[0].isdigit():
                return t
        return first[0]
    except Exception:  # noqa: BLE001
        return ""


def humanize_uptime(sec: int) -> str:
    if sec <= 0:
        return "-"
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    if d:
        return f"{d} 天"
    if h:
        return f"{h} 小时 {m} 分"
    return f"{m} 分"


def overview(*, xray_bin: str, xray_service: str, agent_service: str) -> dict[str, Any]:
    # CPU + 网速共用一次短采样，少睡一轮
    cpu_a = _cpu_snapshot()
    net_a = net_snapshot()
    time.sleep(0.35)
    cpu_b = _cpu_snapshot()
    net_b = net_snapshot()

    iface = net_b.get("iface") or net_a.get("iface") or ""
    if net_a.get("iface") == net_b.get("iface"):
        rx_rate = max(0, int((net_b["rx"] - net_a["rx"]) / 0.35))
        tx_rate = max(0, int((net_b["tx"] - net_a["tx"]) / 0.35))
    else:
        rx_rate = tx_rate = 0

    xr_up = service_uptime_seconds(xray_service)
    ag_up = service_uptime_seconds(agent_service)
    host_up = host_uptime_seconds()
    return {
        "cpu": {
            "percent": cpu_percent_from_snapshots(cpu_a, cpu_b),
            "count": cpu_count(),
        },
        "mem": mem_info(),
        "swap": swap_info(),
        "disk": disk_info("/"),
        "load": load_avg(),
        "net": {
            "iface": iface,
            "rx": net_b.get("rx", 0),
            "tx": net_b.get("tx", 0),
            "rx_rate": rx_rate,
            "tx_rate": tx_rate,
        },
        "conns": conn_counts(),
        "host": {
            "uptime": host_up,
            "uptime_h": humanize_uptime(host_up),
            "hostname": socket.gethostname(),
        },
        "xray": {
            "status": service_active(xray_service),
            "version": xray_version(xray_bin),
            "uptime": xr_up,
            "uptime_h": humanize_uptime(xr_up),
        },
        "agent": {
            "status": service_active(agent_service),
            "uptime": ag_up,
            "uptime_h": humanize_uptime(ag_up),
        },
    }
