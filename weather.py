"""天气（和风天气 QWeather 免费 API）。

注意：和风天气自 2026 年起停用公共 API 域名（geoapi/devapi.qweather.com），
改为每个账号独立的 API Host（控制台-设置 中查看，形如 xxx.xy.qweatherapi.com）。
需在 .env 配置 QWEATHER_API_HOST。
"""
import json

import httpx

from ...config import Settings
from ..registry import Tool


async def _get_weather(args: dict, settings: Settings) -> str:
    key = settings.qweather_api_key
    if not key:
        return "未配置和风天气 API Key（QWEATHER_API_KEY），无法查询天气"
    host = settings.qweather_api_host or "geoapi.qweather.com"
    base = f"https://{host}"
    city = str(args.get("city") or settings.home_city).strip()
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            geo = await client.get(
                f"{base}/geo/v2/city/lookup",
                params={"location": city, "key": key},
            )
        except Exception as e:
            return f"天气服务连接失败：{e!r}"
        if geo.status_code != 200:
            return (
                f"城市查询失败（HTTP {geo.status_code}）：{geo.text[:200]}"
                + ("（如为 404 空响应：2026 年起和风已停用公共域名，请在 .env 配置 QWEATHER_API_HOST，"
                   "值见 console.qweather.com 控制台-设置）" if geo.status_code == 404 else "")
            )
        try:
            locs = geo.json().get("location") or []
        except Exception:
            return f"城市查询响应异常：{geo.text[:200]}"
        if not locs:
            return f"未找到城市：{city}"
        loc_id, loc_name = locs[0]["id"], locs[0]["name"]
        now_resp = await client.get(
            f"{base}/v7/weather/now", params={"location": loc_id, "key": key}
        )
        day_resp = await client.get(
            f"{base}/v7/weather/3d", params={"location": loc_id, "key": key}
        )
        # 3d 接口没有降水概率（pop），只有 24h 逐时有；取每小时 pop 的当日最大值
        hour_resp = await client.get(
            f"{base}/v7/weather/24h", params={"location": loc_id, "key": key}
        )
        if now_resp.status_code != 200 or day_resp.status_code != 200:
            return (
                f"天气数据查询失败（HTTP {now_resp.status_code}/{day_resp.status_code}）："
                f"{now_resp.text[:200]}"
            )
    pop_by_day = {}
    try:
        for h in (hour_resp.json().get("hourly", []) if hour_resp.status_code == 200 else []):
            fx = (h.get("fxTime") or "")[:10]
            try:
                pop = int(h.get("pop") or 0)
            except Exception:
                pop = 0
            if fx:
                pop_by_day[fx] = max(pop_by_day.get(fx, 0), pop)
    except Exception:
        pop_by_day = {}

    try:
        now = now_resp.json().get("now", {})
        daily = [
            {
                "date": d.get("fxDate"),
                "day": d.get("textDay"),
                "night": d.get("textNight"),
                "temp_min": d.get("tempMin"),
                "temp_max": d.get("tempMax"),
                "pop": pop_by_day.get(d.get("fxDate")),   # 降水概率 %（来自 24h）
                "precip": d.get("precip"),                # 降水量 mm
                "wind": ("%s %s级" % (d.get("windDirDay"), d.get("windScaleDay"))) if d.get("windDirDay") else None,
            }
            for d in day_resp.json().get("daily", [])
        ]
    except Exception:
        return "天气数据解析失败"
    return json.dumps(
        {
            "city": loc_name,
            "now": {
                "temp": now.get("temp"),
                "text": now.get("text"),
                "humidity": now.get("humidity"),
                "wind_dir": now.get("windDir"),
            },
            "forecast": daily,
        },
        ensure_ascii=False,
    )


def tools(settings: Settings) -> list[Tool]:
    return [
        Tool(
            name="get_weather",
            description="查询指定城市（默认湖州市吴兴区，用户在某高校）的实时天气和未来 3 天预报。",
            parameters={
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "城市或校区名，如 湖州、某高校、吴兴；缺省使用用户默认城市（湖州）"}
                },
            },
            handler=lambda a: _get_weather(a, settings),
        ),
    ]
