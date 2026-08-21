"""
把 Ombre Brain 的记忆迁移进 Memory Vault。

用法:
    python scripts/migrate_from_ob.py ob_export.json --dry-run   # 先看会发生什么
    python scripts/migrate_from_ob.py ob_export.json            # 真的写入

输入 JSON 是一个数组，每项至少要有 bucket_id / content，其余可选:
    {"bucket_id": "8f0ef559ef6f", "kind": "letter",      # letter | plan | bucket
     "name": "冷启动日记 #5", "content": "...",
     "domain": "创作,自省", "tags": ["日记"], "importance": 10,
     "weight": 50.0, "pinned": true, "archived": false, "resolved": false,
     "valence": 0.5, "arousal": 0.3, "why_remembered": "...",
     "created_at": "2026-08-04 09:58:18", "author": "Cael"}

OB 里这边没有对应列的字段（tags / 情感 V-A / importance / why_remembered /
原始 bucket_id …）统一原样存进 meta 列的 JSON，迁移不丢数据。
幂等：靠 meta.ob_bucket_id 判重，重复跑不会产生副本。
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.db import init_db, get_db_connection  # noqa: E402

# OB 的主题域 → Vault 的 category。OB 一个桶可能有多个域，取第一个能对上的。
# 与 src/db.py 的 CHECK 约束保持一致；写错分类会在插入时被数据库拒掉
VALID_CATEGORY = {'preference', 'task', 'decision', 'knowledge',
                  'fiction_inspiration', 'diary'}

DOMAIN_MAP = {
    '创作': 'fiction_inspiration', '阅读': 'fiction_inspiration',
    '编程': 'knowledge', 'AI': 'knowledge', '学习': 'knowledge',
    '数字': 'knowledge', '技术': 'knowledge', '工作': 'knowledge',
    '社交': 'preference', '人际': 'preference', '恋爱': 'preference',
    '家庭': 'preference', '心理': 'preference', '情绪': 'preference',
    '内心': 'preference', '自省': 'preference', '情感': 'preference',
}
# 创作类的域优先判定：OB 一个桶常有多个域（"数字,创作"），按出现顺序取第一个会
# 让"数字"压过"创作"，把故事概念错归成 knowledge
CREATIVE_DOMAINS = ('创作', '阅读')
# 名字里带这些词的是"定下来的规矩"，归到 decision。
# 必须排在创作判定之后——"卷心菜旧神设定"里的"设定"是创作用语，不是规矩
DECISION_HINTS = ('规范', '契约', '规则', '决定', '边界', '设定')
# task 只从 kind=='plan' 来。原先把"工作"域也映射成 task，导致
# "工作与写作状态"这类近况记录被当成待办
PLAN_TYPE_MAP = {'工作': 'plan-work', '编程': 'plan-dev', 'AI': 'plan-dev',
                 '开发': 'plan-dev', '学习': 'plan-dev'}


def pick_category(item) -> str:
    # 手工指定优先：自动映射靠域和标题猜，总有猜不准的
    # （比如"工作与写作状态"域里带"创作"，但它是近况不是灵感）。
    # 在导出 JSON 里写 "category": "preference" 就能覆盖，不用去拧规则。
    manual = item.get('category')
    if manual in VALID_CATEGORY:
        return manual
    if item.get('kind') == 'letter':
        return 'diary'
    if item.get('kind') == 'plan':
        return 'task'
    domains = [d.strip() for d in str(item.get('domain') or '').split(',') if d.strip()]
    # ① 只要沾创作/阅读就是灵感，不管它排在第几个、名字里有没有"设定"
    if any(d in CREATIVE_DOMAINS for d in domains):
        return 'fiction_inspiration'
    # ② 再看名字是不是"定下来的规矩"
    name = item.get('name') or ''
    if any(h in name for h in DECISION_HINTS):
        return 'decision'
    # ③ 最后按域映射
    for d in domains:
        hit = DOMAIN_MAP.get(d)
        if hit:
            return hit
    return 'knowledge'


def pick_plan_type(item) -> str:
    """计划的三分类；对不上就归生活。"""
    for d in str(item.get('domain') or '').split(','):
        hit = PLAN_TYPE_MAP.get(d.strip())
        if hit:
            return hit
    name = item.get('name') or ''
    if any(k in name for k in ('开发', '前端', '后端', 'plutocael', 'OB')):
        return 'plan-dev'
    return 'plan-life'


def pick_confidence(item) -> str:
    """OB 的 importance 是 1-10，Vault 只有三档。"""
    imp = item.get('importance')
    if imp is None:
        return 'high'
    imp = float(imp)
    return 'high' if imp >= 8 else ('medium' if imp >= 5 else 'low')


def pick_status(item) -> str:
    if item.get('archived'):
        return 'archived'
    if item.get('resolved'):
        return 'completed'
    return 'active'


def pick_weight(item) -> float:
    """OB 权重量纲很杂（固化=999、plan=50、动态 0.3~16），压到 0~1。"""
    if item.get('pinned'):
        return 1.0
    w = item.get('weight')
    if w is None:
        return 1.0
    try:
        w = float(w)
    except (TypeError, ValueError):
        return 1.0
    if w >= 50:          # plan / 固化那一档
        return 1.0
    return max(0.05, min(1.0, w / 16.0))


def build_meta(item) -> str:
    """OB 里这边没地方放的字段，原样留档。"""
    keep = ('bucket_id', 'name', 'domain', 'tags', 'importance', 'weight',
            'valence', 'arousal', 'why_remembered', 'author', 'kind')
    meta = {f'ob_{k}': item[k] for k in keep if item.get(k) not in (None, '', [])}
    meta['migrated_at'] = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    return json.dumps(meta, ensure_ascii=False)


def already_imported(conn, bucket_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM memory_entries WHERE meta LIKE ? LIMIT 1",
        (f'%"ob_bucket_id": "{bucket_id}"%',)
    ).fetchone()
    return row is not None


def migrate(items, db_path, user_id='default', dry_run=False):
    init_db(db_path)
    conn = get_db_connection(db_path)
    stats = {'imported': 0, 'skipped': 0, 'by_category': {}}
    try:
        for it in items:
            bid = it.get('bucket_id') or ''
            content = (it.get('content') or '').strip()
            if not content:
                stats['skipped'] += 1
                continue
            if bid and already_imported(conn, bid):
                stats['skipped'] += 1
                continue

            cat = pick_category(it)
            row = {
                'entry_id': f"ob-{bid}" if bid else None,
                'category': cat,
                'type': pick_plan_type(it) if cat == 'task' and it.get('kind') == 'plan' else None,
                'content': content,
                'confidence': pick_confidence(it),
                'status': pick_status(it),
                'pin': 1 if it.get('pinned') else 0,
                'weight': pick_weight(it),
                'created_at': it.get('created_at') or datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
            }
            stats['by_category'][cat] = stats['by_category'].get(cat, 0) + 1
            stats['imported'] += 1
            if dry_run:
                print(f"  [{cat:18}] {row['status']:9} pin={row['pin']} "
                      f"w={row['weight']:.2f} {(it.get('name') or content)[:34]}")
                continue

            conn.execute(
                """INSERT INTO memory_entries
                   (entry_id, user_id, session_id, category, type, content,
                    confidence, status, pin, anchor, weight, created_at,
                    last_accessed_at, meta)
                   VALUES (?,?,?,?,?,?,?,?,?,0,?,?,?,?)""",
                (row['entry_id'], user_id, 'ob-migration', row['category'],
                 row['type'], row['content'], row['confidence'], row['status'],
                 row['pin'], row['weight'], row['created_at'],
                 row['created_at'], build_meta(it))
            )
        if not dry_run:
            conn.commit()
    finally:
        conn.close()
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('export_json')
    ap.add_argument('--db', default=os.getenv('MEMORY_DB_PATH', 'memory.db'))
    ap.add_argument('--user-id', default='default')
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()

    with open(a.export_json, encoding='utf-8') as f:
        items = json.load(f)
    print(f"读入 {len(items)} 条{'（演练，不写库）' if a.dry_run else ''}\n")
    s = migrate(items, a.db, a.user_id, a.dry_run)
    print(f"\n导入 {s['imported']} 条，跳过 {s['skipped']} 条")
    for k, v in sorted(s['by_category'].items()):
        print(f"   {k:20} {v}")


if __name__ == '__main__':
    main()
