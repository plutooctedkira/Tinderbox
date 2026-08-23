"""
Memory Vault - MCP 协议服务入口

提供:
- 10 个 Tool: store_memory / extract_and_store_from_text / query_memories /
  write_diary / read_diary / create_plan / list_plans / read_plans /
  set_plan_progress / supersede_memory
- 1 个 Resource: 主动上下文浮现
"""

import json
import logging
import os
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO")),
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s"
)

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from src.db import get_db_connection, init_db, get_feature_flag
from src.storage import insert_memory, supersede_memory
from src.ingest import extract_memories_with_retry
from src.retriever import retrieve_memories, get_active_surfaced_memories
from src.hooks import load_plugins

# 初始化数据库
init_db()
load_plugins()

# 传输方式由环境变量决定：
#   不设 = stdio（本地 Cline/Claude Desktop 拉起进程用）
#   MCP_TRANSPORT=streamable-http = 起 HTTP 服务（服务器上给远程客户端连）
MCP_TRANSPORT = os.getenv("MCP_TRANSPORT", "stdio")

# 反代进来的请求 Host 头是外部域名，SDK 的 DNS rebinding 保护默认只认 localhost，
# 会回 421。MCP_ALLOWED_HOSTS 用逗号分隔配上外部域名即可。
_allowed = [h.strip() for h in os.getenv("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]

mcp = FastMCP(
    "Memory-Vault-Engine",
    host=os.getenv("MCP_HOST", "127.0.0.1"),
    port=int(os.getenv("MCP_PORT", "3456")),
    transport_security=TransportSecuritySettings(
        allowed_hosts=_allowed + ["127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*"],
        allowed_origins=["*"],
    ) if _allowed else None,
)


@mcp.tool()
def store_memory(
    category: str,
    content: str,
    user_id: str = "default",
    session_id: str = "",
    confidence: str = "high",
    anchor: int = 0,
) -> str:
    """保存单条记忆或灵感。自动同步 FTS5 索引并导出 Obsidian 文件。

    Args:
        user_id: 用户标识
        session_id: 会话标识
        category: 分类 (preference|task|decision|knowledge|fiction_inspiration)
        content: 记忆内容
        confidence: 置信度 (high|medium|low)
        anchor: 是否锚定（锚定记忆不受衰减影响）
    """
    conn = get_db_connection()
    try:
        valid_categories = [
            "preference", "task", "decision",
            "knowledge", "fiction_inspiration"
        ]
        if category not in valid_categories:
            return f"错误: category 必须是 {valid_categories} 之一"
        if confidence not in ("high", "medium", "low"):
            return "错误: confidence 必须是 high/medium/low"

        entry_id = insert_memory(
            conn, user_id, session_id, category, content, confidence, anchor
        )
        return f"记忆已存储 [{category}]。ID: {entry_id}"
    finally:
        conn.close()


@mcp.tool()
def extract_and_store_from_text(
    raw_text: str,
    user_id: str = "default",
    session_id: str = "",
) -> str:
    """使用 DeepSeek 自动清洗文本/对话，提炼分类并批量落盘。

    支持长文本自动分块处理。
    """
    try:
        items = extract_memories_with_retry(raw_text)
    except Exception as e:
        return f"DeepSeek 提取失败: {e}"

    if not items:
        return "未从文本中提取到有效记忆或灵感。"

    conn = get_db_connection()
    saved_count = 0
    try:
        for item in items:
            insert_memory(
                conn, user_id, session_id,
                category=item.get("category", "knowledge"),
                content=item.get("content"),
                confidence=item.get("confidence", "high"),
                anchor=item.get("anchor", 0),
                keywords=item.get("keywords", []),
            )
            saved_count += 1
        return f"DeepSeek 成功处理文本，保存 {saved_count} 条记忆。"
    finally:
        conn.close()


@mcp.tool()
def query_memories(
    query: str,
    user_id: str = "default",
    top_k: int = 10,
    include_archive: bool = False,
) -> list[dict]:
    """检索相匹配的记忆与灵感卡片。

    Args:
        user_id: 用户标识
        query: 搜索关键词
        top_k: 返回条数
        include_archive: 是否包含归档数据
    """
    conn = get_db_connection()
    try:
        return retrieve_memories(conn, user_id, query, top_k, include_archive)
    finally:
        conn.close()


@mcp.tool()
def write_diary(content: str, title: str = "", user_id: str = "default",
                session_id: str = "mcp") -> str:
    """写一篇日记。日记存成 category='diary' 的记忆，会出现在 Dashboard 的「日记」页。

    用于记录当天的经历、心情、想法这类连续性的自述内容；
    零散的事实/偏好/待办请用 store_memory，别混进日记。

    Args:
        content: 日记正文
        title: 标题（可选，留空则列表里只显示日期）
        user_id: 用户标识
        session_id: 会话标识
    """
    conn = get_db_connection()
    try:
        # 标题存 meta，不进正文——正文保持干净，检索命中的才是真内容
        meta = json.dumps({"title": title.strip()}, ensure_ascii=False) if title.strip() else None
        entry_id = insert_memory(conn, user_id, session_id, "diary", content,
                                 "high", meta=meta)
        return f"日记已保存：{entry_id}"
    finally:
        conn.close()


@mcp.tool()
def read_diary(user_id: str = "default", limit: int = 10, query: str = "") -> list[dict]:
    """读日记。不给 query 就按时间倒序返回最近的几篇；给了就在日记里检索。

    Args:
        user_id: 用户标识
        limit: 返回篇数
        query: 可选，检索关键词（留空=最近几篇）
    """
    conn = get_db_connection()
    try:
        if query.strip():
            rows = conn.execute(
                """SELECT entry_id, content, created_at, weight
                   FROM memory_entries
                   WHERE user_id = ? AND category = 'diary' AND status = 'active'
                     AND content LIKE ?
                   ORDER BY created_at DESC LIMIT ?""",
                (user_id, f"%{query}%", limit)
            ).fetchall()
            return [dict(r) for r in rows]
        rows = conn.execute(
            """SELECT entry_id, content, created_at, weight
               FROM memory_entries
               WHERE user_id = ? AND category = 'diary' AND status = 'active'
               ORDER BY created_at DESC LIMIT ?""",
            (user_id, limit)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


PLAN_TYPES = ("plan-life", "plan-work", "plan-dev")


@mcp.tool()
def create_plan(content: str, plan_type: str = "plan-life",
                user_id: str = "default", session_id: str = "mcp") -> str:
    """新建一条计划/待办。会出现在 Dashboard 的「计划」页，按类型分组。

    Args:
        content: 计划内容
        plan_type: plan-life（生活）/ plan-work（工作）/ plan-dev（开发）
        user_id: 用户标识
        session_id: 会话标识
    """
    if plan_type not in PLAN_TYPES:
        return f"plan_type 只能是 {', '.join(PLAN_TYPES)}"
    conn = get_db_connection()
    try:
        if not get_feature_flag(conn, "feature.plan"):
            return "计划功能已关闭（feature.plan=false）"
        entry_id = insert_memory(conn, user_id, session_id, "task", content,
                                 "high", mtype=plan_type)
        return f"计划已创建：{entry_id}"
    finally:
        conn.close()


@mcp.tool()
def list_plans(user_id: str = "default", plan_type: str = "",
               include_completed: bool = False) -> list[dict]:
    """列出计划。默认只看进行中的。

    Args:
        user_id: 用户标识
        plan_type: 可选，只看某一类（plan-life / plan-work / plan-dev）
        include_completed: 是否连已完成的一起返回
    """
    conn = get_db_connection()
    try:
        if not get_feature_flag(conn, "feature.plan"):
            return []
        sql = ["SELECT entry_id, content, type AS plan_type, status, created_at,",
               "COALESCE(progress,0) AS progress",
               "FROM memory_entries",
               "WHERE user_id = ? AND category = 'task' AND type LIKE 'plan-%'"]
        args = [user_id]
        sql.append("AND status IN ('active','completed')" if include_completed
                   else "AND status = 'active'")
        if plan_type:
            sql.append("AND type = ?")
            args.append(plan_type)
        sql.append("ORDER BY status, created_at DESC LIMIT 200")
        rows = conn.execute(" ".join(sql), args).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@mcp.tool()
def read_plans(user_id: str = "default", plan_type: str = "",
               include_completed: bool = False, query: str = "",
               limit: int = 50) -> list[dict]:
    """读计划（直接按 type 过滤查询，不走通用检索）。

    与 list_plans 的区别：多了 query 关键词过滤，可以直接搜计划内容。

    Args:
        user_id: 用户标识
        plan_type: 可选，只看某一类（plan-life / plan-work / plan-dev）
        include_completed: 是否连已完成的一起返回
        query: 可选，关键词过滤（在计划内容里模糊匹配）
        limit: 返回条数
    """
    conn = get_db_connection()
    try:
        if not get_feature_flag(conn, "feature.plan"):
            return []
        sql = ["SELECT entry_id, content, type AS plan_type, status, created_at,",
               "COALESCE(progress,0) AS progress",
               "FROM memory_entries",
               "WHERE user_id = ? AND category = 'task' AND type LIKE 'plan-%'"]
        args = [user_id]
        sql.append("AND status IN ('active','completed')" if include_completed
                   else "AND status = 'active'")
        if plan_type:
            sql.append("AND type = ?")
            args.append(plan_type)
        if query.strip():
            sql.append("AND content LIKE ?")
            args.append(f"%{query}%")
        sql.append("ORDER BY status, created_at DESC LIMIT ?")
        args.append(limit)
        rows = conn.execute(" ".join(sql), args).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@mcp.tool()
def set_plan_progress(entry_id: str, progress: int) -> str:
    """调节一条计划的完成度（0-100）。

    比"完成/未完成"更细：做了一半就写 50，卡住了往回调也行。
    100 会自动把状态置为 completed，其余为 active。

    Args:
        entry_id: 计划的 entry_id
        progress: 0-100 的整数
    """
    pg = max(0, min(100, int(progress)))
    conn = get_db_connection()
    try:
        if not get_feature_flag(conn, "feature.plan"):
            return "计划功能已关闭（feature.plan=false）"
        row = conn.execute(
            "SELECT COALESCE(progress,0) FROM memory_entries WHERE entry_id = ? AND category = 'task'",
            (entry_id,)
        ).fetchone()
        if row is None:
            return f"找不到这条计划：{entry_id}"
        old = row[0]
        conn.execute("UPDATE memory_entries SET progress = ?, status = ? WHERE entry_id = ?",
                     (pg, "completed" if pg >= 100 else "active", entry_id))
        conn.commit()
        return f"{entry_id}：{old}% → {pg}%" + ("（已完成）" if pg >= 100 else "")
    finally:
        conn.close()


@mcp.tool()
def supersede_memory(
    old_entry_id: str,
    new_content: str,
    new_category: str = None,
    new_confidence: str = None,
) -> str:
    """版本更迭：用新内容取代一条旧记忆。
    旧记忆保留但标记为 superseded，不再出现在常规检索结果中。
    """
    conn = get_db_connection()
    try:
        old_id, new_id = supersede_memory(
            conn, old_entry_id, new_content, new_category, new_confidence
        )
        return f"版本更迭成功。旧: {old_id} → 新: {new_id}"
    except ValueError as e:
        return f"操作失败: {e}"
    finally:
        conn.close()


@mcp.resource("memo://vault/active_context/{user_id}")
def get_active_context(user_id: str) -> str:
    """主动上下文浮现：按权重与时效性自动推送高价值记忆。"""
    conn = get_db_connection()
    try:
        items = get_active_surfaced_memories(conn, user_id, limit=10)
        if not items:
            return "暂无活跃上下文记忆。"

        lines = ["# 主动上下文浮现\n"]
        for i, item in enumerate(items, 1):
            pin_mark = "📌" if item.get("pin") else ""
            anchor_mark = "⚓" if item.get("anchor") else ""
            lines.append(
                f"## {i}. [{item['category']}] {pin_mark}{anchor_mark}\n"
                f"置信度: {item['confidence']} | 权重: {item['weight']:.2f}\n\n"
                f"{item['content']}\n"
            )
        return "\n".join(lines)
    finally:
        conn.close()


if __name__ == "__main__":
    if MCP_TRANSPORT == "streamable-http":
        logging.getLogger(__name__).info(
            "Streamable HTTP 模式: http://%s:%s%s",
            mcp.settings.host, mcp.settings.port, mcp.settings.streamable_http_path
        )
    mcp.run(transport=MCP_TRANSPORT)
