"""数据修正：把误放在小说灵感里的计划放回计划，并重新聚合卷宗。

1. 修正分类：误放的计划从 fiction_inspiration 改回 task + plan-dev
2. 重置聚合：清空所有 topic_id 和 topics 表
3. 重新回填：用新的标题逻辑（meta.ob_name）重新聚合

用法：
    python scripts/fix_and_backfill.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from src.db import init_db
from src.aggregator import aggregate_memory

# 误放在小说灵感里的计划（entry_id -> (category, type)）
MISPLACED_PLANS = [
    ("ob-2f42d2531127", "task", "plan-dev"),  # 文字游戏开发（已完成开发并部署）
]


def fix_and_backfill(db_path):
    conn = init_db(db_path)

    # 1. 修正分类
    for entry_id, category, mtype in MISPLACED_PLANS:
        conn.execute(
            "UPDATE memory_entries SET category = ?, type = ? WHERE entry_id = ?",
            (category, mtype, entry_id)
        )
        print(f"修正分类: {entry_id} -> {category}/{mtype}")

    # 2. 重置聚合
    conn.execute("UPDATE memory_entries SET topic_id = NULL")
    conn.execute("DELETE FROM topics")
    conn.commit()
    print("已重置所有卷宗聚合")

    # 3. 重新回填
    rows = conn.execute(
        """SELECT entry_id, user_id, category, type, content
           FROM memory_entries
           WHERE topic_id IS NULL
             AND (category = 'fiction_inspiration'
                  OR (category = 'task' AND type LIKE 'plan-%'))
           ORDER BY created_at ASC, entry_id ASC
        """
    ).fetchall()
    print(f"重新回填 {len(rows)} 条记忆")
    for r in rows:
        agg_category = ("fiction_inspiration" if r["category"] == "fiction_inspiration"
                        else "plan")
        aggregate_memory(conn, r["entry_id"], r["user_id"], agg_category, r["content"])

    stats = conn.execute(
        "SELECT category, COUNT(*) FROM topics GROUP BY category ORDER BY category"
    ).fetchall()
    print("回填后卷宗统计:")
    for s in stats:
        print(f"  {s[0]}: {s[1]} 个卷宗")

    conn.close()


if __name__ == "__main__":
    fix_and_backfill(os.getenv("MEMORY_DB_PATH", "memory.db"))
