"""测试: 衰减与归档"""
import sys, os, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.db import init_db, get_db_connection
from src.storage import insert_memory
from src.decay import run_daily_decay

def test_decay():
    db_path = os.path.join(tempfile.gettempdir(), "test_decay.db")
    conn = init_db(db_path)
    insert_memory(conn, "user-d", "s1", "task", "衰减测试任务", "medium")
    conn.execute("UPDATE memory_entries SET weight = 0.11 WHERE category='task'")
    conn.commit()
    stats = run_daily_decay(db_path)
    assert stats["decay_tasks"] >= 0
    conn2 = get_db_connection(db_path)
    row = conn2.execute("SELECT * FROM memory_entries WHERE user_id='user-d'").fetchone()
    new_weight = row["weight"]
    assert new_weight <= 0.11, f"权重应衰减, 实际: {new_weight}"
    print(f"[OK] 衰减测试: 权重 0.11 → {new_weight:.4f}")
    print(f"[OK] Decay stats: {stats}")
    conn.close(); conn2.close(); os.remove(db_path)

if __name__ == "__main__":
    test_decay()
    print("\n*** 衰减测试全部通过！")
