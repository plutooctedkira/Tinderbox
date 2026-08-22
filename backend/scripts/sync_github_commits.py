"""GitHub commit 自动同步到开发日志。

拉取指定 repo 的 commit history，按天汇总成开发日志（type='dev_log'），
写入记忆库。当天已有日志则跳过，避免重复。

用法：
    python scripts/sync_github_commits.py owner/repo --days 7 --branch main

环境变量：
    GITHUB_TOKEN    可选；私有 repo 或提高 rate limit（5000/h）时需要
    MEMORY_DB_PATH  数据库路径（默认 memory.db）
"""

import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from src.db import init_db
from src.storage import insert_memory

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")


def fetch_commits(repo, branch="main", per_page=100):
    """拉取 repo 的 commit history（最多 per_page 条）。"""
    url = (f"https://api.github.com/repos/{repo}/commits"
           f"?sha={branch}&per_page={per_page}")
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "memory-vault-sync",
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


def summarize_by_day(commits, days):
    """按天汇总 commits。返回 {date: [commit, ...]}，只保留最近 days 天。"""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date()
    by_day = {}
    for c in commits:
        date = c["commit"]["author"]["date"][:10]  # YYYY-MM-DD
        try:
            d = datetime.strptime(date, "%Y-%m-%d").date()
        except ValueError:
            continue
        if d < cutoff:
            continue
        by_day.setdefault(date, []).append(c)
    return by_day


def format_day_log(date, commits):
    """把某天的 commits 格式化成一条开发日志正文。"""
    lines = [f"开发日志 {date}\n"]
    for c in commits:
        msg = c["commit"]["message"].split("\n")[0].strip()  # 取第一行
        author = c["commit"]["author"]["name"]
        sha = c["sha"][:7]
        lines.append(f"- {msg} ({sha}, {author})")
    return "\n".join(lines)


def day_already_logged(conn, date):
    """检查某天是否已有开发日志（避免重复写入）。"""
    row = conn.execute(
        "SELECT COUNT(*) FROM memory_entries "
        "WHERE type='dev_log' AND content LIKE ?",
        (f"开发日志 {date}%",),
    ).fetchone()
    return row[0] > 0


def main():
    ap = argparse.ArgumentParser(description="GitHub commit 同步到开发日志")
    ap.add_argument("repo", help="GitHub 仓库，格式 owner/repo")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--days", type=int, default=7,
                    help="同步最近 N 天的 commits（默认 7）")
    ap.add_argument("--user", default="default")
    args = ap.parse_args()

    conn = init_db(os.getenv("MEMORY_DB_PATH", "memory.db"))

    try:
        commits = fetch_commits(args.repo, args.branch)
    except Exception as e:
        conn.close()
        print(f"拉取 commits 失败: {e}", file=sys.stderr)
        sys.exit(1)

    by_day = summarize_by_day(commits, args.days)

    saved = 0
    for date in sorted(by_day):
        if day_already_logged(conn, date):
            continue
        content = format_day_log(date, by_day[date])
        insert_memory(conn, args.user, "github-sync", "knowledge", content,
                      "high", mtype="dev_log")
        saved += 1
        print(f"已写入开发日志: {date}（{len(by_day[date])} commits）")

    conn.close()
    print(f"完成：共写入 {saved} 条开发日志（其余为已存在，跳过）。")


if __name__ == "__main__":
    main()
