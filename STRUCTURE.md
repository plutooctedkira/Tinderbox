# Memory Vault 记忆系统 — 结构与运行说明

> 路径：`C:\Users\PlutootedKira\memory_vault`
> 版本：v6.0 生产级（含后续演进：CJK 分词检索、命中加权、计划/日记/开发日志、Obsidian 迁移）

Memory Vault 是一个基于 **SQLite + FTS5 + Python** 的高性能底层记忆与小说灵感引擎。
前端是纯 HTML/JS 单页（无构建），后端是标准库 `http.server` 的轻量 API 服务，
两者通过 `fetch('http://localhost:8765/api/...')` 通信，另有 MCP 服务供 AI 客户端挂载。

---

## 一、目录结构

```
memory_vault/
├── frontend/                 # 前端（纯静态，无构建）
│   ├── dashboard_v3.html     # 主看板（单文件：HTML + CSS + JS 全内联）
│   ├── logo.png              # 侧栏/页头 logo
│   └── test.html             # API 联调测试页
│
├── backend/                  # 后端（Python）
│   ├── dashboard.py          # ★ HTTP API 服务（入口，:8765）
│   ├── mcp_server.py         # MCP 协议服务（stdio / streamable-http）
│   ├── requirements.txt      # 运行时依赖
│   ├── requirements-dev.txt  # 开发/测试依赖(含 pytest)
│   ├── .env                  # 环境变量（数据库路径、DeepSeek Key 等）
│   ├── memory.db             # SQLite 主库（WAL 模式）
│   ├── src/                  # 核心模块
│   │   ├── db.py             # 连接 + Schema + 触发器 + CJK 分词
│   │   ├── storage.py        # 存储 + 版本更迭 + Obsidian 导出
│   │   ├── ingest.py         # DeepSeek LLM 提取 + Chunking
│   │   ├── retriever.py      # FTS5 检索 + 命中加权 + 主动浮现
│   │   ├── decay.py          # 每日衰减 + 归档 + 日志 GC
│   │   └── __init__.py
│   ├── scripts/
│   │   └── migrate_from_ob.py  # Obsidian 迁移脚本
│   ├── tests/                # 单元测试
│   ├── ob_*.json             # Obsidian 导出的中间数据（迁移用）
│   └── .venv/                # Python 虚拟环境
│
├── obsidian_vault/           # Obsidian 笔记目录（.md 按分类分文件夹）
│   ├── knowledge/  decision/  task/  preference/  fiction_inspiration/  diary/
│
├── Memory_Vault_Architecture.md   # 架构方案文档
└── .gitignore
```

---

## 二、数据层（src/db.py）

### 连接工厂 `get_db_connection()`

每条连接都做四件事，缺一不可：

```python
conn = sqlite3.connect(db_path, timeout=30.0, check_same_thread=False)
conn.row_factory = sqlite3.Row
conn.execute("PRAGMA journal_mode=WAL")      # 多读单写，读不阻塞读
conn.execute("PRAGMA synchronous=NORMAL")    # 性能与安全折中
conn.execute("PRAGMA busy_timeout=30000")    # 写冲突等 30 秒
conn.create_function("seg", 1, segment_cjk)  # 注册 CJK 分词函数给触发器用
```

### CJK 分词（关键修复）

FTS5 的 `unicode61` 分词器**不切中日韩文字**——整句中文变成一个 token，
结果搜"咖啡"是 0 条。解决办法：

- `segment_cjk(text)` 用正则把每个中日韩字符用空格隔开：`喝咖啡` → ` 喝 咖 啡 `
- 写入时经 `seg()` 函数进入 FTS 索引，检索时对查询做同样处理
- 检索整体加引号当**短语**搜，保留字与字的相邻关系（"喝咖啡"不会命中"喝一杯冰美式咖啡"）

```python
_CJK = r'[一-鿿㐀-䶿぀-ヿ가-힯]'
def segment_cjk(text):
    return re.sub(f'({_CJK})', r' \1 ', text or '')
```

### 表结构

| 表 | 用途 |
|----|------|
| `memory_entries` | 主记忆表（category/type/content/confidence/status/pin/anchor/weight/superseded_by/meta 等） |
| `memory_fts` | FTS5 全文索引虚拟表 |
| `memory_logs` | 审计日志（INSERT/UPDATE/DELETE/RECALL_HIT/DECAY/SUPERSEDE） |
| `memory_entries_archive` | 冷归档表（硬迁移用） |
| `memory_fts_archive` | 冷归档 FTS5 索引 |

### 触发器（数据库层自动同步）

- **FTS5 同步**：`trg_memory_fts_insert/update/delete` — 内容增删改自动同步索引
- **审计日志**：`trg_log_insert/update/delete` — 所有 DML 自动记录快照

---

## 三、核心模块

### src/storage.py — 存储与导出
- `insert_memory(...)` 插入记忆，`mtype` 写进 `type` 列（计划分组用），`meta` 存标题
- `supersede_memory(...)` 版本更迭：旧条目 → `superseded`，新条目 → `active`
- `export_to_obsidian(...)` 导出 .md 到 Obsidian Vault

### src/retriever.py — 检索与命中加权
- `retrieve_memories(...)` FTS5 BM25 检索，综合评分 `-bm25 * weight`
- `_search_fts5(...)` 真正走 FTS5；失败才回退 LIKE（MATCH 左边必须写真实表名）
- `boost_hits(...)` 命中加权：`weight +0.1`（封顶 1.0），记 RECALL_HIT
- `get_active_surfaced_memories(...)` 主动浮现：pin > anchor > decision > task > knowledge > fiction > preference

### src/decay.py — 衰减与 GC（每日 cron）

```
1. task 权重 ×0.95（下限 0.1）
2. preference/knowledge/fiction ×0.99
3. weight ≤0.1 → status='archived'（软归档）
4. weight ≤0.05 + 180天未访问 → 迁移 archive 表（硬归档）
5. 清理 90 天前日志
```

> 决策/日记/开发日志不参与衰减；`anchor=1` 或 `pin=1` 豁免衰减。

### src/ingest.py — DeepSeek LLM 提取
- 长文本按 2500 字 Chunking（200 字重叠）
- DeepSeek API 调用（正则清洗 Markdown 代码块 + 指数退避重试）
- 输出 `[{category, content, confidence, keywords}]`

---

## 四、API 服务（backend/dashboard.py）

### 架构

- 基于标准库 `http.server` + `ThreadingMixIn`（多线程并发）
- 监听 `127.0.0.1:8765`
- 每次请求**重新读**前端 HTML（`load_html()`），改完前端不用重启后端
- 提供静态图片（logo 等）服务，带路径穿越防护（只取文件名 + 白名单后缀）

### GET 接口

| 路径 | 说明 |
|------|------|
| `/` `/index.html` | 返回前端看板 HTML |
| `/api/all` | 所有活跃记忆 |
| `/api/search?q=xx` | FTS5 检索（CJK 分词，命中加权） |
| `/api/detail?id=xx` | 单条详情 |
| `/api/stats` | 统计（总数/活跃/归档/取代/日志/均权/按分类） |
| `/api/dashboard` | 仪表盘（活跃/今日/冲突/最近活动） |
| `/api/diary` | 日记（category=diary） |
| `/api/plans` | 计划（category=task + type=plan-*） |
| `/api/devlogs` | 开发日志（type=dev_log） |
| `/api/archived` | 归档记忆 |
| `/api/conflicts` | 待合并冲突（含旧记忆 old） |
| `/api/pin?id=xx` | 切换置顶 |
| `/api/decay?id=xx` | 详情页衰减曲线数据 |

### POST 接口

| 路径 | 说明 |
|------|------|
| `/api/add` | 新增记忆（title 存 meta，type 存 type 列） |
| `/api/supersede` | 版本更迭 |
| `/api/anchor` | 切换锚定（永不衰减） |
| `/api/set_weight` | 手动设权重（0-1） |
| `/api/plan_progress` | 计划进度（100=completed） |
| `/api/toggle_status` | 切换状态 |
| `/api/restore` | 恢复归档 |
| `/api/resolve` | 解决冲突（keep/archive） |
| `/api/batch` | 批量删除/恢复 |

---

## 五、前端看板（frontend/dashboard_v3.html）

单文件 SPA（HTML + CSS + JS 全内联），通过 `fetch('http://localhost:8765/api/...')` 调后端。

### 侧栏分区

- **概览**：仪表盘 / 全部记忆（内置置顶搜索） / 添加记忆
- **日记 & 计划**：日记 / 计划 / 开发日志
- **管理**：归档记忆 / 数据统计

### 主要功能

| 面板 | 功能 |
|------|------|
| 仪表盘 | 今日记忆概览三卡片（活跃/今日/冲突）+ 最近活动；卡片可点击弹窗 |
| 全部记忆 | 卡片式浏览 + 置顶搜索栏 |
| 日记 | 按时间倒序，写日记 |
| 计划 | 按类型分组（生活/工作/开发），进度/完成切换 |
| 开发日志 | 已完成记录，写日志 |
| 归档记忆 | 三点菜单（恢复/删除）+ 多选批量操作 |
| 数据统计 | 总数分布 |

### 详情弹窗

- 置顶 / 锚定 / 版本更迭 / 手动设权重 / 衰减曲线

---

## 六、MCP 服务（backend/mcp_server.py）

- **stdio**（默认）：本地 Cline/Claude Desktop 拉起进程用
- **streamable-http**：设 `MCP_TRANSPORT=streamable-http` 起 HTTP 服务供远程客户端
- 9 个 Tool：`store_memory` / `extract_and_store_from_text` / `query_memories` / `write_diary` / `read_diary` / `create_plan` / `list_plans` / `set_plan_progress` / `supersede_memory`
- 1 个 Resource：`active_context` 主动上下文浮现
- 支持 `MCP_ALLOWED_HOSTS` 配外部域名（反代场景）

---

## 七、如何运行

### 启动后端

```bash
cd C:\Users\PlutootedKira\memory_vault\backend
.venv\Scripts\activate          # 用虚拟环境（已有 .venv）
python dashboard.py             # 监听 http://127.0.0.1:8765
```

### 打开前端

浏览器打开（或直接访问 http://127.0.0.1:8765）：

```
C:\Users\PlutootedKira\memory_vault\frontend\dashboard_v3.html
```

### 每日衰减（可选，VPS/计划任务）

```cron
0 3 * * * cd /path/to/backend && python -c "from src.decay import run_daily_decay; run_daily_decay('memory.db')"
```

### MCP 挂载（Cline/Claude）

```json
{
  "mcpServers": {
    "memory-vault": {
      "command": "python",
      "args": ["C:\\Users\\PlutootedKira\\memory_vault\\backend\\mcp_server.py"],
      "env": {
        "DEEPSEEK_API_KEY": "sk-xxx",
        "MEMORY_DB_PATH": "C:\\Users\\PlutootedKira\\memory_vault\\backend\\memory.db"
      }
    }
  }
}
```

---

## 八、关键设计决策（踩坑记录）

| 问题 | 解决方案 |
|------|---------|
| 中文检索搜不到 | `segment_cjk()` CJK 逐字分词 + 短语检索 |
| FTS5 从未生效 | MATCH 左边用真实表名（非别名） |
| 命中权重算了没用 | `_search_fts5` 结果按 combined_score 重排 |
| RECALL_HIT 没记录 | 检索路径统一走 `boost_hits()` |
| 计划无法分组 | `insert_memory` 补 `mtype` 参数写 type 列 |
| 开发日志加分类要重建表 | 用 type='dev_log' 标记（不动 category） |
| 前端改完要重启后端 | `load_html()` 每次请求重读文件 |
| 标题混进正文 | 标题存 `meta.title`，正文保持干净 |
