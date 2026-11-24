"""jarvis-core 主入口：OpenAI 兼容接口（chat/models/audio）+ 管理接口。

对外（Open WebUI / 手机端）暴露：
- POST /v1/chat/completions    对话（Agent 工具循环 + 记忆注入 + 流式可选）
- GET  /v1/models              可用模型列表
- POST /v1/audio/transcriptions 语音转写（STT）
- POST /v1/audio/speech        语音合成（TTS）

管理接口（均需 AUTH_TOKEN）：
- GET  /healthz               健康检查（公开）
- GET  /v1/cost/summary       月度成本汇总
- GET  /v1/memory/facts       查看长期记忆
- DELETE /v1/memory/facts/{id} 删除记忆
"""
import asyncio
import base64
import hmac
import ipaddress
import json
import logging
import time
import traceback
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select, text

from . import accounting, db, phone_agent, promotions, vision
from . import auth as auth_core
from .agent.loop import AgentResult, run_agent
from .config import get_settings
from .knowledge import harvest as kb_harvest
from .knowledge import store as kb_store
from .memory import extractor, store as memory_store
from .models.classifier import classify_turn
from .models.provider import EmbeddingClient, ProviderRouter
from .push import configured as push_configured
from .relay import RelayHub
from .scheduler.jobs import SchedulerService
from .tools.bootstrap import build_registry
from .tools.dynamic.manager import DynamicToolsManager
from .voice import stt as stt_mod
from .voice import tts as tts_mod

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("core")

settings = get_settings()


def _ensure_calendar() -> None:
    """确保 Radicale 日历集合存在（失败不影响启动）。"""
    try:
        from .tools.builtin.calendar import _calendar  # noqa: PLC2701

        _calendar(settings)
        logger.info("日历集合已就绪")
    except Exception:
        logger.warning("初始化日历失败（稍后用到时会重试）: %s", traceback.format_exc(limit=1))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 数据库（失败降级：聊天照常，记忆/记账暂不可用）
    try:
        await db.init_db()
        logger.info("数据库已初始化")
    except Exception:
        logger.exception("数据库初始化失败，进入降级运行模式")

    # 模型路由
    try:
        cheap_spec = settings.cheap_spec()
        app.state.router = ProviderRouter(
            settings.enabled_providers(), cheap_specs=[cheap_spec] if cheap_spec else []
        )
        logger.info(
            "模型路由: 强档=%s 便宜档=%s",
            [p["name"] for p in app.state.router.providers],
            [p["name"] for p in app.state.router.cheap_providers] or "（未配置，退化为主档）",
        )
    except Exception as e:
        logger.error("模型路由初始化失败: %s", e)
        app.state.router = None

    # Embedding（可选）
    app.state.embedder = None
    if settings.siliconflow_api_key:
        app.state.embedder = EmbeddingClient(
            settings.siliconflow_base_url, settings.siliconflow_api_key, settings.embed_model
        )

    # 调度器
    app.state.scheduler = SchedulerService(settings, db.SessionLocal, lambda: _agent_deps(app))
    try:
        await app.state.scheduler.start()
    except Exception:
        logger.exception("调度器启动失败（简报/提醒暂不可用）")

    # 工具注册表（依赖 scheduler）
    app.state.registry = build_registry(
        settings,
        db.SessionLocal,
        app.state.scheduler,
        lambda: app.state.router,
        lambda: app.state.embedder,
    )

    # 动态工具层：配置式(YAML) + MCP + 自我写码
    app.state.dynamic = None
    try:
        app.state.dynamic = DynamicToolsManager(
            settings, app.state.registry, lambda: app.state.router
        )
        await app.state.dynamic.start()
    except Exception:
        logger.exception("动态工具层启动失败（内置工具不受影响）")

    _ensure_calendar()
    yield
    app.state.scheduler.shutdown()
    if app.state.dynamic is not None:
        try:
            await app.state.dynamic.shutdown()
        except Exception:
            logger.exception("动态工具层关闭失败")


def _agent_deps(app: FastAPI):
    return (app.state.router, app.state.registry, app.state.embedder)


app = FastAPI(title="jarvis-core", version="0.1.0", lifespan=lifespan)

# 手机 ↔ 电脑端 relay（内存队列；uvicorn 单 worker，见 Dockerfile）
app.state.relay = RelayHub()


# ---------------------------------------------------------------- 鉴权
# 认证失败计数（防暴力破解）：{来源IP: [失败时间戳]}
_auth_fails: dict[str, list[float]] = defaultdict(list)
_AUTH_FAIL_WINDOW = 300.0   # 统计窗口 5 分钟
_AUTH_FAIL_LIMIT = 20       # 窗口内失败超过此数则临时封禁

# 危险接口允许的来源网段：Tailscale(100.64/10) + 容器/内网
_PRIVATE_NETS = tuple(
    ipaddress.ip_network(n)
    for n in ("100.64.0.0/10", "172.16.0.0/12", "10.0.0.0/8", "192.168.0.0/16", "127.0.0.0/8")
)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


async def require_auth(request: Request) -> None:
    """鉴权：账号会话 token（新）或 AUTH_TOKEN（兼容旧脚本/旧设备），失败限流。"""
    host = _client_ip(request)
    auth = request.headers.get("authorization", "")
    if settings.auth_token and hmac.compare_digest(auth, f"Bearer {settings.auth_token}"):
        _auth_fails.pop(host, None)   # 正确令牌：清零失败计数
        return
    if auth.startswith("Bearer "):
        token = auth[len("Bearer "):].strip()
        if token:
            uid = await auth_core.resolve_session(db.SessionLocal, token)
            if uid:
                _auth_fails.pop(host, None)
                request.state.user_id = uid
                return
    # 既没配令牌也没建账号时保持旧的开放行为（仅内网自用场景）
    if not settings.auth_token:
        try:
            if await auth_core.count_users(db.SessionLocal) == 0:
                return
        except Exception:
            pass
    now = time.time()
    fails = [t for t in _auth_fails[host] if now - t < _AUTH_FAIL_WINDOW]
    fails.append(now)
    _auth_fails[host] = fails
    if len(fails) > _AUTH_FAIL_LIMIT:
        logger.warning("已封禁来源=%s（5分钟内失败 %d 次）", host, len(fails))
        raise HTTPException(status_code=429, detail="too many failed attempts, try later")
    logger.warning("鉴权失败 来源=%s 5分钟内失败=%d次", host, len(fails))
    raise HTTPException(status_code=401, detail="invalid token")


async def require_admin(request: Request) -> None:
    """危险操作（安装/审批工具、删除记忆）需要管理员口令头 X-Jarvis-Admin。

    注意：公网请求会经本机 HTTPS 代理转发，核心看到的来源 IP 会变成内网地址，
    因此不能依赖来源网段判断，必须用独立口令（.env 的 ADMIN_SECRET）。
    """
    secret = settings.admin_secret
    if not secret:
        await require_private(request)   # 未配置口令时退回按来源网段限制
        return
    given = request.headers.get("x-jarvis-admin", "")
    if not hmac.compare_digest(given, secret):
        logger.warning("已拦截危险接口访问 来源=%s 路径=%s", _client_ip(request), request.url.path)
        raise HTTPException(status_code=403, detail="admin secret required")


async def require_private(request: Request) -> None:
    """危险操作（安装/审批工具、删除记忆）只允许私有网络来源。

    这样即使令牌在公网上被嗅探，攻击者也无法借"自装工具"在服务器上执行代码。
    """
    host = _client_ip(request)
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        raise HTTPException(status_code=403, detail="forbidden")
    if not any(ip in net for net in _PRIVATE_NETS):
        logger.warning("已拦截来自公网的危险接口访问 来源=%s 路径=%s", host, request.url.path)
        raise HTTPException(status_code=403, detail="restricted to private network")


# ---------------------------------------------------------------- 数据模型
class ChatMessage(BaseModel):
    role: str
    content: Any = None
    name: Optional[str] = None


class ChatRequest(BaseModel):
    model: str = ""
    messages: list[ChatMessage]
    tools: Optional[list[dict]] = None
    stream: bool = False
    temperature: Optional[float] = None
    model_config = {"extra": "ignore"}


class SpeechRequest(BaseModel):
    input: str
    voice: str = ""
    model: str = ""
    model_config = {"extra": "ignore"}


def _client_messages(req: ChatRequest) -> list[dict]:
    return [m.model_dump(exclude_none=True) for m in req.messages]


def _last_user_text(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            content = m.get("content")
            if isinstance(content, str):
                return content
    return ""


def _openai_response(result: AgentResult) -> dict:
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": result.final.model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": result.final.content or ""},
                "finish_reason": result.final.finish_reason or "stop",
            }
        ],
        "usage": {
            "prompt_tokens": result.usage["prompt_tokens"],
            "completion_tokens": result.usage["completion_tokens"],
            "total_tokens": result.usage["prompt_tokens"] + result.usage["completion_tokens"],
        },
    }


def _chunk(delta: dict | None, finish: str | None, model: str) -> dict:
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}],
    }


async def _sse_stream(result: AgentResult, temperature: float | None, tier: str = "strong") -> Any:
    """最终答案流式输出。

    stream_mode=instant（默认）：直接按块吐出循环里已生成的答案——零额外调用，最快最省。
    stream_mode=live：再调一次模型做真流式（首字更快，但多一次生成、总耗时和成本翻倍）。
    """
    temp = temperature if temperature is not None else 0.7
    first = _chunk({"role": "assistant"}, None, result.final.model)
    yield f"data: {json.dumps(first, ensure_ascii=False)}\n\n"

    if settings.stream_mode != "live":
        content = result.final.content or ""
        for i in range(0, len(content), 24):
            piece = content[i : i + 24]
            yield f"data: {json.dumps(_chunk({'content': piece}, None, result.final.model), ensure_ascii=False)}\n\n"
        last = _chunk({}, "stop", result.final.model)
        yield f"data: {json.dumps(last, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"
        return

    streamed = False
    try:
        router: ProviderRouter = app.state.router
        msgs = list(result.messages) + [
            {"role": "system", "content": "请直接输出最终答案，不要调用工具。"}
        ]
        async for piece in router.stream(msgs, temperature=temp, tier=tier):
            streamed = True
            yield f"data: {json.dumps(_chunk({'content': piece['delta']}, None, piece['model']), ensure_ascii=False)}\n\n"
    except Exception:
        logger.exception("流式输出失败，回退到已生成的答案")
    if not streamed:
        content = result.final.content or ""
        yield f"data: {json.dumps(_chunk({'content': content}, None, result.final.model), ensure_ascii=False)}\n\n"
    last = _chunk({}, "stop", result.final.model)
    yield f"data: {json.dumps(last, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"


# ---------------------------------------------------------------- 对话
@app.post("/v1/chat/completions", dependencies=[Depends(require_auth)])
async def chat_completions(req: ChatRequest):
    if app.state.router is None:
        return JSONResponse(
            status_code=503,
            content={"error": {"message": "没有可用的模型 provider，请检查 .env", "type": "no_provider"}},
        )
    messages = _client_messages(req)
    if not any(m.get("role") == "user" and m.get("content") for m in messages):
        raise HTTPException(status_code=400, detail="至少需要一条 user 消息")

    # 记忆注入（Embedding 未配置时跳过）
    memory_facts: list[str] = []
    if app.state.embedder is not None:
        try:
            query = _last_user_text(messages)
            if query:
                vectors, usage = await app.state.embedder.embed([query])
                pairs = await memory_store.search_facts(
                    db.SessionLocal, vectors[0], k=settings.memory_top_k
                )
                memory_facts = [c for c, _sim in pairs]
                try:
                    await accounting.record_embed(db.SessionLocal, usage)
                except Exception:
                    pass
        except Exception:
            logger.exception("记忆检索失败，本次对话跳过记忆")

    # 知识库自动检索（always-on RAG）：不依赖模型自觉调 kb_search
    if settings.kb_auto_rag and app.state.embedder is not None:
        try:
            q = _last_user_text(messages)
            if q:
                hits = await kb_store.search(
                    db.SessionLocal, app.state.embedder, q,
                    collection=None, top_k=settings.kb_auto_rag_top_k,
                    min_similarity=settings.kb_min_similarity,
                )
                if hits:
                    ctx = "\n\n".join("【%s】%s" % (h["title"], (h["content"] or "")[:600])
                                      for h in hits)
                    messages = [{
                        "role": "system",
                        "content": ("以下是从用户知识库检索到的片段（用户自己环境的实测记录）。"
                                    "回答前先看这些片段：与本轮问题相关就以它们为准（它们可能与你"
                                    "训练的常识冲突，以片段为准）；不相关就忽略，不要强行套用。\n\n" + ctx),
                    }] + messages
                    logger.info("KB 自动注入 %d 条", len(hits))
        except Exception:
            logger.exception("知识库自动检索失败，本次跳过")

    # 智能路由：判断这条指令走便宜档还是强档（省 token）
    tier = "strong"
    if settings.model_policy == "smart":
        user_text = _last_user_text(messages)
        if user_text:
            try:
                kind = await classify_turn(
                    app.state.router, user_text, settings.classifier_enabled
                )
                tier = "cheap" if kind == "simple" else "strong"
                logger.info("智能路由: %.40s → %s", user_text, tier)
            except Exception:
                logger.exception("分类失败，保守走强档")

    # Agent 主循环
    try:
        result = await run_agent(
            app.state.router,
            app.state.registry,
            messages,
            memory_facts=memory_facts,
            max_steps=settings.max_agent_steps,
            temperature=req.temperature,
            tz=settings.tz,
            tier=tier,
        )
    except Exception as e:
        logger.exception("Agent 执行失败")
        return JSONResponse(
            status_code=502,
            content={"error": {"message": str(e), "type": "agent_error"}},
        )

    # 记账 + 审计落库（失败不阻塞响应）
    try:
        await accounting.record_usage(
            db.SessionLocal,
            result.usage["provider"],
            result.usage["model"],
            result.usage["prompt_tokens"],
            result.usage["completion_tokens"],
        )
    except Exception:
        logger.exception("记账失败")
    try:
        new_msgs = result.messages[1 + len(messages):]
        await db.store_exchange(db.SessionLocal, new_msgs)
    except Exception:
        logger.exception("审计落库失败")

    # 后台记忆抽取
    if app.state.embedder is not None:
        user_text = _last_user_text(messages)
        if user_text:
            asyncio.create_task(
                extractor.extract_and_store(
                    app.state.router, app.state.embedder, db.SessionLocal, user_text, settings
                )
            )

    if req.stream:
        return StreamingResponse(
            _sse_stream(result, req.temperature, tier), media_type="text/event-stream"
        )
    return JSONResponse(_openai_response(result))


# ---------------------------------------------------------------- 模型列表
@app.get("/v1/models", dependencies=[Depends(require_auth)])
async def list_models():
    providers = app.state.router.providers if app.state.router else []
    return {
        "object": "list",
        "data": [
            {"id": p["name"], "object": "model", "owned_by": "jarvis", "detail": p["model"]}
            for p in providers
        ],
    }


# ---------------------------------------------------------------- 语音
@app.post("/v1/audio/transcriptions", dependencies=[Depends(require_auth)])
async def transcribe(file: UploadFile = File(...), model: str = Form("")):
    data = await file.read()
    if len(data) > 25 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="音频过大（上限 25MB）")
    try:
        text = await stt_mod.transcribe(settings, file.filename or "audio.mp3", data)
    except Exception as e:
        logger.exception("STT 失败")
        return JSONResponse(status_code=502, content={"error": {"message": f"STT 失败: {e!r}"}})
    return {"text": text}


@app.post("/v1/audio/speech", dependencies=[Depends(require_auth)])
async def speech(req: SpeechRequest):
    if len(req.input) > 2000:
        raise HTTPException(status_code=413, detail="文本过长（上限 2000 字）")
    try:
        audio = await tts_mod.synthesize(settings, req.input)
    except Exception as e:
        logger.exception("TTS 失败")
        return JSONResponse(status_code=502, content={"error": {"message": f"TTS 失败: {e!r}"}})
    return Response(content=audio, media_type="audio/mpeg")


# ---------------------------------------------------------------- 视觉分析
class VisionRequest(BaseModel):
    image_base64: str
    prompt: str = ""
    mime: str = "image/jpeg"


@app.post("/v1/vision/analyze", dependencies=[Depends(require_auth)])
async def vision_analyze(req: VisionRequest):
    """把一张图片交给视觉模型分析（手机拍照/截屏、电脑截屏都走这个接口）"""
    if not vision.configured(settings):
        raise HTTPException(status_code=503, detail="未配置视觉模型（缺 SILICONFLOW_API_KEY）")
    raw_b64 = req.image_base64.split(",")[-1].strip()
    try:
        raw = base64.b64decode(raw_b64)
    except Exception:
        raise HTTPException(status_code=400, detail="图片 base64 解析失败")
    if not raw:
        raise HTTPException(status_code=400, detail="图片为空")
    if len(raw) > settings.vision_max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"图片过大（{len(raw) // 1024}KB，上限 {settings.vision_max_bytes // 1024}KB）",
        )
    try:
        result = await vision.analyze_image(
            settings, raw, req.mime or "image/jpeg", req.prompt or None
        )
    except Exception as e:
        logger.exception("视觉分析失败")
        return JSONResponse(status_code=502, content={"error": {"message": f"视觉分析失败: {e!r}"}})
    return result.as_dict()


# ---------------------------------------------------------------- 代操作（视觉 → 动作）
class PhoneNextRequest(BaseModel):
    goal: str
    plan: str = ""
    history: list[str] = []
    image_base64: str
    mime: str = "image/jpeg"


@app.post("/v1/phone/next", dependencies=[Depends(require_auth)])
async def phone_next(req: PhoneNextRequest):
    """手机代操作一步：截图 + 目标 + 最近动作 → 下一步动作（执行在 App 侧）。"""
    if not vision.configured(settings):
        raise HTTPException(status_code=503, detail="未配置视觉模型（缺 SILICONFLOW_API_KEY）")
    goal = (req.goal or "").strip()
    if not goal:
        raise HTTPException(status_code=400, detail="goal 不能为空")
    raw_b64 = req.image_base64.split(",")[-1].strip()
    try:
        raw = base64.b64decode(raw_b64)
    except Exception:
        raise HTTPException(status_code=400, detail="图片 base64 解析失败")
    if not raw:
        raise HTTPException(status_code=400, detail="图片为空")
    if len(raw) > settings.vision_max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"图片过大（{len(raw) // 1024}KB，上限 {settings.vision_max_bytes // 1024}KB）",
        )
    try:
        return await phone_agent.next_step(
            settings, goal, req.plan, req.history, raw, req.mime or "image/jpeg"
        )
    except Exception as e:
        logger.exception("代操作决策失败")
        return JSONResponse(status_code=502, content={"error": {"message": f"代操作决策失败: {e!r}"}})


# ---------------------------------------------------------------- 管理接口
DEMO_HTML = Path(__file__).parent / "static" / "demo.html"


@app.get("/demo", include_in_schema=False)
async def demo_page():
    """内置演示页：无需前端框架，浏览器直接和中枢对话。"""
    return FileResponse(DEMO_HTML)


# ---------------------------------------------------------------- 动态工具管理
@app.get("/v1/tools", dependencies=[Depends(require_auth)])
async def list_tools():
    tools = app.state.registry.list_tools()
    return {
        "count": len(tools),
        "tools": [
            {"name": t.name, "description": t.description, "source": t.source}
            for t in tools
        ],
    }


@app.post("/v1/tools/reload", dependencies=[Depends(require_auth), Depends(require_admin)])
async def reload_tools():
    if app.state.dynamic is None:
        raise HTTPException(status_code=503, detail="动态工具层未启动")
    try:
        return await app.state.dynamic.reload()
    except Exception as e:
        logger.exception("工具重载失败")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/v1/tools/pending", dependencies=[Depends(require_auth), Depends(require_admin)])
async def pending_tools():
    if app.state.dynamic is None:
        raise HTTPException(status_code=503, detail="动态工具层未启动")
    return {"pending": app.state.dynamic.list_pending()}


@app.post("/v1/tools/pending/{pid}/approve", dependencies=[Depends(require_auth), Depends(require_admin)])
async def approve_pending_tool(pid: str):
    if app.state.dynamic is None:
        raise HTTPException(status_code=503, detail="动态工具层未启动")
    try:
        tool = await app.state.dynamic.approve_pending(pid)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("批准工具失败")
        raise HTTPException(status_code=500, detail=str(e))
    return {"approved": tool.name, "description": tool.description, "source": tool.source}


@app.delete("/v1/tools/pending/{pid}", dependencies=[Depends(require_auth), Depends(require_admin)])
async def reject_pending_tool(pid: str):
    if app.state.dynamic is None:
        raise HTTPException(status_code=503, detail="动态工具层未启动")
    if not app.state.dynamic.reject_pending(pid):
        raise HTTPException(status_code=404, detail="待批准工具不存在")
    return {"rejected": pid}


@app.delete("/v1/tools/{name}", dependencies=[Depends(require_auth), Depends(require_admin)])
async def uninstall_tool(name: str):
    if app.state.dynamic is None:
        raise HTTPException(status_code=503, detail="动态工具层未启动")
    try:
        result = app.state.dynamic.uninstall(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    return result


@app.get("/healthz")
async def healthz():
    db_ok = False
    try:
        async with db.SessionLocal() as s:
            await s.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        pass
    providers = [p["name"] for p in app.state.router.providers] if app.state.router else []
    return {
        "status": "ok" if db_ok else "degraded",
        "db": db_ok,
        "providers": providers,
        "push": push_configured(settings),
        "time": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------- App 更新（App 内自动提示新版本）
@app.get("/v1/app/version")
async def app_version():
    """公开：App 首次安装还没有令牌时也要能查到新版本（只回版本号与下载地址）。"""
    """App 启动时查最新版本；没发布过就返回 versionCode=0（App 视为最新）。"""
    empty = {"versionCode": 0, "versionName": "", "apkUrl": "", "note": "", "size": 0, "sha256": ""}
    meta = Path(settings.app_release_dir) / "app-release.json"
    if not meta.is_file():
        return empty
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return empty
        data.setdefault("apkUrl", "/v1/app/download")
        return data
    except Exception:
        logger.warning("app-release.json 解析失败，按「无更新」处理")
        return empty


@app.get("/v1/app/download", dependencies=[Depends(require_auth)])
async def app_download():
    """下载最新 APK（App 内升级用；需要 AUTH_TOKEN）。"""
    apk = Path(settings.app_release_dir) / "app.apk"
    if not apk.is_file():
        raise HTTPException(status_code=404, detail="还没有发布过 APK")
    return FileResponse(apk, media_type="application/vnd.android.package-archive",
                        filename="jarvis-latest.apk")


# ---------------------------------------------------------------- 账号 / 登录
_login_fails: dict[str, list[float]] = defaultdict(list)


def _login_guard(host: str) -> None:
    now = time.time()
    fails = [t for t in _login_fails.get(host, []) if now - t < 300]
    _login_fails[host] = fails
    if len(fails) >= 8:
        raise HTTPException(status_code=429, detail="登录尝试过于频繁，5 分钟后再试")


class AuthRegisterRequest(BaseModel):
    username: str
    password: str
    install_code: str = ""


class AuthLoginRequest(BaseModel):
    username: str
    password: str


class AuthPasswordRequest(BaseModel):
    old_password: str
    new_password: str


@app.get("/v1/auth/status")
async def auth_status():
    """公开：App 用来自查「有没有账号 / 能不能注册」。"""
    n = await auth_core.count_users(db.SessionLocal)
    return {"has_user": n > 0, "can_register": n == 0}


@app.post("/v1/auth/register")
async def auth_register(req: AuthRegisterRequest, request: Request):
    """注册（不再需要安装码）：只在还没有账号时开放一次，注册成功后即锁死。"""
    host = _client_ip(request)
    _login_guard(host)
    if await auth_core.count_users(db.SessionLocal) > 0:
        raise HTTPException(status_code=409, detail="账号已存在，请直接登录")
    username = (req.username or "").strip()
    if not auth_core.valid_username(username):
        raise HTTPException(status_code=400, detail="用户名 3–32 位，仅字母数字和 ._-")
    if len(req.password or "") < 6:
        raise HTTPException(status_code=400, detail="密码至少 6 位")
    await auth_core.create_user(db.SessionLocal, username, req.password)
    token, exp, uname = await auth_core.login(db.SessionLocal, username, req.password)
    _login_fails.pop(host, None)
    logger.info("账号已注册：%s", username)
    return {"token": token, "expiresAt": exp.isoformat(), "username": uname}


@app.post("/v1/auth/login")
async def auth_login(req: AuthLoginRequest, request: Request):
    host = _client_ip(request)
    _login_guard(host)
    res = await auth_core.login(db.SessionLocal, (req.username or "").strip(), req.password or "")
    if not res:
        _login_fails.setdefault(host, []).append(time.time())
        raise HTTPException(status_code=401, detail="用户名或密码不对")
    token, exp, uname = res
    _login_fails.pop(host, None)
    return {"token": token, "expiresAt": exp.isoformat(), "username": uname}


@app.get("/v1/auth/me", dependencies=[Depends(require_auth)])
async def auth_me(request: Request):
    uid = getattr(request.state, "user_id", None)
    if not uid:
        return {"username": "legacy", "expiresAt": ""}
    async with db.SessionLocal() as s:
        u = await s.get(db.User, uid)
    return {"username": u.username if u else "", "expiresAt": ""}


@app.post("/v1/auth/logout", dependencies=[Depends(require_auth)])
async def auth_logout(request: Request):
    auth = request.headers.get("authorization", "")
    token = auth[len("Bearer "):].strip() if auth.startswith("Bearer ") else ""
    if token and getattr(request.state, "user_id", None):
        await auth_core.revoke_session(db.SessionLocal, token)
    return {"ok": True}


@app.post("/v1/auth/password", dependencies=[Depends(require_auth)])
async def auth_password(req: AuthPasswordRequest, request: Request):
    uid = getattr(request.state, "user_id", None)
    if not uid:
        raise HTTPException(status_code=400, detail="仅账号登录后可改密码")
    if len(req.new_password or "") < 6:
        raise HTTPException(status_code=400, detail="新密码至少 6 位")
    ok = await auth_core.change_password(
        db.SessionLocal, uid, req.old_password or "", req.new_password or ""
    )
    if not ok:
        raise HTTPException(status_code=403, detail="原密码不对")
    return {"ok": True, "relogin": True}


@app.post("/v1/app/release")
async def app_release(request: Request, versionCode: int = 0, versionName: str = "",
                      sha256: str = "", note: str = ""):
    """HTTP 发布 APK（走 443，避开 Tailscale 中继）。需要 APP_UPLOAD_SECRET 头。"""
    secret = settings.app_upload_secret
    if not secret:
        raise HTTPException(status_code=503, detail="未配置 APP_UPLOAD_SECRET，HTTP 发布通道未启用")
    given = request.headers.get("x-jarvis-upload", "")
    if not hmac.compare_digest(given, secret):
        raise HTTPException(status_code=403, detail="upload secret required")
    # 说明文字走头（URL 编码），避免中文进 query 被 HTTP 解析器拒绝
    note_hdr = request.headers.get("x-jarvis-note", "")
    if note_hdr:
        from urllib.parse import unquote as _unquote
        note = _unquote(note_hdr)
    body = await request.body()
    if len(body) < 1_000_000:
        raise HTTPException(status_code=400, detail="文件太小，不像是 APK")
    if len(body) > 120_000_000:
        raise HTTPException(status_code=413, detail="文件过大（上限 120MB）")
    import hashlib as _hashlib
    got = _hashlib.sha256(body).hexdigest()
    if sha256 and not hmac.compare_digest(got, sha256.strip().lower()):
        raise HTTPException(status_code=400, detail=f"sha256 不一致：服务器算出 {got[:12]}…")
    rel = Path(settings.app_release_dir)
    rel.mkdir(parents=True, exist_ok=True)
    tmp = rel / "app.apk.part"
    tmp.write_bytes(body)
    tmp.replace(rel / "app.apk")
    site = Path("/app/download/jarvis.apk")
    try:
        site.parent.mkdir(parents=True, exist_ok=True)
        site.write_bytes(body)
    except Exception as e:  # noqa: BLE001
        logger.warning("写入网站下载目录失败（只更新了 App 更新包）：%s", e)
    meta = {
        "versionCode": versionCode,
        "versionName": versionName,
        "apkUrl": "https://your-domain.example.com/download/jarvis.apk",
        "size": len(body),
        "sha256": got,
        "note": note,
        "publishedAt": datetime.now(timezone.utc).isoformat(),
    }
    (rel / "app-release.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info("HTTP 发布完成：%s (%s) %.1fMB", versionName, versionCode, len(body) / 1048576)
    return {"ok": True, **meta}


class SystemEventRequest(BaseModel):
    kind: str
    payload: dict = {}


@app.post("/v1/system/event", dependencies=[Depends(require_auth)])
async def system_event(req: SystemEventRequest):
    """PC 端（磁盘清理等）上报事件，简报据此决定要不要写一句。"""
    async with db.SessionLocal() as s:
        s.add(db.SystemEvent(kind=req.kind[:32], payload=req.payload or {}))
        await s.commit()
    logger.info("系统事件：%s %s", req.kind, str(req.payload)[:200])
    return {"ok": True}


@app.get("/v1/cost/summary", dependencies=[Depends(require_auth)])
async def cost_summary(month: str | None = None):
    try:
        return await accounting.month_summary(db.SessionLocal, month)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("成本汇总失败")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------- 网站留言（公开接口）
_comment_last: dict[str, float] = {}   # 每个 IP 上次发帖时间，用于限频
_COMMENT_INTERVAL = 20.0               # 同一 IP 最少间隔（秒）


class CommentRequest(BaseModel):
    name: str = ""
    content: str


@app.get("/v1/comments")
async def list_comments(limit: int = 50):
    """公开：读取最新留言（个人站用）"""
    limit = max(1, min(100, limit))
    try:
        async with db.SessionLocal() as s:
            rows = (
                await s.execute(
                    select(db.Comment)
                    .where(db.Comment.approved.is_(True))
                    .order_by(db.Comment.created_at.desc())
                    .limit(limit)
                )
            ).scalars().all()
    except Exception as e:
        logger.exception("读取留言失败")
        raise HTTPException(status_code=500, detail=str(e))
    return {
        "count": len(rows),
        "items": [
            {
                "id": r.id,
                "name": r.name,
                "content": r.content,
                "created_at": r.created_at.isoformat() if r.created_at else "",
            }
            for r in rows
        ],
    }


@app.post("/v1/comments")
async def create_comment(req: CommentRequest, request: Request):
    """公开：发表留言（限频 + 长度限制 + 拒绝 HTML）"""
    content = (req.content or "").strip()
    name = (req.name or "").strip()[:20]
    if not content:
        raise HTTPException(status_code=400, detail="内容不能为空")
    if len(content) > 500:
        raise HTTPException(status_code=413, detail="留言太长了（上限 500 字）")
    if "<" in content or "<" in name:
        raise HTTPException(status_code=400, detail="不支持 HTML 标签")
    if not name:
        name = "匿名"

    ip = _client_ip(request) or "unknown"
    now = time.time()
    last = _comment_last.get(ip, 0.0)
    if now - last < _COMMENT_INTERVAL:
        wait = int(_COMMENT_INTERVAL - (now - last)) + 1
        raise HTTPException(status_code=429, detail=f"发得太快了，请等 {wait} 秒")
    _comment_last[ip] = now

    ip_hash = hmac.new(b"jarvis-comment", ip.encode(), "sha256").hexdigest()[:16]
    try:
        async with db.SessionLocal() as s:
            row = db.Comment(name=name, content=content, ip_hash=ip_hash)
            s.add(row)
            await s.commit()
            await s.refresh(row)
            item = {
                "id": row.id,
                "name": row.name,
                "content": row.content,
                "created_at": row.created_at.isoformat() if row.created_at else "",
            }
    except Exception as e:
        logger.exception("保存留言失败")
        raise HTTPException(status_code=500, detail=str(e))
    logger.info("新留言 来自=%s 名字=%s 长度=%d", ip, name, len(content))
    return {"ok": True, "item": item}


@app.delete("/v1/comments/{cid}", dependencies=[Depends(require_auth), Depends(require_admin)])
async def delete_comment(cid: int):
    """管理：删除一条留言（需要管理员口令）"""
    try:
        async with db.SessionLocal() as s:
            row = await s.get(db.Comment, cid)
            if row is None:
                raise HTTPException(status_code=404, detail="没有这条留言")
            await s.delete(row)
            await s.commit()
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("删除留言失败")
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True, "deleted": cid}


# ---------------------------------------------------------------- 外挂知识库（RAG）
class KbSearchRequest(BaseModel):
    query: str
    collection: str | None = None
    top_k: int = 6


class KbAddRequest(BaseModel):
    content: str
    title: str = ""
    collection: str = "default"
    source_url: str = ""
    tags: str = ""


class KbHarvestRequest(BaseModel):
    topic: str
    collection: str | None = None
    max_pages: int = 5
    background: bool = True


_harvest_tasks: dict[str, dict] = {}


async def _run_harvest(task_id: str, req: KbHarvestRequest) -> None:
    state = _harvest_tasks[task_id]

    async def progress(stage: str, detail: str = "") -> None:
        state["stage"] = stage
        state["detail"] = detail[:200]
        state["updated_at"] = datetime.now(timezone.utc).isoformat()

    try:
        state["status"] = "running"
        state["result"] = await kb_harvest.harvest(
            settings,
            app.state.router,
            app.state.embedder,
            db.SessionLocal,
            req.topic,
            collection=req.collection or req.topic,
            max_pages=req.max_pages,
            progress=progress,
        )
        state["status"] = "done"
    except Exception as e:  # noqa: BLE001
        logger.exception("知识库采集失败")
        state["status"] = "failed"
        state["error"] = str(e)[:300]
    finally:
        state["finished_at"] = datetime.now(timezone.utc).isoformat()


@app.get("/v1/kb/status", dependencies=[Depends(require_auth)])
async def kb_status():
    """知识库总体情况（知识库数/文档数/切片数）"""
    return await kb_store.stats(db.SessionLocal)


@app.get("/v1/kb/collections", dependencies=[Depends(require_auth)])
async def kb_collections():
    """列出所有外挂知识库"""
    return {"collections": await kb_store.list_collections(db.SessionLocal)}


@app.get("/v1/kb/documents", dependencies=[Depends(require_auth)])
async def kb_documents(collection: str | None = None, limit: int = 50):
    """列出知识库里的文档"""
    return {"documents": await kb_store.list_documents(db.SessionLocal, collection, limit)}


@app.post("/v1/kb/search", dependencies=[Depends(require_auth)])
async def kb_search(req: KbSearchRequest):
    """检索知识库（RAG 召回）"""
    hits = await kb_store.search(
        db.SessionLocal,
        app.state.embedder,
        req.query,
        collection=req.collection,
        top_k=req.top_k or settings.kb_top_k,
        min_similarity=settings.kb_min_similarity,
    )
    return {"found": len(hits), "results": hits}


@app.post("/v1/kb/documents", dependencies=[Depends(require_auth)])
async def kb_add_document(req: KbAddRequest):
    """手动录入一份知识（自动切片 + 向量化）"""
    try:
        info = await kb_store.add_document(
            db.SessionLocal,
            app.state.embedder,
            req.collection,
            req.title,
            req.content,
            source_url=req.source_url,
            tags=req.tags,
            origin="manual",
            chunk_size=settings.kb_chunk_size,
            chunk_overlap=settings.kb_chunk_overlap,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, **info}


@app.post("/v1/kb/harvest", dependencies=[Depends(require_auth)])
async def kb_harvest_start(req: KbHarvestRequest):
    """【自己建知识库】给主题 → 全网搜索 → 筛选整理 → 建成独立知识库

    默认后台执行，立即返回 task_id，用 GET /v1/kb/harvest/{task_id} 查进度。
    """
    if not (req.topic or "").strip():
        raise HTTPException(status_code=400, detail="topic 不能为空")
    task_id = uuid.uuid4().hex[:12]
    _harvest_tasks[task_id] = {
        "task_id": task_id,
        "topic": req.topic,
        "collection": kb_store.normalize_collection(req.collection or req.topic),
        "status": "queued",
        "stage": "queued",
        "detail": "",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    if req.background:
        asyncio.create_task(_run_harvest(task_id, req))
        return {"ok": True, "task_id": task_id, "status": "queued",
                "poll": f"/v1/kb/harvest/{task_id}"}
    await _run_harvest(task_id, req)
    return _harvest_tasks[task_id]


@app.get("/v1/kb/harvest/{task_id}", dependencies=[Depends(require_auth)])
async def kb_harvest_status(task_id: str):
    """查看采集任务进度/结果"""
    state = _harvest_tasks.get(task_id)
    if state is None:
        raise HTTPException(status_code=404, detail="没有这个任务（重启后任务列表会清空）")
    return state


@app.delete("/v1/kb/documents/{doc_id}", dependencies=[Depends(require_auth), Depends(require_admin)])
async def kb_delete_document(doc_id: int):
    """删除一份知识（旧资料换新资料）"""
    return {"ok": True, **(await kb_store.delete_document(db.SessionLocal, doc_id))}


@app.delete("/v1/kb/collections/{name}", dependencies=[Depends(require_auth), Depends(require_admin)])
async def kb_delete_collection(name: str):
    """整个知识库删除"""
    return {"ok": True, **(await kb_store.delete_collection(db.SessionLocal, name))}


# ---------------------------------------------------------------- 优惠 / 权益
class PromoCaptureRequest(BaseModel):
    image_base64: str = ""
    text: str = ""
    source: str = ""
    mime: str = "image/jpeg"


class PromoRefreshRequest(BaseModel):
    topics: list[str] = []
    max_pages: int = 2
    force: bool = False   # True=不看到点没到点，全部采一遍


@app.post("/v1/promo/capture", dependencies=[Depends(require_auth)])
async def promo_capture(req: PromoCaptureRequest):
    """把一页「会员权益/优惠活动」页面（截图或文本）结构化成可检索的权益条目。

    手机端读屏或截图后调用；**不涉及账号登录**，不会把手机端挤下线。
    """
    image = None
    if req.image_base64:
        try:
            image = base64.b64decode(req.image_base64)
        except Exception:  # noqa: BLE001
            raise HTTPException(status_code=400, detail="image_base64 解码失败")
    try:
        return await promotions.capture(
            settings, app.state.router, app.state.embedder, db.SessionLocal,
            image=image, text=req.text, source=req.source, mime=req.mime,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        logger.exception("权益采集失败")
        raise HTTPException(status_code=500, detail=str(e)[:200])


@app.post("/v1/promo/refresh", dependencies=[Depends(require_auth)])
async def promo_refresh(req: PromoRefreshRequest):
    """采集优惠：默认走「自适应调度」（只采到点的源，并按更新规律调整频率）

    不登录任何账号；耗时取决于有几个源到点（每个源约 20~40 秒）。
    """
    if req.topics:
        topics = [(t, t) for t in req.topics]
        return await promotions.refresh(
            settings, app.state.router, app.state.embedder, db.SessionLocal,
            topics=topics, max_pages=max(1, min(4, req.max_pages)),
        )
    return await promotions.refresh_due(
        settings, app.state.router, app.state.embedder, db.SessionLocal,
        force=req.force, max_pages=max(1, min(4, req.max_pages)),
    )


@app.get("/v1/promo/sources", dependencies=[Depends(require_auth)])
async def promo_sources():
    """看看各采集源的「更新规律」学得怎么样了（间隔/上次新增/连续空手次数）"""
    return {"sources": await promotions.list_sources(db.SessionLocal)}


@app.get("/v1/promo/offers", dependencies=[Depends(require_auth)])
async def promo_offers(limit: int = 30):
    """列出已知的优惠/权益"""
    return await promotions.list_offers(db.SessionLocal, limit)


@app.get("/v1/promo/briefing", dependencies=[Depends(require_auth)])
async def promo_briefing(limit: int = 20):
    """生成优惠简报文本（可推送）"""
    return {"text": await promotions.briefing(db.SessionLocal, limit)}


@app.post("/v1/promo/prune", dependencies=[Depends(require_auth), Depends(require_admin)])
async def promo_prune(days: int = 60):
    """清理过期优惠（默认 60 天前的）"""
    return await promotions.prune(db.SessionLocal, days)


@app.get("/v1/memory/facts", dependencies=[Depends(require_auth), Depends(require_admin)])
async def memory_facts(limit: int = 50):
    limit = max(1, min(200, limit))
    try:
        async with db.SessionLocal() as s:
            rows = (
                await s.execute(select(db.Fact).order_by(db.Fact.last_accessed_at.desc()).limit(limit))
            ).scalars().all()
        return {
            "facts": [
                {
                    "id": f.id,
                    "content": f.content,
                    "created_at": f.created_at.isoformat() if f.created_at else None,
                    "last_accessed_at": f.last_accessed_at.isoformat() if f.last_accessed_at else None,
                }
                for f in rows
            ]
        }
    except Exception as e:
        logger.exception("读取记忆失败")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/v1/memory/facts/{fact_id}", dependencies=[Depends(require_auth), Depends(require_admin)])
async def delete_memory_fact(fact_id: int):
    try:
        ok = await memory_store.delete_fact(db.SessionLocal, fact_id)
    except Exception as e:
        logger.exception("删除记忆失败")
        raise HTTPException(status_code=500, detail=str(e))
    if not ok:
        raise HTTPException(status_code=404, detail="记忆不存在")
    return {"deleted": fact_id}


# ---------------------------------------------------------------- 手机 ↔ 电脑端 relay
# 手机把要说的话投到这里，电脑端（jarvis-bridge）长轮询领取、调 DSH、回传结果。
# 公网入口是普通 HTTP 反代，所以用长轮询而不是 WebSocket。
class RelayInbox(BaseModel):
    kind: str = "text"          # text | screenshot
    text: str = ""
    image_b64: str = ""
    speak_on: str = "phone"     # phone | pc


class RelayResult(BaseModel):
    id: str
    full: str = ""
    short: str = ""
    speak_on: str = "phone"
    error: str = ""


RELAY_WAIT = 60.0   # 手机侧最多等多久（秒）；比反代超时短一点更安全


@app.post("/v1/relay/inbox", dependencies=[Depends(require_auth)])
async def relay_inbox(req: RelayInbox):
    """手机投递一条消息，阻塞等电脑端处理完。"""
    hub: RelayHub = app.state.relay
    # 桥刚启动/刚重连时可能还没完成第一次 pull，先给它一点时间再判离线
    deadline = time.time() + 15
    while not hub.pc_online() and time.time() < deadline:
        await asyncio.sleep(0.5)
    if not hub.pc_online():
        return JSONResponse(status_code=503, content={"ok": False, "error": "电脑端不在线"})
    job = await hub.submit(req.model_dump())
    try:
        await asyncio.wait_for(job.done.wait(), timeout=RELAY_WAIT)
    except asyncio.TimeoutError:
        return JSONResponse(
            status_code=504, content={"ok": False, "error": "电脑端超时未回", "id": job.id}
        )
    res = job.result or {}
    return {"ok": not res.get("error"), "id": job.id, **res}


@app.get("/v1/relay/pull", dependencies=[Depends(require_auth)])
async def relay_pull(wait: int = 25):
    """电脑端长轮询：有任务立即返回，没有则挂起到 wait 秒后返回 204。"""
    wait = max(0, min(30, int(wait)))
    job = await app.state.relay.pull(wait)
    if job is None:
        return Response(status_code=204)
    return {"id": job.id, "payload": job.payload}


@app.post("/v1/relay/result", dependencies=[Depends(require_auth)])
async def relay_result(req: RelayResult):
    """电脑端回传结果。"""
    ok = await app.state.relay.complete(req.id, req.model_dump())
    if not ok:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    return {"ok": True}


@app.get("/v1/relay/status", dependencies=[Depends(require_auth)])
async def relay_status():
    """看一眼电脑端在不在线、有没有积压。"""
    return app.state.relay.stats()


# ---------------------------------------------------------------- MCP over HTTP
# 给 Grok Bot 等外部 agent 调：JSON-RPC 2.0，工具复用上面的 relay 队列。
# 挂在 /v1 下是为了复用现有反代与 require_auth，不新增公网端口。
async def _require_mcp_auth(request: Request) -> None:
    """MCP 客户端不一定加 Bearer 前缀（Grok Bot 的 AUTH_TOKEN 字段就是裸 token），两种都认。"""
    auth = request.headers.get("authorization", "")
    if settings.auth_token and hmac.compare_digest(auth, settings.auth_token):
        return
    await require_auth(request)


@app.get("/v1/mcp")
async def mcp_get(request: Request):
    """MCP 客户端挂载前会先 GET 探测/建 SSE 通道。只注册 POST 会被 FastAPI 判 405。"""
    accept = request.headers.get("accept", "").lower()
    if "text/event-stream" in accept:
        return Response(content=": jarvis-core-bridge ready\n\n", media_type="text/event-stream", status_code=200)
    return {
        "ok": True,
        "server": "jarvis-core-bridge",
        "transport": "streamable-http",
        "endpoint": "/v1/mcp",
        "hint": "JSON-RPC 2.0 over POST /v1/mcp",
    }


@app.post("/v1/mcp", dependencies=[Depends(_require_mcp_auth)])
async def mcp_endpoint(payload: dict):
    from . import mcp as mcp_core

    resp = await mcp_core.handle(payload, app.state.relay)
    if resp is None:
        return Response(status_code=204)
    return resp
