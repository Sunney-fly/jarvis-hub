"""日历（Radicale，CalDAV 协议）。同步库放线程池执行。"""
import asyncio
import json
from datetime import datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from ...config import Settings
from ..registry import Tool

_clients: dict = {}


def _client(settings: Settings):
    from caldav import DAVClient  # 延迟导入，加快启动

    key = (settings.radicale_url, settings.radicale_user, settings.radicale_password)
    if key not in _clients:
        _clients[key] = DAVClient(
            url=settings.radicale_url,
            username=settings.radicale_user,
            password=settings.radicale_password,
        )
    return _clients[key]


def _calendar(settings: Settings):
    client = _client(settings)
    principal = client.principal()
    for cal in principal.calendars():
        if cal.name == settings.radicale_calendar:
            return cal
    return principal.make_calendar(name=settings.radicale_calendar)


def _parse_event(ev) -> dict:
    try:
        ical = ev.icalendar_instance
        vevent = ical.vevent

        def val(attr: str, default: str = "") -> str:
            obj = getattr(vevent, attr, None)
            return str(obj.value) if obj is not None and hasattr(obj, "value") else default

        return {
            "summary": val("summary"),
            "start": val("dtstart"),
            "end": val("dtend"),
            "location": val("location"),
            "description": val("description")[:300],
        }
    except Exception:
        data = getattr(ev, "data", None)
        return {"raw": str(data)[:300]}


def _sync_list(settings: Settings, days: int) -> list[dict]:
    cal = _calendar(settings)
    now = datetime.now(ZoneInfo(settings.tz))
    end = now + timedelta(days=days)
    events = cal.search(start=now, end=end, expand=True)
    return [_parse_event(ev) for ev in events]


def _sync_create(settings: Settings, summary: str, start_iso: str, end_iso: str, description: str) -> None:
    tz = ZoneInfo(settings.tz)
    start = datetime.fromisoformat(start_iso)
    end = datetime.fromisoformat(end_iso)
    if start.tzinfo is None:
        start = start.replace(tzinfo=tz)
    if end.tzinfo is None:
        end = end.replace(tzinfo=tz)
    if end <= start:
        raise ValueError("结束时间必须晚于开始时间")
    clean_summary = summary.replace("\r", " ").replace("\n", " ").replace(",", "，")[:200]
    clean_desc = description.replace("\r", " ").replace("\n", " ")[:1000]
    ical_text = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//jarvis-core//CN\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:{uuid4().hex}@jarvis\r\n"
        f"DTSTART:{start:%Y%m%dT%H%M%S}\r\n"
        f"DTEND:{end:%Y%m%dT%H%M%S}\r\n"
        f"SUMMARY:{clean_summary}\r\n"
        f"DESCRIPTION:{clean_desc}\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    _calendar(settings).save_event(ical_text)


async def _list_handler(args: dict, settings: Settings) -> str:
    try:
        days = max(1, min(31, int(args.get("days") or 7)))
    except (TypeError, ValueError):
        days = 7
    try:
        events = await asyncio.to_thread(_sync_list, settings, days)
    except Exception as e:
        return f"查询日历失败：{e!r}"
    return json.dumps({"events": events}, ensure_ascii=False)


async def _create_handler(args: dict, settings: Settings) -> str:
    summary = str(args.get("summary") or "").strip()
    start = str(args.get("start") or "").strip()
    end = str(args.get("end") or "").strip()
    if not (summary and start and end):
        return "缺少参数：summary、start、end（ISO 8601 时间）均必填"
    description = str(args.get("description") or "")
    try:
        await asyncio.to_thread(_sync_create, settings, summary, start, end, description)
    except Exception as e:
        return f"创建日程失败：{e!r}"
    return f"已创建日程「{summary}」：{start} ~ {end}"


def tools(settings: Settings) -> list[Tool]:
    return [
        Tool(
            name="list_calendar_events",
            description="查询未来 N 天（默认 7 天）的日历日程。",
            parameters={
                "type": "object",
                "properties": {"days": {"type": "integer", "description": "未来几天，默认 7，最大 31"}},
            },
            handler=lambda a: _list_handler(a, settings),
        ),
        Tool(
            name="create_calendar_event",
            description="在日历中创建日程。start/end 用 ISO 8601 带时区格式（如 2025-06-01T14:00:00+08:00）。",
            parameters={
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "日程标题"},
                    "start": {"type": "string", "description": "开始时间，ISO 8601"},
                    "end": {"type": "string", "description": "结束时间，ISO 8601"},
                    "description": {"type": "string", "description": "备注（可选）"},
                },
                "required": ["summary", "start", "end"],
            },
            handler=lambda a: _create_handler(a, settings),
        ),
    ]
