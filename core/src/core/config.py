"""配置系统：全部来自环境变量（compose 的 env_file=.env）。"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ---- 基础 ----
    tz: str = "Asia/Shanghai"
    auth_token: str = ""
    # 一次性安装码：App 注册账号时必须带上它，避免「谁先下载 APK 谁抢注账号」
    install_code: str = ""
    # 登录会话有效期（天）
    session_days: int = 90
    # HTTP 上传发布口令（走 443，绕开 Tailscale 中继；未配置则禁用该通道）
    app_upload_secret: str = ""
    # 危险接口（安装/审批工具、删除记忆）的额外管理员口令；
    # 公网请求经 HTTPS 代理转发后来源 IP 会变成内网地址，故必须用独立口令而非网段判断
    admin_secret: str = ""

    # ---- 数据库 ----
    database_url: str = "postgresql+asyncpg://jarvis:jarvis@localhost:5432/jarvis"

    # ---- 模型 ----
    primary_model: str = "deepseek"
    backup_models: str = "qwen,glm"
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_model: str = "deepseek-chat"
    qwen_api_key: str = ""
    qwen_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    qwen_model: str = "qwen-plus"
    glm_api_key: str = ""
    glm_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    glm_model: str = "glm-4-plus"

    # ---- Embedding / STT ----
    siliconflow_api_key: str = ""
    siliconflow_base_url: str = "https://api.siliconflow.cn/v1"
    embed_model: str = "BAAI/bge-m3"
    stt_provider: str = "siliconflow"
    stt_model: str = "FunAudioLLM/SenseVoiceSmall"

    # ---- TTS ----
    tts_provider: str = "edge"  # edge（免费）| minimax（更自然）
    edge_voice: str = "zh-CN-YunxiNeural"
    minimax_api_key: str = ""
    minimax_group_id: str = ""
    minimax_voice_id: str = "male-qn-qingse"

    # ---- 推送 ----
    wxpusher_app_token: str = ""
    wxpusher_uids: str = ""

    # ---- 视觉 ----
    vision_model: str = "Qwen/Qwen3-VL-32B-Instruct"
    vision_max_bytes: int = 4_000_000   # 单张图片上限 4MB

    # ---- 工具 ----
    qweather_api_key: str = ""
    qweather_api_host: str = ""  # 2026 起和风要求使用独立 API Host（控制台-设置 查看）
    home_city: str = "湖州"  # 默认天气城市（某高校位于湖州市吴兴区）
    searxng_url: str = "http://searxng:8080"
    # 国内服务器只能连通部分搜索引擎（google/duckduckgo/brave 不通，baidu 有验证码）
    searxng_engines: str = "sogou,360search,yandex"
    radicale_url: str = "http://radicale:5232/"
    radicale_user: str = "jarvis"
    radicale_password: str = ""
    radicale_calendar: str = "jarvis"
    browser_url: str = "http://browser:8080"
    app_release_dir: str = "release"  # App 更新发布目录（挂载 ./release）：app.apk + app-release.json
    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_pass: str = ""
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_pass: str = ""
    mail_from: str = ""

    # ---- 行为 ----
    max_agent_steps: int = 8
    briefing_time: str = "08:00"
    daily_briefing: bool = True
    monthly_budget_cny: float = 80.0
    memory_top_k: int = 8
    memory_similarity_threshold: float = 0.85
    stream_mode: str = "instant"          # instant=直接吐已生成答案（零额外调用，默认）| live=再调一次做真流式

    # ---- 动态工具（自己装工具的三层机制）----
    tools_d_dir: str = "tools.d"          # 声明式工具目录（*.yaml 与 generated/ pending/）
    mcp_config: str = "mcp.json"          # MCP 服务器配置文件
    mcp_enabled: bool = True              # 是否启用 MCP 客户端
    tool_install_mode: str = "confirm"    # confirm=装前需批准 | auto=自动安装
    review_generated_tools: bool = True   # 自动安装时用第二个模型审查代码
    tool_code_timeout: float = 20.0       # 冒烟测试超时（秒）

    # ---- 外挂知识库（RAG）----
    kb_enabled: bool = True
    kb_top_k: int = 6                 # 检索返回条数
    kb_min_similarity: float = 0.25   # 低于该相似度不采用
    kb_auto_rag: bool = True          # 每次对话先自动检索知识库并注入（不依赖模型自觉调工具）
    kb_auto_rag_top_k: int = 4        # 自动注入的条数
    kb_chunk_size: int = 900          # 切片大小（字符）
    kb_chunk_overlap: int = 150       # 切片重叠
    kb_harvest_max_pages: int = 5     # 单次全网采集最多入库几篇
    kb_harvest_max_chars: int = 12000 # 单页最多交给模型筛选的字数

    # ---- 智能路由（省钱：小事用便宜档，大事用强档）----
    model_policy: str = "smart"           # smart=自动分档 | fixed=全部走强档
    classifier_enabled: bool = True       # smart 模式下用便宜模型当判官（未配置便宜档时自动退化为启发式）
    cheap_provider: str = ""              # 便宜档 provider 名（如 siliconflow）
    cheap_model: str = ""                 # 便宜档模型名（覆盖该 provider 默认模型，如 Qwen/Qwen2.5-7B-Instruct）

    # ---- 价格（元/百万 token）----
    price_deepseek_in: float = 2.0
    price_deepseek_out: float = 8.0
    price_qwen_in: float = 4.0
    price_qwen_out: float = 12.0
    price_glm_in: float = 5.0
    price_glm_out: float = 20.0
    price_siliconflow_in: float = 0.0    # SiliconFlow 免费模型默认 0；付费模型按实际改
    price_siliconflow_out: float = 0.0
    embed_price_per_1m: float = 0.5

    # ---- 模型路由 ----
    @property
    def provider_order(self) -> list[str]:
        order = [self.primary_model.strip()] if self.primary_model.strip() else []
        for name in self.backup_models.split(","):
            name = name.strip()
            if name and name not in order:
                order.append(name)
        return order

    def provider_spec(self, name: str) -> dict | None:
        table = {
            "deepseek": dict(
                base_url=self.deepseek_base_url,
                api_key=self.deepseek_api_key,
                model=self.deepseek_model,
            ),
            "qwen": dict(
                base_url=self.qwen_base_url,
                api_key=self.qwen_api_key,
                model=self.qwen_model,
            ),
            "glm": dict(
                base_url=self.glm_base_url,
                api_key=self.glm_api_key,
                model=self.glm_model,
            ),
            "siliconflow": dict(
                base_url=self.siliconflow_base_url,
                api_key=self.siliconflow_api_key,
                model=self.cheap_model or "Qwen/Qwen2.5-7B-Instruct",
            ),
        }
        spec = table.get(name)
        if spec and spec["api_key"]:
            spec["name"] = name
            return spec
        return None

    def enabled_providers(self) -> list[dict]:
        return [s for s in (self.provider_spec(n) for n in self.provider_order) if s]

    def cheap_spec(self) -> dict | None:
        """便宜档 provider 规格（未配置或没有 key 时返回 None → 便宜档退化为主档）。"""
        if not self.cheap_provider:
            return None
        spec = self.provider_spec(self.cheap_provider)
        if spec is None:
            return None
        if self.cheap_model:
            spec["model"] = self.cheap_model
        return spec

    def provider_prices(self, name: str) -> tuple[float, float]:
        return {
            "deepseek": (self.price_deepseek_in, self.price_deepseek_out),
            "qwen": (self.price_qwen_in, self.price_qwen_out),
            "glm": (self.price_glm_in, self.price_glm_out),
            "siliconflow": (self.price_siliconflow_in, self.price_siliconflow_out),
        }.get(name, (0.0, 0.0))


@lru_cache
def get_settings() -> Settings:
    return Settings()
