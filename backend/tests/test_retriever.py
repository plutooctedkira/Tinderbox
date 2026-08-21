"""测试: FTS5 检索与主动上下文浮现"""
import sys, os, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.db import init_db, get_db_connection
from src.storage import insert_memory
from src.retriever import retrieve_memories, get_active_surfaced_memories

def test_retrieve():
    db_path = os.path.join(tempfile.gettempdir(), "test_retrieve.db")
    conn = init_db(db_path)
    insert_memory(conn, "user-r", "s1", "knowledge", "Python是一门强大的编程语言", "high")
    insert_memory(conn, "user-r", "s1", "task", "学习SQLite的FTS5全文检索", "medium")
    insert_memory(conn, "user-r", "s1", "fiction_inspiration", "一个关于AI觉醒的故事", "high")
    results = retrieve_memories(conn, "user-r", "Python", top_k=5)
    assert len(results) > 0
    assert any("Python" in r["content"] for r in results)
    print(f"[OK] 检索 'Python': {len(results)} 条结果")
    results2 = retrieve_memories(conn, "user-r", "FTS5")
    assert len(results2) > 0
    print(f"[OK] 检索 'FTS5': {len(results2)} 条结果")
    conn.close(); os.remove(db_path)

def test_active_surface():
    db_path = os.path.join(tempfile.gettempdir(), "test_surface.db")
    conn = init_db(db_path)
    insert_memory(conn, "user-s", "s1", "decision", "决定使用SQLite作为存储", "high", anchor=1)
    insert_memory(conn, "user-s", "s1", "preference", "喜欢深色主题", "high")
    surfaced = get_active_surfaced_memories(conn, "user-s", limit=5)
    assert len(surfaced) >= 2
    assert surfaced[0]["category"] == "decision"
    print(f"[OK] 主动浮现: {len(surfaced)} 条, 首条=[{surfaced[0]['category']}]")
    conn.close(); os.remove(db_path)

if __name__ == "__main__":
    test_retrieve()
    test_active_surface()
    print("\n*** 检索测试全部通过！")
