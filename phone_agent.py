"""手机代操作（阶段 0）：视觉 → 动作闭环的「一步」。

设计参考 ESP32 iPhone Agent（Henry Yuan）的 see-think-act：planner 写计划、actor 每轮看
截图给 1–4 个动作、每步回读「画面变没变」。这里改成安卓 + 中文 + 严格 JSON，方便手机侧解析。

只做决策，不碰手机：截屏与执行都在 App 侧。
"""
import json
import logging
import re

from .config import Settings
from . import vision

logger = logging.getLogger("core.phone")

ACTIONS = {"tap", "swipe", "type", "back", "home", "wait", "done", "fail"}
MAX_ACTIONS = 4
MAX_STEPS_HINT = 12

INSTRUCTIONS = """你在远程操作一台安卓手机，只能看到当前这一张截图，任务是：{goal}

坐标用 0–1000 的相对值（x 从左到右，y 从上到下），瞄准元素的视觉中心，不要瞄准边缘或文字标签。
只看截图里真实存在的元素，绝不猜位置；没看到就 fail 并说明你看到了什么。
历史里写了「画面没变」就说明上一步点空了：换更准的位置或换种办法，不要原样重试。
滑动没有惯性：短滑多次比一次长滑稳（100–250 单位）。
只在输入框有焦点（键盘弹出）时才 type；输入中文也没问题。
一次可以给多个动作，但必须互不依赖（例如连点几个同类勾选项）；会打开新界面的动作只能给一个。
完成后给 done，answer 用一句话说结果；做不到给 fail，reason 说明卡在哪。

当前计划：{plan}
最近动作（{n} 条）：
{history}

只输出一行严格 JSON，不要 markdown 代码块、不要解释：
{{"think":"上一步生效了吗，现在做什么、为什么","plan":"还没有计划就写不超过 6 步的计划，已有计划填空字符串",
 "actions":[{{"do":"tap","x":500,"y":800}}]}}
可用 do：tap(x,y,precise 可选) / swipe(x1,y1,x2,y2) / type(text) / back / home / wait / done(answer) / fail(reason)"""


def _num(v, lo=0, hi=1000) -> int:
    try:
        n = int(round(float(v)))
    except (TypeError, ValueError):
        n = 0
    return max(lo, min(hi, n))


def _text(v, limit=300) -> str:
    return str(v or "")[:limit].strip()


def sanitize(actions) -> list[dict]:
    """只保留白名单动作，坐标钳到 0–1000，最多 4 个。"""
    out: list[dict] = []
    if not isinstance(actions, list):
        return out
    for a in actions[:MAX_ACTIONS]:
        if not isinstance(a, dict):
            continue
        do = str(a.get("do") or "").strip().lower()
        if do not in ACTIONS:
            continue
        item: dict = {"do": do}
        if do == "tap":
            item["x"], item["y"] = _num(a.get("x")), _num(a.get("y"))
            if a.get("precise"):
                item["precise"] = True
        elif do == "swipe":
            for k in ("x1", "y1", "x2", "y2"):
                item[k] = _num(a.get(k))
        elif do == "type":
            item["text"] = _text(a.get("text"), 200)
        elif do == "done":
            item["answer"] = _text(a.get("answer"), 300)
        elif do == "fail":
            item["reason"] = _text(a.get("reason"), 300)
        out.append(item)
    return out


def parse_reply(text: str) -> dict:
    """从模型回复里抠出 JSON；失败返回 {"error": 原因, "raw": 原文截断}。"""
    raw = (text or "").strip()
    body = raw
    fence = re.search(r"```(?:json)?\s*(.*?)```", raw, re.S)
    if fence:
        body = fence.group(1).strip()
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end <= start:
        return {"error": "模型没有给出 JSON", "raw": raw[:500]}
    try:
        data = json.loads(body[start:end + 1])
    except Exception as e:
        return {"error": f"JSON 解析失败: {e}", "raw": raw[:500]}
    if not isinstance(data, dict):
        return {"error": "JSON 不是对象", "raw": raw[:500]}
    return data


async def next_step(settings: Settings, goal: str, plan: str, history: list[str],
                    image_bytes: bytes, mime: str = "image/jpeg") -> dict:
    """一次 see-think-act：截图 + 目标 + 最近动作 → think/plan/actions。"""
    hist = "\n".join(f"- {h}" for h in (history or [])[-8:]) or "（无，这是第一步）"
    prompt = INSTRUCTIONS.format(goal=goal, plan=plan or "（还没计划）",
                                 n=len(history or []), history=hist)
    result = await vision.analyze_image(settings, image_bytes, mime, prompt=prompt, max_tokens=700)
    data = parse_reply(result.text)
    out = {
        "think": _text(data.get("think"), 400),
        "plan": _text(data.get("plan"), 600),
        "actions": sanitize(data.get("actions")),
        "done": False,
        "answer": "",
        "model": result.model,
        "latency_ms": result.latency_ms,
        "usage": {
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
        },
    }
    for a in out["actions"]:
        if a["do"] == "done":
            out["done"], out["answer"] = True, a.get("answer", "")
        elif a["do"] == "fail":
            out["done"], out["answer"] = True, a.get("reason", "失败")
    if "error" in data:
        out["error"] = data["error"]
        out["raw"] = data.get("raw", "")
    if not out["actions"]:
        out["error"] = out.get("error") or "模型没给出可用动作"
    logger.info("代操作一步 goal=%s actions=%s done=%s 耗时=%dms",
                goal[:40], [a["do"] for a in out["actions"]], out["done"], result.latency_ms)
    return out
