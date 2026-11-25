"""复杂度分类器测试：启发式快速通道 + LLM 判官。"""
from core.models.classifier import classify_turn, heuristic_quick_decision
from core.models.provider import ChatResult


class FakeRouter:
    """判官假路由器：返回预设的判定词。"""

    def __init__(self, verdict="simple", fail=False):
        self.verdict = verdict
        self.fail = fail
        self.calls = []

    async def chat(self, messages, tools=None, temperature=0.7, max_tokens=None, tier="strong"):
        self.calls.append(tier)
        if self.fail:
            raise RuntimeError("judge down")
        return ChatResult(
            content=self.verdict, tool_calls=None, provider="fake", model="fake",
            prompt_tokens=10, completion_tokens=2, finish_reason="stop",
        )


def test_greetings_are_simple():
    for text in ["你好", "hi", "在吗", "谢谢", "早上好", "好的"]:
        assert heuristic_quick_decision(text) == "simple", text


def test_short_question_is_complex():
    assert heuristic_quick_decision("今天会下雨吗？") == "complex"


def test_task_keywords_are_complex():
    for text in ["帮我写一篇总结", "分析一下这个数据", "写个 Python 脚本", "对比 A 和 B"]:
        assert heuristic_quick_decision(text) == "complex", text


def test_long_text_is_complex():
    assert heuristic_quick_decision("这是一段很长的文字。" * 30) == "complex"


def test_ambiguous_returns_none():
    assert heuristic_quick_decision("我昨天去了趟超市，买了很多东西") is None


async def test_llm_judge_simple():
    # 只有「13~200 字、且不含工具/任务关键词」的模糊句才会走到判官。
    # 含「天气」的句子在启发式里直接判 complex（要真实数据，走强档），是本意。
    router = FakeRouter("simple")
    assert await classify_turn(router, "我昨天去了趟超市，买了很多东西", judge_enabled=True) == "simple"
    assert router.calls == ["cheap"]  # 判官用便宜档


async def test_llm_judge_complex():
    router = FakeRouter("complex")
    assert await classify_turn(router, "帮我规划一个周末旅行", judge_enabled=True) == "complex"


async def test_judge_failure_falls_back_to_complex():
    router = FakeRouter(fail=True)
    text = "我昨天去了趟超市买了很多东西，感觉那里的水果都挺新鲜的"
    assert await classify_turn(router, text, judge_enabled=True) == "complex"


async def test_judge_disabled_ambiguous_goes_complex():
    router = FakeRouter("simple")
    text = "我昨天去了趟超市买了很多东西，感觉那里的水果都挺新鲜的"
    assert await classify_turn(router, text, judge_enabled=False) == "complex"
    assert router.calls == []
