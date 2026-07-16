"""入站元数据存储（SQLite）。流量/到期由 Agent 记账，与 3X-UI 入站字段对齐。"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional


DEFAULT_SETTINGS = {
    "auth": "password",
    "accounts": [],
    "udp": False,
    "ip": "127.0.0.1",
}
DEFAULT_SNIFFING = {"enabled": False, "destOverride": ["http", "tls"]}


class InboundStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS inbounds (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    port INTEGER NOT NULL,
                    protocol TEXT NOT NULL DEFAULT 'socks',
                    remark TEXT NOT NULL DEFAULT '',
                    enable INTEGER NOT NULL DEFAULT 1,
                    total INTEGER NOT NULL DEFAULT 0,
                    up INTEGER NOT NULL DEFAULT 0,
                    down INTEGER NOT NULL DEFAULT 0,
                    expiry_time INTEGER NOT NULL DEFAULT 0,
                    settings TEXT NOT NULL,
                    stream_settings TEXT NOT NULL DEFAULT '{}',
                    sniffing TEXT NOT NULL,
                    tag TEXT NOT NULL UNIQUE,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_inbounds_port ON inbounds(port)"
            )
            conn.commit()

    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)

    @staticmethod
    def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "up": int(row["up"]),
            "down": int(row["down"]),
            "total": int(row["total"]),
            "remark": row["remark"] or "",
            "enable": bool(row["enable"]),
            "expiryTime": int(row["expiry_time"]),
            "listen": "",
            "port": int(row["port"]),
            "protocol": row["protocol"] or "socks",
            "settings": row["settings"],
            "streamSettings": row["stream_settings"] or "{}",
            "sniffing": row["sniffing"],
            "tag": row["tag"],
        }

    def list_all(self) -> list[dict[str, Any]]:
        with self._lock, self._conn() as conn:
            rows = conn.execute("SELECT * FROM inbounds ORDER BY id").fetchall()
        return [self.row_to_dict(r) for r in rows]

    def get(self, inbound_id: int) -> Optional[dict[str, Any]]:
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM inbounds WHERE id = ?", (inbound_id,)
            ).fetchone()
        return self.row_to_dict(row) if row else None

    def get_by_tag(self, tag: str) -> Optional[dict[str, Any]]:
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM inbounds WHERE tag = ?", (tag,)
            ).fetchone()
        return self.row_to_dict(row) if row else None

    def used_ports(self) -> set[int]:
        with self._lock, self._conn() as conn:
            rows = conn.execute("SELECT port FROM inbounds").fetchall()
        return {int(r["port"]) for r in rows}

    def add(
        self,
        *,
        port: int,
        protocol: str = "socks",
        remark: str = "",
        enable: bool = True,
        total: int = 0,
        expiry_time: int = 0,
        settings: Any = None,
        stream_settings: str = "{}",
        sniffing: Any = None,
    ) -> dict[str, Any]:
        settings_raw = (
            settings
            if isinstance(settings, str)
            else json.dumps(settings or DEFAULT_SETTINGS, ensure_ascii=False)
        )
        sniff_raw = (
            sniffing
            if isinstance(sniffing, str)
            else json.dumps(sniffing or DEFAULT_SNIFFING, ensure_ascii=False)
        )
        now = self._now_ms()
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO inbounds (
                    port, protocol, remark, enable, total, up, down, expiry_time,
                    settings, stream_settings, sniffing, tag, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 0, 0, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(port),
                    protocol or "socks",
                    remark or "",
                    1 if enable else 0,
                    int(total or 0),
                    int(expiry_time or 0),
                    settings_raw,
                    stream_settings or "{}",
                    sniff_raw,
                    f"tmp-{now}",
                    now,
                    now,
                ),
            )
            inbound_id = int(cur.lastrowid)
            tag = f"in-{inbound_id}"
            conn.execute(
                "UPDATE inbounds SET tag = ? WHERE id = ?", (tag, inbound_id)
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM inbounds WHERE id = ?", (inbound_id,)
            ).fetchone()
        return self.row_to_dict(row)

    def update(self, inbound_id: int, fields: dict[str, Any]) -> Optional[dict[str, Any]]:
        allowed = {
            "port": "port",
            "protocol": "protocol",
            "remark": "remark",
            "enable": "enable",
            "total": "total",
            "up": "up",
            "down": "down",
            "expiryTime": "expiry_time",
            "settings": "settings",
            "streamSettings": "stream_settings",
            "sniffing": "sniffing",
        }
        sets: list[str] = []
        vals: list[Any] = []
        for k, col in allowed.items():
            if k not in fields:
                continue
            val = fields[k]
            if col == "enable":
                if isinstance(val, bool):
                    val = 1 if val else 0
                elif isinstance(val, str):
                    val = 1 if val.lower() in ("true", "1", "yes") else 0
                else:
                    val = 1 if val else 0
            elif col in ("settings", "stream_settings", "sniffing") and not isinstance(val, str):
                val = json.dumps(val, ensure_ascii=False)
            sets.append(f"{col} = ?")
            vals.append(val)
        if not sets:
            return self.get(inbound_id)
        sets.append("updated_at = ?")
        vals.append(self._now_ms())
        vals.append(inbound_id)
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                f"UPDATE inbounds SET {', '.join(sets)} WHERE id = ?",
                vals,
            )
            if cur.rowcount == 0:
                return None
            conn.commit()
            row = conn.execute(
                "SELECT * FROM inbounds WHERE id = ?", (inbound_id,)
            ).fetchone()
        return self.row_to_dict(row) if row else None

    def delete(self, inbound_id: int) -> bool:
        with self._lock, self._conn() as conn:
            cur = conn.execute("DELETE FROM inbounds WHERE id = ?", (inbound_id,))
            conn.commit()
            return cur.rowcount > 0

    def reset_traffic(self, inbound_id: int) -> Optional[dict[str, Any]]:
        return self.update(inbound_id, {"up": 0, "down": 0})

    def add_traffic_delta(self, inbound_id: int, up_delta: int, down_delta: int) -> None:
        if up_delta <= 0 and down_delta <= 0:
            return
        with self._lock, self._conn() as conn:
            conn.execute(
                """
                UPDATE inbounds
                SET up = up + ?, down = down + ?, updated_at = ?
                WHERE id = ?
                """,
                (max(0, int(up_delta)), max(0, int(down_delta)), self._now_ms(), inbound_id),
            )
            conn.commit()

    def ensure_shared_placeholder(self, port: int) -> dict[str, Any]:
        """保证存在 id=1 的共享占位入站（供 Bot 节点 xui_inbound_id 使用）。"""
        existing = self.get(1)
        if existing:
            return existing
        settings = {
            "auth": "password",
            "accounts": [],
            "udp": False,
            "ip": "127.0.0.1",
        }
        now = self._now_ms()
        with self._lock, self._conn() as conn:
            # 若表非空但没有 id=1（异常情况），仍插入并强制 id=1
            conn.execute(
                """
                INSERT INTO inbounds (
                    id, port, protocol, remark, enable, total, up, down, expiry_time,
                    settings, stream_settings, sniffing, tag, created_at, updated_at
                ) VALUES (1, ?, 'socks', 'shared-placeholder', 1, 0, 0, 0, 0, ?, '{}', ?, 'in-1', ?, ?)
                """,
                (
                    int(port),
                    json.dumps(settings, ensure_ascii=False),
                    json.dumps(DEFAULT_SNIFFING, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            conn.commit()
        return self.get(1)  # type: ignore[return-value]
