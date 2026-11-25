"""测试环境：在导入 core 前固定环境变量，保证测试离线可跑。"""
import os

os.environ.setdefault("AUTH_TOKEN", "test-token")
os.environ.setdefault("DAILY_BRIEFING", "false")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://jarvis:test@127.0.0.1:59999/jarvis")
os.environ.setdefault("DEEPSEEK_API_KEY", "test-key")
os.environ.setdefault("PRIMARY_MODEL", "deepseek")
os.environ.setdefault("BACKUP_MODELS", "qwen")
os.environ.setdefault("QWEN_API_KEY", "")
os.environ.setdefault("SILICONFLOW_API_KEY", "")
os.environ.setdefault("WXPUSHER_APP_TOKEN", "")
os.environ.setdefault("WXPUSHER_UIDS", "")
os.environ.setdefault("TZ", "Asia/Shanghai")
os.environ.setdefault("MODEL_POLICY", "smart")
os.environ.setdefault("CLASSIFIER_ENABLED", "false")
