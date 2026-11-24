"""记忆抽取：从用户消息中提取长期事实 → 向量化 → 入库（后台执行）。"""
import json
import logging

from .. import accounting
from ..config import Settings
from ..models.provider import EmbeddingClient, ProviderRouter
from . import store

logger = logging.getLogger("core.memory")

EXTRACT_PROMPT = """从用户消息中提取值得长期记住的信息，例如：个人偏好、计划、重要事实、人际关系、居住城市、职业、技能等。
规则：
1. 只提取明确陈述的事实，不要推测；
2. 每项用一句简短的中文陈述（不超过 30 字）；
3. 输出 JSON 数组；没有可记的就输出 []。
只输出 JSON，不要任何其它文字。"""


def _parse_facts(raw: str | None) -> list[str]:
    if not raw:
        return []
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # 兜底：尝试提取第一个 [...] 片段
        start, end = text.find("["), text.rfind("]")
        if start == -1 or end <= start:
            return []
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return []
    if not isinstance(data, list):
        return []
    return [str(f).strip()[:300] for f in data if str(f).strip()][:8]


async def extract_and_store(
    router: ProviderRouter,
    embedder: EmbeddingClient,
    session_factory,
    user_text: str,
    settings: Settings,
) -> None:
    if not user_text or not user_text.strip():
        return
    try:
        result = await router.chat(
            [
                {"role": "system", "content": EXTRACT_PROMPT},
                {"role": "user", "content": user_text[:2000]},
            ],
            temperature=0.1,
            tier="cheap",  # 后台记忆抽取走便宜档，省钱
        )
        await accounting.record_usage(
            session_factory, result.provider, result.model, result.prompt_tokens, result.completion_tokens
        )
        facts = _parse_facts(result.content)
        if not facts:
            return
        vectors, usage = await embedder.embed(facts)
        await accounting.record_embed(session_factory, usage)
        for fact, vec in zip(facts, vectors):
            await store.add_fact(session_factory, fact, vec, settings.memory_similarity_threshold)
        logger.info("记忆抽取完成：%d 条", len(facts))
    except Exception:
        logger.exception("记忆抽取失败（不影响主流程）")
