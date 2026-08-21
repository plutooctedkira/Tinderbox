"""测试: 记忆存储与 supersede"""
import sys, os, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.db import init_db, get_db_connection
from src.storage import insert_memory, supersede_memory

def test_insert_memory():
    db_path = os.path.join(tempfile.gettempdir(), "test_storage_insert.db")
    conn = init_db(db_path)
    eid = insert_memory(conn, "user-1", "sess-1", "knowledge", "测试记忆内容", "high")
    assert eid.startswith("mem-")
    row = conn.execute("SELECT * FROM memory_entries WHERE entry_id=?", (eid,)).fetchone()
    assert row["content"] == "测试记忆内容"
    assert row["status"] == "active"
    fts = conn.execute("SELECT * FROM memory_fts WHERE entry_id=?", (eid,)).fetchone()
    assert fts is not None
    print(f"[OK] insert_memory: {eid} (FTS5 已同步)")
    conn.close(); os.remove(db_path)

def test_supersede_memory():
    db_path = os.path.join(tempfile.gettempdir(), "test_supersede.db")
    conn = init_db(db_path)
    old_id = insert_memory(conn, "user-1", "sess-1", "task", "旧版本任务", "medium")
    old, new = supersede_memory(conn, old_id, "新版本任务内容", new_confidence="high")
    assert old == old_id
    assert new.startswith("mem-")
    assert new != old
    old_row = conn.execute("SELECT * FROM memory_entries WHERE entry_id=?", (old,)).fetchone()
    assert old_row["status"] == "superseded"
    assert old_row["superseded_by"] == new
    new_row = conn.execute("SELECT * FROM memory_entries WHERE entry_id=?", (new,)).fetchone()
    assert new_row["status"] == "active"
    assert new_row["content"] == "新版本任务内容"
    fts_old = conn.execute("SELECT * FROM memory_fts WHERE entry_id=?", (old,)).fetchone()
    assert fts_old is None, "superseded 条目应从 FTS5 清除"
    fts_new = conn.execute("SELECT * FROM memory_fts WHERE entry_id=?", (new,)).fetchone()
    assert fts_new is not None, "新版本应在 FTS5 中"
    logs = conn.execute("SELECT * FROM memory_logs WHERE action='SUPERSEDE'").fetchall()
    assert len(logs) >= 1
    print(f"[OK] supersede_memory: {old} → {new}")
    conn.close(); os.remove(db_path)

if __name__ == "__main__":
    test_insert_memory()
    test_supersede_memory()
    print("\n*** 存储测试全部通过！")
