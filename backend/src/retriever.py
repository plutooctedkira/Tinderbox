"""
Memory Vault - FTS5 检索与时间混合衰减模块

包含:
- FTS5 BM25 全文检索
- 时间加权混合评分: score = bm25_score * weight
- last_accessed_at 自动更新
- 主动上下文浮现（get_active_surfaced_memories）
- 冷归档深度回溯
"""

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from .db import segment_cjk

logger = logging.getLogger("memory.retriever")


def _search_fts5(
    conn: sqlite3.Connection,
    fts_table: str,
    data_table: str,
    user_id: str,
    query: str,
    top_k: int = 10,
) -> list[dict]:
    """
    内部 FTS5 检索方法。
    使用 bm25() 排序，结合 weight 时间衰减。
    """
    # 查询要和入索引时用同一套分词（见 db.segment_cjk），否则中文永远搜不到。
    # 整体加引号当短语搜：保证被拆开的字必须相邻，"喝咖啡"不会命中"喝一杯冰美式咖啡"
    safe_query = segment_cjk(query).strip().replace('"', '""')
    if not safe_query:
        return []
    fts_query = f'"{safe_query}"'

    try:
        # MATCH 左边必须写真实表名，写别名会报 no such column（原来就栽在这，
        # 异常被下面的 except 吞掉，导致 FTS5 从未真正生效、每次都在全表 LIKE）
        rows = conn.execute(f"""
            SELECT m.entry_id, m.content, m.category, m.confidence,
                   m.status, m.weight,
                   bm25({fts_table}) AS bm25_score
            FROM {fts_table}
            JOIN {data_table} m ON {fts_table}.entry_id = m.entry_id
            WHERE {fts_table} MATCH ?
              AND m.user_id = ?
              AND m.status = 'active'
            ORDER BY bm25_score
            LIMIT ?
        """, (fts_query, user_id, top_k)).fetchall()
    except sqlite3.OperationalError as e:
        # 真出了 FTS 语法问题才回退。LIKE 比的是未分词的原文，所以用原始 query
        logger.warning("FTS5 检索失败，回退 LIKE 全表扫描: %s", e)
        rows = conn.execute(f"""
            SELECT m.entry_id, m.content, m.category, m.confidence,
                   m.status, m.weight, -1.0 AS bm25_score
            FROM {data_table} m
            WHERE m.content LIKE ?
              AND m.user_id = ?
              AND m.status = 'active'
            ORDER BY m.weight DESC, m.last_accessed_at DESC
            LIMIT ?
        """, (f"%{query}%", user_id, top_k)).fetchall()

    results = []
    for r in rows:
        # 综合评分 = -bm25_score * weight
        # bm25() 返回负值（越小越相关），取反后乘权重
        bm25 = r["bm25_score"] or 0
        weight = r["weight"] or 1.0
        combined_score = -bm25 * weight

        results.append({
            "id": r["entry_id"],
            "content": r["content"],
            "category": r["category"],
            "confidence": r["confidence"],
            "status": r["status"],
            "weight": weight,
            "score": round(combined_score, 4),
        })

    # SQL 只按 bm25 排了序，weight 没参与；这里按 combined_score 重排，
    # 衰减权重才真正影响结果顺序（原来算了但没用上）
    results.sort(key=lambda r: r["score"], reverse=True)
    return results


def retrieve_memories(
    conn: sqlite3.Connection,
    user_id: str,
    query: str,
    top_k: int = 10,
    include_archive: bool = False,
) -> list[dict]:
    """
    检索记忆。

    Args:
        conn: 数据库连接
        user_id: 用户标识
        query: 搜索查询
        top_k: 返回条数
        include_archive: 是否包含冷归档数据（深度回溯）

    Returns:
        记忆列表，按 score 降序
    """
    results = _search_fts5(conn, "memory_fts", "memory_entries",
                           user_id, query, top_k)

    if include_archive:
        archive_results = _search_fts5(conn, "memory_fts_archive",
                                       "memory_entries_archive",
                                       user_id, query, top_k)
        results = sorted(
            results + archive_results,
            key=lambda x: x.get("score", 0),
            reverse=True
        )[:top_k]

    if results:
        boost_hits(conn, [r["id"] for r in results])

    logger.info("检索 '%s' → %d 条结果", query, len(results))
    return results


def get_active_surfaced_memories(
    conn: sqlite3.Connection,
    user_id: str,
    limit: int = 10,
) -> list[dict]:
    """
    主动上下文浮现：按权重 × 时效性自动推送高价值记忆。
    优先: pin > anchor > 决策 > 任务 > 知识 > 偏好 > 灵感
    """
    rows = conn.execute("""
        SELECT entry_id, content, category, confidence, weight,
               pin, anchor, last_accessed_at
        FROM memory_entries
        WHERE user_id = ?
          AND status = 'active'
        ORDER BY
            pin DESC,
            anchor DESC,
            CASE category
                WHEN 'decision'  THEN 1
                WHEN 'task'      THEN 2
                WHEN 'knowledge' THEN 3
                WHEN 'fiction_inspiration' THEN 4
                WHEN 'preference' THEN 5
                ELSE 6
            END,
            weight DESC,
            last_accessed_at DESC NULLS LAST
        LIMIT ?
    """, (user_id, limit)).fetchall()

    results = []
    for r in rows:
        results.append({
            "id": r["entry_id"],
            "content": r["content"],
            "category": r["category"],
            "confidence": r["confidence"],
            "weight": r["weight"],
            "pin": bool(r["pin"]),
            "anchor": bool(r["anchor"]),
        })

    return results


HIT_BOOST = 0.1   # 每次被检索命中，权重加这么多
WEIGHT_CAP = 1.0  # 封顶，否则常被搜到的会无限涨


def boost_hits(conn: sqlite3.Connection, hit_ids: list[str]):
    """检索命中：权重 +0.1（封顶 1.0）、刷新访问时间、记一条 RECALL_HIT。

    衰减是每天 ×0.95 往下掉，命中往上顶——两股力量决定一条记忆能活多久。
    RECALL_HIT 同时是详情页那条衰减曲线上绿点的数据来源。
    """
    if not hit_ids:
        return
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    ph = ",".join("?" * len(hit_ids))
    conn.execute(
        f"""UPDATE memory_entries
            SET weight = MIN(COALESCE(weight, 1.0) + ?, ?),
                last_accessed_at = ?
            WHERE entry_id IN ({ph})""",
        (HIT_BOOST, WEIGHT_CAP, now, *hit_ids)
    )
    conn.executemany(
        "INSERT INTO memory_logs (entry_id, action, timestamp) VALUES (?, 'RECALL_HIT', ?)",
        [(eid, now) for eid in hit_ids]
    )
    conn.commit()  # 一次提交，原来是分两次
