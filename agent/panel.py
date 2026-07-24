"""Web 面板：登录会话 + 页面 + 面板 JSON 接口。账号数据与 Bot 共用 store。"""
from __future__ import annotations

import hmac
import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import sysinfo, xrayver

log = logging.getLogger("socks-agent.panel")

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

router = APIRouter()

GiB = 1024 ** 3


def _main():
    from . import main as m  # 延迟导入，避免循环依赖
    return m


def _logged_in(request: Request) -> bool:
    return bool(request.session.get("user"))


def _ok(obj: Any = None, msg: str = "") -> dict:
    return {"success": True, "msg": msg, "obj": obj}


def _fail(msg: str, obj: Any = None) -> dict:
    return {"success": False, "msg": msg, "obj": obj}


def _need_login() -> JSONResponse:
    return JSONResponse(_fail("未登录或会话已过期"), status_code=401)


# ---------------- 页面 ----------------

@router.get("/panel/login", response_class=HTMLResponse)
def login_page(request: Request):
    if _logged_in(request):
        return RedirectResponse("/panel", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request, "error": ""})


@router.post("/panel/login", response_class=HTMLResponse)
async def do_login(request: Request):
    form = await request.form()
    user = (form.get("username") or "").strip()
    pw = (form.get("password") or "").strip()
    cfg = _main().cfg
    ok_user = hmac.compare_digest(user, cfg.panel_user or "")
    ok_pass = hmac.compare_digest(pw, cfg.panel_pass or "")
    if not (cfg.panel_user and cfg.panel_pass and ok_user and ok_pass):
        return templates.TemplateResponse(
            "login.html", {"request": request, "error": "用户名或密码错误"}, status_code=401
        )
    request.session["user"] = user
    request.session["ts"] = int(time.time())
    return RedirectResponse("/panel", status_code=302)


@router.post("/panel/logout")
def do_logout(request: Request):
    request.session.clear()
    return _ok(msg="已登出")


@router.get("/panel", response_class=HTMLResponse)
def panel_page(request: Request):
    if not _logged_in(request):
        return RedirectResponse("/panel/login", status_code=302)
    cfg = _main().cfg
    return templates.TemplateResponse(
        "panel.html",
        {"request": request, "user": request.session.get("user"), "shared_port": cfg.shared_port},
    )


# ---------------- 面板接口（会话鉴权） ----------------

@router.get("/panel/api/overview")
def api_overview(request: Request):
    if not _logged_in(request):
        return _need_login()
    m = _main()
    cfg = m.cfg
    data = sysinfo.overview(
        xray_bin=cfg.xray_bin,
        xray_service=cfg.xray_service,
        agent_service=cfg.agent_service,
    )
    data["shared_port"] = cfg.shared_port
    data["inbound_count"] = len(m.store.list_all())
    data["public_ip"] = m.resolve_public_ip()
    data["standalone"] = True
    return _ok(data)


@router.get("/panel/api/settings")
def api_get_settings(request: Request):
    if not _logged_in(request):
        return _need_login()
    m = _main()
    return _ok({
        "public_ip": m.resolve_public_ip(),
        "shared_port": m.cfg.shared_port,
        "listen_port": m.cfg.listen_port,
    })


@router.post("/panel/api/settings")
async def api_set_settings(request: Request):
    if not _logged_in(request):
        return _need_login()
    m = _main()
    body = await request.json()
    if "public_ip" in body:
        ip = str(body.get("public_ip") or "").strip()
        m.local_settings.set("public_ip", ip)
    return _ok({
        "public_ip": m.resolve_public_ip(),
        "shared_port": m.cfg.shared_port,
        "listen_port": m.cfg.listen_port,
    }, msg="已保存")


@router.get("/panel/api/inbounds")
def api_inbounds(request: Request):
    if not _logged_in(request):
        return _need_login()
    m = _main()
    try:
        m._sync_traffic_once()
    except Exception:  # noqa: BLE001
        pass
    return _ok(m.store.list_all())


def _accounts_settings(user: str, pw: str) -> dict:
    return {
        "auth": "password",
        "accounts": [{"user": user, "pass": pw}] if user else [],
        "udp": False,
        "ip": "127.0.0.1",
    }


@router.post("/panel/api/inbounds")
async def api_add_inbound(request: Request):
    if not _logged_in(request):
        return _need_login()
    m = _main()
    body = await request.json()
    try:
        port = int(body.get("port") or 0)
        if port <= 0:
            return JSONResponse(_fail("端口无效"))
        user = (body.get("user") or "").strip()
        pw = (body.get("pass") or "").strip()
        if not user or not pw:
            return JSONResponse(_fail("用户名和密码必填"))
        total = int(float(body.get("total_gb") or 0) * GiB)
        days = int(body.get("expiry_days") or 0)
        expiry = int(time.time() * 1000) + days * 86400 * 1000 if days > 0 else 0
        with m._ops_lock:
            if port in m.store.used_ports():
                return JSONResponse(_fail(f"端口 {port} 已被占用"))
            inb = m.store.add(
                port=port,
                protocol="socks",
                remark=body.get("remark") or "",
                enable=bool(body.get("enable", True)),
                total=total,
                expiry_time=expiry,
                settings=json.dumps(_accounts_settings(user, pw), ensure_ascii=False),
            )
            m._persist_add(inb)
        return _ok(inb)
    except Exception as e:  # noqa: BLE001
        log.exception("panel add inbound failed")
        return JSONResponse(_fail(str(e)))


@router.post("/panel/api/inbounds/{inbound_id}")
async def api_update_inbound(inbound_id: int, request: Request):
    if not _logged_in(request):
        return _need_login()
    m = _main()
    body = await request.json()
    try:
        with m._ops_lock:
            old = m.store.get(inbound_id)
            if old is None:
                return JSONResponse(_fail(f"入站 {inbound_id} 不存在"))
            fields: dict[str, Any] = {}
            if "port" in body and int(body["port"]) > 0:
                fields["port"] = int(body["port"])
            if "enable" in body:
                fields["enable"] = bool(body["enable"])
            if "remark" in body:
                fields["remark"] = body["remark"] or ""
            if "total_gb" in body:
                fields["total"] = int(float(body.get("total_gb") or 0) * GiB)
            if "expiry_days" in body:
                days = int(body.get("expiry_days") or 0)
                fields["expiryTime"] = (
                    int(time.time() * 1000) + days * 86400 * 1000 if days > 0 else 0
                )
            user = (body.get("user") or "").strip()
            pw = (body.get("pass") or "").strip()
            if user and pw:
                fields["settings"] = json.dumps(
                    _accounts_settings(user, pw), ensure_ascii=False
                )
            updated = m.store.update(inbound_id, fields)
            if updated is None:
                return JSONResponse(_fail("更新失败"))
            m._persist_update(old, updated, fields)
        return _ok(updated)
    except Exception as e:  # noqa: BLE001
        log.exception("panel update inbound failed")
        return JSONResponse(_fail(str(e)))


@router.post("/panel/api/inbounds/{inbound_id}/del")
def api_del_inbound(inbound_id: int, request: Request):
    if not _logged_in(request):
        return _need_login()
    m = _main()
    try:
        if inbound_id == 1:
            return JSONResponse(_fail("共享占位入站(id=1)不可删除"))
        with m._ops_lock:
            old = m.store.get(inbound_id)
            if old is None:
                return _ok(msg="已不存在")
            m.store.delete(inbound_id)
            m._persist_remove(old)
        return _ok(True)
    except Exception as e:  # noqa: BLE001
        log.exception("panel del inbound failed")
        return JSONResponse(_fail(str(e)))


@router.post("/panel/api/inbounds/{inbound_id}/reset-traffic")
def api_reset_traffic(inbound_id: int, request: Request):
    if not _logged_in(request):
        return _need_login()
    m = _main()
    try:
        with m._ops_lock:
            inb = m.store.reset_traffic(inbound_id)
            if inb is None:
                return JSONResponse(_fail(f"入站 {inbound_id} 不存在"))
            tag = inb.get("tag") or f"in-{inbound_id}"
            try:
                m.xray.reset_inbound_stats(tag)
            except Exception as e:  # noqa: BLE001
                log.debug("panel reset xray stats: %s", e)
            m._last_stats[tag] = (0, 0)
        return _ok(True, msg="流量已清零")
    except Exception as e:  # noqa: BLE001
        log.exception("panel reset traffic failed")
        return JSONResponse(_fail(str(e)))


@router.get("/panel/api/suggest-port")
def api_suggest_port(request: Request):
    if not _logged_in(request):
        return _need_login()
    m = _main()
    import random
    used = m.store.used_ports()
    for _ in range(80):
        port = random.randint(20000, 65000)
        if port not in used:
            return _ok({"port": port})
    return JSONResponse(_fail("无可用端口"))


@router.get("/panel/api/traffic")
def api_traffic(request: Request):
    if not _logged_in(request):
        return _need_login()
    m = _main()
    try:
        cur = m.xray.query_inbound_traffic()
    except Exception:  # noqa: BLE001
        cur = {}
    return _ok({tag: {"up": up, "down": down} for tag, (up, down) in cur.items()})


@router.get("/panel/api/logs")
def api_logs(request: Request, service: str = "xray", lines: int = 100):
    if not _logged_in(request):
        return _need_login()
    m = _main()
    cfg = m.cfg
    svc = cfg.xray_service if service == "xray" else cfg.agent_service
    lines = max(10, min(500, int(lines)))
    try:
        r = subprocess.run(
            ["journalctl", "-u", svc, "-n", str(lines), "--no-pager"],
            capture_output=True, text=True, timeout=15, check=False,
        )
        return _ok({"service": svc, "text": r.stdout or r.stderr or ""})
    except Exception as e:  # noqa: BLE001
        return JSONResponse(_fail(str(e)))


@router.get("/panel/api/xray/versions")
def api_xray_versions(request: Request):
    if not _logged_in(request):
        return _need_login()
    m = _main()
    try:
        versions = xrayver.list_versions()
        current = sysinfo.xray_version(m.cfg.xray_bin)
        return _ok({"current": current, "versions": versions})
    except Exception as e:  # noqa: BLE001
        return JSONResponse(_fail(f"获取版本列表失败: {e}"))


@router.post("/panel/api/xray/switch")
async def api_xray_switch(request: Request):
    if not _logged_in(request):
        return _need_login()
    m = _main()
    body = await request.json()
    tag = (body.get("tag") or "").strip()
    if not tag:
        return JSONResponse(_fail("缺少版本号"))
    try:
        ports = [int(i["port"]) for i in m._enabled_list() if i.get("port")]

        def verify_restart() -> None:
            m.xray.restart_service(expected_ports=ports)

        with m._ops_lock:
            result = xrayver.switch_version(
                tag, xray_bin=m.cfg.xray_bin, verify_restart=verify_restart
            )
        result["version"] = sysinfo.xray_version(m.cfg.xray_bin)
        return _ok(result, msg=f"已切换到 {tag}")
    except Exception as e:  # noqa: BLE001
        log.exception("panel xray switch failed")
        return JSONResponse(_fail(str(e)))


@router.post("/panel/api/ops/{action}")
def api_ops(action: str, request: Request):
    if not _logged_in(request):
        return _need_login()
    m = _main()
    cfg = m.cfg
    try:
        if action == "restart-xray":
            m.xray.restart_service(
                expected_ports=[int(i["port"]) for i in m._enabled_list() if i.get("port")]
            )
            return _ok(msg="xray 已重启")
        if action == "reload":
            with m._ops_lock:
                m._apply_xray()
            return _ok(msg="已从数据库重载配置")
        if action == "restart-agent":
            # 延迟重启，先把响应返回
            subprocess.Popen(
                ["sh", "-c", f"sleep 1; systemctl restart {cfg.agent_service}"]
            )
            return _ok(msg="agent 重启中（约 2 秒后恢复）")
        if action == "firewall-check":
            return _ok(_firewall_check(m))
        return JSONResponse(_fail(f"未知操作: {action}"))
    except Exception as e:  # noqa: BLE001
        log.exception("panel ops failed: %s", action)
        return JSONResponse(_fail(str(e)))


def _firewall_check(m) -> dict[str, Any]:
    cfg = m.cfg
    ports = sorted({int(i["port"]) for i in m._enabled_list() if i.get("port")})
    listen = {p: m.xray._port_open(p) for p in ports}
    listen[cfg.listen_port] = _tcp_listening(cfg.listen_port)
    fw = "未检测到 UFW/firewalld"
    try:
        r = subprocess.run(["ufw", "status"], capture_output=True, text=True, timeout=8, check=False)
        if r.returncode == 0 and r.stdout.strip():
            fw = "UFW: " + r.stdout.strip().splitlines()[0]
    except Exception:  # noqa: BLE001
        pass
    return {"ports_listening": listen, "firewall": fw}


def _tcp_listening(port: int) -> bool:
    import socket
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.5):
            return True
    except OSError:
        return False
