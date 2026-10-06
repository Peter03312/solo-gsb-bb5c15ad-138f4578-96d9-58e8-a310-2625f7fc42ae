"""SQLite 数据访问层。

- 显式事务：写操作在 BEGIN IMMEDIATE 事务中进行，防止答卷与结算并发竞态。
- 所有时间戳统一为服务端 Unix 秒（INTEGER）。
- events 表为操作时间线：事件与业务状态在同一写事务内提交，自增 id 即提交顺序。
"""

import json
import os
import sqlite3
import time
from contextlib import contextmanager

DB_PATH = os.environ.get("SNIFF_DB_PATH", "/data/sniff.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tests (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    code               TEXT NOT NULL UNIQUE,          -- 负责人管理码
    token_hash         TEXT NOT NULL,                 -- 负责人令牌哈希
    sample_a           TEXT NOT NULL,
    sample_b           TEXT NOT NULL,
    deadline           INTEGER NOT NULL,
    min_valid          INTEGER NOT NULL,
    created_at         INTEGER NOT NULL,
    settled_at         INTEGER,
    ended_at           INTEGER,                          -- 负责人提前结束收集时间（秒）；NULL 表示未提前结束
    end_reason         TEXT,                             -- 提前结束原因（trim 后 1～200 字）
    valid_count        INTEGER,
    correct_count      INTEGER,
    p_value            REAL,
    distinguishable    INTEGER
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,   -- 全局提交顺序（稳定排序键）
    test_id     INTEGER NOT NULL REFERENCES tests(id),
    ts          INTEGER NOT NULL,                    -- 服务端事件时间（秒，事务内采集）
    type        TEXT NOT NULL,   -- create/answer/adjust/end/settle/history_start/share_create/share_revoke
    payload     TEXT NOT NULL DEFAULT '{}'           -- JSON，盲态字段读取时再裁剪
);

CREATE INDEX IF NOT EXISTS idx_events_test ON events(test_id, id);

CREATE TABLE IF NOT EXISTS assignments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    test_id      INTEGER NOT NULL REFERENCES tests(id),
    panelist     TEXT NOT NULL,
    code         TEXT NOT NULL UNIQUE,                -- 评员盲评码
    odd_pos      INTEGER NOT NULL,                    -- 异样杯位置 1/2/3
    odd_sample   TEXT NOT NULL,                       -- 异样杯样本编号（A 或 B）
    answer       INTEGER,                             -- 1/2/3；弃权为 -1；未提交 NULL
    answered_at  INTEGER,
    status       TEXT NOT NULL DEFAULT 'active',      -- active 有效名单；withdrawn 已撤回
    UNIQUE(test_id, panelist)
);

CREATE INDEX IF NOT EXISTS idx_assignments_test ON assignments(test_id);

-- 已结算实验的脱敏结果分享凭证（不记名只读能力凭证）：
-- 仅已结算测试可生成；到期或撤销后读取一律拒绝。
-- (test_id, note, ttl_seconds) 唯一：同实验同备注同有效期重复请求返回原凭证。
CREATE TABLE IF NOT EXISTS shares (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    test_id      INTEGER NOT NULL REFERENCES tests(id),
    share_code   TEXT NOT NULL UNIQUE,                -- 一次性随机分享凭证（读取凭证）
    note         TEXT NOT NULL DEFAULT '',            -- 负责人备注（归一化 trim 后存储）
    ttl_seconds  INTEGER NOT NULL,                    -- 有效期（秒），最长 7 天
    created_at   INTEGER NOT NULL,                    -- 生成时间（秒）
    expires_at   INTEGER NOT NULL,                    -- 到期时间（秒，服务端时钟判定）
    revoked_at   INTEGER,                             -- 撤销时间（秒）；NULL 未撤销
    UNIQUE(test_id, note, ttl_seconds)
);

CREATE INDEX IF NOT EXISTS idx_shares_test ON shares(test_id);
"""


def connect():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = connect()
    try:
        conn.executescript(_SCHEMA)
        # 兼容旧库：为已存在的 assignments 表补 status 列
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(assignments)")}
        if "status" not in cols:
            conn.execute(
                "ALTER TABLE assignments ADD COLUMN status TEXT NOT NULL "
                "DEFAULT 'active'"
            )
        # 兼容旧库：为已存在的 tests 表补提前结束相关列
        tcols = {r["name"] for r in conn.execute("PRAGMA table_info(tests)")}
        if "ended_at" not in tcols:
            conn.execute("ALTER TABLE tests ADD COLUMN ended_at INTEGER")
        if "end_reason" not in tcols:
            conn.execute("ALTER TABLE tests ADD COLUMN end_reason TEXT")
        conn.commit()
    finally:
        conn.close()
    # 兼容旧库：events 表上线前创建的测试没有任何操作记录。
    # 为每个这样的测试补一条明确的“记录起点”标记，不重建、不虚构既往操作。
    backfill_history_markers()


def backfill_history_markers():
    """为缺少任何事件的旧测试补 history_start 起点标记（幂等）。"""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, created_at FROM tests t WHERE NOT EXISTS ("
            "SELECT 1 FROM events e WHERE e.test_id = t.id)"
        ).fetchall()
        marker_ts = int(time.time())
        for r in rows:
            conn.execute(
                "INSERT INTO events (test_id, ts, type, payload) "
                "VALUES (?, ?, 'history_start', ?)",
                (
                    r["id"],
                    marker_ts,
                    json.dumps({
                        "note": "操作时间线自本次升级起开始记录；该测试创建于旧版本，"
                                "既往创建、名单调整、答卷与结算操作无可追溯记录，"
                                "不做历史重建。",
                        "legacy_created_at": r["created_at"],
                    }, ensure_ascii=False),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def log_event(conn, test_id: int, ts: int, event_type: str, payload: dict):
    """在当前写事务内追加一条时间线事件；随业务状态一起提交或回滚。"""
    conn.execute(
        "INSERT INTO events (test_id, ts, type, payload) VALUES (?, ?, ?, ?)",
        (test_id, ts, event_type,
         json.dumps(payload, ensure_ascii=False)),
    )


@contextmanager
def write_tx():
    """立即拿写锁的事务；异常回滚，正常提交。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def read_conn():
    return connect()
