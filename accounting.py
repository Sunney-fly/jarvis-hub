"""成本记账：每次模型调用入账，月度超预算时微信告警一次。"""
import logging
from datetime import datetime, timezone

from sqlalchemy import func, select

from . import db
from .config import get_settings
from .push import push_text

logger = logging.getLogger("core.accounting")


def estimate_cost(provider: str, prompt_tokens: int, completion_tokens: int) -> float:
    settings = get_settings()
    pin, pout = settings.provider_prices(provider)
    return prompt_tokens / 1e6 * pin + completion_tokens / 1e6 * pout


def _month_key(dt: datetime) -> str:
    return dt.strftime("%Y-%m")


async def record_usage(session_factory, provider: str, model: str, prompt_tokens: int, completion_tokens: int) -> None:
    settings = get_settings()
    cost = estimate_cost(provider, prompt_tokens, completion_tokens)
    async with session_factory() as s:
        s.add(
            db.CostEntry(
                provider=provider,
                model=model or "",
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cost_cny=round(cost, 6),
            )
        )
        await s.commit()
    await _check_budget(session_factory, cost)


async def record_embed(session_factory, tokens: int) -> None:
    settings = get_settings()
    cost = tokens / 1e6 * settings.embed_price_per_1m
    async with session_factory() as s:
        s.add(
            db.CostEntry(
                provider="embedding",
                model=settings.embed_model,
                prompt_tokens=tokens,
                completion_tokens=0,
                cost_cny=round(cost, 6),
            )
        )
        await s.commit()
    await _check_budget(session_factory, cost)


async def _check_budget(session_factory, added_cost: float) -> None:
    settings = get_settings()
    if settings.monthly_budget_cny <= 0:
        return
    try:
        now = datetime.now(timezone.utc)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        async with session_factory() as s:
            total = (
                await s.execute(
                    select(func.coalesce(func.sum(db.CostEntry.cost_cny), 0.0)).where(
                        db.CostEntry.created_at >= month_start
                    )
                )
            ).scalar()
        if float(total) > settings.monthly_budget_cny:
            key = f"budget_warned_{_month_key(now)}"
            if await db.get_pref(session_factory, key) is None:
                await push_text(
                    settings,
                    f"⚠️ 本月 API 成本已达 ¥{float(total):.2f}，超过预算 ¥{settings.monthly_budget_cny}",
                    title="成本预警",
                )
                await db.set_pref(session_factory, key, "1")
    except Exception:
        logger.exception("预算检查失败（不影响主流程）")


async def month_summary(session_factory, month: str | None = None) -> dict:
    """月度成本汇总：total + by_provider。month 格式 YYYY-MM，缺省当前月。"""
    now = datetime.now(timezone.utc)
    if month:
        try:
            start = datetime.strptime(month, "%Y-%m").replace(tzinfo=timezone.utc)
        except ValueError as e:
            raise ValueError("month 格式应为 YYYY-MM") from e
    else:
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    async with session_factory() as s:
        rows = (
            await s.execute(
                select(db.CostEntry.provider, db.CostEntry.model,
                       func.sum(db.CostEntry.prompt_tokens),
                       func.sum(db.CostEntry.completion_tokens),
                       func.sum(db.CostEntry.cost_cny))
                .where(db.CostEntry.created_at >= start, db.CostEntry.created_at < end)
                .group_by(db.CostEntry.provider, db.CostEntry.model)
            )
        ).all()
    by_provider: dict[str, float] = {}
    by_model: list[dict] = []
    total = 0.0
    for provider, model, pt, ct, cost in rows:
        cost_f = float(cost or 0)
        total += cost_f
        by_provider[provider] = round(by_provider.get(provider, 0.0) + cost_f, 6)
        by_model.append(
            {
                "provider": provider,
                "model": model,
                "prompt_tokens": int(pt or 0),
                "completion_tokens": int(ct or 0),
                "cost_cny": round(cost_f, 6),
            }
        )
    return {
        "month": start.strftime("%Y-%m"),
        "total_cny": round(total, 4),
        "budget_cny": get_settings().monthly_budget_cny,
        "by_provider": by_provider,
        "by_model": by_model,
    }
