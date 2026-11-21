"""SQLite 号池 + 注册结果存储。

表结构：
  outlook_accounts: 接码号池（多种邮箱混放，kind 列区分 + 状态机）
  registered:       注册成功结果（凭证 JSON）

关于 outlook_accounts 这个表名：
    它现在装的不止 outlook（还有 gmail / icloud / qq ...），名字已经不准，
    但改表名要动迁移和一堆 SQL，收益只是好看一点，风险不值。
    真正区分类型的是 kind 列。

凭证字段用「并集列」而不是 extra_json：
    outlook/gmail 用 password+client_id+refresh_token，
    icloud 这类中转只用 relay_url，各自把不用的列留空。
    几种邮箱的规模下，并集列比 JSON 好 —— 能建索引、能加约束、
    SQL 里直接看得见。加新邮箱时如果要新字段，就再 ALTER 加一列。
"""
from __future__ import annotations

import base64
import json
import os
import re
import sqlite3
import sys
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

DB_PATH = Path(os.getenv("NEXUSFLOW_DB_PATH", Path(__file__).resolve().parent / "webui.db"))

_lock = threading.Lock()  # SQLite 写入串行化


def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init_db():
    con = _conn()
    con.executescript("""
        CREATE TABLE IF NOT EXISTS outlook_accounts (
            email           TEXT PRIMARY KEY,
            password        TEXT,
            client_id       TEXT,
            refresh_token   TEXT,
            relay_url       TEXT,       -- 中转取码 URL（icloud 类用，其余留空）
            kind            TEXT NOT NULL DEFAULT 'outlook',
                            -- 邮箱类型，对应 mail_providers 注册表的 kind
            workflow_scope  TEXT NOT NULL DEFAULT 'legacy',
                            -- integration / legacy：两套编排互不抢号、独立计数
            status          TEXT NOT NULL DEFAULT 'available',
                            -- available / in_use / done / failed
            imported_at     REAL,
            claimed_at      REAL,
            finished_at     REAL,
            fail_reason     TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_outlook_status ON outlook_accounts(status);
        -- idx_outlook_kind 不在这里建：老库此刻还没有 kind 列，
        -- 建索引会当场报错。放到下面补完列之后再建。

        CREATE TABLE IF NOT EXISTS settings (
            key     TEXT PRIMARY KEY,
            value   TEXT
        );

        CREATE TABLE IF NOT EXISTS proxy_pool (
            proxy       TEXT PRIMARY KEY,
            created_at  REAL NOT NULL,
            updated_at  REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS registered (
            email           TEXT PRIMARY KEY,
            workflow_scope  TEXT NOT NULL DEFAULT 'legacy',
            password        TEXT,
            access_token    TEXT,
            session_token   TEXT,
            refresh_token   TEXT,
            id_token        TEXT,
            device_id       TEXT,
            csrf_token      TEXT,
            cookie_header   TEXT,
            totp_secret     TEXT,
            totp_factor_id  TEXT,
            extra_json      TEXT,
            created_at      REAL
        );

        CREATE TABLE IF NOT EXISTS runs (
            run_id          TEXT PRIMARY KEY,
            email           TEXT,
            status          TEXT,        -- running / done / failed
            started_at      REAL,
            finished_at     REAL,
            log_path        TEXT,
            error           TEXT,
            error_category  TEXT         -- network / account / unknown
        );

        CREATE TABLE IF NOT EXISTS integration_jobs (
            job_id       TEXT PRIMARY KEY,
            status       TEXT NOT NULL,
            updated_at   REAL NOT NULL,
            payload_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS recovery_batches (
            batch_id     TEXT PRIMARY KEY,
            status       TEXT NOT NULL,
            updated_at   REAL NOT NULL,
            payload_json TEXT NOT NULL
        );
    """)
    con.commit()
    # 老 DB migrate：error_category 在后期才加，对已建表补列
    cur = con.execute("PRAGMA table_info(runs)")
    cols = {r[1] for r in cur.fetchall()}
    if "error_category" not in cols:
        con.execute("ALTER TABLE runs ADD COLUMN error_category TEXT")
        con.commit()

    # 老 DB migrate：号池多邮箱混放（kind / relay_url 在后期才加）
    # 存量行全部是 outlook 时代导进去的，DEFAULT 'outlook' 正好把它们
    # 归位，不需要额外 UPDATE。重复执行无副作用。
    cur = con.execute("PRAGMA table_info(outlook_accounts)")
    acc_cols = {r[1] for r in cur.fetchall()}
    if "kind" not in acc_cols:
        con.execute(
            "ALTER TABLE outlook_accounts ADD COLUMN kind TEXT NOT NULL DEFAULT 'outlook'"
        )
        con.commit()
    if "relay_url" not in acc_cols:
        con.execute("ALTER TABLE outlook_accounts ADD COLUMN relay_url TEXT")
        con.commit()
    if "workflow_scope" not in acc_cols:
        con.execute(
            "ALTER TABLE outlook_accounts ADD COLUMN workflow_scope TEXT "
            "NOT NULL DEFAULT 'legacy'"
        )
        con.commit()
    # 索引建在补列之后，否则老库上 CREATE INDEX 会因为没有 kind 列而失败
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_outlook_kind ON outlook_accounts(kind, status)"
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_outlook_workflow_scope "
        "ON outlook_accounts(workflow_scope, status)"
    )
    con.commit()

    # 老 DB migrate：registered 的 2FA 两列（totp_secret / totp_factor_id）后期才加。
    # secret 一次性下发、服务端取不回，务必单独补列持久化。重复执行无副作用。
    cur = con.execute("PRAGMA table_info(registered)")
    reg_cols = {r[1] for r in cur.fetchall()}
    if "workflow_scope" not in reg_cols:
        con.execute(
            "ALTER TABLE registered ADD COLUMN workflow_scope TEXT "
            "NOT NULL DEFAULT 'legacy'"
        )
        con.commit()
    if "totp_secret" not in reg_cols:
        con.execute("ALTER TABLE registered ADD COLUMN totp_secret TEXT")
        con.commit()
    if "totp_factor_id" not in reg_cols:
        con.execute("ALTER TABLE registered ADD COLUMN totp_factor_id TEXT")
        con.commit()
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_registered_workflow_scope "
        "ON registered(workflow_scope, created_at)"
    )
    _backfill_integration_scope(con)
    con.close()


def _backfill_integration_scope(con: sqlite3.Connection) -> None:
    """Mark legacy rows created by the unified flow without touching user imports."""
    emails: set[str] = set()
    try:
        rows = con.execute("SELECT payload_json FROM integration_jobs").fetchall()
    except sqlite3.Error:
        rows = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except Exception:
            continue

        def collect(value) -> None:
            if isinstance(value, dict):
                email = str(value.get("email") or "").strip().lower()
                if "@" in email:
                    emails.add(email)
                message = str(value.get("message") or "")
                if message:
                    match = re.search(
                        r"([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})",
                        message,
                    )
                    if match:
                        emails.add(match.group(1).lower())
                for nested in value.values():
                    if isinstance(nested, (dict, list)):
                        collect(nested)
                return
            if isinstance(value, list):
                for nested in value:
                    collect(nested)

        collect(payload)
    if not emails:
        return
    for email in emails:
        con.execute(
            "UPDATE outlook_accounts SET workflow_scope='integration' WHERE email=?",
            (email,),
        )
        con.execute(
            "UPDATE registered SET workflow_scope='integration' WHERE email=?",
            (email,),
        )
    con.commit()


# ──────────────────────── outlook 号池 ────────────────────────


def parse_lines(text: str, kind: str = "") -> list[dict]:
    """解析导入文本，委托给 mail_providers 注册表。

    kind 指定 → 用该 provider 的格式解析（推荐）
    kind 为空 → 按段数猜（段数唯一时才行，Outlook/Gmail 都是 4 段会猜不出）

    非法行抛 ImportValidationError（带行号和原因），**不再静默跳过**。
    以前这里是 `if len(parts) != 4: continue`，用户看到"导入成功"
    但号少了几个，完全没法排查。
    """
    from mail_providers import parse_import_text

    return parse_import_text(text or "", kind)


def import_accounts(
    text: str,
    kind: str = "",
    workflow_scope: str = "legacy",
) -> dict:
    """批量入库。已存在的 email 仅在凭证变化时更新。

    解析阶段全对才写：有一行非法就整批拒绝（抛 ImportValidationError），
    不会出现"写进去一半"对不上账的情况。
    """
    rows = parse_lines(text, kind)
    workflow_scope = (
        workflow_scope if workflow_scope in {"integration", "legacy"} else "legacy"
    )
    now = time.time()
    inserted = updated = skipped = 0
    with _lock:
        con = _conn()
        for r in rows:
            row_kind = r.get("kind") or kind or "outlook"
            # 凭证并集：不同 provider 用不同子集，没有的留空字符串
            password = r.get("password", "") or ""
            client_id = r.get("client_id", "") or ""
            refresh = r.get("refresh_token", "") or ""
            relay = r.get("relay_url", "") or ""

            cur = con.execute(
                "SELECT refresh_token, relay_url, kind, workflow_scope "
                "FROM outlook_accounts WHERE email=?",
                (r["email"],),
            )
            existing = cur.fetchone()
            if existing is None:
                con.execute(
                    "INSERT INTO outlook_accounts(email, password, client_id, refresh_token, "
                    "relay_url, kind, workflow_scope, status, imported_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 'available', ?)",
                    (
                        r["email"], password, client_id, refresh, relay, row_kind,
                        workflow_scope, now,
                    ),
                )
                inserted += 1
            elif (
                (existing["refresh_token"] or "") != refresh
                or (existing["relay_url"] or "") != relay
                or (existing["kind"] or "") != row_kind
                or (existing["workflow_scope"] or "legacy") != workflow_scope
            ):
                # 凭证或类型变了 → 覆盖并重置为可用
                con.execute(
                    "UPDATE outlook_accounts SET refresh_token=?, password=?, client_id=?, "
                    "relay_url=?, kind=?, workflow_scope=?, status='available', "
                    "imported_at=?, fail_reason=NULL "
                    "WHERE email=?",
                    (
                        refresh, password, client_id, relay, row_kind,
                        workflow_scope, now, r["email"],
                    ),
                )
                updated += 1
            else:
                skipped += 1
        con.commit()
    return {"parsed": len(rows), "inserted": inserted, "updated": updated, "skipped": skipped}


def count_accounts(status: str = "", kind: str = "", workflow_scope: str = "") -> int:
    con = _conn()
    sql = "SELECT COUNT(*) FROM outlook_accounts"
    where, args = [], []
    if status:
        where.append("status=?")
        args.append(status)
    if kind:
        where.append("kind=?")
        args.append(kind.strip().lower())
    if workflow_scope:
        where.append("workflow_scope=?")
        args.append(workflow_scope.strip().lower())
    if where:
        sql += " WHERE " + " AND ".join(where)
    return con.execute(sql, args).fetchone()[0]


def list_accounts(
    status: str = "",
    limit: int = 50,
    offset: int = 0,
    kind: str = "",
    workflow_scope: str = "",
) -> list[dict]:
    con = _conn()
    sql = "SELECT * FROM outlook_accounts"
    where, args = [], []
    if status:
        where.append("status=?")
        args.append(status)
    if kind:
        where.append("kind=?")
        args.append(kind.strip().lower())
    if workflow_scope:
        where.append("workflow_scope=?")
        args.append(workflow_scope.strip().lower())
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY imported_at DESC LIMIT ? OFFSET ?"
    args += [limit, offset]
    return [dict(r) for r in con.execute(sql, args).fetchall()]


def stats_by_kind(workflow_scope: str = "") -> dict:
    """按邮箱类型分组统计，给 WebUI 顶部展示"每种邮箱各有多少号"。"""
    con = _conn()
    scope = (workflow_scope or "").strip().lower()
    where = " WHERE workflow_scope=?" if scope else ""
    args = (scope,) if scope else ()
    cur = con.execute(
        "SELECT kind, status, COUNT(*) AS n FROM outlook_accounts"
        f"{where} GROUP BY kind, status",
        args,
    )
    out: dict[str, dict] = {}
    for r in cur.fetchall():
        k = r["kind"] or "outlook"
        slot = out.setdefault(
            k, {"available": 0, "in_use": 0, "done": 0, "failed": 0, "total": 0}
        )
        slot[r["status"]] = r["n"]
        slot["total"] += r["n"]
    return out


def get_account(email: str) -> Optional[dict]:
    con = _conn()
    cur = con.execute("SELECT * FROM outlook_accounts WHERE email=?", (email.lower(),))
    row = cur.fetchone()
    return dict(row) if row else None


def claim_account(email: str, workflow_scope: str = "") -> Optional[dict]:
    """原子 claim 指定邮箱（available / failed -> in_use）。

    failed 也允许重试 claim：之前 OpenAI 风控误判 / 网络抖动等导致 fail 的号
    应允许用户手动重试，已 done 的号才禁止重 claim（防误覆盖凭证）。

    按 email 指定时不过滤 kind —— 用户点名要这个号，它是什么类型
    由记录自己的 kind 列说了算，调用方读 account["kind"] 即可。
    """
    email = (email or "").strip().lower()
    if not email:
        return None
    with _lock:
        con = _conn()
        scope_sql = " AND workflow_scope=?" if workflow_scope else ""
        args = (email, workflow_scope) if workflow_scope else (email,)
        cur = con.execute(
            "SELECT * FROM outlook_accounts WHERE email=? "
            "AND status IN ('available', 'failed')" + scope_sql,
            args,
        )
        row = cur.fetchone()
        if not row:
            return None
        rc = con.execute(
            "UPDATE outlook_accounts SET status='in_use', claimed_at=?, fail_reason=NULL "
            "WHERE email=? AND status IN ('available', 'failed')" + scope_sql,
            (time.time(), *args),
        )
        con.commit()
        if rc.rowcount != 1:
            return None
        return dict(row)


def claim_next(kind: str = "", workflow_scope: str = "") -> Optional[dict]:
    """原子 claim 任一 available 号。

    kind 指定 → 只从该类型里挑（"选了 gmail 就只跑 gmail 号"）
    kind 为空 → 全池子里挑最早导入的

    多类型混放的关键就在这里：号池里 outlook 和 gmail 并存，
    但当前配置选了哪种，就只 claim 哪种，不会串。
    """
    k = (kind or "").strip().lower()
    scope = (workflow_scope or "").strip().lower()
    with _lock:
        con = _conn()
        for _ in range(50):  # 有限重试，避免并发抢号时无限递归爆栈
            if k and scope:
                cur = con.execute(
                    "SELECT * FROM outlook_accounts WHERE status='available' "
                    "AND kind=? AND workflow_scope=?"
                    "ORDER BY imported_at ASC LIMIT 1",
                    (k, scope),
                )
            elif k:
                cur = con.execute(
                    "SELECT * FROM outlook_accounts WHERE status='available' AND kind=? "
                    "ORDER BY imported_at ASC LIMIT 1",
                    (k,),
                )
            elif scope:
                cur = con.execute(
                    "SELECT * FROM outlook_accounts WHERE status='available' "
                    "AND workflow_scope=? ORDER BY imported_at ASC LIMIT 1",
                    (scope,),
                )
            else:
                cur = con.execute(
                    "SELECT * FROM outlook_accounts WHERE status='available' "
                    "ORDER BY imported_at ASC LIMIT 1"
                )
            row = cur.fetchone()
            if not row:
                return None
            rc = con.execute(
                "UPDATE outlook_accounts SET status='in_use', claimed_at=? "
                "WHERE email=? AND status='available'",
                (time.time(), row["email"]),
            )
            con.commit()
            if rc.rowcount == 1:
                return dict(row)
            # 被别的线程抢走了，换下一个再试
        return None


def mark_done(email: str) -> None:
    with _lock:
        con = _conn()
        con.execute(
            "UPDATE outlook_accounts SET status='done', finished_at=?, fail_reason=NULL WHERE email=?",
            (time.time(), email.lower()),
        )
        con.commit()


def mark_failed(email: str, reason: str = "") -> None:
    with _lock:
        con = _conn()
        con.execute(
            "UPDATE outlook_accounts SET status='failed', finished_at=?, fail_reason=? WHERE email=?",
            (time.time(), (reason or "")[:500], email.lower()),
        )
        con.commit()


def release_unused(email: str) -> None:
    """claim 后没真注册（异常 / 用户取消）→ 还回 available。"""
    with _lock:
        con = _conn()
        con.execute(
            "UPDATE outlook_accounts SET status='available', claimed_at=NULL "
            "WHERE email=? AND status='in_use'",
            (email.lower(),),
        )
        con.commit()


def reset_to_available(email: str) -> bool:
    """手动重置单个号：done / failed → available，清空时间戳和失败原因。

    场景：注册成功但 refresh_token 没拿到，主人想重新跑一遍这个号。
    """
    with _lock:
        con = _conn()
        rc = con.execute(
            "UPDATE outlook_accounts SET status='available', claimed_at=NULL, "
            "finished_at=NULL, fail_reason=NULL "
            "WHERE lower(email)=lower(?)",
            (email,),
        )
        con.commit()
        return rc.rowcount > 0


def bulk_reset_to_available(emails: list[str]) -> int:
    """批量重置多个号。返回实际被改的行数。"""
    if not emails:
        return 0
    with _lock:
        con = _conn()
        rc = con.execute(
            f"UPDATE outlook_accounts SET status='available', claimed_at=NULL, "
            f"finished_at=NULL, fail_reason=NULL "
            f"WHERE lower(email) IN ({','.join(['lower(?)'] * len(emails))})",
            emails,
        )
        con.commit()
        return rc.rowcount


def reset_failed_to_available(workflow_scope: str = "") -> int:
    """把所有 failed 号一次性重置为 available（清掉 fail_reason）。返回受影响行数。

    场景：代理短暂抽风导致一波号被冤枉标 failed，主人想给它们一次机会。
    """
    scope = (workflow_scope or "").strip().lower()
    where = " AND workflow_scope=?" if scope else ""
    args = (scope,) if scope else ()
    with _lock:
        con = _conn()
        rc = con.execute(
            "UPDATE outlook_accounts SET status='available', fail_reason=NULL, "
            "finished_at=NULL WHERE status='failed'" + where,
            args,
        )
        con.commit()
        return rc.rowcount


def release_stale_in_use(
    stale_seconds: float = 1800,
    workflow_scope: str = "",
) -> int:
    """把 claimed_at 超过 N 秒还在 in_use 的号释放回 available。

    场景：上次 webui 强退/进程崩溃，号卡在 in_use 永远不释放。默认 30 分钟。
    """
    with _lock:
        con = _conn()
        cutoff = time.time() - stale_seconds
        scope = (workflow_scope or "").strip().lower()
        scope_sql = " AND workflow_scope=?" if scope else ""
        args = (cutoff, scope) if scope else (cutoff,)
        rc = con.execute(
            "UPDATE outlook_accounts SET status='available', claimed_at=NULL "
            "WHERE status='in_use' AND (claimed_at IS NULL OR claimed_at < ?)"
            + scope_sql,
            args,
        )
        con.commit()
        return rc.rowcount


def delete_account(email: str) -> bool:
    with _lock:
        con = _conn()
        rc = con.execute("DELETE FROM outlook_accounts WHERE email=?", (email.lower(),))
        con.commit()
        return rc.rowcount > 0


def delete_accounts_by_status(status: str, workflow_scope: str = "") -> int:
    """按状态批量删除。status 必须是 available/in_use/done/failed 之一；
    传 'all' 删全部。返回受影响行数。"""
    valid = {"available", "in_use", "done", "failed", "all"}
    s = (status or "").strip().lower()
    if s not in valid:
        return 0
    scope = (workflow_scope or "").strip().lower()
    with _lock:
        con = _conn()
        if s == "all":
            if scope:
                rc = con.execute(
                    "DELETE FROM outlook_accounts WHERE workflow_scope=?",
                    (scope,),
                )
            else:
                rc = con.execute("DELETE FROM outlook_accounts")
        else:
            if scope:
                rc = con.execute(
                    "DELETE FROM outlook_accounts WHERE status=? AND workflow_scope=?",
                    (s, scope),
                )
            else:
                rc = con.execute(
                    "DELETE FROM outlook_accounts WHERE status=?",
                    (s,),
                )
        con.commit()
        return rc.rowcount


def delete_accounts_by_emails(emails: list[str]) -> int:
    """按 email 列表批量删除。返回受影响行数。"""
    cleaned = [e.strip().lower() for e in (emails or []) if e and e.strip()]
    if not cleaned:
        return 0
    with _lock:
        con = _conn()
        placeholders = ",".join("?" * len(cleaned))
        rc = con.execute(
            f"DELETE FROM outlook_accounts WHERE email IN ({placeholders})",
            cleaned,
        )
        con.commit()
        return rc.rowcount


def stats(workflow_scope: str = "") -> dict:
    scope = (workflow_scope or "").strip().lower()
    where = " WHERE workflow_scope=?" if scope else ""
    args = (scope,) if scope else ()
    con = _conn()
    cur = con.execute(
        "SELECT status, COUNT(*) AS n FROM outlook_accounts"
        f"{where} GROUP BY status",
        args,
    )
    out = {"available": 0, "in_use": 0, "done": 0, "failed": 0, "total": 0}
    for r in cur.fetchall():
        out[r["status"]] = r["n"]
        out["total"] += r["n"]
    return out


def stats_by_workflow() -> dict:
    return {
        "all": stats(),
        "integration": stats("integration"),
        "legacy": stats("legacy"),
    }


# ──────────────────────── 注册结果存储 ────────────────────────


def save_registered(d: dict) -> None:
    """保存注册成功（或部分成功）的凭证。覆盖同邮箱旧记录。

    凭证三件套（access_token / session_token / refresh_token）单独存列；
    其余字段（如 device_id / cookie_header / id_token / 自定义元数据）打包进 extra_json。
    """
    email = (d.get("email") or "").lower()
    if not email:
        return
    workflow_scope = str(d.get("workflow_scope") or "legacy").strip().lower()
    if workflow_scope not in {"integration", "legacy"}:
        workflow_scope = "legacy"
    password = d.get("password", "") or ""
    extra = {k: v for k, v in d.items() if k not in {
        "email", "workflow_scope", "password", "access_token", "session_token", "refresh_token",
        "id_token", "device_id", "csrf_token", "cookie_header",
        "totp_secret", "totp_factor_id",
    }}
    with _lock:
        con = _conn()
        # ⚠️ INSERT OR REPLACE 是**整行替换**，不是按字段合并 —— 没写的列会被清空。
        #    重跑同一个邮箱时这会咬人：第一轮 register_password 设了密码但 OTP 超时，
        #    save_password_early 把密码存下了；第二轮 OpenAI 已经认识这个邮箱了，
        #    走 passwordless_login 分支根本不调 register_password，
        #    这一轮的 d["password"] 是空的 —— 直接 REPLACE 就把上一轮的密码冲没了。
        #    密码是 OpenAI 侧的**持久状态**，"这一轮没设" ≠ "这个号没有密码"，
        #    所以空值不覆盖非空旧值。
        #    token 三件套正相反：每轮跑都是全新的，旧的可能已失效，照常整列覆盖。
        # totp_secret 和密码同理，甚至更严：secret【一次性下发、服务端取不回】，
        #    丢了 = 该号 2FA 永久锁死。重跑同邮箱（已绑过 2FA）时这一轮不会再绑，
        #    d 里没有 secret —— 绝不能拿空值把库里已存的 secret 冲没。
        #    与密码合成一次 SELECT，顺带把两列旧值一起兜住。
        totp_secret = (d.get("totp_secret") or "").strip()
        totp_factor_id = (d.get("totp_factor_id") or "").strip()
        if not password or not totp_secret:
            row = con.execute(
                "SELECT password, totp_secret, totp_factor_id FROM registered WHERE email=?",
                (email,),
            ).fetchone()
            if row:
                if not password and (row["password"] or "").strip():
                    password = row["password"]
                if not totp_secret and (row["totp_secret"] or "").strip():
                    totp_secret = row["totp_secret"]
                    # factor_id 跟着 secret 走：本轮没绑就沿用旧的
                    totp_factor_id = totp_factor_id or (row["totp_factor_id"] or "")
        con.execute(
            "INSERT OR REPLACE INTO registered "
            "(email, workflow_scope, password, access_token, session_token, refresh_token, "
            "id_token, device_id, csrf_token, cookie_header, "
            "totp_secret, totp_factor_id, extra_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                email,
                workflow_scope,
                password,
                d.get("access_token", ""),
                d.get("session_token", ""),
                d.get("refresh_token", ""),
                d.get("id_token", ""),
                d.get("device_id", ""),
                d.get("csrf_token", ""),
                d.get("cookie_header", ""),
                totp_secret,
                totp_factor_id,
                json.dumps(extra, ensure_ascii=False) if extra else None,
                time.time(),
            ),
        )
        con.commit()


def save_password_early(
    email: str,
    password: str,
    workflow_scope: str = "legacy",
) -> None:
    """密码一在 OpenAI 侧生效就落盘，不等整个注册流程跑完。

    由 AuthFlow 的 on_password 回调触发（register_password 里 POST 200 之后）。
    此刻账号+密码在 OpenAI 那边已经建好，但本地还要过发码/验证/建账户三关，
    挂在任何一关都走不到 save_registered ——
    密码只活在内存里，进程一退号就成了谁也登不进去的孤儿。

    只写 email + password；token 三件套留空，等流程跑通后 save_registered
    用同一个 email 主键覆盖同一行补上。extra_json 打 pending 标记，
    方便一眼认出"有密码没凭证"的半成品行（跑通后会被 save_registered 清掉）。

    ⚠️ 行已存在时**只 UPDATE password**，绝不动已有的 token：
       重跑一个之前跑通过的邮箱时，不能把人家的凭证清空。
    """
    email = (email or "").strip().lower()
    password = (password or "").strip()
    workflow_scope = (
        workflow_scope if workflow_scope in {"integration", "legacy"} else "legacy"
    )
    if not email or not password:
        return
    with _lock:
        con = _conn()
        con.execute(
            "INSERT INTO registered "
            "(email, workflow_scope, password, access_token, session_token, refresh_token, "
            "id_token, device_id, csrf_token, cookie_header, extra_json, created_at) "
            "VALUES (?, ?, ?, '', '', '', '', '', '', '', ?, ?) "
            "ON CONFLICT(email) DO UPDATE SET password=excluded.password, "
            "workflow_scope=excluded.workflow_scope",
            (
                email,
                workflow_scope,
                password,
                json.dumps({"pending": True}, ensure_ascii=False),
                time.time(),
            ),
        )
        con.commit()


def save_totp_early(
    email: str,
    secret: str,
    factor_id: str = "",
    workflow_scope: str = "legacy",
) -> None:
    """2FA secret 一从 enroll 响应拿到就落盘，不等整个注册流程跑完。

    由 registrar 的 _bind_2fa_hook 触发（钩子在「拿到 session」和「Codex 授权 /
    绑手机号接码」之间调 bind_totp_2fa_inline，成功即拿到 secret）。

    ⚠️ 早落盘的理由和 save_password_early 一模一样、甚至更急：
       secret 绑成之后，流程还要走 Codex 授权 + add-phone 接码（可能好几分钟），
       这段时间 secret 只活在 registrar 内存的 _tfa_box 里。接码太久用户一关进程，
       secret 就永久蒸发 —— 而它【一次性下发、服务端取不回】，丢了该号 2FA 锁死。
       所以一拿到手就先写库，后面接码怎么中断都不怕。

    只写 totp 两列；token / 密码留给后续 save_registered 用同一 email 主键补齐。
    ⚠️ 行已存在时**只 UPDATE totp 两列**，绝不动已有的密码 / token
       —— 重跑老号时不能把人家已存的凭证清空。
    """
    email = (email or "").strip().lower()
    secret = (secret or "").strip()
    if not email or not secret:
        return
    factor_id = (factor_id or "").strip()
    workflow_scope = (
        workflow_scope if workflow_scope in {"integration", "legacy"} else "legacy"
    )
    with _lock:
        con = _conn()
        con.execute(
            "INSERT INTO registered "
            "(email, workflow_scope, password, access_token, session_token, refresh_token, "
            "id_token, device_id, csrf_token, cookie_header, "
            "totp_secret, totp_factor_id, extra_json, created_at) "
            "VALUES (?, ?, '', '', '', '', '', '', '', '', ?, ?, ?, ?) "
            "ON CONFLICT(email) DO UPDATE SET "
            "workflow_scope=excluded.workflow_scope, "
            "totp_secret=excluded.totp_secret, "
            "totp_factor_id=excluded.totp_factor_id",
            (
                email,
                workflow_scope,
                secret,
                factor_id,
                json.dumps({"pending": True}, ensure_ascii=False),
                time.time(),
            ),
        )
        con.commit()


def normalize_totp_secret(raw: str) -> str:
    """把用户手填的 TOTP secret 规范化成可用的 base32，非法值抛 ValueError。

    登录侧（auth_flow._totp_now）拿到 secret 直接 b32decode，**不做任何校验** ——
    脏值存进去要等到真登录时才炸，那时只看到一句 base32 解码异常，
    根本看不出是手填填错了。所以校验必须挡在写库这一关。

    接受的输入：
      - 裸 base32:  JBSWY3DPEHPK3PXP / jbswy3dp ehpk 3pxp / JBSW-Y3DP-EHPK
      - otpauth URI: otpauth://totp/ChatGPT:a@b.com?secret=JBSWY3DP&issuer=...
        （从手机 App 导出/二维码解码出来的就是这个格式，直接粘进来很常见）
    """
    s = (raw or "").strip()
    if not s:
        return ""
    # otpauth:// URI 抽 secret 参数
    if s.lower().startswith("otpauth://"):
        try:
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(s).query)
            s = (qs.get("secret") or [""])[0]
        except Exception:
            raise ValueError("otpauth 链接解析失败，请直接填 secret")
        if not s:
            raise ValueError("otpauth 链接里没有 secret 参数")
    # 去掉分隔符（手机 App 展示时常带空格/连字符）并统一大写
    s = s.replace(" ", "").replace("-", "").replace("_", "").upper()
    # base32 只有 A-Z 和 2-7，先挡掉明显非法字符再解码，报错更好懂
    if not s or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567=" for c in s):
        raise ValueError("TOTP secret 含非法字符（base32 只允许 A-Z 和 2-7）")
    try:
        # 补 padding 后试解，解得开才算合法。auth_flow 那边也是这么补的。
        decoded = base64.b32decode(s + "=" * (-len(s) % 8))
    except Exception:
        raise ValueError("TOTP secret 不是合法的 base32")
    if len(decoded) < 10:
        raise ValueError(f"TOTP secret 太短（解出 {len(decoded)} 字节，通常应为 20 字节）")
    return s


def update_registered_manual(email: str, password: Optional[str] = None,
                             totp_secret: Optional[str] = None) -> bool:
    """手动修正某个已注册账号的密码 / TOTP secret。

    ⚠️ 只改**本地库**，不会同步到 OpenAI —— 这里改密码不等于改了账号密码。
       用途是把外部已知的凭证补进来，或修正记录错误。

    传 None = 该字段不动（不是清空）。用 None 而不是空串做"不修改"的标记，
    是为了留出"主人真想清空某字段"的余地（传空串即清空）。

    totp_secret 会先过 normalize_totp_secret 校验，非法直接抛 ValueError；
    宁可这里报错，也不能让脏值躺进库里等登录时才炸。

    返回 False 表示该邮箱不存在（不会凭空插入新行 —— 手填是"修正已有记录"，
    真要新增外部账号是另一件事，走单独的导入功能）。
    """
    email = (email or "").strip().lower()
    if not email:
        return False
    sets, vals = [], []
    if password is not None:
        sets.append("password=?")
        vals.append(password)
    if totp_secret is not None:
        # 空串 = 主人主动清空；非空则必须过校验
        sets.append("totp_secret=?")
        vals.append(normalize_totp_secret(totp_secret) if totp_secret.strip() else "")
    if not sets:
        return False
    with _lock:
        con = _conn()
        row = con.execute("SELECT email FROM registered WHERE email=?", (email,)).fetchone()
        if not row:
            return False
        vals.append(email)
        con.execute(f"UPDATE registered SET {', '.join(sets)} WHERE email=?", vals)
        con.commit()
        return True


def update_plus_check(email: str, plus_info: dict) -> None:
    """把 Plus 检查结果写入 extra_json.plus_check。"""
    email = email.lower()
    con = _conn()
    cur = con.execute("SELECT extra_json FROM registered WHERE email=?", (email,))
    row = cur.fetchone()
    if not row:
        return
    extra = {}
    if row["extra_json"]:
        try:
            extra = json.loads(row["extra_json"])
        except Exception:
            extra = {}
    extra["plus_check"] = plus_info
    with _lock:
        con.execute(
            "UPDATE registered SET extra_json=? WHERE email=?",
            (json.dumps(extra, ensure_ascii=False), email),
        )
        con.commit()


def _registered_where(filt: str, workflow_scope: str = "") -> str:
    clauses = []
    if filt == "successful":
        clauses.append(
            "(length(access_token) > 0 "
            "OR length(session_token) > 0 "
            "OR length(refresh_token) > 0)"
        )
    elif filt == "has_rt":
        clauses.append("length(refresh_token) > 0")
    elif filt == "no_rt":
        clauses.append("coalesce(length(refresh_token),0) = 0")
    elif filt == "unchecked":
        clauses.append("(extra_json IS NULL OR extra_json NOT LIKE '%\"plus_check\"%')")
    elif filt == "free":
        clauses.append("extra_json LIKE '%\"free\"%'")
    elif filt == "plus":
        clauses.append("(extra_json LIKE '%\"plus_eligible\"%' OR extra_json LIKE '%\"plus_active\"%')")
    elif filt == "not_plus":
        clauses.append(
            "(extra_json IS NULL OR ("
            "extra_json NOT LIKE '%\"plus_eligible\"%' AND "
            "extra_json NOT LIKE '%\"plus_active\"%'))"
        )
    elif filt == "banned":
        clauses.append("extra_json LIKE '%\"banned\"%'")
    elif filt == "token_invalid":
        # token_invalid 从 2026-08-10 起会写库，得能筛出来，否则等于埋了：
        # 它既不在 unchecked 里（已有结论），又不在 free/plus/banned 里。
        clauses.append("extra_json LIKE '%\"token_invalid\"%'")
    scope = (workflow_scope or "").strip().lower()
    if scope in {"integration", "legacy"}:
        clauses.append(f"workflow_scope='{scope}'")
    return ("WHERE " + " AND ".join(clauses)) if clauses else ""


def count_registered(filter_rt: str = "all", workflow_scope: str = "") -> int:
    with closing(_conn()) as con:
        cur = con.execute(
            f"SELECT COUNT(*) FROM registered "
            f"{_registered_where(filter_rt, workflow_scope)}"
        )
        return cur.fetchone()[0]


def list_registered_emails(
    filter_rt: str = "unchecked",
    workflow_scope: str = "",
    limit: int = 50000,
) -> list[str]:
    """Return matching email addresses without loading credential columns."""
    where = _registered_where(filter_rt, workflow_scope)
    with closing(_conn()) as con:
        rows = con.execute(
            f"SELECT email FROM registered {where} "
            "ORDER BY created_at DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
        return [str(row["email"]) for row in rows]


def list_registered(
    limit: int = 20,
    offset: int = 0,
    filter_rt: str = "all",
    workflow_scope: str = "",
) -> list[dict]:
    where = _registered_where(filter_rt, workflow_scope)
    with closing(_conn()) as con:
        cur = con.execute(
            f"SELECT email, workflow_scope, password, totp_secret, "
            f"length(access_token) AS at_len, length(session_token) AS st_len, "
            f"length(refresh_token) AS rt_len, extra_json, created_at FROM registered "
            f"{where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
        raw_rows = cur.fetchall()
    rows = []
    for r in raw_rows:
        d = dict(r)
        plus = None
        credential_source = "registration"
        credential_source_label = "自动注册"
        if d.get("extra_json"):
            try:
                extra = json.loads(d["extra_json"])
                plus = extra.get("plus_check")
                credential_source = (
                    extra.get("credential_source") or credential_source
                )
                credential_source_label = (
                    extra.get("credential_source_label")
                    or credential_source_label
                )
            except Exception:
                pass
        d["plus_check"] = plus
        d["credential_source"] = credential_source
        d["credential_source_label"] = credential_source_label
        d.pop("extra_json", None)
        rows.append(d)
    return rows


def list_registered_full(
    limit: int = 5000,
    workflow_scope: str = "",
) -> list[dict]:
    """返回完整凭证（用于批量导出）。每行同 get_registered 的格式，外加 relay_url。

    ⚠️ relay_url（中转取件链接）**不在 registered 表里**，它跟着号池那一行走
       （outlook_accounts.relay_url，icloud_relay 这类号一号一条 token）。
       导出格式「邮箱----密码----2FA----取件url」要用它，所以这里 LEFT JOIN 带出来。
       用 JOIN 而不是给 registered 加列的原因：不用迁移、**已经注册完的老号也能导**
       （只要号池那行还在）；号池行被删掉就是空串，照约定留空、分隔符保留。
    """
    scope = (workflow_scope or "").strip().lower()
    where = " WHERE r.workflow_scope=?" if scope else ""
    args = (scope, limit) if scope else (limit,)
    with closing(_conn()) as con:
        cur = con.execute(
            "SELECT r.*, a.relay_url AS relay_url "
            "FROM registered r LEFT JOIN outlook_accounts a ON a.email = r.email "
            f"{where} "
            "ORDER BY r.created_at DESC LIMIT ?",
            args,
        )
        raw_rows = cur.fetchall()
    out = []
    for row in raw_rows:
        d = dict(row)
        if d.get("extra_json"):
            try:
                d["extra"] = json.loads(d["extra_json"])
            except Exception:
                d["extra"] = {}
        d.pop("extra_json", None)
        out.append(d)
    return out


def list_registered_by_emails(emails: list[str]) -> list[dict]:
    """按 email 列表返回完整凭证（批量导出勾选的号用）。

    - 行序 = created_at 倒序，和「注册结果」表格里看到的一致，方便核对。
    - 查不到的 email 直接不出现（号已被删掉的情况），不报错。
    - SQLite 单条语句变量数有上限（默认 999），所以分批查。
    - relay_url 从号池表 LEFT JOIN 带出（原因见 list_registered_full）。
    """
    cleaned = [e.strip().lower() for e in (emails or []) if e and e.strip()]
    if not cleaned:
        return []

    con = _conn()
    out = []
    CHUNK = 500
    for i in range(0, len(cleaned), CHUNK):
        part = cleaned[i:i + CHUNK]
        placeholders = ",".join("?" * len(part))
        cur = con.execute(
            f"SELECT r.*, a.relay_url AS relay_url "
            f"FROM registered r LEFT JOIN outlook_accounts a ON a.email = r.email "
            f"WHERE r.email IN ({placeholders})",
            part,
        )
        for row in cur.fetchall():
            d = dict(row)
            if d.get("extra_json"):
                try:
                    d["extra"] = json.loads(d["extra_json"])
                except Exception:
                    d["extra"] = {}
            d.pop("extra_json", None)
            out.append(d)

    out.sort(key=lambda d: d.get("created_at") or 0, reverse=True)
    return out


def get_registered(email: str) -> Optional[dict]:
    con = _conn()
    cur = con.execute("SELECT * FROM registered WHERE email=?", (email.lower(),))
    row = cur.fetchone()
    if not row:
        return None
    out = dict(row)
    if out.get("extra_json"):
        try:
            out["extra"] = json.loads(out["extra_json"])
        except Exception:
            out["extra"] = {}
    out.pop("extra_json", None)
    return out


def delete_registered(email: str) -> bool:
    with _lock:
        con = _conn()
        rc = con.execute("DELETE FROM registered WHERE email=?", (email.lower(),))
        con.commit()
        return rc.rowcount > 0


def delete_registered_by_emails(emails: list[str]) -> int:
    cleaned = [e.strip().lower() for e in (emails or []) if e and e.strip()]
    if not cleaned:
        return 0
    with _lock:
        con = _conn()
        placeholders = ",".join("?" * len(cleaned))
        rc = con.execute(
            f"DELETE FROM registered WHERE email IN ({placeholders})",
            cleaned,
        )
        con.commit()
        return rc.rowcount


def delete_all_registered(workflow_scope: str = "") -> int:
    scope = (workflow_scope or "").strip().lower()
    with _lock:
        con = _conn()
        if scope:
            rc = con.execute(
                "DELETE FROM registered WHERE workflow_scope=?",
                (scope,),
            )
        else:
            rc = con.execute("DELETE FROM registered")
        con.commit()
        return rc.rowcount


# ──────────────────────── 运行记录 ────────────────────────


def create_run(run_id: str, email: str, log_path: str) -> None:
    with _lock:
        con = _conn()
        con.execute(
            "INSERT INTO runs(run_id, email, status, started_at, log_path) "
            "VALUES (?, ?, 'running', ?, ?)",
            (run_id, email.lower(), time.time(), log_path),
        )
        con.commit()


def finish_run(run_id: str, status: str, error: str = "", category: str = "") -> None:
    with _lock:
        con = _conn()
        con.execute(
            "UPDATE runs SET status=?, finished_at=?, error=?, error_category=? WHERE run_id=?",
            (status, time.time(), (error or "")[:500], category or None, run_id),
        )
        con.commit()


def list_runs(limit: int = 50) -> list[dict]:
    con = _conn()
    cur = con.execute(
        "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,),
    )
    return [dict(r) for r in cur.fetchall()]


def save_integration_job(job: dict) -> None:
    job_id = str(job.get("id") or "").strip()
    if not job_id:
        return
    with _lock, closing(_conn()) as con:
        con.execute(
            "INSERT INTO integration_jobs(job_id,status,updated_at,payload_json)"
            " VALUES (?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET"
            " status=excluded.status,updated_at=excluded.updated_at,"
            "payload_json=excluded.payload_json",
            (
                job_id,
                str(job.get("status") or "idle"),
                float(job.get("updated_at") or time.time()),
                json.dumps(job, ensure_ascii=False),
            ),
        )
        con.commit()


def get_latest_integration_job() -> Optional[dict]:
    with closing(_conn()) as con:
        row = con.execute(
            "SELECT payload_json FROM integration_jobs"
            " ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["payload_json"])
    except Exception:
        return None


def save_recovery_batch(job: dict) -> None:
    batch_id = str(job.get("id") or "").strip()
    if not batch_id:
        return
    with _lock, closing(_conn()) as con:
        con.execute(
            "INSERT INTO recovery_batches(batch_id,status,updated_at,payload_json)"
            " VALUES (?,?,?,?) ON CONFLICT(batch_id) DO UPDATE SET"
            " status=excluded.status,updated_at=excluded.updated_at,"
            " payload_json=excluded.payload_json",
            (
                batch_id,
                str(job.get("status") or "running"),
                float(job.get("updated_at") or time.time()),
                json.dumps(job, ensure_ascii=False),
            ),
        )
        con.commit()


def get_recovery_batch(batch_id: str) -> Optional[dict]:
    with closing(_conn()) as con:
        row = con.execute(
            "SELECT payload_json FROM recovery_batches WHERE batch_id=?",
            (str(batch_id or "").strip(),),
        ).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["payload_json"])
    except Exception:
        return None


def list_resumable_recovery_batches() -> list[dict]:
    with closing(_conn()) as con:
        rows = con.execute(
            "SELECT payload_json FROM recovery_batches"
            " WHERE status IN ('running','paused')"
            " ORDER BY updated_at ASC"
        ).fetchall()
    out = []
    for row in rows:
        try:
            job = json.loads(row["payload_json"])
        except Exception:
            continue
        if isinstance(job, dict):
            out.append(job)
    return out


def get_latest_recovery_batch() -> Optional[dict]:
    with closing(_conn()) as con:
        row = con.execute(
            "SELECT payload_json FROM recovery_batches"
            " ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
    if not row:
        return None
    try:
        job = json.loads(row["payload_json"])
        return job if isinstance(job, dict) else None
    except Exception:
        return None


# ──────────────────────── settings (KV) ────────────────────────


def get_setting(key: str, default: str = "") -> str:
    with closing(_conn()) as con:
        cur = con.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = cur.fetchone()
        return row["value"] if row else default


def list_proxy_pool() -> list[str]:
    with closing(_conn()) as con:
        return [
            row["proxy"]
            for row in con.execute(
                "SELECT proxy FROM proxy_pool ORDER BY created_at ASC"
            ).fetchall()
        ]


def replace_proxy_pool(items: list[str]) -> list[str]:
    now = time.time()
    cleaned = list(dict.fromkeys(str(item).strip() for item in items if str(item).strip()))
    with _lock, closing(_conn()) as con:
        con.execute("DELETE FROM proxy_pool")
        con.executemany(
            "INSERT INTO proxy_pool(proxy, created_at, updated_at) VALUES (?, ?, ?)",
            [(proxy, now + index / 1000, now) for index, proxy in enumerate(cleaned)],
        )
        con.commit()
    return cleaned


def delete_proxy(proxy: str) -> bool:
    with _lock, closing(_conn()) as con:
        rc = con.execute("DELETE FROM proxy_pool WHERE proxy=?", (proxy,))
        con.commit()
        return rc.rowcount > 0


def set_setting(key: str, value) -> None:
    with _lock, closing(_conn()) as con:
        con.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        con.commit()


# ──────────────────────── 邮箱来源配置 ────────────────────────


def get_mail_config() -> dict:
    """返回邮箱来源配置（密码类字段隐藏明文）。

    provider 声明的配置项自动带出来 —— 加新邮箱时这里不用改，
    新 provider 的 config_fields 会自动出现在返回值里。
    """
    from mail_providers import list_providers

    out = {"mail_source": get_setting("mail_source", "outlook")}
    for p in list_providers():
        for f in p["config_fields"]:
            key = f["key"]
            if f.get("type") == "password":
                out[key] = "***" if get_setting(key) else ""
            else:
                out[key] = get_setting(key, "")
    return out


def save_mail_config(data: dict) -> None:
    """保存邮箱配置。password 类字段传 '***' 表示不修改。

    mail_source 校验改成查 mail_providers 注册表：
        以前是写死的白名单 ("outlook", "cf_temp")，选了别的会被
        **静默改回 outlook** —— 用户看到的是"保存成功但选择没生效"。
        现在未知来源直接抛错，问题当场暴露。
    """
    from mail_providers import get_provider_class, list_providers

    if "mail_source" in data:
        src = str(data["mail_source"]).strip().lower()
        get_provider_class(src)  # 未注册的 kind 会抛 MailProviderError
        set_setting("mail_source", src)

    # 按 provider 声明的字段保存，加新邮箱时这里零改动
    for p in list_providers():
        for f in p["config_fields"]:
            key = f["key"]
            if key not in data:
                continue
            val = data[key]
            if f.get("type") == "password":
                if not val or val == "***":
                    continue  # 没填 / 是掩码 → 保持原值
            set_setting(key, str(val).strip())


def get_secret_setting(key: str) -> str:
    """内部用：拿密码类配置的明文。"""
    return get_setting(key, "")


def get_mail_settings() -> dict:
    """内部用：给 create_mail_provider 的 settings（含明文密钥）。

    跟 get_mail_config 的区别：这个不打码，只在服务端构造 provider 时用，
    绝不能直接返回给前端。
    """
    from mail_providers import list_providers

    out = {"mail_source": get_setting("mail_source", "outlook")}
    for p in list_providers():
        for f in p["config_fields"]:
            out[f["key"]] = get_setting(f["key"], "")
    return out


def get_cf_admin_token() -> str:
    """内部用：拿明文 admin_token。"""
    return get_setting("cf_admin_token", "")


# ──────────────────────── NexusFlow 一体化配置 ────────────────────────


def get_integration_config() -> dict:
    """返回一体化流程配置。密钥只返回是否已设置。"""
    mailhub_token = get_setting("nx_mailhub_token") or os.getenv("MAILHUB_ADMIN_TOKEN", "")
    icloud_token = get_setting("nx_icloud_token") or os.getenv("ICLOUD_PRIME_API_TOKEN", "")
    return {
        "mailhub_url": get_setting(
            "nx_mailhub_url", os.getenv("MAILHUB_INTERNAL_URL", "http://mail-hub:8090")
        ),
        "mailhub_token": "***" if mailhub_token else "",
        "icloud_url": get_setting(
            "nx_icloud_url", os.getenv("ICLOUD_INTERNAL_URL", "http://icloud-prime:8081")
        ),
        "icloud_token": "***" if icloud_token else "",
        "icloud_account_id": get_setting("nx_icloud_account_id", ""),
        "receiver_mode": get_setting("nx_receiver_mode", "icloud"),
        "imap_host": get_setting("nx_imap_host", ""),
        "imap_port": get_setting("nx_imap_port", "993"),
        "imap_email": get_setting("nx_imap_email", ""),
        "imap_password": "***" if get_setting("nx_imap_password") else "",
        "imap_folder": get_setting("nx_imap_folder", "INBOX"),
        "imap_ssl": get_setting("nx_imap_ssl", "1") in ("1", "true"),
        "proxy": get_setting("nx_proxy", ""),
        "otp_timeout": get_setting("nx_otp_timeout", "180"),
        "want_access_token": get_setting("nx_want_access_token", "1") in ("1", "true"),
        "want_session_token": get_setting("nx_want_session_token", "1") in ("1", "true"),
        "want_refresh_token": get_setting("nx_want_refresh_token", "1") in ("1", "true"),
        "want_2fa": get_setting("nx_want_2fa", "0") in ("1", "true"),
        "continuous": get_setting("nx_continuous", "1") in ("1", "true"),
        "target_count": int(get_setting("nx_target_count", "0") or 0),
        "cool_down_seconds": int(get_setting("nx_cool_down_seconds", "30") or 30),
        "auto_qualify": get_setting("nx_auto_qualify", "1") in ("1", "true"),
        "momo_proxy": get_setting("nx_momo_proxy", ""),
        "upi_proxy": get_setting("nx_upi_proxy", ""),
        "payment_timeout": int(get_setting("nx_payment_timeout", "45") or 45),
    }


def get_integration_internal_config() -> dict:
    """内部编排使用，包含明文服务密钥。绝不能直接返回前端。"""
    cfg = get_integration_config()
    cfg["mailhub_token"] = (
        get_setting("nx_mailhub_token") or os.getenv("MAILHUB_ADMIN_TOKEN", "")
    )
    cfg["icloud_token"] = (
        get_setting("nx_icloud_token") or os.getenv("ICLOUD_PRIME_API_TOKEN", "")
    )
    cfg["imap_password"] = get_setting("nx_imap_password", "")
    cfg["imap_ssl"] = get_setting("nx_imap_ssl", "1") in ("1", "true")
    cfg["otp_timeout"] = int(get_setting("nx_otp_timeout", "180") or 180)
    cfg["target_count"] = max(0, int(get_setting("nx_target_count", "0") or 0))
    cfg["cool_down_seconds"] = max(
        0, min(3600, int(get_setting("nx_cool_down_seconds", "30") or 30))
    )
    cfg["payment_timeout"] = max(
        10, min(180, int(get_setting("nx_payment_timeout", "45") or 45))
    )
    for key in ("want_access_token", "want_session_token", "want_refresh_token", "want_2fa"):
        cfg[key] = get_setting(f"nx_{key}", "0" if key == "want_2fa" else "1") in (
            "1", "true", "yes", "on",
        )
    cfg["continuous"] = get_setting("nx_continuous", "1") in (
        "1", "true", "yes", "on",
    )
    cfg["auto_qualify"] = get_setting("nx_auto_qualify", "1") in (
        "1", "true", "yes", "on",
    )
    return cfg


def save_integration_config(data: dict) -> None:
    """保存一体化配置；密钥传 '***' 或留空时保持原值。"""
    for key in (
        "mailhub_url", "icloud_url", "icloud_account_id", "receiver_mode",
        "imap_host", "imap_port", "imap_email", "imap_folder", "proxy", "otp_timeout",
        "target_count", "cool_down_seconds", "momo_proxy", "upi_proxy",
        "payment_timeout",
    ):
        if key in data and data.get(key) is not None:
            set_setting(f"nx_{key}", str(data.get(key)).strip())
    for key in ("mailhub_token", "icloud_token"):
        value = data.get(key)
        if value and value != "***":
            set_setting(f"nx_{key}", str(value).strip())
    if data.get("imap_password") and data["imap_password"] != "***":
        set_setting("nx_imap_password", str(data["imap_password"]).strip())
    if "imap_ssl" in data:
        value = data["imap_ssl"]
        enabled = value if isinstance(value, bool) else str(value).lower() in (
            "1", "true", "yes", "on",
        )
        set_setting("nx_imap_ssl", "1" if enabled else "0")
    for key in (
        "want_access_token", "want_session_token", "want_refresh_token", "want_2fa",
        "continuous", "auto_qualify",
    ):
        if key in data:
            value = data[key]
            enabled = value if isinstance(value, bool) else str(value).lower() in (
                "1", "true", "yes", "on",
            )
            set_setting(f"nx_{key}", "1" if enabled else "0")


# ──────────────────────── SMS 接码配置 ────────────────────────


def get_sms_config() -> dict:
    """返回 SMS 接码配置（api_key 隐藏明文）。

    sms_enabled:        '0'/'1' 是否启用接码（命中 add-phone 时才会用）
    sms_provider:       smsbower
    sms_country:        国家代码或 ID（推荐 '52' = Thailand，OpenAI 走 SMS 的唯一稳定国家）
    sms_service:        服务代码（OpenAI = 'dr'）
    sms_max_price:      号码最高单价（SmsBower / SmsBower 用，单位平台货币；空 / -1 = 不限）
    sms_reuse_phone:    '0'/'1' 同号复用（SmsBower / SmsBower 支持，省钱）
    sms_phone_success_max: 同号最多复用几次（默认 3）
    sms_auto_country:   '0'/'1' 自动选最优国家（按价格 + 库存）
    sms_auto_min_stock: 自动选国家最低库存（默认 20）
    sms_auto_max_price: 自动选国家最高单价（默认 0 = 不限）
    """
    return {
        "sms_enabled":             get_setting("sms_enabled", "0"),
        "sms_provider":            get_setting("sms_provider", "smsbower"),
        "sms_api_key":             "***" if get_setting("sms_api_key") else "",
        "sms_country":             get_setting("sms_country", "52"),
        "sms_service":             get_setting("sms_service", "dr"),
        "sms_max_price":           get_setting("sms_max_price", ""),
        "sms_fixed_price":         get_setting("sms_fixed_price", ""),
        "sms_reuse_phone":         get_setting("sms_reuse_phone", "0"),
        "sms_phone_success_max":   get_setting("sms_phone_success_max", "3"),
        "sms_auto_country":        get_setting("sms_auto_country", "0"),
        "sms_strict_whitelist":    get_setting("sms_strict_whitelist", "0"),
        "sms_allowed_countries":   get_setting("sms_allowed_countries", ""),
        "sms_auto_min_stock":      get_setting("sms_auto_min_stock", "20"),
        "sms_auto_max_price":      get_setting("sms_auto_max_price", ""),
        "sms_max_phone_attempts":  get_setting("sms_max_phone_attempts", ""),
        "sms_per_phone_timeout":   get_setting("sms_per_phone_timeout", "80"),
    }


def save_sms_config(data: dict) -> None:
    """保存 SMS 配置。sms_api_key 传 '***' 表示不修改。"""
    # 校验 provider
    valid_providers = {"smsbower", "herosms"}
    if "sms_provider" in data:
        p = str(data["sms_provider"]).strip().lower()
        if p not in valid_providers:
            p = "smsbower"
        set_setting("sms_provider", p)
    # 字符串字段直接落
    for key in (
        "sms_country", "sms_service", "sms_max_price", "sms_fixed_price",
        "sms_phone_success_max", "sms_auto_min_stock", "sms_auto_max_price",
        "sms_max_phone_attempts", "sms_per_phone_timeout",
        "sms_allowed_countries",
    ):
        if key in data:
            set_setting(key, str(data[key]).strip())
    # 布尔字段（前端传 '0'/'1' 或 bool）
    for key in ("sms_enabled", "sms_reuse_phone", "sms_auto_country", "sms_strict_whitelist"):
        if key in data:
            v = data[key]
            if isinstance(v, bool):
                set_setting(key, "1" if v else "0")
            else:
                s = str(v).strip().lower()
                set_setting(key, "1" if s in ("1", "true", "yes", "on") else "0")
    # API key（'***' 不修改）
    if data.get("sms_api_key") and data["sms_api_key"] != "***":
        set_setting("sms_api_key", str(data["sms_api_key"]).strip())


def get_sms_internal_config() -> dict:
    """内部用：拿明文 sms_api_key,供 sms_provider 实例化使用。"""
    return {
        "sms_enabled":             get_setting("sms_enabled", "0") in ("1", "true"),
        "sms_provider":            get_setting("sms_provider", "smsbower"),
        "sms_api_key":             get_setting("sms_api_key", ""),
        "sms_country":             get_setting("sms_country", "52"),
        "sms_service":             get_setting("sms_service", "dr"),
        "sms_max_price":           get_setting("sms_max_price", ""),
        "sms_fixed_price":         get_setting("sms_fixed_price", ""),
        "sms_reuse_phone":         get_setting("sms_reuse_phone", "0") in ("1", "true"),
        "sms_phone_success_max":   get_setting("sms_phone_success_max", "3"),
        "sms_auto_country":        get_setting("sms_auto_country", "0") in ("1", "true"),
        "sms_strict_whitelist":    get_setting("sms_strict_whitelist", "0") in ("1", "true"),
        "sms_allowed_countries":   get_setting("sms_allowed_countries", ""),
        "sms_auto_min_stock":      get_setting("sms_auto_min_stock", "20"),
        "sms_auto_max_price":      get_setting("sms_auto_max_price", ""),
        "sms_max_phone_attempts":  get_setting("sms_max_phone_attempts", ""),
        "sms_per_phone_timeout":   get_setting("sms_per_phone_timeout", "80"),
    }


# ──────────────────────── 自动导出配置 (CPA / SUB2API) ────────────────────────


def get_export_config() -> dict:
    """返回导出配置（敏感字段做明文/'***' 占位）。

    给前端展示用：
      cpa_mgmt_key / sub2api_api_key 已设置时返回 '***'，未设置返回 ''。
      保存时传 '***' 代表不修改。
    """
    return {
        # CPA
        "cpa_enabled":     get_setting("export_cpa_enabled", "0"),
        "cpa_url":         get_setting("export_cpa_url", ""),
        "cpa_mgmt_key":    "***" if get_setting("export_cpa_mgmt_key") else "",
        "cpa_timeout":     get_setting("export_cpa_timeout", "30"),
        # SUB2API
        "sub2api_enabled":    get_setting("export_sub2api_enabled", "0"),
        "sub2api_url":        get_setting("export_sub2api_url", ""),
        "sub2api_api_key":    "***" if get_setting("export_sub2api_api_key") else "",
        "sub2api_group_ids":  get_setting("export_sub2api_group_ids", "2"),
        "sub2api_timeout":    get_setting("export_sub2api_timeout", "30"),
    }


def save_export_config(data: dict) -> None:
    """保存导出配置。密文字段传 '***' 表示不修改。"""
    # 布尔开关
    for key_in, key_out in (
        ("cpa_enabled",     "export_cpa_enabled"),
        ("sub2api_enabled", "export_sub2api_enabled"),
    ):
        if key_in in data:
            v = data[key_in]
            if isinstance(v, bool):
                set_setting(key_out, "1" if v else "0")
            else:
                s = str(v).strip().lower()
                set_setting(key_out, "1" if s in ("1", "true", "yes", "on") else "0")
    # 字符串字段（明文）
    for key_in, key_out in (
        ("cpa_url",            "export_cpa_url"),
        ("cpa_timeout",        "export_cpa_timeout"),
        ("sub2api_url",        "export_sub2api_url"),
        ("sub2api_group_ids",  "export_sub2api_group_ids"),
        ("sub2api_timeout",    "export_sub2api_timeout"),
    ):
        if key_in in data:
            set_setting(key_out, str(data[key_in] or "").strip())
    # 密文字段（'***' 不修改）
    if data.get("cpa_mgmt_key") and data["cpa_mgmt_key"] != "***":
        set_setting("export_cpa_mgmt_key", str(data["cpa_mgmt_key"]).strip())
    if data.get("sub2api_api_key") and data["sub2api_api_key"] != "***":
        set_setting("export_sub2api_api_key", str(data["sub2api_api_key"]).strip())


def get_export_internal_config() -> dict:
    """内部用：拿明文密钥 + 解析后的 enabled 布尔。供 registrar / app.test 调用。

    返回两个子配置 dict，可分别传给 exporter.export_to_cpa / export_to_sub2api。
    """
    cpa = {
        "enabled":      get_setting("export_cpa_enabled", "0") in ("1", "true"),
        "cpa_url":      get_setting("export_cpa_url", ""),
        "cpa_mgmt_key": get_setting("export_cpa_mgmt_key", ""),
        "cpa_timeout":  get_setting("export_cpa_timeout", "30"),
    }
    sub2api = {
        "enabled":            get_setting("export_sub2api_enabled", "0") in ("1", "true"),
        "sub2api_url":        get_setting("export_sub2api_url", ""),
        "sub2api_api_key":    get_setting("export_sub2api_api_key", ""),
        "sub2api_group_ids":  get_setting("export_sub2api_group_ids", "2"),
        "sub2api_timeout":    get_setting("export_sub2api_timeout", "30"),
    }
    return {"cpa": cpa, "sub2api": sub2api}


# 模块加载时自动建表
init_db()
