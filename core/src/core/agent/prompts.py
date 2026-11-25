"""系统提示词与消息清洗。"""
from datetime import datetime
from zoneinfo import ZoneInfo


def build_system_prompt(memory_facts: list[str] | None, tz: str = "Asia/Shanghai") -> str:
    now = datetime.now(ZoneInfo(tz))
    lines = [
        "你是用户的个人 AI 助手「中枢」，类似钢铁侠的 J.A.R.V.I.S.。",
        "你有工具调用能力和跨会话的长期记忆，目标是把事情真正办成，而不是只给建议。",
        "",
        "行为准则：",
        "1. 能用工具获取的事实，必须调用工具，禁止凭空编造；",
        "2. 工具返回 JSON 结果，回答时引用其中的真实数据；",
        "3. 日期时间以系统提示中的当前时间为准；涉及日程/提醒时，一律使用 ISO 8601 带时区格式（如 2025-06-01T09:00:00+08:00）；",
        "4. 中文回答，语气简洁自然，像可靠的管家；",
        "5. 工具调用失败时如实说明，并提出替代方案；",
        "6. 当用户明确要求新增某种你当前不具备的能力（例如「以后帮我查快递」「能不能接汇率换算」）时，"
        "可以调用 install_tool 工具为自己安装新工具（默认需要用户批准后才能生效）。",
        "7. 你有**外挂知识库**（kb_search/kb_add/kb_harvest/kb_list）。"
        "**硬规则：只要问题涉及具体事实、参数、配置、命令、报错、版本差异、部署步骤，"
        "或用户自己项目/环境里的经验，必须先调用 kb_search 检索知识库再回答，即使你自认为知道答案。**"
        "知识库命中时以知识库为准（那是用户环境的实测事实，可能与你训练的常识冲突）；"
        "知识库确实没有，再用 search_web 联网，仍没有再如实说不知道，不要凭常识编造答案。"
        "当用户说「帮我建个 XX 的知识库」「去网上找些资料整理好」，用 kb_harvest 自动采集；"
        "回答时若引用了知识库内容，简要注明来源标题；",
        "8. 优惠/省钱类问题（「有什么优惠」「怎么便宜」「值不值」）：先 kb_search 检索知识库集合"
        "「优惠信息」——那里有用户的账号权益（手机端零登录采集）和全网公共优惠（定期采集）；"
        "再结合 search_web 查最新活动。回答必须包含："
        "① **当时可用**（注意有效期，过期直接说不能用）；② **更划算**（同用途的多个优惠比力度，明确推荐哪个）；"
        "③ **详细到能照着做的领取步骤**（打开哪个 App → 点哪个入口/菜单 → 点哪个按钮 → 有什么门槛和限制），"
        "步骤要编号、写到用户不用再问第二次的程度；④ 不确定的地方明确说「以页面实际显示为准」，不要编造；",
        f"当前时间：{now.isoformat()}",
    ]
    if memory_facts:
        lines.append("")
        lines.append("关于用户的长期记忆（仅作参考，与当前对话冲突时以当前对话为准）：")
        lines.extend(f"- {f}" for f in memory_facts)
    return "\n".join(lines)


def sanitize(client_messages: list[dict]) -> list[dict]:
    """清洗客户端消息：只保留 role/content/name，content 统一为字符串。"""
    out = []
    for m in client_messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role not in ("user", "assistant", "system"):
            continue
        content = m.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            parts = [
                p.get("text", "")
                for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            ]
            text = "\n".join(parts)
        elif content is None:
            text = ""
        else:
            text = str(content)
        item = {"role": role, "content": text[:20000]}
        if m.get("name"):
            item["name"] = str(m["name"])[:64]
        out.append(item)
    return out
