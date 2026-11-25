"""提醒：写入 scheduled_tasks 表，由调度器执行并微信推送。"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ...config import Settings
from ...db import ScheduledTask
from ..registry import Tool


async def _set_reminder(args: dict, settings: Settings, session_factory, scheduler) -> str:
    message = str(args.get("message") or "").strip()
    if not message:
        return "提醒内容不能为空"
    tz = ZoneInfo(settings.tz)
    run_at: datetime | None = None
    if args.get("at"):
        try:
            run_at = datetime.fromisoformat(str(args["at"]))
            if run_at.tzinfo is None:
                run_at = run_at.replace(tzinfo=tz)
        except ValueError:
            return "at 时间格式无效，请用 ISO 8601（如 2025-06-01T21:30:00+08:00）"
    elif args.get("in_minutes"):
        try:
            run_at = datetime.now(tz) + timedelta(minutes=float(args["in_minutes"]))
        except (TypeError, ValueError):
            return "in_minutes 必须是数字"
    else:
        return "需要 at（ISO 时间）或 in_minutes（分钟数）之一"
    if run_at <= datetime.now(tz):
        return "提醒时间必须在未来"
    task = ScheduledTask(kind="reminder", payload={"message": message[:500]}, run_at=run_at)
    async with session_factory() as s:
        s.add(task)
        await s.commit()
        await s.refresh(task)
    try:
        await scheduler.add_task(task)
    except Exception as e:
        return f"提醒已入库但调度失败：{e!r}（重启后会自动恢复调度）"
    return f"已设置提醒「{message}」，时间：{run_at.isoformat()}"


def tools(settings: Settings, session_factory, scheduler) -> list[Tool]:
    return [
        Tool(
            name="set_reminder",
            description="设置定时提醒，到点通过微信推送。at（ISO 8601 带时区）与 in_minutes（N 分钟后）二选一。",
            parameters={
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "提醒内容"},
                    "at": {"type": "string", "description": "提醒时间，ISO 8601，如 2025-06-01T21:30:00+08:00"},
                    "in_minutes": {"type": "number", "description": "多少分钟后提醒"},
                },
                "required": ["message"],
            },
            handler=lambda a: _set_reminder(a, settings, session_factory, scheduler),
        ),
    ]
