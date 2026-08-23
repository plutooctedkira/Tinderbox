"""回填卷宗：对没有 topic_id 的旧 plan / fiction_inspiration 记忆做聚合。

卷宗聚合功能上线前插入的旧记忆没有 topic_id，本脚本按时间顺序逐个补聚合。
幂等：只处理 topic_id 为空的记忆，可重复运行。

用法：
    python scripts/backfill_topics.py

环境变量：
    MEMORY_DB_PATH  数据库路径（默认 memory.db）
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from src.db import init_db
from src.aggregator import aggregate_memory


def backfill(db_path):
    conn = init_db(db_path)

    # 只回填没有 topic_id 的 plan（task + plan-*）和灵感（fiction_inspiration）
    # 按创建时间升序：最早的一条会新建卷，后续同主题的追加
    rows = conn.execute(
        """SELECT entry_id, user_id, category, type, content
           FROM memory_entries
           WHERE topic_id IS NULL
             AND (category = 'fiction_inspiration'
                  OR (category = 'task' AND type LIKE 'plan-%'))
           ORDER BY created_at ASC, entry_id ASC
        """
    ).fetchall()

    print(f"待回填记忆: {len(rows)} 条")
    for r in rows:
        agg_category = ("fiction_inspiration" if r["category"] == "fiction_inspiration"
                        else "plan")
        tid = aggregate_memory(conn, r["entry_id"], r["user_id"],
                               agg_category, r["content"])
        mark = "新建/追加" if tid else "失败"
        print(f"  {r['entry_id']} [{agg_category}] -> {tid} ({mark})")

    stats = conn.execute(
        "SELECT category, COUNT(*) FROM topics GROUP BY category ORDER BY category"
    ).fetchall()
    print("回填后卷宗统计:")
    for s in stats:
        print(f"  {s[0]}: {s[1]} 个卷宗")

    conn.close()


if __name__ == "__main__":
    backfill(os.getenv("MEMORY_DB_PATH", "memory.db"))
