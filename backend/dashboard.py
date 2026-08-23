import json, os, sys, sqlite3, urllib.parse
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv
load_dotenv()
from src.db import init_db, get_db_connection, segment_cjk, get_feature_flag
from src.retriever import boost_hits
from src.hooks import load_plugins
init_db()
load_plugins()

# v3 是带日记/计划分页的完整界面（在 frontend/ 下）
HTML_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "frontend", "dashboard_v3.html")


def load_html() -> str:
    """每次请求都重读文件。

    原来是启动时读一次存进内存，结果改完前端不重启后端就一直发旧页面，
    浏览器怎么刷新都没用——白白排查很久。本地开发用的面板，这点开销无所谓。
    """
    with open(HTML_PATH, "r", encoding="utf-8") as f:
        return f.read()

class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False, default=str).encode())

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        p = urllib.parse.urlparse(self.path)
        q = dict(urllib.parse.parse_qsl(p.query))

        # 前端目录下的图片（logo 等）。只放行图片后缀 + 只取文件名，
        # 防止 ../../ 之类的路径穿越读到别的文件
        if p.path.lower().endswith((".png", ".jpg", ".jpeg", ".svg", ".webp", ".gif")):
            name = os.path.basename(p.path)
            fp = os.path.join(os.path.dirname(HTML_PATH), name)
            if os.path.isfile(fp):
                ext = name.rsplit(".", 1)[-1].lower()
                mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                        "svg": "image/svg+xml", "webp": "image/webp", "gif": "image/gif"}[ext]
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                with open(fp, "rb") as f:
                    self.wfile.write(f.read())
            else:
                self.send_response(404); self.end_headers()
            return

        if p.path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(load_html().encode())
            return
        conn = get_db_connection()
        try:
            if p.path == "/api/all":
                rows = conn.execute("SELECT entry_id,category,type,content,confidence,status,weight,pin,created_at FROM memory_entries WHERE status='active' ORDER BY pin DESC,weight DESC,created_at DESC LIMIT 200").fetchall()
                return self._json({"memories": [dict(r) for r in rows]})
            elif p.path == "/api/search":
                sq = q.get("q","")
                if not sq: return self._json({"results":[]})
                try:
                    # MATCH 左边必须是真实表名（写别名会报 no such column，异常被吞掉后
                    # 每次都退回 LIKE 全表扫）；查询词按 seg() 分词，中文才搜得到
                    seg_q = segment_cjk(sq).strip().replace('"', '""')
                    rows = conn.execute("SELECT m.entry_id AS id,m.content,m.category,m.confidence,m.weight,bm25(memory_fts) AS bm25 FROM memory_fts JOIN memory_entries m ON memory_fts.entry_id=m.entry_id WHERE memory_fts MATCH ? AND m.status='active' ORDER BY bm25 LIMIT 50",(f'"{seg_q}"',)).fetchall()
                except Exception as e:
                    rows = conn.execute("SELECT entry_id AS id,content,category,confidence,weight,-1.0 AS bm25 FROM memory_entries WHERE content LIKE ? AND status='active' ORDER BY weight DESC LIMIT 50",(f"%{sq}%",)).fetchall()
                results = []
                for r in rows:
                    d = dict(r)
                    d["score"] = round(-(d.get("bm25",0) or 0) * (d.get("weight",1) or 1), 4)
                    results.append(d)
                # 这里原来直接返回，绕过了 retriever 的命中处理，所以一条 RECALL_HIT 都没记过
                boost_hits(conn, [d["id"] for d in results])
                return self._json({"results": results})
            elif p.path == "/api/detail":
                row = conn.execute("SELECT * FROM memory_entries WHERE entry_id=?",(q.get("id",""),)).fetchone()
                return self._json({"memory": dict(row) if row else None})
            elif p.path == "/api/stats":
                t=conn.execute("SELECT COUNT(*) FROM memory_entries").fetchone()[0]
                a=conn.execute("SELECT COUNT(*) FROM memory_entries WHERE status='active'").fetchone()[0]
                ar=conn.execute("SELECT COUNT(*) FROM memory_entries WHERE status='archived'").fetchone()[0]
                s=conn.execute("SELECT COUNT(*) FROM memory_entries WHERE status='superseded'").fetchone()[0]
                l=conn.execute("SELECT COUNT(*) FROM memory_logs").fetchone()[0]
                aw=conn.execute("SELECT AVG(weight) FROM memory_entries WHERE status='active'").fetchone()[0]
                bc={}
                for r in conn.execute("SELECT category,COUNT(*) c FROM memory_entries GROUP BY category"): bc[r[0]]=r[1]
                return self._json({"total":t,"active":a,"archived":ar,"superseded":s,"logs_total":l,"avg_weight":round(aw or 0,2),"by_category":bc})
            elif p.path == "/api/dashboard":
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                a=conn.execute("SELECT COUNT(*) FROM memory_entries WHERE status='active'").fetchone()[0]
                tc=conn.execute("SELECT COUNT(*) FROM memory_entries WHERE created_at >= ?",(today,)).fetchone()[0]
                cf=conn.execute("SELECT COUNT(*) FROM memory_entries WHERE status='pending_merge'").fetchone()[0]
                rec=conn.execute("SELECT entry_id,substr(content,1,80) AS content,category,confidence,weight,status,created_at FROM memory_entries ORDER BY created_at DESC LIMIT 20").fetchall()
                return self._json({"active":a,"today":tc,"conflicts":cf,"recent":[dict(r) for r in rec]})
            elif p.path == "/api/pin":
                eid=q.get("id","")
                old=conn.execute("SELECT pin FROM memory_entries WHERE entry_id=?",(eid,)).fetchone()
                if old:
                    np=0 if old[0] else 1
                    conn.execute("UPDATE memory_entries SET pin=? WHERE entry_id=?",(np,eid))
                    conn.commit()
                    return self._json({"pin":np})
                return self._json({"error":"not found"},404)
            elif p.path == "/api/decay":
                # 详情页那条权重曲线的数据：起点、衰减速率、每次命中的时间点
                # 字段要覆盖详情弹层用到的全部（少一个 confidence 就会 TypeError 白屏）
                m = conn.execute("SELECT entry_id,content,category,confidence,status,weight,pin,anchor,user_id,superseded_by,created_at,last_accessed_at,meta FROM memory_entries WHERE entry_id=?",
                                 (q.get("id",""),)).fetchone()
                if not m:
                    return self._json({"error": "not found"}, 404)
                d = dict(m)
                # 速率与 src/decay.py 的 cron 保持一致；锚定/置顶豁免衰减
                if d["pin"] or d["anchor"]:
                    rate = None
                elif d["category"] == "task":
                    rate = 0.95
                elif d["category"] in ("preference", "knowledge", "fiction_inspiration"):
                    rate = 0.99
                else:
                    rate = None   # decision / diary 不参与衰减
                hits = conn.execute("SELECT timestamp FROM memory_logs WHERE entry_id=? AND action='RECALL_HIT' ORDER BY timestamp",
                                    (d["entry_id"],)).fetchall()
                # 标题：新写的在 meta.title，从 OB 迁来的在 meta.ob_name
                try:
                    mj = json.loads(d.pop("meta", None) or "{}") or {}
                    d["title"] = mj.get("title") or mj.get("ob_name") or ""
                except Exception:
                    d["title"] = ""
                d["decay_rate"] = rate
                d["floor"] = 0.1
                d["hit_boost"] = 0.1
                d["hits"] = [r["timestamp"] for r in hits]
                return self._json(d)
            elif p.path == "/api/devlogs":
                # 开发日志用 type='dev_log' 标记，不动 category。
                # 因为 SQLite 改不了 CHECK 约束，加新分类要重建整张表，
                # 而计划本来就是 category='task'+type='plan-*' 这个套路，保持一致
                rows = conn.execute("SELECT entry_id,content,category,confidence,weight,status,created_at FROM memory_entries WHERE type='dev_log' AND status IN ('active','completed') ORDER BY created_at DESC LIMIT 200").fetchall()
                return self._json({"memories": [dict(r) for r in rows]})
            elif p.path == "/api/plans":
                if not get_feature_flag(conn, "feature.plan"):
                    return self._json({"plans": []})
                # 计划 = category='task' 且 type 以 plan- 开头（前端 savePlan 传的 type
                # 是 plan-life / plan-work / plan-dev）。已完成的也要返回，前端会置灰显示
                rows = conn.execute("SELECT entry_id,content,type AS plan_type,status,confidence,weight,created_at,COALESCE(progress,0) AS progress FROM memory_entries WHERE category='task' AND type LIKE 'plan-%' AND status IN ('active','completed') ORDER BY created_at DESC LIMIT 200").fetchall()
                return self._json({"plans": [dict(r) for r in rows]})
            elif p.path == "/api/diary":
                # 日记就是 category='diary' 的记忆（前端 saveDiary 走的也是 /api/add），
                # 按时间倒序，最近的在最上面
                rows = conn.execute("SELECT entry_id,content,category,confidence,weight,status,created_at,meta FROM memory_entries WHERE category='diary' AND status='active' ORDER BY created_at DESC LIMIT 200").fetchall()
                out = []
                for r in rows:
                    d = dict(r)
                    # 新写的日记标题在 meta.title；从 OB 迁移的在 meta.ob_name
                    title = ""
                    try:
                        mj = json.loads(d.get("meta") or "{}") or {}
                        title = mj.get("title") or mj.get("ob_name") or ""
                    except Exception:
                        pass
                    d["title"] = title
                    d.pop("meta", None)
                    out.append(d)
                return self._json({"memories": out})
            elif p.path == "/api/archived":
                rows=conn.execute("SELECT entry_id,substr(content,1,80) AS content,category,confidence,weight,status,created_at FROM memory_entries WHERE status='archived' ORDER BY created_at DESC LIMIT 100").fetchall()
                return self._json({"memories":[dict(r) for r in rows]})
            elif p.path == "/api/conflicts":
                rows=conn.execute("SELECT entry_id,content,category,confidence,weight,created_at,superseded_by FROM memory_entries WHERE status='pending_merge' ORDER BY created_at DESC").fetchall()
                conflicts = []
                for r in rows:
                    d = dict(r)
                    if d.get("superseded_by"):
                        old = conn.execute("SELECT entry_id,content,category,confidence,created_at FROM memory_entries WHERE entry_id=?",(d["superseded_by"],)).fetchone()
                        d["old"] = dict(old) if old else None
                    else:
                        d["old"] = None
                    conflicts.append(d)
                return self._json({"conflicts":conflicts})
            elif p.path == "/api/topics":
                rows = conn.execute("SELECT topic_id, category, title, keywords, summary, entry_count, created_at, updated_at FROM topics ORDER BY category, updated_at DESC").fetchall()
                return self._json({"topics": [dict(r) for r in rows]})
            elif p.path == "/api/topic":
                tid = q.get("id", "")
                topic = conn.execute("SELECT * FROM topics WHERE topic_id=?", (tid,)).fetchone()
                if not topic:
                    return self._json({"error": "not found"}, 404)
                memories = conn.execute("SELECT entry_id, content, category, confidence, weight, status, created_at FROM memory_entries WHERE topic_id=? ORDER BY created_at DESC", (tid,)).fetchall()
                return self._json({"topic": dict(topic), "memories": [dict(r) for r in memories]})
            elif p.path == "/api/config":
                rows = conn.execute("SELECT key, value, description, updated_at FROM config ORDER BY key").fetchall()
                return self._json({"flags": [dict(r) for r in rows]})
            return self._json({"error":"Not found"},404)
        finally:
            conn.close()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}
        if self.path == "/api/add":
            from src.storage import insert_memory
            conn = get_db_connection()
            try:
                # 标题存进 meta，不塞进正文——正文要保持干净，检索和摘要都靠它
                title = (body.get("title") or "").strip()
                meta = json.dumps({"title": title}, ensure_ascii=False) if title else None
                eid = insert_memory(conn, body.get("user_id","default"), body.get("session_id","web"), body.get("category","knowledge"), body.get("content",""), body.get("confidence","medium"), mtype=body.get("type"), meta=meta)
                return self._json({"entry_id":eid})
            finally:
                conn.close()
        elif self.path == "/api/supersede":
            from src.storage import supersede_memory
            conn = get_db_connection()
            try:
                o,n = supersede_memory(conn, body.get("old_entry_id"), body.get("new_content"), body.get("new_category"), body.get("new_confidence"))
                return self._json({"old":o,"new":n})
            except ValueError as e:
                return self._json({"error":str(e)},400)
            finally:
                conn.close()
        elif self.path == "/api/anchor":
            # 锚定 = 永不衰减（decay.py 里 anchor=0 才参与衰减）
            conn = get_db_connection()
            try:
                eid = body.get("entry_id")
                row = conn.execute("SELECT anchor FROM memory_entries WHERE entry_id=?", (eid,)).fetchone()
                if not row:
                    return self._json({"error": "not found"}, 404)
                na = 0 if row[0] else 1
                conn.execute("UPDATE memory_entries SET anchor=? WHERE entry_id=?", (na, eid))
                conn.commit()
                return self._json({"anchor": na})
            finally:
                conn.close()

        elif self.path == "/api/set_weight":
            conn = get_db_connection()
            try:
                w = max(0.0, min(1.0, float(body.get("weight", 1.0))))
                conn.execute("UPDATE memory_entries SET weight=? WHERE entry_id=?",
                             (w, body.get("entry_id")))
                conn.commit()
                return self._json({"weight": round(w, 2)})
            except (TypeError, ValueError):
                return self._json({"error": "weight 必须是 0-1 的数字"}, 400)
            finally:
                conn.close()

        elif self.path == "/api/plan_progress":
            # 进度和 status 保持同步：100=completed，其余=active。
            # 这样旧的按 status 过滤的地方不用改也还是对的
            conn = get_db_connection()
            try:
                if not get_feature_flag(conn, "feature.plan"):
                    return self._json({"error": "计划功能已关闭"}, 403)
                pg = max(0, min(100, int(body.get("progress", 0))))
                st = "completed" if pg >= 100 else "active"
                conn.execute("UPDATE memory_entries SET progress=?, status=? WHERE entry_id=?",
                             (pg, st, body.get("entry_id")))
                conn.commit()
                return self._json({"progress": pg, "status": st})
            except (TypeError, ValueError):
                return self._json({"error": "progress 必须是 0-100 的整数"}, 400)
            finally:
                conn.close()

        elif self.path == "/api/toggle_status":
            conn = get_db_connection()
            try:
                conn.execute("UPDATE memory_entries SET status=? WHERE entry_id=?", (body.get("status"), body.get("entry_id")))
                conn.commit()
                return self._json({"status": "ok"})
            finally:
                conn.close()

        elif self.path == "/api/restore":
            conn = get_db_connection()
            try:
                conn.execute("UPDATE memory_entries SET status='active',weight=1.0 WHERE entry_id=? AND status='archived'",(body.get("entry_id"),))
                conn.commit()
                return self._json({"status":"ok"})
            finally:
                conn.close()
        elif self.path == "/api/resolve":
            conn = get_db_connection()
            try:
                action = body.get("action","keep")
                eid = body.get("entry_id")
                if action == "keep":
                    conn.execute("UPDATE memory_entries SET status='active' WHERE entry_id=?",(eid,))
                else:
                    conn.execute("UPDATE memory_entries SET status='archived' WHERE entry_id=?",(eid,))
                conn.commit()
                return self._json({"status":"ok"})
            finally:
                conn.close()
        elif self.path == "/api/batch":
            conn = get_db_connection()
            try:
                action = body.get("action")
                ids = body.get("ids", [])
                if action == "delete" and ids:
                    ph = ",".join("?" * len(ids))
                    conn.execute(f"DELETE FROM memory_entries WHERE entry_id IN ({ph})", ids)
                elif action == "restore" and ids:
                    ph = ",".join("?" * len(ids))
                    conn.execute(f"UPDATE memory_entries SET status='active',weight=1.0 WHERE entry_id IN ({ph})", ids)
                conn.commit()
                return self._json({"status":"ok","count":len(ids)})
            finally:
                conn.close()

        elif self.path == "/api/config":
            conn = get_db_connection()
            try:
                key = (body.get("key") or "").strip()
                value = str(body.get("value", "true")).strip().lower()
                if not key.startswith("feature."):
                    return self._json({"error": "key 必须以 feature. 开头"}, 400)
                if value not in ("true", "false"):
                    return self._json({"error": "value 必须是 true 或 false"}, 400)
                conn.execute(
                    "INSERT INTO config (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                    "updated_at=strftime('%Y-%m-%d %H:%M:%S','now','utc')",
                    (key, value)
                )
                conn.commit()
                return self._json({"key": key, "value": value})
            finally:
                conn.close()

        elif self.path == "/api/reclassify":
            # 手动分配：修改记忆的分类和类型，并重新聚合卷宗
            conn = get_db_connection()
            try:
                entry_id = body.get("entry_id")
                category = body.get("category")
                mtype = body.get("type")
                valid = ["preference", "task", "decision", "knowledge",
                         "fiction_inspiration", "diary"]
                if category not in valid:
                    return self._json({"error": "非法分类"}, 400)
                # 记录旧卷宗（用于后续清理空卷宗）
                old = conn.execute(
                    "SELECT topic_id, user_id, content FROM memory_entries WHERE entry_id=?",
                    (entry_id,)
                ).fetchone()
                old_topic_id = old["topic_id"] if old else None

                # 更新分类/类型，并清除 topic_id（分类变了，卷宗归属失效）
                conn.execute(
                    "UPDATE memory_entries SET category=?, type=?, topic_id=NULL "
                    "WHERE entry_id=?",
                    (category, mtype or None, entry_id)
                )
                conn.commit()

                # 清理旧卷宗：空了就删除，否则 entry_count 减 1
                if old_topic_id:
                    cnt = conn.execute(
                        "SELECT COUNT(*) FROM memory_entries WHERE topic_id=?",
                        (old_topic_id,)
                    ).fetchone()[0]
                    if cnt == 0:
                        conn.execute("DELETE FROM topics WHERE topic_id=?", (old_topic_id,))
                    else:
                        conn.execute(
                            "UPDATE topics SET entry_count = entry_count - 1 WHERE topic_id=?",
                            (old_topic_id,)
                        )
                    conn.commit()

                # 若新分类是小说灵感，重新聚合到卷宗
                if get_feature_flag(conn, "feature.aggregate") and category == "fiction_inspiration":
                    row = conn.execute(
                        "SELECT user_id, content FROM memory_entries WHERE entry_id=?",
                        (entry_id,)
                    ).fetchone()
                    if row:
                        try:
                            from src.aggregator import aggregate_memory
                            aggregate_memory(conn, entry_id, row["user_id"],
                                             "fiction_inspiration", row["content"])
                        except Exception:
                            pass  # 聚合失败不影响分类修改
                return self._json({"status": "ok", "category": category, "type": mtype})
            finally:
                conn.close()

        elif self.path == "/api/edit":
            # 编辑记忆内容（可选同时改分类/类型，并重新聚合）
            conn = get_db_connection()
            try:
                entry_id = body.get("entry_id")
                content = body.get("content")
                category = body.get("category")
                mtype = body.get("type")
                if not content or not content.strip():
                    return self._json({"error": "内容不能为空"}, 400)
                valid = ["preference", "task", "decision", "knowledge",
                         "fiction_inspiration", "diary"]
                if category not in valid:
                    return self._json({"error": "非法分类"}, 400)

                old = conn.execute(
                    "SELECT category, topic_id, user_id FROM memory_entries WHERE entry_id=?",
                    (entry_id,)
                ).fetchone()
                old_topic_id = old["topic_id"] if old else None

                conn.execute(
                    "UPDATE memory_entries SET content=?, category=?, type=?, topic_id=NULL WHERE entry_id=?",
                    (content, category, mtype or None, entry_id)
                )
                conn.commit()

                if old_topic_id:
                    cnt = conn.execute(
                        "SELECT COUNT(*) FROM memory_entries WHERE topic_id=?",
                        (old_topic_id,)
                    ).fetchone()[0]
                    if cnt == 0:
                        conn.execute("DELETE FROM topics WHERE topic_id=?", (old_topic_id,))
                    else:
                        conn.execute(
                            "UPDATE topics SET entry_count=entry_count-1 WHERE topic_id=?",
                            (old_topic_id,)
                        )
                    conn.commit()

                if get_feature_flag(conn, "feature.aggregate") and category == "fiction_inspiration":
                    try:
                        from src.aggregator import aggregate_memory
                        aggregate_memory(conn, entry_id, old["user_id"] if old else "default",
                                         "fiction_inspiration", content)
                    except Exception:
                        pass

                return self._json({"status": "ok"})
            finally:
                conn.close()

        elif self.path == "/api/delete":
            # 删除单条记忆，并清理空卷宗
            conn = get_db_connection()
            try:
                entry_id = body.get("entry_id")
                old = conn.execute(
                    "SELECT topic_id FROM memory_entries WHERE entry_id=?", (entry_id,)
                ).fetchone()
                old_topic_id = old["topic_id"] if old else None

                conn.execute("DELETE FROM memory_entries WHERE entry_id=?", (entry_id,))
                conn.commit()

                if old_topic_id:
                    cnt = conn.execute(
                        "SELECT COUNT(*) FROM memory_entries WHERE topic_id=?",
                        (old_topic_id,)
                    ).fetchone()[0]
                    if cnt == 0:
                        conn.execute("DELETE FROM topics WHERE topic_id=?", (old_topic_id,))
                    else:
                        conn.execute(
                            "UPDATE topics SET entry_count=entry_count-1 WHERE topic_id=?",
                            (old_topic_id,)
                        )
                    conn.commit()

                return self._json({"status": "ok"})
            finally:
                conn.close()

        return self._json({"error":"Not found"},404)

def main():
    port = int(os.getenv("DASHBOARD_PORT", "8765"))
    server = ThreadingHTTPServer(("127.0.0.1", port), DashboardHandler)
    print(f"Dashboard: http://localhost:{port}")
    server.serve_forever()

if __name__ == "__main__":
    main()