"""工具装配：把内置工具按依赖注入注册进注册表。"""
from ..config import Settings
from .builtin import browser, calendar, email, knowledge, notes, reminder, search, weather
from .registry import Tool, ToolRegistry


def build_registry(
    settings: Settings,
    session_factory,
    scheduler=None,
    router_getter=None,
    embedder_getter=None,
) -> ToolRegistry:
    registry = ToolRegistry()
    collections: list[list[Tool]] = [
        search.tools(settings),
        weather.tools(settings),
        calendar.tools(settings),
        email.tools(settings),
        notes.tools(settings, session_factory),
        reminder.tools(settings, session_factory, scheduler),
        browser.tools(settings),
    ]
    if settings.kb_enabled:
        collections.append(
            knowledge.tools(
                settings,
                session_factory,
                router_getter or (lambda: None),
                embedder_getter or (lambda: None),
            )
        )
    for group in collections:
        for tool in group:
            registry.register(tool)
    return registry
