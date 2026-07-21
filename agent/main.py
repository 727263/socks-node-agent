"""SOCKS Agent HTTP API —— 响应格式对齐 3X-UI，供 Bot 的 panel_type=agent 调用。"""
from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.middleware.sessions import SessionMiddleware

from .config import AgentConfig, load_config
from .store import InboundStore
from .xrayctl import XrayController

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("socks-agent")

cfg: AgentConfig
store: InboundStore
xray: XrayController
_stop = threading.Event()
# xray 启动后的 stats 快照，用于把增量累加到 DB（避免重启后回退）
_last_stats: dict[str, tuple[int, int]] = {}
_ops_lock = threading.RLock()


def ok(obj: Any = None, msg: str = "") -> dict:
    return {"success": True, "msg": msg, "obj": obj}


def fail(msg: str, obj: Any = None) -> dict:
    return {"success": False, "msg": msg, "obj": obj}


def _parse_enable(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    return str(val).lower() in ("true", "1", "yes")


def _enabled_list() -> list[dict]:
    return [i for i in store.list_all() if i.get("enable")]


def _normalize_settings(raw: Any) -> str:
    """统一 settings 为 JSON 字符串，保证 auth + accounts。"""
    if isinstance(raw, str):
        try:
            obj = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            obj = {}
    elif isinstance(raw, dict):
        obj = dict(raw)
    else:
        obj = {}
    obj.setdefault("auth", "password")
    obj.setdefault("udp", False)
    obj.setdefault("ip", "127.0.0.1")
    accounts = obj.get("accounts") or obj.get("users") or []
    if accounts and "accounts" not in obj:
        obj["accounts"] = accounts
    obj.pop("users", None)
    return json.dumps(obj, ensure_ascii=False)


def _apply_xray() -> None:
    """写完整 Xray 配置并重启（比热加载可靠）。"""
    enabled = _enabled_list()
    xray.apply_from_store(enabled)
    ports = sorted({int(i["port"]) for i in enabled if i.get("port")})
    log.info("Xray reloaded: %d inbounds, ports=%s", len(enabled), ports)


def _persist_and_hot(inb: dict, *, live_add: bool) -> None:
    """落盘并全量重启 Xray（live_add 参数保留兼容，已忽略）。"""
    del inb, live_add
    _apply_xray()



def _sync_traffic_once() -> None:
    global _last_stats
    try:
        current = xray.query_inbound_traffic()
    except Exception as e:  # noqa: BLE001
        log.warning("traffic query failed: %s", e)
        return
    for tag, (up, down) in current.items():
        prev = _last_stats.get(tag, (0, 0))
        # xray 重启后计数归零：若当前值小于上次，视为新基线，不扣减
        if up < prev[0] or down < prev[1]:
            delta_up, delta_down = up, down
        else:
            delta_up, delta_down = up - prev[0], down - prev[1]
        _last_stats[tag] = (up, down)
        inb = store.get_by_tag(tag)
        if inb is None:
            continue
        store.add_traffic_delta(int(inb["id"]), delta_up, delta_down)


def _enforce_once() -> None:
    """流量超限 / 到期 → 自动 disable（对齐 3X-UI 行为）。"""
    now_ms = int(time.time() * 1000)
    for inb in store.list_all():
        if not inb.get("enable"):
            continue
        total = int(inb.get("total") or 0)
        used = int(inb.get("up") or 0) + int(inb.get("down") or 0)
        exp = int(inb.get("expiryTime") or 0)
        over_quota = total > 0 and used >= total
        expired = exp > 0 and exp <= now_ms
        if not (over_quota or expired):
            continue
        reason = "quota" if over_quota else "expiry"
        log.info("Auto-disable inbound %s (%s)", inb["id"], reason)
        with _ops_lock:
            updated = store.update(int(inb["id"]), {"enable": False})
            if updated:
                try:
                    _persist_and_hot(updated, live_add=False)
                except Exception as e:  # noqa: BLE001
                    log.exception("auto-disable apply failed: %s", e)


def _bg_loop() -> None:
    traffic_every = max(5, cfg.traffic_sync_seconds)
    enforce_every = max(5, cfg.enforce_seconds)
    t_next = time.time()
    e_next = time.time()
    while not _stop.is_set():
        now = time.time()
        if now >= t_next:
            try:
                _sync_traffic_once()
            except Exception as e:  # noqa: BLE001
                log.exception("traffic sync: %s", e)
            t_next = now + traffic_every
        if now >= e_next:
            try:
                _enforce_once()
            except Exception as e:  # noqa: BLE001
                log.exception("enforce: %s", e)
            e_next = now + enforce_every
        _stop.wait(1.0)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global cfg, store, xray
    cfg = load_config()
    if not cfg.api_token:
        log.warning("AGENT_API_TOKEN is empty — all requests will be rejected")
    Path(cfg.data_dir).mkdir(parents=True, exist_ok=True)
    store = InboundStore(str(Path(cfg.data_dir) / "agent.db"))
    xray = XrayController(
        xray_bin=cfg.xray_bin,
        config_path=cfg.xray_config,
        api_addr=cfg.xray_api_addr,
        service_name=cfg.xray_service,
    )
    # 确保共享占位入站存在，并同步到 Xray
    store.ensure_shared_placeholder(cfg.shared_port)
    try:
        xray.sync_live_from_store(_enabled_list())
    except Exception as e:  # noqa: BLE001
        log.exception("initial xray sync failed: %s", e)
    _stop.clear()
    t = threading.Thread(target=_bg_loop, name="agent-bg", daemon=True)
    t.start()
    log.info(
        "SOCKS Agent ready on %s:%d shared_port=%d",
        cfg.listen_host, cfg.listen_port, cfg.shared_port,
    )
    yield
    _stop.set()
    t.join(timeout=3)


app = FastAPI(title="SOCKS Node Agent", lifespan=lifespan)

# 面板会话密钥：优先用 env 的 PANEL_SECRET（重启后会话不失效），否则随机
_session_secret = os.getenv("PANEL_SECRET", "").strip() or secrets.token_hex(16)
app.add_middleware(
    SessionMiddleware,
    secret_key=_session_secret,
    session_cookie="agent_panel",
    max_age=7 * 86400,
    same_site="lax",
    https_only=False,
)

from . import panel  # noqa: E402  (在 app 定义后导入，避免循环)

app.include_router(panel.router)


def require_token(
    authorization: Optional[str] = Header(default=None),
    x_api_token: Optional[str] = Header(default=None, alias="X-API-Token"),
) -> None:
    token = ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    elif x_api_token:
        token = x_api_token.strip()
    if not cfg.api_token or token != cfg.api_token:
        raise HTTPException(status_code=401, detail="unauthorized")


@app.get("/api/health")
def health():
    return ok({"status": "ok", "panel": "agent"})


@app.get("/api/inbounds/list", dependencies=[Depends(require_token)])
def list_inbounds():
    _sync_traffic_once()
    return ok(store.list_all())


@app.post("/api/inbounds/add", dependencies=[Depends(require_token)])
async def add_inbound(request: Request):
    body = await request.json()
    try:
        port = int(body.get("port") or 0)
        if port <= 0:
            return JSONResponse(fail("invalid port"))
        with _ops_lock:
            if port in store.used_ports():
                return JSONResponse(fail(f"port {port} already used"))
            inb = store.add(
                port=port,
                protocol=body.get("protocol") or "socks",
                remark=body.get("remark") or "",
                enable=_parse_enable(body.get("enable", True)),
                total=int(body.get("total") or 0),
                expiry_time=int(body.get("expiryTime") or 0),
                settings=_normalize_settings(body.get("settings")),
                stream_settings=body.get("streamSettings") or "{}",
                sniffing=body.get("sniffing"),
            )
            _persist_and_hot(inb, live_add=True)
        return ok(inb)
    except Exception as e:  # noqa: BLE001
        log.exception("add inbound failed")
        return JSONResponse(fail(str(e)))


@app.post("/api/inbounds/update/{inbound_id}", dependencies=[Depends(require_token)])
async def update_inbound(inbound_id: int, request: Request):
    body = await request.json()
    try:
        with _ops_lock:
            old = store.get(inbound_id)
            if old is None:
                return JSONResponse(fail(f"inbound {inbound_id} not found"))
            fields = {}
            for k in (
                "port", "protocol", "remark", "enable", "total", "up", "down",
                "expiryTime", "settings", "streamSettings", "sniffing",
            ):
                if k in body:
                    if k == "settings":
                        fields[k] = _normalize_settings(body[k])
                    else:
                        fields[k] = body[k]
            updated = store.update(inbound_id, fields)
            if updated is None:
                return JSONResponse(fail(f"inbound {inbound_id} not found"))
            _persist_and_hot(updated, live_add=bool(updated.get("enable")))
        return ok(updated)
    except Exception as e:  # noqa: BLE001
        log.exception("update inbound failed")
        return JSONResponse(fail(str(e)))


@app.post("/api/inbounds/del/{inbound_id}", dependencies=[Depends(require_token)])
def del_inbound(inbound_id: int):
    try:
        with _ops_lock:
            old = store.get(inbound_id)
            if old is None:
                return ok(None, msg="already gone")
            if inbound_id == 1:
                # 占位入站：不删库，清空账号并保持端口
                cleared = store.update(
                    1,
                    {
                        "settings": {
                            "auth": "password",
                            "accounts": [],
                            "udp": False,
                            "ip": "127.0.0.1",
                        },
                        "enable": True,
                        "total": 0,
                        "up": 0,
                        "down": 0,
                        "expiryTime": 0,
                        "remark": "shared-placeholder",
                    },
                )
                if cleared:
                    _persist_and_hot(cleared, live_add=True)
                return ok(cleared)
            store.delete(inbound_id)
            _persist_and_hot(old, live_add=False)
        return ok(True)
    except Exception as e:  # noqa: BLE001
        log.exception("del inbound failed")
        return JSONResponse(fail(str(e)))


@app.post("/api/xray/reload", dependencies=[Depends(require_token)])
def reload_xray():
    """全量从 DB 重写 Xray 配置并重启（迁移/同步后修复用）。"""
    try:
        with _ops_lock:
            _apply_xray()
        return ok({"inbounds": len(_enabled_list())})
    except Exception as e:  # noqa: BLE001
        log.exception("reload xray failed")
        return JSONResponse(fail(str(e)))


@app.post(
    "/api/inbounds/resetAllClientTraffics/{inbound_id}",
    dependencies=[Depends(require_token)],
)
def reset_traffic(inbound_id: int):
    try:
        with _ops_lock:
            inb = store.reset_traffic(inbound_id)
            if inb is None:
                return JSONResponse(fail(f"inbound {inbound_id} not found"))
            tag = inb.get("tag") or f"in-{inbound_id}"
            try:
                xray.reset_inbound_stats(tag)
            except Exception as e:  # noqa: BLE001
                log.debug("reset xray stats: %s", e)
            _last_stats[tag] = (0, 0)
        return ok(True)
    except Exception as e:  # noqa: BLE001
        log.exception("reset traffic failed")
        return JSONResponse(fail(str(e)))


def main() -> None:
    import uvicorn

    c = load_config()
    uvicorn.run(
        "agent.main:app",
        host=c.listen_host,
        port=c.listen_port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
