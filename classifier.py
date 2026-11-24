"""请求复杂度分类器：决定这条指令走便宜档还是强档。

策略（两层）：
1. 启发式快速通道：明显寒暄/短句 → simple；明显任务词（写/分析/代码…）→ complex
2. LLM 判官：拿不准时用便宜档模型做一次微型判断（约几十 token，便宜档通常免费/几分钱）
判官失败时保守默认 complex（宁可多用点 token，不降质量）。
"""
import logging

logger = logging.getLogger("core.classifier")

JUDGE_PROMPT = """判断下面这条用户消息该由哪档模型处理：
- simple：寒暄、日常闲聊、简单事实问答、单步查询（如"你好""现在几点""今天星期几"）
- complex：需要推理、多步任务、创作、写代码、数据分析、深度解释、规划

只输出一个词：simple 或 complex。"""

GREETINGS = {
    "你好", "您好", "hi", "hello", "hey", "在吗", "在不在", "早上好", "中午好", "晚上好",
    "早安", "晚安", "谢谢", "多谢", "辛苦了", "好的", "ok", "收到", "再见", "拜拜", "嗯", "哦",
}
COMPLEX_KEYWORDS = (
    "写一", "帮我写", "生成", "分析", "对比", "比较", "总结", "概括", "规划", "计划",
    "方案", "代码", "程序", "脚本", "debug", "调试", "重构", "review", "审查",
    "深入", "原理", "论文", "报告", "翻译这", "翻译下", "解释为什么", "怎么做", "如何实现",
    "设计", "架构", "优化", "改", "修", "部署", "教程", "教程",
)

# 需要调用工具/实时数据的查询：小模型调工具不稳，一律走强档
TOOL_KEYWORDS = (
    "天气", "气温", "下雨", "温度", "空气质量", "风力", "紫外线", "限行",
    "搜索", "搜一下", "搜搜", "查一下", "帮我查", "查查", "查询", "新闻", "最新",
    "日历", "日程", "安排", "会议", "提醒", "备忘", "便签", "记一下", "记下",
    "邮件", "发邮件", "收件箱", "未读", "股价", "汇率", "快递", "航班",
)


def heuristic_quick_decision(text: str) -> str | None:
    """明确时直接判定，拿不准返回 None（交给 LLM 判官）。"""
    t = text.strip().lower()
    if not t:
        return "simple"
    # 工具类查询优先走强档（保证工具调用成功率，避免小模型瞎编）
    for kw in TOOL_KEYWORDS:
        if kw in t:
            return "complex"
    # 任务词优先：短句里出现"写/分析"等也要走强档
    for kw in COMPLEX_KEYWORDS:
        if kw in t:
            return "complex"
    if len(t) <= 12:
        if t in GREETINGS:
            return "simple"
        if t.endswith("?") or t.endswith("？"):
            return "complex"  # 短问题也可能需要真实信息，走强档保险
        return "simple"
    if len(t) > 200:
        return "complex"
    return None


async def classify_turn(router, text: str, judge_enabled: bool = True) -> str:
    """返回 "simple" | "complex"。"""
    quick = heuristic_quick_decision(text)
    if quick is not None:
        return quick
    if not judge_enabled or router is None:
        return "complex"
    try:
        result = await router.chat(
            [
                {"role": "system", "content": JUDGE_PROMPT},
                {"role": "user", "content": text[:1000]},
            ],
            temperature=0,
            tier="cheap",
        )
    except Exception as e:
        logger.warning("分类判官调用失败，保守走强档: %s", e)
        return "complex"
    answer = (result.content or "").strip().lower()
    if "simple" in answer:
        return "simple"
    return "complex"
