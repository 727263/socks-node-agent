"""xray 内核版本管理：列出 / 下载替换 / 校验 / 回滚（对齐 XUI 的切换版本）。"""
from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import tempfile
import urllib.request
import zipfile
from typing import Any, Callable

log = logging.getLogger("socks-agent.xrayver")

XRAY_REPO = "XTLS/Xray-core"
_UA = {"User-Agent": "socks-node-agent"}


def detect_asset() -> str:
    m = platform.machine().lower()
    if m in ("x86_64", "amd64"):
        return "Xray-linux-64.zip"
    if m in ("aarch64", "arm64"):
        return "Xray-linux-arm64-v8a.zip"
    if m in ("armv7l", "armv7", "armv6l"):
        return "Xray-linux-arm32-v7a.zip"
    return "Xray-linux-64.zip"


def list_versions(limit: int = 30) -> list[dict[str, Any]]:
    url = f"https://api.github.com/repos/{XRAY_REPO}/releases?per_page={limit}"
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    out: list[dict[str, Any]] = []
    for r in data:
        tag = r.get("tag_name")
        if not tag:
            continue
        out.append({
            "tag": tag,
            "prerelease": bool(r.get("prerelease")),
            "published_at": r.get("published_at") or "",
        })
    return out


def _download_xray(tag: str, dest_dir: str) -> str:
    asset = detect_asset()
    url = f"https://github.com/{XRAY_REPO}/releases/download/{tag}/{asset}"
    zip_path = os.path.join(dest_dir, asset)
    log.info("downloading xray %s: %s", tag, url)
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=120) as resp, open(zip_path, "wb") as f:
        shutil.copyfileobj(resp, f)
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        member = next((n for n in names if n == "xray" or n.endswith("/xray")), None)
        if member is None:
            raise RuntimeError(f"release {tag} 压缩包内未找到 xray 可执行文件")
        z.extract(member, dest_dir)
        extracted = os.path.join(dest_dir, member)
    return extracted


def switch_version(
    tag: str,
    *,
    xray_bin: str,
    verify_restart: Callable[[], None],
) -> dict[str, Any]:
    """下载指定版本替换 xray_bin；校验重启失败自动回滚。verify_restart 需在失败时抛异常。"""
    if not tag:
        raise RuntimeError("缺少版本号")
    if not os.path.isabs(xray_bin):
        raise RuntimeError(f"XRAY_BIN 非法: {xray_bin}")

    tmp = tempfile.mkdtemp(prefix="xray-ver-")
    backup = f"{xray_bin}.bak"
    had_backup = False
    try:
        new_bin = _download_xray(tag, tmp)
        os.makedirs(os.path.dirname(xray_bin), exist_ok=True)
        if os.path.exists(xray_bin):
            shutil.copy2(xray_bin, backup)
            had_backup = True
        shutil.copy2(new_bin, xray_bin)
        os.chmod(xray_bin, 0o755)
        try:
            verify_restart()
        except Exception as e:  # noqa: BLE001
            log.warning("switch to %s failed, rolling back: %s", tag, e)
            if had_backup:
                shutil.copy2(backup, xray_bin)
                os.chmod(xray_bin, 0o755)
                try:
                    verify_restart()
                except Exception:  # noqa: BLE001
                    log.exception("rollback restart also failed")
            raise RuntimeError(f"切换到 {tag} 失败，已回滚: {e}") from e
        log.info("xray switched to %s", tag)
        return {"tag": tag, "xray_bin": xray_bin}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        if had_backup:
            try:
                os.remove(backup)
            except OSError:
                pass
