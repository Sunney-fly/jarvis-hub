"""调度器：早间简报（cron）+ 提醒任务（一次性），重启后从库中恢复。"""
import asyncio
import logging
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from sqlalchemy import select

from .. import accounting, db
from ..agent.loop import run_agent
from ..push import push_text

logger = logging.getLogger("core.scheduler")

BRIEFING_PROMPT = (
    "生成今天的简报。规则（必须遵守）：\n"
    "· 纯文本，禁止任何 Markdown 符号：不要 * # ` - > •，不要加粗、不要分隔线；\n"
    "· 每行一件事，最多 6 行，总长不超过 220 字，不要标题、不要寒暄；\n"
    "· 只写今天确实有内容的；某一方面没有内容就整行省掉，禁止写「暂无」「无」「没有」这类占位；\n"
    "· 天气：调用 get_weather，city 传「某高校」，写成一行，必须带降水概率（pop 字段，单位 %）；"
    "pop≥50 或白天/夜间文案含「雨/雪」时在行尾加「，带伞」，否则不加；pop 为 null 表示超出逐时预报范围，此时省略概率、禁止编造；"
    "如「某高校 多云 17~21℃，降水概率 20%」「某高校 小雨 16~20℃，降水概率 80%，带伞」；\n"
    "· 日程：调用 list_calendar_events，只写今天有时间的；没有就省略这一行；\n"
    "· 邮件：只有确实重要才写一行；没有就省略；\n"
    "· 续火花：调用 douyin_spark（action=status），只有今天还有没发出的才写一行「续火花：HH:MM 还有 N 人」；全部完成或不适用就省略；\n"
    "· 清理：见下方【今天有没有清理】。有则写一行，如「磁盘清理：C 盘清出 12.4GB」；没有则绝对不要提清理。\n"
    "直接输出最终文案，不要解释。"
)


PLACEHOLDERS = ("暂无", "无内容", "没有内容", "无日程", "暂无日程", "暂无邮件", "无邮件",
                "暂无提醒", "无提醒", "暂无待办", "无待办", "暂时没有")


TITLES = ("今日简报", "早间简报", "简报", "早上好", "你好")
EMPTY_TAIL = re.compile(r"[：:]\s*(暂无|无|没有|无内容|暂无内容|暂无日程|暂无邮件|无日程|无邮件)\s*$")


def tidy_briefing(text: str) -> str:
    """把模型输出清成纯文本：去 Markdown、去标题、去占位行、去重复、限行数。"""
    out, seen = [], set()
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        line = line.lstrip("#>•·-\t ").strip()
        line = line.replace("**", "").replace("`", "").replace("*", "").strip()
        if not line:
            continue
        if line in TITLES:
            continue
        if any(p in line for p in PLACEHOLDERS):
            continue
        if EMPTY_TAIL.search(line):
            continue
        if line in seen:
            continue
        seen.add(line)
        out.append(line)
        if len(out) >= 6:
            break
    return "\n".join(out)


class SchedulerService:
    def __init__(self, settings, session_factory, deps_provider):
        self.settings = settings
        self.sf = session_factory
        self.deps = deps_provider  # callable -> (router, registry, embedder)
        self.scheduler = AsyncIOScheduler(timezone=ZoneInfo(settings.tz))
        self._jobs: dict[str, object] = {}

    async def start(self) -> None:
        if self.settings.daily_briefing:
            try:
                hour_s, minute_s = self.settings.briefing_time.split(":")
                self.scheduler.add_job(
                    self.run_briefing,
                    CronTrigger(hour=int(hour_s), minute=int(minute_s)),
                    id="briefing",
                    misfire_grace_time=7200,
                    coalesce=True,
                )
            except (ValueError, AttributeError):
                logger.error("BRIEFING_TIME 格式无效（应为 HH:MM），已禁用简报")
        try:
            await self._load_pending()
        except Exception:
            logger.exception("加载待执行任务失败（数据库不可用时提醒功能暂不可用）")
        self.scheduler.start()
        logger.info("调度器已启动")

    def shutdown(self) -> None:
        try:
            self.scheduler.shutdown(wait=False)
        except Exception:
            pass

    async def _load_pending(self) -> None:
        async with self.sf() as s:
            rows = (
                await s.execute(
                    select(db.ScheduledTask).where(db.ScheduledTask.status == "pending")
                )
            ).scalars().all()
        now = datetime.now(timezone.utc)
        for task in rows:
            if task.run_at and task.run_at <= now:
                continue  # 过期的一次性任务直接丢弃
            self.schedule(task)
        logger.info("从库中恢复 %d 个待执行任务", len(self._jobs))

    def schedule(self, task: db.ScheduledTask) -> None:
        job_id = f"task-{task.id}"
        if job_id in self._jobs:
            return
        trigger = CronTrigger.from_crontab(task.cron) if task.cron else DateTrigger(run_date=task.run_at)
        self._jobs[task.id] = self.scheduler.add_job(
            self._run_task, trigger, args=[task.id], id=job_id,
            misfire_grace_time=3600, coalesce=True,
        )

    async def add_task(self, task: db.ScheduledTask) -> None:
        self.schedule(task)

    async def _run_task(self, task_id: str) -> None:
        async with self.sf() as s:
            task = await s.get(db.ScheduledTask, task_id)
            if task is None or task.status != "pending":
                return
            task.status = "running"
            task.last_run_at = db.utcnow()
            await s.commit()
        ok, error = True, None
        try:
            await self._execute(task)
        except Exception as e:
            logger.exception("任务 %s 执行失败", task_id)
            ok, error = False, str(e)[:500]
        async with self.sf() as s:
            task = await s.get(db.ScheduledTask, task_id)
            if task is not None:
                task.status = "done" if ok else "error"
                task.last_error = error
                await s.commit()
        self._jobs.pop(task.id, None)

    async def _execute(self, task: db.ScheduledTask) -> None:
        if task.kind == "reminder":
            message = str((task.payload or {}).get("message") or "⏰ 时间到了")
            ok = await push_text(self.settings, message, title="提醒")
            logger.info("提醒推送结果: %s", ok)
        else:
            logger.warning("未知任务类型: %s", task.kind)

    async def _clean_context(self) -> str:
        """今天有磁盘清理事件就返回一行摘要，没有则明确告知「没有」（模型据此省略）。"""
        try:
            tz = ZoneInfo(self.settings.tz)
            start = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
            async with self.sf() as s:
                row = (await s.execute(
                    select(db.SystemEvent)
                    .where(db.SystemEvent.kind == "disk_clean")
                    .where(db.SystemEvent.created_at >= start)
                    .order_by(db.SystemEvent.created_at.desc())
                    .limit(1)
                )).scalar_one_or_none()
        except Exception:
            logger.exception("读取清理事件失败")
            return "今天没有清理。"
        if row is None:
            return "今天没有清理。"
        # 同一天可能跑多次：合并成一条，避免简报里出现多行重复
        async with self.sf() as s:
            rows = (await s.execute(
                select(db.SystemEvent)
                .where(db.SystemEvent.kind == "disk_clean")
                .where(db.SystemEvent.created_at >= start)
            )).scalars().all()
        runs = 0
        gb_total = 0.0
        files_total = 0
        for ev in rows:
            data = ev.payload or {}
            gb = float(data.get("deleted_gb") or 0)
            if gb <= 0:
                continue
            runs += 1
            gb_total += gb
            files_total += int(data.get("deleted_files") or 0)
        if runs == 0:
            return "今天没有清理。"
        if runs == 1:
            return "今天清理过：删了 %.1fGB、%d 个文件。" % (gb_total, files_total)
        return "今天清理过 %d 次：共删 %.1fGB、%d 个文件（合并成一行写）。" % (runs, gb_total, files_total)

    async def run_briefing(self) -> None:
        """早间简报：用 Agent 循环查天气/日程/邮件，汇总后微信推送。"""
        router, registry, _embedder = self.deps()
        if router is None:
            logger.error("简报任务：没有可用的模型")
            return
        logger.info("开始生成早间简报")
        prompt = BRIEFING_PROMPT + "\n\n【今天有没有清理】" + await self._clean_context()
        # 网络/模型抖动时重试两次；三次都失败也要告诉你，不能静默消失
        result = None
        last_exc = None
        for attempt in range(3):
            try:
                result = await run_agent(
                    router, registry, [{"role": "user", "content": prompt}],
                    max_steps=6, temperature=0.6, tz=self.settings.tz,
                )
                break
            except Exception as exc:
                last_exc = exc
                logger.warning("简报第 %d/3 次失败：%s: %s",
                               attempt + 1, type(exc).__name__, str(exc)[:160])
                if attempt < 2:
                    await asyncio.sleep(60)
        if result is None:
            logger.exception("简报三次都失败，放弃今日", exc_info=last_exc)
            reason = type(last_exc).__name__ if last_exc else "未知原因"
            await push_text(
                self.settings,
                "今天的简报没生成：模型或网络不可用（" + reason + "）。网络恢复后可以让我补一份。",
                title="简报失败",
            )
            return
        try:
            await accounting.record_usage(
                self.sf,
                result.usage["provider"],
                result.usage["model"],
                result.usage["prompt_tokens"],
                result.usage["completion_tokens"],
            )
        except Exception:
            logger.exception("简报记账失败（不影响推送）")
        ok = await push_text(self.settings, tidy_briefing(result.final.content or ""), title="今日简报")
        logger.info("简报推送结果: %s", ok)
