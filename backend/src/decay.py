"""
Memory Vault - 每日衰减与日志垃圾回收模块

包含:
- 权重自然衰减（任务5%/天，知识1%/天）
- 软归档（weight <= 0.1）
- 硬归档迁移（weight <= 0.05 + 180天未访问 → archive 表）
- 90天日志 GC
- 过期日志月份表清理
"""

import sqlite3
import logging
from datetime import datetime, timedelta, timezone

from .db import get_db_connection, get_feature_flag
from .hooks import trigger

logger = logging.getLogger("memory.decay")


def run_daily_decay(db_path: str):
    """
    每日后台 GC 任务：
    1. 权重自然衰减
    2. 软归档
    3. 硬归档迁移
    4. 日志 GC
    5. 过期月份表清理
    """
    # 必须走工厂函数：它注册了 FTS 触发器要用的 seg()，也带上了 30s 忙等待。
    # 这里原先直接 sqlite3.connect()，衰减改 status 触发重建索引时会报 no such function
    conn = get_db_connection(db_path)

    stats = {"decay_tasks": 0, "decay_knowledge": 0,
             "soft_archive": 0, "hard_migrate": 0,
             "logs_cleaned": 0, "tables_dropped": 0}

    try:
        if not get_feature_flag(conn, "feature.decay"):
            logger.info("衰减功能已关闭（feature.decay=false），跳过")
            return stats
        with conn:
            # 1. 任务衰减 5%
            cur = conn.execute("""
                UPDATE memory_entries
                SET weight = MAX(weight * 0.95, 0.1)
                WHERE category = 'task'
                  AND anchor = 0 AND pin = 0
                  AND status = 'active'
            """)
            stats["decay_tasks"] = cur.rowcount

            # 2. 知识/偏好/灵感衰减 1%
            cur = conn.execute("""
                UPDATE memory_entries
                SET weight = MAX(weight * 0.99, 0.1)
                WHERE category IN (
                    'preference','knowledge','fiction_inspiration')
                  AND anchor = 0 AND pin = 0
                  AND status = 'active'
            """)
            stats["decay_knowledge"] = cur.rowcount

            # 3. 软归档：weight <= 0.1 → status='archived'
            cur = conn.execute("""
                UPDATE memory_entries
                SET status = 'archived'
                WHERE weight <= 0.1
                  AND anchor = 0 AND pin = 0
                  AND status = 'active'
            """)
            stats["soft_archive"] = cur.rowcount

            # 4. 硬归档迁移：weight <= 0.05 + 180天未访问
            cutoff = (datetime.now(timezone.utc) - timedelta(days=180)).strftime(
                "%Y-%m-%d %H:%M:%S")
            rows = conn.execute("""
                SELECT entry_id, user_id, session_id, category, type,
                       content, confidence, status, pin, anchor, weight,
                       superseded_by, created_at, last_accessed_at
                FROM memory_entries
                WHERE weight <= 0.05
                  AND (last_accessed_at IS NULL
                       OR last_accessed_at < ?)
                  AND status IN ('archived', 'superseded')
            """, (cutoff,)).fetchall()

            for row in rows:
                now_utc = datetime.now(timezone.utc).strftime(
                    "%Y-%m-%d %H:%M:%S")
                conn.execute("""
                    INSERT OR IGNORE INTO memory_entries_archive
                    (entry_id, user_id, session_id, category, type,
                     content, confidence, status, pin, anchor, weight,
                     superseded_by, created_at, last_accessed_at, archived_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (*row, now_utc))
                # 从热表删除（DELETE 触发器自动清理 FTS5 索引）
                conn.execute(
                    "DELETE FROM memory_entries WHERE entry_id = ?",
                    (row["entry_id"],)
                )
                stats["hard_migrate"] += 1

            # 5. 日志 GC：删除 90 天前的日志
            cur = conn.execute("""
                DELETE FROM memory_logs
                WHERE timestamp < datetime('now', '-90 days')
            """)
            stats["logs_cleaned"] = cur.rowcount

            # 6. 过期日志月份表清理
            stats["tables_dropped"] = _drop_expired_log_tables(conn)

        logger.info(
            "Daily Decay 完成: 任务衰减=%d, 知识衰减=%d, "
            "软归档=%d, 硬迁移=%d, 日志清理=%d, 表清理=%d",
            stats["decay_tasks"], stats["decay_knowledge"],
            stats["soft_archive"], stats["hard_migrate"],
            stats["logs_cleaned"], stats["tables_dropped"]
        )

        # 写入汇总审计日志
        conn.execute("""
            INSERT INTO memory_logs (entry_id, action, new_value, timestamp)
            VALUES ('system', 'DECAY', ?, ?)
        """, (str(stats),
              datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()

    except Exception as e:
        logger.error("Decay job 失败: %s", e, exc_info=True)
        raise
    finally:
        conn.close()

    trigger("memory_decayed", stats=stats)
    return stats


def _drop_expired_log_tables(conn: sqlite3.Connection,
                             retention_days: int = 90) -> int:
    """删除超过保留期限的日志月份表"""
    cutoff = (datetime.now(timezone.utc) - timedelta(
        days=retention_days)).strftime("%Y%m")

    tables = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name LIKE 'memory_logs_%'"
    ).fetchall()

    dropped = 0
    for (name,) in tables:
        month_str = name.replace("memory_logs_", "")
        if month_str.isdigit() and len(month_str) == 6 and month_str < cutoff:
            conn.execute(f"DROP TABLE IF EXISTS {name}")
            dropped += 1
            logger.info("删除过期日志表: %s", name)

    return dropped


if __name__ == "__main__":
    import os
    logging.basicConfig(level=logging.INFO)
    from dotenv import load_dotenv
    load_dotenv()
    db_path = os.getenv("MEMORY_DB_PATH", "memory.db")
    stats = run_daily_decay(db_path)
    print("Decay stats:", stats)
