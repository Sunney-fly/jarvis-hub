"""优惠 / 权益信息模块。

核心约束（用户明确要求）：**绝不在服务器上登录用户账号** —— 那会把手机端挤下线。
所以分两层采集，都不需要账号密码：

  L1「公共优惠」：服务端定期从公开渠道采集（银行/出行/外卖/会员/通信/电商活动），
                 进知识库集合「优惠信息」。
  L2「账号专属权益」：由手机端读屏或截图/分享触发（本地发起、零登录），
                 结构化后上传到同一个集合。

提问时两层一起检索，按「当时可用 + 更划算」给建议；过期条目定期清理。
"""
import json
import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from .config import Settings
from .db import KbDocument

logger = logging.getLogger("core.promotions")

PROMO_COLLECTION = "优惠信息"

# L1 采集源：(分类名, 采集搜索词, 初始间隔小时)
DEFAULT_SOURCES: list[tuple[str, str, float]] = [
    ("银行信用卡", "银行 信用卡 优惠活动 本月 薅羊毛", 24.0),
    ("出行", "打车 优惠券 领取 最新活动", 12.0),
    ("餐饮外卖", "外卖 红包 优惠券 领取", 12.0),
    ("影音会员", "视频 音乐 会员 优惠 活动", 48.0),
    ("通信", "运营商 流量 套餐 优惠活动", 72.0),
    ("电商", "电商 补贴 优惠券 攻略", 12.0),
]
DEFAULT_TOPICS = [(kw, name) for name, kw, _ in DEFAULT_SOURCES]   # 兼容旧调用

MIN_INTERVAL = 6.0      # 最快 6 小时一次（活动更新再快也没必要更勤）
MAX_INTERVAL = 168.0    # 最慢 7 天一次（长期没动静的源）

# 结构化抽取提示词（支持图片或纯文本）
STRUCT_PROMPT = """你在帮用户整理"账号里能领的优惠/权益"。下面是{kind}。

请提取出所有**用户可以领取或使用的优惠权益**，只输出 JSON（不要任何解释、不要 Markdown 代码块）：

{{
  "source": "来源平台名，例如 网易云音乐",
  "items": [
    {{
      "name": "权益名，例如 cdf中免会员购38元券",
      "value": "价值/力度，例如 满39-38、128元券包",
      "condition": "适用条件或门槛，例如 VIP专享、满100可用",
      "expiry": "有效期，未知就写 未知",
      "entry": "在哪领/怎么用，例如 会员中心-会员福利-去使用"
    }}
  ]
}}

要求：
- 宁少勿滥：只提取明确的优惠权益，忽略广告位、推荐位、导航
- 不确定的字段写"未知"，不要编造
- 如果内容里没有任何优惠权益，输出 {{"source": "", "items": []}}

{content}"""


def _now() -> datetime:
    return datetime.now(timezone(timedelta(hours=8)))


def _extract_json(text: str) -> dict:
    """从模型输出里抠出 JSON（容错：可能带 ```json 包裹或前后废话）"""
    if not text:
        return {}
    s = text.strip()
    s = re.sub(r"^```(?:json)?|```$", "", s, flags=re.MULTILINE).strip()
    try:
        return json.loads(s)
    except Exception:  # noqa: BLE001
        m = re.search(r"\{.*\}", s, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:  # noqa: BLE001
                return {}
    return {}


def _to_markdown(source: str, items: list[dict], origin: str, collected_at: str) -> str:
    lines = [
        f"# {source or '未知来源'} · 可领权益（{origin}，采集于 {collected_at}）",
        "",
        "| 权益 | 价值 | 条件 | 有效期 | 入口 |",
        "|---|---|---|---|---|",
    ]
    for it in items:
        lines.append(
            "| {name} | {value} | {condition} | {expiry} | {entry} |".format(
                name=(it.get("name") or "").replace("|", "/")[:60],
                value=(it.get("value") or "未知").replace("|", "/")[:40],
                condition=(it.get("condition") or "未知").replace("|", "/")[:40],
                expiry=(it.get("expiry") or "未知").replace("|", "/")[:30],
                entry=(it.get("entry") or "未知").replace("|", "/")[:60],
            )
        )
    lines.append("")
    lines.append("原始数据（供程序读取）：")
    lines.append("```json")
    lines.append(json.dumps({"source": source, "items": items}, ensure_ascii=False))
    lines.append("```")
    return "\n".join(lines)


async def capture(
    settings: Settings,
    router,
    embedder,
    session_factory,
    image: bytes | None = None,
    text: str | None = None,
    source: str = "",
    mime: str = "image/jpeg",
) -> dict:
    """把一页"会员中心/优惠列表"（图片或文字）结构化成权益条目并入库。"""
    from . import vision
    from .knowledge import store as kb

    if not image and not (text or "").strip():
        raise ValueError("需要提供图片或文字")

    if image:
        prompt = STRUCT_PROMPT.format(kind="一张手机截屏（优惠/权益页面）", content="")
        result = await vision.analyze_image(settings, image, mime=mime, prompt=prompt, max_tokens=1500)
        raw = result.text
        origin = "截图采集"
    else:
        prompt = STRUCT_PROMPT.format(kind="一段优惠信息文本", content=(text or "")[:6000])
        if router is None:
            raise RuntimeError("没有可用模型")
        result = await router.chat(
            messages=[{"role": "user", "content": prompt}], temperature=0.1,
            max_tokens=1500, tier="cheap",
        )
        raw = result.content or ""
        origin = "文本采集"

    data = _extract_json(raw)
    items = data.get("items") or []
    src = (source or data.get("source") or "未知来源").strip()[:60]
    if not items:
        return {"ok": False, "reason": "没识别出优惠权益", "raw": raw[:300], "source": src}

    collected = _now().strftime("%Y-%m-%d %H:%M")
    md = _to_markdown(src, items, origin, collected)
    info = await kb.add_document(
        session_factory,
        embedder,
        PROMO_COLLECTION,
        f"{src} 可领权益（{collected}）",
        md,
        source_url="",
        tags=f"优惠,权益,{src}",
        origin="manual",
    )
    logger.info("优惠入库 source=%s items=%d", src, len(items))
    return {"ok": True, "source": src, "count": len(items), "items": items, **info}


async def ensure_sources(session_factory) -> int:
    """把默认采集源写进表（首次初始化）"""
    from .db import PromoSource

    async with session_factory() as s:
        rows = (await s.execute(select(PromoSource))).scalars().all()
        existing = {r.name for r in rows}
        added = 0
        for name, kw, interval in DEFAULT_SOURCES:
            if name not in existing:
                s.add(PromoSource(name=name, keywords=kw, interval_hours=interval))
                added += 1
        if added:
            await s.commit()
    return added


async def due_sources(session_factory, force: bool = False) -> list:
    """挑出到点该采的源（自适应间隔；force=True 全部采一遍）"""
    from .db import PromoSource

    await ensure_sources(session_factory)
    now = datetime.now(timezone.utc)
    async with session_factory() as s:
        rows = (
            await s.execute(select(PromoSource).where(PromoSource.enabled.is_(True)))
        ).scalars().all()
        due = []
        for r in rows:
            if force or r.last_run_at is None:
                due.append(r)
                continue
            last = r.last_run_at
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            hours = (now - last).total_seconds() / 3600.0
            if hours >= float(r.interval_hours or 24.0):
                due.append(r)
        return due


async def refresh_due(
    settings: Settings, router, embedder, session_factory,
    force: bool = False, max_pages: int = 2,
) -> dict:
    """自适应采集：只跑到点的源，并依据「这次有没有新内容」调整该源的采集间隔。

    规律：有更新 → 间隔 × 0.75（更勤，抓热点）；连续没动静 → 间隔 × 1.5（更省）。
    """
    from .db import PromoSource
    from .knowledge import harvest as kb_harvest
    from .knowledge import store as kb

    await kb.ensure_collection(session_factory, PROMO_COLLECTION,
                               description="公共优惠 + 账号权益（自动采集）")
    due = await due_sources(session_factory, force=force)
    async with session_factory() as s:
        total_sources = len((await s.execute(select(PromoSource))).scalars().all())

    out = {
        "checked": len(due),
        "skipped_not_due": max(0, total_sources - len(due)),
        "total_added": 0,
        "sources": [],
    }
    for src in due:
        try:
            r = await kb_harvest.harvest(
                settings, router, embedder, session_factory,
                src.keywords, collection=PROMO_COLLECTION, max_pages=max_pages,
            )
            added = len(r.get("added") or [])
        except Exception as e:  # noqa: BLE001
            logger.warning("优惠采集失败 %s: %s", src.name, e)
            added = 0

        async with session_factory() as s:
            row = (
                await s.execute(select(PromoSource).where(PromoSource.id == src.id))
            ).scalar_one_or_none()
            if row is not None:
                row.last_run_at = datetime.now(timezone.utc)
                row.last_new_count = added
                row.total_new = (row.total_new or 0) + added
                old = float(row.interval_hours or 24.0)
                if added > 0:
                    row.consecutive_empty = 0
                    row.interval_hours = max(MIN_INTERVAL, round(old * 0.75, 1))
                else:
                    row.consecutive_empty = (row.consecutive_empty or 0) + 1
                    row.interval_hours = min(MAX_INTERVAL, round(old * 1.5, 1))
                await s.commit()
                out["sources"].append({
                    "name": src.name,
                    "added": added,
                    "interval_hours": row.interval_hours,
                    "consecutive_empty": row.consecutive_empty,
                })
        out["total_added"] += added

    out["kb_total"] = await kb.stats(session_factory)
    return out


async def list_sources(session_factory) -> list[dict]:
    """看看每个源的"更新规律"学成什么样了"""
    from .db import PromoSource

    await ensure_sources(session_factory)
    async with session_factory() as s:
        rows = (await s.execute(select(PromoSource).order_by(PromoSource.name))).scalars().all()
    return [
        {
            "name": r.name,
            "keywords": r.keywords,
            "interval_hours": r.interval_hours,
            "last_run_at": r.last_run_at.isoformat() if r.last_run_at else "",
            "last_new_count": r.last_new_count,
            "consecutive_empty": r.consecutive_empty,
            "total_new": r.total_new,
            "enabled": r.enabled,
        }
        for r in rows
    ]


async def refresh(
    settings: Settings,
    router,
    embedder,
    session_factory,
    topics: list[tuple[str, str]] | None = None,
    max_pages: int = 2,
) -> dict:
    """按指定主题采集（不传主题则走自适应调度）"""
    if topics is None:
        return await refresh_due(settings, router, embedder, session_factory, force=True, max_pages=max_pages)
    from .knowledge import harvest as kb_harvest
    from .knowledge import store as kb

    await kb.ensure_collection(session_factory, PROMO_COLLECTION, description="公共优惠 + 账号权益（自动采集）")
    summary = {"topics": [], "added": 0, "skipped": 0}
    for topic, tag in topics:
        try:
            r = await kb_harvest.harvest(
                settings, router, embedder, session_factory,
                topic, collection=PROMO_COLLECTION, max_pages=max_pages,
            )
            added = len(r.get("added") or [])
            summary["topics"].append({"topic": topic, "tag": tag, "added": added,
                                      "searched": r.get("searched"), "skipped": r.get("skipped_irrelevant")})
            summary["added"] += added
            summary["skipped"] += int(r.get("skipped_irrelevant") or 0)
        except Exception as e:  # noqa: BLE001
            logger.warning("优惠采集失败 %s: %s", topic, e)
            summary["topics"].append({"topic": topic, "tag": tag, "error": str(e)[:120]})
    summary["kb_total"] = await kb.stats(session_factory)
    return summary


async def list_offers(session_factory, limit: int = 30) -> dict:
    """列出已知的优惠/权益条目"""
    from .knowledge import store as kb

    docs = await kb.list_documents(session_factory, PROMO_COLLECTION, limit=limit)
    return {"collection": PROMO_COLLECTION, "count": len(docs), "documents": docs}


async def prune(session_factory, days: int = 60) -> dict:
    """清理过期的公共优惠（按采集时间，账号权益也一并清理，避免给出过时建议）"""
    from .knowledge import store as kb

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    async with session_factory() as s:
        rows = (
            await s.execute(
                select(KbDocument).where(KbDocument.collection == PROMO_COLLECTION)
            )
        ).scalars().all()
    removed = 0
    for doc in rows:
        created = doc.created_at
        if created is not None and created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if created is None or created < cutoff:
            await kb.delete_document(session_factory, doc.id)
            removed += 1
    return {"collection": PROMO_COLLECTION, "removed": removed, "kept": len(rows) - removed, "older_than_days": days}


async def briefing(session_factory, limit: int = 20) -> str:
    """生成一份"当前可用优惠"简报文本（可推送）"""
    docs = (await list_offers(session_factory, limit))["documents"]
    if not docs:
        return "目前还没有采集到优惠信息。可以对贾维斯说「去网上找找最近的优惠活动」。"
    lines = [f"【优惠简报 {_now().strftime('%m-%d')}】共 {len(docs)} 条来源：", ""]
    for d in docs[:limit]:
        lines.append(f"· {d['title']}")
    lines.append("")
    lines.append("想问具体某一个，直接说；例如「打车有什么优惠」「我会员里能领什么」。")
    return "\n".join(lines)
