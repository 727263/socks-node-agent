"""系统与服务状态采集（Linux，读 /proc + systemctl）。"""
from __future__ import annotations

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


def cpu_percent(interval: float = 0.3) -> float:
    a = _cpu_snapshot()
    if a is None:
        return 0.0
    time.sleep(interval)
    b = _cpu_snapshot()
    if b is None:
        return 0.0
    dt = b[0] - a[0]
    di = b[1] - a[1]
    if dt <= 0:
        return 0.0
    return round((1 - di / dt) * 100, 1)


def mem_info() -> dict[str, int]:
    total = avail = 0
    for ln in _read("/proc/meminfo").splitlines():
        if ln.startswith("MemTotal:"):
            total = int(ln.split()[1]) * 1024
        elif ln.startswith("MemAvailable:"):
            avail = int(ln.split()[1]) * 1024
    used = max(0, total - avail)
    percent = round(used / total * 100, 1) if total else 0.0
    return {"total": total, "used": used, "avail": avail, "percent": percent}


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
    # /proc/uptime 第一个字段是系统开机至今秒数，与 systemd monotonic 同源
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
        # 形如 "Xray 1.4.2 (Xray, ...)" -> 取版本号
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
        return f"{d}天{h}小时"
    if h:
        return f"{h}小时{m}分"
    return f"{m}分"


def overview(*, xray_bin: str, xray_service: str, agent_service: str) -> dict[str, Any]:
    xr_up = service_uptime_seconds(xray_service)
    ag_up = service_uptime_seconds(agent_service)
    return {
        "cpu": {"percent": cpu_percent(), "count": cpu_count()},
        "mem": mem_info(),
        "load": load_avg(),
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
