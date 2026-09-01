"""SOCKS5 极简节点 Agent：直接管控 Xray，无需 3X-UI。"""

from __future__ import annotations

from pathlib import Path

__version__ = "1.1.4"



def read_version() -> str:
    """优先读安装目录旁的 VERSION 文件，否则用包内默认值。"""
    candidates = [
        Path(__file__).resolve().parent.parent / "VERSION",
        Path("/opt/socks-agent/VERSION"),
    ]
    for p in candidates:
        try:
            if p.is_file():
                v = p.read_text(encoding="utf-8").strip().splitlines()[0].strip()
                if v:
                    return v
        except OSError:
            continue
    return __version__
