"""自我写码：助手为自己生成新工具（生成 → 安全闸 → 审查 → 冒烟测试 → 注册）。

生成与审查都走主模型路由（review 时换一个调用，实现"第二意见"）。
"""
import json
import logging
from dataclasses import dataclass

import httpx

from ...config import Settings

logger = logging.getLogger("core.dynamic.selfcoder")

GENERATE_PROMPT = """为中枢编写一个新工具。
需求：{requirement}{name_hint}
输出严格 JSON（不要 markdown 代码块）：
{{
  "name": "snake_case 工具名",
  "description": "一句话中文描述，说明用途",
  "parameters": {{JSON Schema，type 必须是 object，字段带中文 description}},
  "code": "Python 代码，必须包含 async def handler(args: dict, ctx) -> str",
  "test_args": {{冒烟测试用的安全参数，可以是空对象}}
}}
代码规则：
1. 只允许 import：json、re、math、datetime、zoneinfo、hashlib、uuid、httpx、base64
2. ctx 提供 ctx.settings（所有配置项，可读 API Key，如 ctx.settings.qweather_api_key）和 ctx.http（httpx.AsyncClient）
3. handler 内部必须捕获异常，出错时返回 "错误: ..." 文本，不要抛出异常
4. 需要外部 API 时优先使用 ctx.settings 里已有的 key；没有对应 key 时返回清晰提示
5. 返回给用户的信息要简洁、有中文解释"""

REVIEW_PROMPT = """你是安全审查员，审查以下将被安装到 AI 助手中的工具代码：
```
{code}
```
检查项：危险 import、eval/exec、文件系统访问、网络外的恶意行为、无限循环、窃取配置。
输出严格 JSON：{{"approve": true 或 false, "reason": "一句话理由"}}"""


@dataclass
class ToolContext:
    """传给生成工具的运行时上下文。"""
    settings: Settings
    http: httpx.AsyncClient


def parse_json_output(raw: str | None) -> dict | None:
    """从模型输出中稳健地解析 JSON（容忍 markdown 代码块）。"""
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


async def generate_spec(router, requirement: str, name_hint: str) -> dict:
    hint = f"\n建议的工具名：{name_hint}" if name_hint else ""
    result = await router.chat(
        [
            {
                "role": "user",
                "content": GENERATE_PROMPT.format(requirement=requirement[:500], name_hint=hint),
            }
        ],
        temperature=0.2,
    )
    spec = parse_json_output(result.content)
    if spec is None:
        raise RuntimeError("模型没有返回有效的 JSON 规格")
    return spec


async def review_code(router, code: str) -> tuple[bool, str]:
    result = await router.chat(
        [{"role": "user", "content": REVIEW_PROMPT.format(code=code[:12000])}],
        temperature=0.1,
        tier="cheap",  # 审查任务相对简单，走便宜档省钱
    )
    verdict = parse_json_output(result.content)
    if verdict is None:
        return False, "审查模型未返回有效结果（保守拒绝）"
    return bool(verdict.get("approve")), str(verdict.get("reason") or "")[:200]
