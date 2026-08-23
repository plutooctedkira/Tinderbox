"""卷宗自动聚合模块。

对 plan（category='task' + type='plan-*'）和 fiction_inspiration 的新记忆，
自动匹配已有主题卷宗（topic）并追加；不匹配则新建卷宗。

流程：
1. 关键词初筛：新记忆关键词与已有卷宗关键词求交集，快速筛出候选（本地、快）
2. LLM 确认：把新记忆 + 候选卷宗交给 DeepSeek 判断归属（精确，失败自动降级）
3. 匹配 → 追加（更新 topic_id + entry_count）；不匹配 → 新建卷宗

核心路径不依赖 LLM：即使 DeepSeek 不可用，也用关键词交集完成聚合。
"""

import json
import logging
import uuid
from datetime import datetime, timezone

logger = logging.getLogger("memory.aggregator")

# 聚合支持的分类 key（与 memory_entries 的映射在 storage.py 里做）
AGG_CATEGORIES = ("plan", "fiction_inspiration")

# 关键词初筛用的常见停用词（单字 + 高频虚词）
_STOPWORDS = set(
    "的了是在我有和就不人都一一个上也很到说要去你会着没有看好自己这"
    "那什么他们我们你们可以因为所以但是然后已经还有这样怎么这个那个"
)


def extract_keywords(text, top_k=5):
    """从文本提取关键词（jieba 分词 + 词频，过滤单字和常见停用词）。"""
    try:
        import jieba
    except ImportError:
        return []
    from collections import Counter
    words = [w.strip() for w in jieba.cut(text) if len(w.strip()) >= 2]
    freq = Counter(w for w in words if w not in _STOPWORDS)
    return [w for w, _ in freq.most_common(top_k)]


def keyword_prescreen(conn, user_id, category, keywords):
    """关键词初筛：返回与新记忆关键词有交集的候选卷宗（按交集大小降序）。"""
    if not keywords:
        return []
    rows = conn.execute(
        "SELECT * FROM topics WHERE user_id = ? AND category = ?",
        (user_id, category)
    ).fetchall()
    kw_set = set(keywords)
    candidates = []
    for r in rows:
        try:
            tk = json.loads(r["keywords"] or "[]")
        except (json.JSONDecodeError, TypeError):
            tk = []
        overlap = kw_set & set(tk)
        if overlap:
            d = dict(r)
            d["_overlap"] = len(overlap)
            candidates.append(d)
    candidates.sort(key=lambda x: -x["_overlap"])
    return candidates


def match_topic_with_llm(content, keywords, candidates):
    """LLM 确认：判断新记忆属于哪个候选卷宗。返回 topic_id 或 None。

    失败（LLM 不可用 / 解析失败）时返回 None，由调用方降级为关键词匹配。
    """
    if not candidates:
        return None
    from .ingest import match_topic_via_llm
    try:
        return match_topic_via_llm(content, keywords, candidates)
    except Exception as e:
        logger.warning("LLM 卷宗确认失败，降级为关键词匹配: %s", e)
        return None


def attach_to_topic(conn, entry_id, topic_id):
    """把记忆追加到卷宗。"""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "UPDATE memory_entries SET topic_id = ? WHERE entry_id = ?",
        (topic_id, entry_id)
    )
    conn.execute(
        "UPDATE topics SET entry_count = entry_count + 1, updated_at = ? "
        "WHERE topic_id = ?",
        (now, topic_id)
    )
    conn.commit()
    logger.info("记忆 %s 追加到卷宗 %s", entry_id, topic_id)


def _extract_title(conn, entry_id, content):
    """提取卷宗标题：优先 meta 里的原始标题（ob_name / title），否则用 content 首行。

    OB 迁来的记忆 meta 里有 ob_name（原始标题），content 首行往往是被提炼过的正文，
    用首行当卷宗标题会把标题"改掉"。这里优先取原始标题。
    """
    row = conn.execute(
        "SELECT meta FROM memory_entries WHERE entry_id = ?", (entry_id,)
    ).fetchone()
    if row and row["meta"]:
        try:
            meta = json.loads(row["meta"])
            for key in ("ob_name", "title"):
                val = meta.get(key)
                if val and str(val).strip():
                    return str(val).strip()[:50]
        except (json.JSONDecodeError, TypeError):
            pass
    first_line = content.strip().split("\n")[0].strip()
    return first_line[:50] or "未命名卷"


def create_topic(conn, user_id, category, content, keywords, entry_id):
    """新建卷宗，并把记忆关联进去。返回新卷宗 topic_id。"""
    topic_id = f"topic-{uuid.uuid4().hex[:12]}"
    # 标题优先用 meta 里的原始标题（ob_name / title），否则用 content 首行
    title = _extract_title(conn, entry_id, content)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO topics (topic_id, user_id, category, title, keywords, "
        "summary, entry_count, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
        (topic_id, user_id, category, title,
         json.dumps(keywords, ensure_ascii=False), content[:200], now, now)
    )
    conn.execute(
        "UPDATE memory_entries SET topic_id = ? WHERE entry_id = ?",
        (topic_id, entry_id)
    )
    conn.commit()
    logger.info("新建卷宗 %s（%s），关联记忆 %s", topic_id, title, entry_id)
    return topic_id


def aggregate_memory(conn, entry_id, user_id, category, content, keywords=None):
    """对新记忆做卷宗聚合。category 为 'plan' 或 'fiction_inspiration'。

    返回 topic_id（新增或匹配到的卷宗）；聚合失败返回 None 且不抛异常，
    由调用方当作非关键路径处理。
    """
    if category not in AGG_CATEGORIES:
        return None
    try:
        kw = list(keywords or []) or extract_keywords(content)
        candidates = keyword_prescreen(conn, user_id, category, kw)

        matched = None
        if candidates:
            # 1) LLM 确认；2) 失败/返回 None 则降级为强关键词匹配
            matched = match_topic_with_llm(content, kw, candidates)
            if matched is None:
                best = candidates[0]
                # 降级阈值：至少 2 个共同关键词才算同主题，避免"世界观"这类
                # 单个通用词的误匹配（LLM 不可用时的保守策略）
                if best.get("_overlap", 0) >= 2:
                    matched = best["topic_id"]

        if matched:
            attach_to_topic(conn, entry_id, matched)
            return matched
        return create_topic(conn, user_id, category, content, kw, entry_id)
    except Exception as e:
        logger.error("卷宗聚合失败（%s）: %s", entry_id, e)
        return None
