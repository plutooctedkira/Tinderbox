"""
测试: 数据库初始化、建表、触发器
"""
import sys
import os
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.db import init_db, get_db_connection


def test_tables_exist():
    """验证所有表已创建"""
    db_path = os.path.join(tempfile.gettempdir(), "test_memory_tables.db")
    conn = init_db(db_path)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    expected = [
        "memory_entries",
        "memory_entries_archive",
        "memory_fts",
        "memory_fts_archive",
        "memory_logs",
    ]
    for t in expected:
        assert any(t in name for name in tables), f"表 {t} 未创建: {tables}"
    print("[OK] 所有表已创建:", tables)
    conn.close()
    os.remove(db_path)


def test_triggers_exist():
    """验证所有触发器已创建"""
    db_path = os.path.join(tempfile.gettempdir(), "test_memory_triggers.db")
    conn = init_db(db_path)
    triggers = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' ORDER BY name")]
    expected = [
        "trg_memory_fts_insert",
        "trg_memory_fts_update",
        "trg_memory_fts_delete",
        "trg_log_insert",
        "trg_log_update",
        "trg_log_delete",
    ]
    for t in expected:
        assert t in triggers, f"触发器 {t} 未创建: {triggers}"
    print("[OK] 所有触发器已创建:", triggers)
    conn.close()
    os.remove(db_path)


def test_fts5_insert_trigger():
    """验证 INSERT 触发器：写入 active 记忆后 FTS5 自动同步"""
    db_path = os.path.join(tempfile.gettempdir(), "test_fts5_insert.db")
    conn = init_db(db_path)

    conn.execute("""
        INSERT INTO memory_entries (entry_id, user_id, category, content, confidence, status)
        VALUES ('test-insert', 'user-a', 'knowledge', 'FTS5触发器测试', 'high', 'active')
    """)
    conn.commit()

    row = conn.execute(
        "SELECT * FROM memory_fts WHERE entry_id = 'test-insert'"
    ).fetchone()
    assert row is not None, "FTS5 未自动索引 active 记忆"
    assert "FTS5触发器测试" in row["tokenized_content"]
    print("[OK] INSERT 触发器: FTS5 自动同步 active 记忆")

    conn.close()
    os.remove(db_path)


def test_fts5_update_trigger_supersede():
    """验证 UPDATE 触发器：变更为 superseded 后 FTS5 自动移除"""
    db_path = os.path.join(tempfile.gettempdir(), "test_fts5_update.db")
    conn = init_db(db_path)

    conn.execute("""
        INSERT INTO memory_entries (entry_id, user_id, category, content, confidence, status)
        VALUES ('test-update', 'user-a', 'task', '需要更新的记忆', 'medium', 'active')
    """)
    conn.commit()

    # 确认初始在 FTS5 中
    before = conn.execute(
        "SELECT * FROM memory_fts WHERE entry_id = 'test-update'"
    ).fetchone()
    assert before is not None

    # 更新为 superseded
    conn.execute(
        "UPDATE memory_entries SET status = 'superseded' WHERE entry_id = 'test-update'"
    )
    conn.commit()

    # 确认从 FTS5 中移除
    after = conn.execute(
        "SELECT * FROM memory_fts WHERE entry_id = 'test-update'"
    ).fetchone()
    assert after is None, "superseded 后 FTS5 索引未清除"
    print("[OK] UPDATE 触发器: superseded 后 FTS5 自动移除")

    conn.close()
    os.remove(db_path)


def test_fts5_delete_trigger():
    """验证 DELETE 触发器：物理删除时 FTS5 自动清理"""
    db_path = os.path.join(tempfile.gettempdir(), "test_fts5_delete.db")
    conn = init_db(db_path)

    conn.execute("""
        INSERT INTO memory_entries (entry_id, user_id, category, content, confidence, status)
        VALUES ('test-delete', 'user-a', 'knowledge', '将被删除的记忆', 'low', 'active')
    """)
    conn.commit()

    conn.execute("DELETE FROM memory_entries WHERE entry_id = 'test-delete'")
    conn.commit()

    row = conn.execute(
        "SELECT * FROM memory_fts WHERE entry_id = 'test-delete'"
    ).fetchone()
    assert row is None, "DELETE 后 FTS5 索引未清理"
    print("[OK] DELETE 触发器: FTS5 索引自动清理")

    conn.close()
    os.remove(db_path)


def test_audit_log_triggers():
    """验证审计日志触发器"""
    db_path = os.path.join(tempfile.gettempdir(), "test_audit_logs.db")
    conn = init_db(db_path)

    # INSERT 日志
    conn.execute("""
        INSERT INTO memory_entries (entry_id, user_id, category, content, confidence, status)
        VALUES ('test-log', 'user-a', 'decision', '日志测试', 'high', 'active')
    """)
    conn.commit()

    logs = conn.execute(
        "SELECT * FROM memory_logs WHERE entry_id = 'test-log'"
    ).fetchall()
    assert len(logs) >= 1, "INSERT 日志未记录"
    assert logs[0]["action"] == "INSERT"

    # UPDATE 日志
    conn.execute(
        "UPDATE memory_entries SET status = 'completed' WHERE entry_id = 'test-log'"
    )
    conn.commit()

    logs = conn.execute(
        "SELECT * FROM memory_logs WHERE entry_id = 'test-log' AND action = 'UPDATE'"
    ).fetchall()
    assert len(logs) >= 1, "UPDATE 日志未记录"

    # DELETE 日志
    conn.execute("DELETE FROM memory_entries WHERE entry_id = 'test-log'")
    conn.commit()

    logs = conn.execute(
        "SELECT * FROM memory_logs WHERE entry_id = 'test-log'"
        " AND action = 'SOFT_DELETE'"
    ).fetchall()
    assert len(logs) >= 1, "DELETE 日志未记录"

    print("[OK] 审计日志触发器: INSERT/UPDATE/DELETE 全部自动记录")
    conn.close()
    os.remove(db_path)


if __name__ == "__main__":
    test_tables_exist()
    test_triggers_exist()
    test_fts5_insert_trigger()
    test_fts5_update_trigger_supersede()
    test_fts5_delete_trigger()
    test_audit_log_triggers()
    print("\n*** 所有数据库测试通过！")
