"""
Memory Vault - DeepSeek LLM 提取与 Chunking 管道

包含:
- 大文本智能 Chunking（2500 字切片）
- DeepSeek API 调用（正则容错 + 指数退避重试）
- 批量提取记忆与灵感
"""

import re
import json
import time
import logging
import os
from typing import Optional
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("memory.ingest")

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
DEEPSEEK_MAX_RETRIES = int(os.getenv("DEEPSEEK_MAX_RETRIES", "3"))
DEEPSEEK_TIMEOUT = int(os.getenv("DEEPSEEK_TIMEOUT", "60"))

CHUNK_SIZE = 2500  # 每块最大字符数
CHUNK_OVERLAP = 200  # 块重叠字符数

EXTRACTION_SYSTEM_PROMPT = """你是一个专业的记忆提取与知识归纳引擎。

请从用户提供的文本中提取以下信息，以 JSON 数组格式返回：

[
  {
    "category": "preference|task|decision|knowledge|fiction_inspiration",
    "content": "提炼后的核心内容（简洁、完整、保留关键细节）",
    "confidence": "high|medium|low",
    "keywords": ["关键词1", "关键词2", "关键词3"]
  }
]

分类标准：
- preference: 用户偏好、习惯、喜恶
- task: 待办事项、任务、承诺
- decision: 决策、选择、判断
- knowledge: 事实、知识点、信息
- fiction_inspiration: 小说灵感、故事构思、人物设定、世界观

规则：
1. 每条记忆单独一个对象，内容精炼但不丢失信息
2. keywords 为 1-5 个用于检索和 Obsidian WikiLink 的关键词
3. 如果文本中没有任何可提取的记忆，返回空数组 []
4. 仅返回 JSON 数组，不要包含任何其他文字或 Markdown 标记
"""


class MemoryExtractionError(Exception):
    """记忆提取异常"""
    pass


def chunk_text(text: str, chunk_size: int = CHUNK_SIZE,
               overlap: int = CHUNK_OVERLAP) -> list[str]:
    """将长文本按 chunk_size 切片，块间有 overlap 重叠"""
    if len(text) <= chunk_size:
        return [text]

    chunks = []
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        chunk = text[start:end]
        chunks.append(chunk)
        start += chunk_size - overlap
    return chunks


def _clean_json_response(raw: str) -> str:
    """正则清洗 LLM 返回的 JSON：去除 Markdown 代码块、多余空白"""
    # 去除 ```json ... ``` 包裹
    cleaned = re.sub(r'```(?:json)?\s*', '', raw)
    cleaned = re.sub(r'```', '', cleaned)
    # 去除首尾空白
    cleaned = cleaned.strip()
    # 尝试提取第一个 [ 到最后一个 ] 之间的内容
    match = re.search(r'\[.*\]', cleaned, re.DOTALL)
    if match:
        cleaned = match.group(0)
    return cleaned


def _call_deepseek_api(prompt: str, retries: int = DEEPSEEK_MAX_RETRIES,
                       system_prompt: str = EXTRACTION_SYSTEM_PROMPT) -> str:
    """调用 DeepSeek API，含指数退避重试"""
    if not DEEPSEEK_API_KEY or DEEPSEEK_API_KEY == "sk-placeholder":
        raise MemoryExtractionError(
            "DEEPSEEK_API_KEY 未配置，请在 .env 中设置有效的 API Key")

    client = OpenAI(
        api_key=DEEPSEEK_API_KEY,
        base_url=DEEPSEEK_BASE_URL,
        timeout=DEEPSEEK_TIMEOUT,
    )

    last_error = None
    for attempt in range(retries):
        try:
            response = client.chat.completions.create(
                model=DEEPSEEK_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
                max_tokens=4096,
            )
            return response.choices[0].message.content

        except Exception as e:
            last_error = e
            wait = 2 ** attempt  # 指数退避: 1s, 2s, 4s
            logger.warning(
                "DeepSeek API 调用失败 (尝试 %d/%d)，%ds 后重试: %s",
                attempt + 1, retries, wait, e
            )
            if attempt < retries - 1:
                time.sleep(wait)

    raise MemoryExtractionError(
        f"DeepSeek API 调用失败（已重试 {retries} 次）: {last_error}")


def extract_memories_from_chunk(chunk_text: str) -> list[dict]:
    """从单块文本中提取记忆列表"""
    raw_response = _call_deepseek_api(chunk_text)
    cleaned = _clean_json_response(raw_response)

    try:
        items = json.loads(cleaned)
        if not isinstance(items, list):
            raise ValueError("返回不是数组")
    except (json.JSONDecodeError, ValueError) as e:
        logger.error("JSON 解析失败。原始响应: %s...", cleaned[:500])
        raise MemoryExtractionError(f"LLM 返回解析失败: {e}")

    # 验证每条的必填字段
    valid_items = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if "category" not in item or "content" not in item:
            continue
        if item["category"] not in (
            "preference", "task", "decision",
            "knowledge", "fiction_inspiration"
        ):
            item["category"] = "knowledge"  # 默认归类
        if "confidence" not in item or item["confidence"] not in (
            "high", "medium", "low"
        ):
            item["confidence"] = "medium"
        if "keywords" not in item or not isinstance(item["keywords"], list):
            item["keywords"] = []
        valid_items.append(item)

    return valid_items


def extract_memories_with_retry(raw_text: str) -> list[dict]:
    """
    智能 Chunking 管道：
    1. 短文本直接提取
    2. 长文本按 2500 字切片，逐块提取后合并去重
    """
    if not raw_text or not raw_text.strip():
        return []

    chunks = chunk_text(raw_text)
    all_items = []

    for i, chunk in enumerate(chunks):
        if len(chunks) > 1:
            logger.info("处理 Chunk %d/%d (%d 字)", i + 1, len(chunks), len(chunk))

        try:
            items = extract_memories_from_chunk(chunk)
            all_items.extend(items)
            logger.debug("Chunk %d 提取 %d 条", i + 1, len(items))
        except MemoryExtractionError as e:
            logger.error("Chunk %d 提取失败: %s", i + 1, e)
            continue

    # 基于 content 相似度简单去重
    seen = set()
    deduped = []
    for item in all_items:
        key = item["content"][:100]
        if key not in seen:
            seen.add(key)
            deduped.append(item)

    logger.info("提取完成: %d 条（去重前 %d 条）", len(deduped), len(all_items))
    return deduped


def extract_fiction_inspiration(text: str) -> list[dict]:
    """专门提取小说灵感的便捷方法"""
    items = extract_memories_with_retry(text)
    return [i for i in items if i.get("category") == "fiction_inspiration"]


TOPIC_MATCH_SYSTEM_PROMPT = """你是记忆卷宗归类助手。卷宗是同一主题/项目/故事线的记忆集合。

给定一条新记忆和若干候选卷宗，判断新记忆属于哪个卷宗。

规则：
1. 如果新记忆与某个卷宗属于同一主题/项目/故事，返回该卷宗的 topic_id
2. 如果都不匹配，返回 none
3. 只返回 topic_id 或 "none"，不要任何其他文字或 Markdown
"""


def match_topic_via_llm(content: str, keywords: list,
                        candidates: list) -> Optional[str]:
    """LLM 判断新记忆属于哪个候选卷宗。返回 topic_id 或 None。"""
    if not candidates:
        return None

    cand_text = "\n".join(
        f"{c['topic_id']}: {c['title']}" for c in candidates
    )
    prompt = (
        f"新记忆内容：\n{content}\n\n"
        f"候选卷宗：\n{cand_text}\n\n"
        f"新记忆属于哪个卷宗？只返回 topic_id 或 none。"
    )

    raw = _call_deepseek_api(prompt, system_prompt=TOPIC_MATCH_SYSTEM_PROMPT)
    result = raw.strip().strip('"\'`').strip()

    valid_ids = {c["topic_id"] for c in candidates}
    if result in valid_ids:
        return result
    if result.lower() == "none":
        return None
    # 兜底：LLM 可能返回带额外文字的 topic_id，尝试从结果里提取
    for tid in valid_ids:
        if tid in result:
            return tid
    return None
