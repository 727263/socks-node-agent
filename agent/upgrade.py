"""Agent 自升级：从 GitHub 拉取 socks-node-agent 包并重启服务。"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Optional

from . import read_version

log = logging.getLogger("agent.upgrade")

DEFAULT_REPO = "727263/socks-node-agent"
DEFAULT_REF = "main"


def agent_home() -> Path:
    """安装根目录：含 agent/、VERSION、requirements.txt。"""
    # /opt/socks-agent/agent/__init__.py -> /opt/socks-agent
    return Path(__file__).resolve().parent.parent


def upgrade_repo() -> str:
    return (os.getenv("AGENT_REPO") or DEFAULT_REPO).strip() or DEFAULT_REPO


def upgrade_ref() -> str:
    return (os.getenv("AGENT_REF") or DEFAULT_REF).strip() or DEFAULT_REF


def info_payload() -> dict[str, Any]:
    return {
        "version": read_version(),
        "id_reuse": True,
        "repo": upgrade_repo(),
        "ref": upgrade_ref(),
    }


def _download_tarball(repo: str, ref: str, dest: Path) -> None:
    url = f"https://codeload.github.com/{repo}/tar.gz/{ref}"
    log.info("download agent package: %s", url)
    req = urllib.request.Request(url, headers={"User-Agent": "socks-node-agent-upgrade"})
    with urllib.request.urlopen(req, timeout=120) as resp, dest.open("wb") as f:
        shutil.copyfileobj(resp, f)


def _extract_bundle(tar_path: Path, work: Path) -> Path:
    with tarfile.open(tar_path, "r:gz") as tf:
        tf.extractall(work)
    dirs = [p for p in work.iterdir() if p.is_dir()]
    if not dirs:
        raise RuntimeError("升级包为空")
    bundle = dirs[0]
    if not (bundle / "agent" / "main.py").is_file():
        raise RuntimeError("升级包缺少 agent/main.py")
    return bundle


def _copy_tree(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


def _pip_install(home: Path) -> None:
    req = home / "requirements.txt"
    if not req.is_file():
        return
    py = home / ".venv" / "bin" / "python"
    if not py.is_file():
        py = Path("python3")
    subprocess.run(
        [str(py), "-m", "pip", "install", "-r", str(req)],
        check=False,
        timeout=300,
        cwd=str(home),
    )


def _restart_later(service: str, delay: float = 1.5) -> None:
    def _run() -> None:
        time.sleep(delay)
        try:
            subprocess.run(
                ["systemctl", "restart", service],
                check=False,
                timeout=60,
            )
            log.info("systemctl restart %s requested", service)
        except Exception as e:  # noqa: BLE001
            log.exception("restart failed: %s", e)

    threading.Thread(target=_run, name="agent-upgrade-restart", daemon=True).start()


def perform_upgrade(
    *,
    repo: Optional[str] = None,
    ref: Optional[str] = None,
    service: str = "socks-agent",
) -> dict[str, Any]:
    """下载并替换代码；成功后安排重启。不改 data/、agent.env。"""
    repo = (repo or upgrade_repo()).strip()
    ref = (ref or upgrade_ref()).strip()
    home = agent_home()
    agent_dir = home / "agent"
    if not agent_dir.is_dir():
        raise RuntimeError(f"找不到 agent 目录: {agent_dir}")

    work = Path(tempfile.mkdtemp(prefix="socks-agent-up-"))
    backup = Path(tempfile.mkdtemp(prefix="socks-agent-bak-"))
    try:
        tar_path = work / "src.tar.gz"
        _download_tarball(repo, ref, tar_path)
        bundle = _extract_bundle(tar_path, work / "extract")

        # 备份当前 agent/
        _copy_tree(agent_dir, backup / "agent")
        for name in ("VERSION", "requirements.txt"):
            src = home / name
            if src.is_file():
                shutil.copy2(src, backup / name)

        try:
            _copy_tree(bundle / "agent", agent_dir)
            if (bundle / "VERSION").is_file():
                shutil.copy2(bundle / "VERSION", home / "VERSION")
            if (bundle / "requirements.txt").is_file():
                shutil.copy2(bundle / "requirements.txt", home / "requirements.txt")
            _pip_install(home)
        except Exception:
            # 回滚 agent/
            log.exception("upgrade apply failed, rolling back")
            _copy_tree(backup / "agent", agent_dir)
            for name in ("VERSION", "requirements.txt"):
                bak = backup / name
                if bak.is_file():
                    shutil.copy2(bak, home / name)
            raise

        new_ver = read_version()
        _restart_later(service)
        return {
            "version": new_ver,
            "repo": repo,
            "ref": ref,
            "restarting": True,
            "service": service,
        }
    finally:
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(backup, ignore_errors=True)
