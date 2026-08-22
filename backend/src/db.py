"""
Memory Vault - 数据库连接与初始化模块

包含:
- SQLite WAL 模式连接
- memory_entries / memory_fts / memory_logs 完整建表
- memory_entries_archive / memory_fts_archive 冷归档表
- FTS5 自动同步触发器 (INSERT / UPDATE / DELETE)
- 审计日志自动触发器 (trg_log_insert / trg_log_update / trg_log_delete)
- 性能索引
"""

import sqlite3
import os
import re
import logging
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("memory.db")

DB_PATH = os.getenv("MEMORY_DB_PATH", "memory.db")
OBSIDIAN_VAULT_PATH = os.getenv("OBSIDIAN_VAULT_PATH", "./obsidian_vault")

# FTS5 的 unicode61 分词器不切中日韩文字：整句中文会变成一个 token，
# 结果只有把原文一字不差打出来才搜得到（搜"咖啡"是 0 条）。
# 这里把每个中日韩字符用空格隔开再入索引，检索时对查询做同样处理并当短语搜，
# 既能命中任意子串，又保留了字与字的相邻关系（"喝咖啡"不会错误命中"喝一杯冰美式咖啡"）。
_CJK = r'[一-鿿㐀-䶿぀-ヿ가-힯]'


def segment_cjk(text) -> str:
    """把中日韩字符逐个空格隔开；英文/数字原样保留。写入与检索必须用同一套。"""
    return re.sub(f'({_CJK})', r' \1 ', text or '')


def get_db_connection(db_path: str = DB_PATH) -> sqlite3.Connection:
    """获取高性能 SQLite 连接（WAL 模式 + 忙等待超时）"""
    conn = sqlite3.connect(db_path, timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    conn.execute("PRAGMA foreign_keys=ON;")
    # FTS 同步触发器里要调 seg()，每条连接都必须注册，否则写入时报 no such function
    conn.create_function("seg", 1, segment_cjk, deterministic=True)
    return conn


# 功能开关的默认值。key 不存在时按默认值判断；init_db 会把它们写进 config 表
FEATURE_FLAGS = {
    "feature.plan": ("true", "计划功能（plan-life/work/dev）"),
    "feature.surface": ("true", "主动上下文浮现"),
    "feature.decay": ("true", "每日权重衰减与归档"),
    "feature.obsidian_export": ("true", "Obsidian .md 导出"),
}


def get_feature_flag(conn, key: str, default: bool = True) -> bool:
    """读取功能开关。value 为 true/1/yes/on 视为开启，其余视为关闭。

    key 在 config 表里不存在时返回 default，保证旧库升级后不破坏行为。
    """
    row = conn.execute(
        "SELECT value FROM config WHERE key = ?", (key,)
    ).fetchone()
    if row is None:
        return default
    return str(row["value"]).strip().lower() in ("true", "1", "yes", "on", "enabled")


SQL_INIT = """
-- ================================================================
-- 1. 主记忆与灵感表
-- ================================================================
CREATE TABLE IF NOT EXISTS memory_entries (
    entry_id        TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    session_id      TEXT,
    category        TEXT NOT NULL CHECK(category IN (
                        'preference','task','decision',
                        'knowledge','fiction_inspiration','diary')),
    type            TEXT,
    content         TEXT NOT NULL,
    confidence      TEXT NOT NULL CHECK(confidence IN (
                        'high','medium','low')),
    status          TEXT DEFAULT 'active' CHECK(status IN (
                        'active','completed','archived',
                        'superseded','pending_merge')),
    pin             INTEGER DEFAULT 0,
    anchor          INTEGER DEFAULT 0,
    weight          REAL DEFAULT 1.0,
    superseded_by   TEXT,
    created_at      TEXT DEFAULT (
        strftime('%Y-%m-%d %H:%M:%S', 'now', 'utc')),
    last_accessed_at TEXT,
    -- 外部系统（OB 等）迁入时，这边没有对应列的元数据原样存成 JSON
    -- （标签/情感 V-A/重要度/why_remembered/原始 bucket_id …），
    -- 免得迁移即丢失；将来想用哪个再提升成正式列
    meta            TEXT
);

-- ================================================================
-- 2. FTS5 全文搜索虚拟表
-- ================================================================
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    entry_id UNINDEXED,
    tokenized_content,
    tokenize='unicode61'
);

-- ================================================================
-- 3. 结构化日志与审计表
-- ================================================================
CREATE TABLE IF NOT EXISTS memory_logs (
    log_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id  TEXT NOT NULL,
    action    TEXT NOT NULL CHECK(action IN (
                  'INSERT','UPDATE','SOFT_DELETE','RECALL_HIT',
                  'DECAY','SUPERSEDE')),
    old_value TEXT,
    new_value TEXT,
    timestamp TEXT DEFAULT (
        strftime('%Y-%m-%d %H:%M:%S', 'now', 'utc'))
);

-- ================================================================
-- 4. 冷归档表（补充设计 4）
-- ================================================================
CREATE TABLE IF NOT EXISTS memory_entries_archive (
    entry_id         TEXT PRIMARY KEY,
    user_id          TEXT NOT NULL,
    session_id       TEXT,
    category         TEXT NOT NULL,
    type             TEXT,
    content          TEXT NOT NULL,
    confidence       TEXT NOT NULL,
    status           TEXT NOT NULL,
    pin              INTEGER DEFAULT 0,
    anchor           INTEGER DEFAULT 0,
    weight           REAL DEFAULT 1.0,
    superseded_by    TEXT,
    created_at       TEXT,
    last_accessed_at TEXT,
    archived_at      TEXT DEFAULT (
        strftime('%Y-%m-%d %H:%M:%S', 'now', 'utc'))
);

-- 冷归档 FTS5 索引
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts_archive USING fts5(
    entry_id UNINDEXED,
    tokenized_content,
    tokenize='unicode61'
);

-- ================================================================
-- 5. FTS5 数据库级自动同步触发器（补充设计 1）
-- ================================================================

-- INSERT: 仅索引 active 状态
DROP TRIGGER IF EXISTS trg_memory_fts_insert;
CREATE TRIGGER trg_memory_fts_insert
AFTER INSERT ON memory_entries
WHEN new.status = 'active'
BEGIN
    INSERT INTO memory_fts(entry_id, tokenized_content)
    VALUES (new.entry_id, seg(new.content));
END;

-- UPDATE: 先删旧索引，若新状态为 active 则重建
DROP TRIGGER IF EXISTS trg_memory_fts_update;
CREATE TRIGGER trg_memory_fts_update
AFTER UPDATE ON memory_entries
BEGIN
    DELETE FROM memory_fts WHERE entry_id = new.entry_id;
    INSERT INTO memory_fts(entry_id, tokenized_content)
    SELECT new.entry_id, seg(new.content)
    WHERE new.status = 'active';
END;

-- DELETE: 物理删除时清理索引
CREATE TRIGGER IF NOT EXISTS trg_memory_fts_delete
AFTER DELETE ON memory_entries
BEGIN
    DELETE FROM memory_fts WHERE entry_id = old.entry_id;
END;

-- ================================================================
-- 6. 审计日志自动触发器（补充设计 5）
-- ================================================================

CREATE TRIGGER IF NOT EXISTS trg_log_insert
AFTER INSERT ON memory_entries
BEGIN
    INSERT INTO memory_logs (entry_id, action, new_value, timestamp)
    VALUES (
        new.entry_id, 'INSERT',
        json_object(
            'category',   new.category,
            'content',    substr(new.content, 1, 500),
            'confidence', new.confidence,
            'status',     new.status,
            'weight',     new.weight
        ),
        strftime('%Y-%m-%d %H:%M:%S', 'now', 'utc')
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_log_update
AFTER UPDATE ON memory_entries
BEGIN
    INSERT INTO memory_logs (entry_id, action, old_value, new_value, timestamp)
    VALUES (
        new.entry_id, 'UPDATE',
        json_object(
            'category',   old.category,
            'content',    substr(old.content, 1, 500),
            'confidence', old.confidence,
            'status',     old.status,
            'weight',     old.weight
        ),
        json_object(
            'category',   new.category,
            'content',    substr(new.content, 1, 500),
            'confidence', new.confidence,
            'status',     new.status,
            'weight',     new.weight
        ),
        strftime('%Y-%m-%d %H:%M:%S', 'now', 'utc')
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_log_delete
AFTER DELETE ON memory_entries
BEGIN
    INSERT INTO memory_logs (entry_id, action, old_value, timestamp)
    VALUES (
        old.entry_id, 'SOFT_DELETE',
        json_object(
            'category',   old.category,
            'content',    substr(old.content, 1, 500),
            'status',     old.status,
            'weight',     old.weight
        ),
        strftime('%Y-%m-%d %H:%M:%S', 'now', 'utc')
    );
END;

-- ================================================================
-- 7. 性能索引
-- ================================================================
CREATE INDEX IF NOT EXISTS idx_user_status
    ON memory_entries(user_id, status);
CREATE INDEX IF NOT EXISTS idx_category_user
    ON memory_entries(category, user_id);
CREATE INDEX IF NOT EXISTS idx_logs_timestamp
    ON memory_logs(timestamp);
CREATE INDEX IF NOT EXISTS idx_archive_user
    ON memory_entries_archive(user_id);

-- ================================================================
-- 8. 功能开关配置表
-- ================================================================
CREATE TABLE IF NOT EXISTS config (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL DEFAULT 'true',
    description TEXT,
    updated_at  TEXT DEFAULT (
        strftime('%Y-%m-%d %H:%M:%S', 'now', 'utc'))
);
"""


def init_db(db_path: str = DB_PATH) -> sqlite3.Connection:
    """初始化数据库：建表、触发器、索引一键执行"""
    conn = get_db_connection(db_path)
    try:
        conn.executescript(SQL_INIT)
        # 老库补列：CREATE TABLE IF NOT EXISTS 不会给已存在的表加字段
        for table, col in [("memory_entries", "meta TEXT"),
                           ("memory_entries_archive", "meta TEXT"),
                           # 计划的完成度 0-100。二元的"完成/未完成"表达不了
                           # "舰船UI重构做了一半"这种状态，用进度条代替开关
                           ("memory_entries", "progress INTEGER DEFAULT 0"),
                           ("memory_entries_archive", "progress INTEGER DEFAULT 0")]:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass  # 列已存在
        # 注册功能开关默认值（INSERT OR IGNORE：已存在的不覆盖，用户改过的保留）
        for key, (val, desc) in FEATURE_FLAGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO config (key, value, description) VALUES (?, ?, ?)",
                (key, val, desc)
            )
        conn.commit()
        logger.info("数据库初始化完成: %s", db_path)
    except Exception as e:
        logger.error("数据库初始化失败: %s", e)
        raise
    return conn


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    conn = init_db()
    print("Tables:", [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")])
    print("Triggers:", [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' ORDER BY name")])
    conn.close()
