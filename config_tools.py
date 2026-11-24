"""声明式配置工具：tools.d/*.yaml → Tool（http / command 两种）。

YAML 格式（以 http 为例）：
    name: track_express
    description: 查快递物流信息
    parameters:
      type: object
      properties:
        number: {type: string, description: 快递单号}
      required: [number]
    kind: http
    http:
      url: https://api.example.com/track
      method: GET                 # 默认 GET
      headers: {Authorization: "Bearer xxx"}
      params_template: {number: "{number}"}   # {参数名} 占位
      json_body_template: {...}               # POST 时用（可选）
      response_path: ".data"      # 从响应 JSON 中取哪段（可选）
      timeout: 20

command 类型：
    kind: command
    command:
      cmd: "curl -s 'https://api.example.com/x?q={query}'"   # 占位符同 http
      timeout: 30
"""
import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import httpx
import yaml

from ..registry import Tool

logger = logging.getLogger("core.dynamic")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"


def fill_template(template: Any, args: dict) -> Any:
    """把模板中的 {参数名} 替换为实际值。"""
    if isinstance(template, str):
        try:
            return template.format(**args)
        except (KeyError, IndexError):
            return template
    if isinstance(template, dict):
        return {k: fill_template(v, args) for k, v in template.items()}
    if isinstance(template, list):
        return [fill_template(v, args) for v in template]
    return template


def extract_path(data: Any, path: str | None) -> Any:
    """按 '.data.results' 形式从 JSON 中取值；'.' 或空返回整体。"""
    if not path or path.strip() == ".":
        return data
    cur = data
    for part in path.strip(".").split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
    return cur


async def _http_execute(cfg: dict, args: dict) -> str:
    url = fill_template(cfg.get("url", ""), args)
    if not url.startswith(("http://", "https://")):
        return f"错误: 无效的 url 模板，参数: {sorted(args)}"
    headers = fill_template(cfg.get("headers") or {}, args)
    params = fill_template(cfg.get("params_template") or {}, args)
    json_body = cfg.get("json_body_template")
    timeout = float(cfg.get("timeout", 20))
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers={"User-Agent": UA}) as client:
        if json_body is not None:
            resp = await client.request(
                cfg.get("method", "GET").upper(), url, headers=headers, params=params or None,
                json=fill_template(json_body, args),
            )
        else:
            resp = await client.request(
                cfg.get("method", "GET").upper(), url, headers=headers, params=params or None
            )
    if resp.status_code >= 400:
        return json.dumps({"error": f"HTTP {resp.status_code}", "body": resp.text[:500]}, ensure_ascii=False)
    try:
        data = resp.json()
    except Exception:
        data = resp.text[:6000]
    out = extract_path(data, cfg.get("response_path"))
    if isinstance(out, str):
        return out
    return json.dumps(out, ensure_ascii=False)[:8000]


async def _command_execute(cfg: dict, args: dict) -> str:
    cmd = fill_template(cfg.get("cmd", ""), args)
    if not cmd:
        return "错误: command.cmd 未配置"
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=float(cfg.get("timeout", 30)))
        except asyncio.TimeoutError:
            proc.kill()
            return "错误: 命令执行超时"
    except Exception as e:
        return f"错误: 命令启动失败: {e!r}"
    text = (out or err or b"").decode("utf-8", "replace")
    return text[:8000]


def build_config_tool(spec: dict, origin: str) -> Tool | None:
    """把一份 YAML 配置转成 Tool；非法配置返回 None。"""
    name = str(spec.get("name") or "").strip()
    description = str(spec.get("description") or "").strip()
    parameters = spec.get("parameters")
    if not name or not isinstance(parameters, dict):
        logger.warning("配置工具 %s 缺少 name/parameters，跳过", origin)
        return None
    kind = spec.get("kind")
    if kind == "http":
        if not isinstance(spec.get("http"), dict):
            logger.warning("配置工具 %s 缺少 http 配置，跳过", origin)
            return None
        handler = lambda a, c=spec["http"]: _http_execute(c, a)
    elif kind == "command":
        if not isinstance(spec.get("command"), dict):
            logger.warning("配置工具 %s 缺少 command 配置，跳过", origin)
            return None
        handler = lambda a, c=spec["command"]: _command_execute(c, a)
    else:
        logger.warning("配置工具 %s kind 无效（http/command），跳过", origin)
        return None
    return Tool(
        name=name,
        description=description,
        parameters=parameters,
        handler=handler,
        source="config",
        extra={"origin": origin},
    )


def load_config_tools(tools_d_dir: str) -> list[Tool]:
    """扫描 tools.d/*.yaml（不含子目录），返回可注册的工具。"""
    d = Path(tools_d_dir)
    if not d.is_dir():
        return []
    tools = []
    for f in sorted(d.glob("*.yaml")):
        try:
            spec = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            tool = build_config_tool(spec, str(f))
            if tool is not None:
                tools.append(tool)
                logger.info("加载配置工具 %s (%s)", tool.name, f.name)
        except Exception as e:
            logger.warning("加载配置工具失败 %s: %s", f, e)
    return tools
