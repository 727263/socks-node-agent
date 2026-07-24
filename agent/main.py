"""SOCKS Agent：本机 Web 面板 + HTTP API（可独立使用，也可对接 Bot）。"""
from __future__ import annotations

import json
import logging
import os
import secrets
import string
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.middleware.sessions import SessionMiddleware

from .config import AgentConfig, load_config
from .local_settings import LocalSettings
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
local_settings: LocalSettings
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


# 仅影响 Agent 记账/展示，不进 xray 运行配置
_META_KEYS = frozenset({"total", "up", "down", "expiryTime", "remark"})
# 会改变 xray 实际监听/认证的字段
_XRAY_KEYS = frozenset({
    "port", "protocol", "enable", "settings", "streamSettings", "sniffing",
})


def _inbound_tag(inb: dict) -> str:
    return str(inb.get("tag") or f"in-{inb['id']}")


def _norm_json_field(val: Any) -> str:
    if isinstance(val, str):
        try:
            obj = json.loads(val) if val else {}
        except json.JSONDecodeError:
            return val
    elif isinstance(val, dict):
        obj = val
    else:
        obj = {}
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)


def _xray_fields_changed(old: dict, fields: dict) -> bool:
    """fields 里是否有真正影响 xray 的变更（值相同则不算）。"""
    for k in _XRAY_KEYS:
        if k not in fields:
            continue
        new_v = fields[k]
        old_v = old.get(k)
        if k == "enable":
            if _parse_enable(new_v) != _parse_enable(old_v):
                return True
        elif k == "port":
            if int(new_v or 0) != int(old_v or 0):
                return True
        elif k in ("settings", "streamSettings", "sniffing"):
            left = _normalize_settings(new_v) if k == "settings" else _norm_json_field(new_v)
            right = (
                _normalize_settings(old_v) if k == "settings" else _norm_json_field(old_v)
            )
            if left != right:
                return True
        else:
            if str(new_v or "") != str(old_v or ""):
                return True
    return False


def _apply_xray() -> None:
    """写完整 Xray 配置并重启（热加载失败时的兜底）。"""
    enabled = _enabled_list()
    xray.apply_from_store(enabled)
    ports = sorted({int(i["port"]) for i in enabled if i.get("port")})
    log.info("Xray reloaded: %d inbounds, ports=%s", len(enabled), ports)


def _sync_config_disk() -> None:
    """只把当前 enabled 入站写到 config.json，不重启。"""
    xray.write_config_only(_enabled_list())


def _apply_live_or_restart(*, add: Optional[dict] = None, remove_tag: Optional[str] = None,
                          replace_old_tag: Optional[str] = None, replace_inb: Optional[dict] = None,
                          ) -> None:
    """先写盘，再尝试热加载；失败则全量重启。"""
    _sync_config_disk()
    try:
        if replace_inb is not None and replace_old_tag is not None:
            xray.replace_inbound_live(replace_old_tag, replace_inb)
            return
        if remove_tag is not None:
            xray.remove_inbound_live(remove_tag)
        if add is not None and add.get("enable"):
            xray.add_inbound_live(add)
        return
    except Exception as e:  # noqa: BLE001
        log.warning("xray live apply failed, fallback restart: %s", e)
    _apply_xray()


def _persist_add(inb: dict) -> None:
    """新建入站：热加；未启用则只写盘。"""
    if not inb.get("enable"):
        _sync_config_disk()
        return
    _apply_live_or_restart(add=inb)


def _persist_remove(inb: dict) -> None:
    """删除入站：热删。"""
    tag = _inbound_tag(inb)
    if inb.get("enable"):
        _apply_live_or_restart(remove_tag=tag)
    else:
        _sync_config_disk()


def _persist_update(old: dict, updated: dict, fields: dict) -> None:
    """按变更类型：仅元数据跳过 xray；否则热更新/替换，失败重启。"""
    if not _xray_fields_changed(old, fields):
        touched = sorted(set(fields) & _META_KEYS) or ["noop"]
        log.info(
            "inbound %s metadata-only update (%s), skip xray reload",
            updated.get("id"), ",".join(touched),
        )
        return

    old_en = _parse_enable(old.get("enable"))
    new_en = _parse_enable(updated.get("enable"))
    old_tag = _inbound_tag(old)

    # 仅开关：关→热删；开→热加（括号避免 & / == 优先级误读）
    only_enable = (set(fields.keys()) & _XRAY_KEYS) == {"enable"}
    if only_enable:
        if old_en and not new_en:
            _apply_live_or_restart(remove_tag=old_tag)
            return
        if not old_en and new_en:
            _apply_live_or_restart(add=updated)
            return

    # 端口/账号/协议等：先删后加；若最终未启用则只删
    if not new_en:
        if old_en:
            _apply_live_or_restart(remove_tag=old_tag)
        else:
            _sync_config_disk()
        return
    _apply_live_or_restart(replace_old_tag=old_tag, replace_inb=updated)


def _persist_and_hot(inb: dict, *, live_add: bool) -> None:
    """兼容旧调用：按 live_add 视为新增或删除后的同步。"""
    if live_add:
        _persist_add(inb)
    else:
        _persist_remove(inb)


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
                    _persist_update(inb, updated, {"enable": False})
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


def _gen_secret(n: int = 10) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


def _seed_shared_socks_if_needed() -> Optional[tuple[dict, dict, dict]]:
    """独立使用：共享入站若无账号，自动生成一组可登录凭据。

    返回 (old, updated, fields)；无需播种时返回 None。
    """
    inb = store.get(1)
    if inb is None:
        return None
    if local_settings.get("shared_seeded"):
        return None
    try:
        settings = json.loads(inb.get("settings") or "{}")
    except json.JSONDecodeError:
        settings = {}
    accounts = settings.get("accounts") or settings.get("users") or []
    if accounts:
        local_settings.set("shared_seeded", True)
        return None
    user = "u" + _gen_secret(8).lower()
    password = _gen_secret(12)
    new_settings = {
        "auth": "password",
        "accounts": [{"user": user, "pass": password}],
        "udp": False,
        "ip": "127.0.0.1",
    }
    settings_json = json.dumps(new_settings, ensure_ascii=False)
    fields = {
        "settings": settings_json,
        "remark": "shared-socks",
        "enable": True,
    }
    updated = store.update(1, fields)
    if updated is None:
        return None
    local_settings.set("shared_seeded", True)
    host = resolve_public_ip() or "YOUR_IP"
    cred_path = Path(cfg.data_dir) / "SHARED_SOCKS.txt"
    try:
        cred_path.write_text(
            f"host={host}\n"
            f"port={cfg.shared_port}\n"
            f"user={user}\n"
            f"pass={password}\n"
            f"link=socks5://{user}:{password}@{host}:{cfg.shared_port}\n",
            encoding="utf-8",
        )
    except OSError as e:
        log.warning("write SHARED_SOCKS.txt failed: %s", e)
    log.info(
        "Standalone shared SOCKS ready: port=%s user=%s (see %s)",
        cfg.shared_port, user, cred_path,
    )
    return inb, updated, fields


def resolve_public_ip() -> str:
    ip = (local_settings.get("public_ip") or "").strip()
    if ip:
        return ip
    return (cfg.public_ip or "").strip()


def panel_credentials() -> tuple[str, str]:
    """面板登录账号：优先 settings.json 覆盖，否则 agent.env / 环境变量。"""
    user = (local_settings.get("panel_user") or "").strip() or (cfg.panel_user or "").strip()
    pw = (local_settings.get("panel_pass") or "").strip() or (cfg.panel_pass or "").strip()
    return user, pw


def update_panel_password(new_pass: str) -> None:
    """写入 settings.json，并尽量同步 agent.env 的 PANEL_PASS（重启后仍生效）。"""
    new_pass = (new_pass or "").strip()
    if not new_pass:
        raise ValueError("新密码不能为空")
    local_settings.set("panel_pass", new_pass)
    env_path = Path(cfg.data_dir).parent / "agent.env"
    if not env_path.is_file():
        return
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
        out: list[str] = []
        found = False
        for line in lines:
            if line.startswith("PANEL_PASS="):
                out.append(f"PANEL_PASS={new_pass}")
                found = True
            else:
                out.append(line)
        if not found:
            out.append(f"PANEL_PASS={new_pass}")
        tmp = env_path.with_suffix(".tmp")
        tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
        tmp.replace(env_path)
        log.info("Updated PANEL_PASS in %s", env_path)
    except OSError as e:
        log.warning("update agent.env PANEL_PASS failed: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global cfg, store, xray, local_settings
    cfg = load_config()
    if not cfg.api_token:
        log.warning(
            "AGENT_API_TOKEN is empty — Bot/API Bearer 将被拒绝；Web 面板仍可独立使用"
        )
    Path(cfg.data_dir).mkdir(parents=True, exist_ok=True)
    local_settings = LocalSettings(str(Path(cfg.data_dir) / "settings.json"))
    if cfg.public_ip and not local_settings.get("public_ip"):
        local_settings.set("public_ip", cfg.public_ip)
    store = InboundStore(str(Path(cfg.data_dir) / "agent.db"))
    xray = XrayController(
        xray_bin=cfg.xray_bin,
        config_path=cfg.xray_config,
        api_addr=cfg.xray_api_addr,
        service_name=cfg.xray_service,
    )
    store.ensure_shared_placeholder(cfg.shared_port)
    with _ops_lock:
        seeded = _seed_shared_socks_if_needed()
        if seeded:
            old, updated, fields = seeded
            try:
                _persist_update(old, updated, fields)
            except Exception as e:  # noqa: BLE001
                log.exception("apply seeded shared socks failed: %s", e)
                try:
                    _apply_xray()
                except Exception:  # noqa: BLE001
                    log.exception("fallback reload after seed failed")
    try:
        xray.sync_live_from_store(_enabled_list())
    except Exception as e:  # noqa: BLE001
        log.exception("initial xray sync failed: %s", e)
    _stop.clear()
    t = threading.Thread(target=_bg_loop, name="agent-bg", daemon=True)
    t.start()
    log.info(
        "SOCKS Agent ready on %s:%d shared_port=%d panel=/panel standalone_ok=1",
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
            _persist_add(inb)
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
            _persist_update(old, updated, fields)
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
                    # 占位：settings 变空账号，需热替换
                    _persist_update(old, cleared, {
                        "settings": cleared.get("settings"),
                        "enable": True,
                        "total": 0,
                        "up": 0,
                        "down": 0,
                        "expiryTime": 0,
                        "remark": "shared-placeholder",
                    })
                return ok(cleared)
            store.delete(inbound_id)
            _persist_remove(old)
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
