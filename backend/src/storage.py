"""
Memory Vault - 记忆存储与 Obsidian 导出模块

包含:
- insert_memory: 插入单条记忆（自动触发 FTS5 索引和审计日志）
- supersede_memory: 版本更迭
- export_to_obsidian: 导出 .md 文件到 Obsidian Vault
"""

import uuid
import json
import os
import subprocess
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

from .db import OBSIDIAN_VAULT_PATH

logger = logging.getLogger("memory.storage")

GIT_AUTO_PUSH = os.getenv("GIT_AUTO_PUSH", "false").lower() == "true"


def insert_memory(
    conn,
    user_id: str,
    session_id: str,
    category: str,
    content: str,
    confidence: str = "high",
    anchor: int = 0,
    keywords: Optional[list] = None,
    mtype: Optional[str] = None,
    meta: Optional[str] = None,
) -> str:
    """
    插入单条记忆。
    FTS5 索引由 trg_memory_fts_insert 自动同步。
    审计日志由 trg_log_insert 自动记录。

    mtype 写进 type 列：计划用它区分 plan-life / plan-work / plan-dev。
    原来这个参数缺失，导致前端传的 type 一路被丢掉、计划无法分组。
    """
    entry_id = f"mem-{uuid.uuid4().hex[:12]}"
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    conn.execute(
        """INSERT INTO memory_entries
           (entry_id, user_id, session_id, category, type, content,
            confidence, status, anchor, created_at, last_accessed_at, meta)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)""",
        (entry_id, user_id, session_id, category, mtype, content,
         confidence, anchor, now_utc, now_utc, meta)
    )
    conn.commit()

    logger.info("记忆已存储: %s [%s]", entry_id, category)

    # Obsidian 导出（非关键路径）
    try:
        export_to_obsidian(entry_id, user_id, category, content,
                           keywords or [], "active")
    except Exception as e:
        logger.warning("Obsidian 导出失败: %s", e)

    return entry_id


def supersede_memory(
    conn,
    old_entry_id: str,
    new_content: str,
    new_category: Optional[str] = None,
    new_confidence: Optional[str] = None,
    new_keywords: Optional[list] = None,
):
    """
    版本更迭：将旧记忆标记为 superseded，创建新版本。
    返回 (old_entry_id, new_entry_id)。
    """
    old_entry = conn.execute(
        """SELECT user_id, session_id, category, confidence, content,
                  pin, anchor, type, status
           FROM memory_entries WHERE entry_id = ?""",
        (old_entry_id,)
    ).fetchone()

    if old_entry is None:
        raise ValueError(f"旧记忆不存在: {old_entry_id}")
    if old_entry["status"] == "superseded":
        raise ValueError(f"该记忆已被取代: {old_entry_id}")

    final_category = new_category or old_entry["category"]
    final_confidence = new_confidence or old_entry["confidence"]
    final_keywords = new_keywords or []
    new_entry_id = f"mem-{uuid.uuid4().hex[:12]}"
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    try:
        with conn:
            # 旧条目 → superseded（触发器自动清除 FTS5）
            conn.execute(
                """UPDATE memory_entries
                   SET status = 'superseded', superseded_by = ?,
                       last_accessed_at = ? WHERE entry_id = ?""",
                (new_entry_id, now_utc, old_entry_id)
            )
            # 新版本（触发器自动加入 FTS5）
            conn.execute(
                """INSERT INTO memory_entries
                   (entry_id, user_id, session_id, category, type,
                    content, confidence, status, pin, anchor,
                    weight, superseded_by, created_at, last_accessed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, 1.0, NULL, ?, ?)""",
                (new_entry_id, old_entry["user_id"], old_entry["session_id"],
                 final_category, old_entry["type"], new_content,
                 final_confidence, old_entry["pin"], old_entry["anchor"],
                 now_utc, now_utc)
            )
            # 审计日志
            conn.execute(
                """INSERT INTO memory_logs (entry_id, action, old_value, new_value)
                   VALUES (?, 'SUPERSEDE', ?, ?)""",
                (old_entry_id, old_entry_id, new_entry_id)
            )
        logger.info("Supersede: %s → %s", old_entry_id, new_entry_id)
    except Exception as e:
        logger.error("Supersede 失败: %s", e)
        raise

    # Obsidian 导出
    try:
        export_to_obsidian(new_entry_id, old_entry["user_id"],
                           final_category, new_content, final_keywords, "active")
        export_to_obsidian(old_entry_id, old_entry["user_id"],
                           old_entry["category"],
                           f"> 此记忆已被取代\n> 新版本: [[{new_entry_id}]]\n\n{old_entry['content']}",
                           [], "superseded")
    except Exception as e:
        logger.warning("Obsidian 导出失败: %s", e)

    return (old_entry_id, new_entry_id)


def export_to_obsidian(
    entry_id: str,
    user_id: str,
    category: str,
    content: str,
    keywords: list,
    status: str = "active",
) -> str:
    """导出 .md 文件到 Obsidian Vault，含 Frontmatter 和动态 WikiLink"""
    vault = Path(OBSIDIAN_VAULT_PATH)
    category_dir = vault / category
    category_dir.mkdir(parents=True, exist_ok=True)

    wikilinks = "\n".join(f"  - [[{kw}]]" for kw in keywords) if keywords else ""
    tags_str = ", ".join(keywords) if keywords else ""

    frontmatter = f"""---
entry_id: {entry_id}
user_id: {user_id}
category: {category}
status: {status}
tags: [{tags_str}]
---

"""
    filepath = category_dir / f"{entry_id}.md"
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(frontmatter + content)
        if wikilinks:
            f.write(f"\n\n## 关联实体\n{wikilinks}\n")

    logger.debug("Obsidian 导出: %s", filepath)

    if GIT_AUTO_PUSH:
        _git_commit_and_push(vault, f"auto: update {entry_id} [{category}]")

    return str(filepath)


def _git_commit_and_push(repo_path: Path, message: str):
    """Git add → commit → push，失败仅记录日志"""
    try:
        subprocess.run(
            ["git", "-C", str(repo_path), "add", "."],
            capture_output=True, timeout=10, check=True
        )
        result = subprocess.run(
            ["git", "-C", str(repo_path), "commit", "-m", message],
            capture_output=True, timeout=10
        )
        if b"nothing to commit" not in result.stderr:
            result.check_returncode()
        subprocess.run(
            ["git", "-C", str(repo_path), "push"],
            capture_output=True, timeout=30, check=True
        )
        logger.debug("Git push 成功")
    except subprocess.TimeoutExpired:
        logger.warning("Git 操作超时")
    except subprocess.CalledProcessError as e:
        logger.warning("Git push 失败: %s", e.stderr.decode()[:200])
    except FileNotFoundError:
        logger.debug("Git 未安装，跳过推送")
