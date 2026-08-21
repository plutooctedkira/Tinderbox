"""测试: Chunking 分块逻辑（不调用 API）"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.ingest import chunk_text, _clean_json_response

def test_chunk_short():
    short = "这是一段短文本"
    chunks = chunk_text(short, 2500)
    assert len(chunks) == 1
    assert chunks[0] == short
    print("[OK] 短文本不分块")

def test_chunk_long():
    long_text = "A" * 5000
    chunks = chunk_text(long_text, 2500, 200)
    assert len(chunks) >= 2
    print(f"[OK] 长文本分 {len(chunks)} 块 (5000 字)")

def test_clean_json():
    raw = '```json\n[{"category":"task","content":"test"}]\n```'
    cleaned = _clean_json_response(raw)
    assert "```" not in cleaned
    assert "\"category\"" in cleaned
    print("[OK] JSON 清洗: 去除 Markdown 代码块")

def test_clean_json_no_marker():
    raw = '[{"category":"knowledge","content":"直接JSON"}]'
    cleaned = _clean_json_response(raw)
    assert cleaned == raw
    print("[OK] JSON 清洗: 纯 JSON 不变")

if __name__ == "__main__":
    test_chunk_short()
    test_chunk_long()
    test_clean_json()
    test_clean_json_no_marker()
    print("\n*** Ingest 测试全部通过！")
