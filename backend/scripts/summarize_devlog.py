"""总结项目开发历史到开发日志。

1. 删除现有所有开发日志（type='dev_log'）
2. 拉取本地 git log，按天汇总 commit message
3. 写入开发日志（category='knowledge' + type='dev_log'）

用法：
    python scripts/summarize_devlog.py
"""

import os
import subprocess
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from src.db import init_db
from src.storage import insert_memory


def get_git_log(repo_path):
    """拉取 git log，返回 [(date, message), ...]。"""
    result = subprocess.run(
        ["git", "-C", repo_path, "log", "--pretty=format:%ad||%s",
         "--date=format:%Y-%m-%d"],
        capture_output=True, text=True, encoding="utf-8"
    )
    commits = []
    for line in result.stdout.strip().split("\n"):
        if "||" in line:
            date, msg = line.split("||", 1)
            commits.append((date, msg.strip()))
    return commits


def summarize_by_day(commits):
    by_day = defaultdict(list)
    for date, msg in commits:
        by_day[date].append(msg)
    return by_day


def main():
    repo_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    conn = init_db(os.getenv("MEMORY_DB_PATH", "memory.db"))

    # 1. 删除现有开发日志
    deleted = conn.execute(
        "DELETE FROM memory_entries WHERE type='dev_log'"
    ).rowcount
    conn.commit()
    print(f"已删除现有开发日志 {deleted} 条")

    # 2. 拉取 git log 并按天汇总
    commits = get_git_log(repo_path)
    by_day = summarize_by_day(commits)
    print(f"拉取 {len(commits)} 条 commit，分布在 {len(by_day)} 天")

    # 3. 按天写入开发日志（按时间正序）
    for date in sorted(by_day):
        lines = [f"开发日志 {date}\n"]
        lines.extend(f"- {msg}" for msg in by_day[date])
        content = "\n".join(lines)
        insert_memory(conn, "default", "git-log", "knowledge", content,
                      "high", mtype="dev_log")
        print(f"写入: {date}（{len(by_day[date])} 条 commit）")

    conn.close()
    print("完成")


if __name__ == "__main__":
    main()
