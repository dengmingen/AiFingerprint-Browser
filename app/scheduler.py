"""定时调度器：按 daily_time 或 interval 周期性触发 RPA 任务。

调度循环随服务启动，每 20 秒扫描一次到期计划；
每个 (计划, 环境) 产生一条独立的 task_run 记录，与手动运行完全一致。

daily 语义：
  - daily_time 为 HH:MM，按计划的 timezone（IANA 名）解释；
    未设置时按系统本地时区，"UTC" 保持旧的 UTC 语义。
  - weekdays 限定生效日（0=周一 … 6=周日；空=每天）。
  - 错过补跑：计划时刻在服务停机期间越过的，服务启动后当日仍会触发一次
    （依据 last_run_at 判断"今天是否已运行"，而非"时刻是否刚过"）。
interval 语义：以 last_run_at 为基准的固定间隔，停机后首个 tick 立即补一次。

自动同步：
  settings.json 中 auto_sync=true 且已配置远端时，每 sync_interval_minutes（默认 30）
  自动推送一次（拉取由用户手动触发或远端反向拉取）。
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import db
from .task_engine import TaskEngine

log = logging.getLogger(__name__)

CHECK_INTERVAL = 20  # 秒
SYNC_INTERVAL_MINUTES = 30  # 自动同步默认间隔
_last_sync: dict[str, datetime] = {}  # {"push": last_push_utc}

_WEEKDAY_NAMES = ["一", "二", "三", "四", "五", "六", "日"]


def _schedule_tz(s: dict) -> tzinfo:
    """计划的解释时区：显式设置且合法用之；否则系统本地（固定偏移）；再退回 UTC。"""
    name = s.get("timezone")
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            pass
    # 标准库拿不到本机 IANA 名，用本地 UTC 偏移构造等价固定时区（对 HH:MM 调度足够）
    local_offset = datetime.now().astimezone().utcoffset() or timedelta(0)
    return timezone(local_offset)


def _weekdays(s: dict) -> set[int]:
    return {int(w) for w in (s.get("weekdays") or []) if str(w).isdigit() or isinstance(w, int)}


def _daily_due_local(s: dict, now_utc: datetime) -> tuple[bool, datetime]:
    """在计划时区内判断 daily 时刻是否到期（含周几过滤）。
    返回 (是否到期, 计划时区的今日运行时刻对应的 UTC 时间)。"""
    tz = _schedule_tz(s)
    now_local = now_utc.astimezone(tz)
    hh, mm = s["daily_time"].split(":")[:2]
    today_run_local = now_local.replace(hour=int(hh), minute=int(mm),
                                        second=0, microsecond=0)
    days = _weekdays(s)
    if days and now_local.weekday() not in days:
        return False, today_run_local.astimezone(timezone.utc)
    return now_local >= today_run_local, today_run_local.astimezone(timezone.utc)


class Scheduler:
    def __init__(self, task_engine: TaskEngine) -> None:
        self.engine = task_engine
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def shutdown(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("调度循环异常")
            await asyncio.sleep(CHECK_INTERVAL)

    async def _tick(self) -> None:
        now = datetime.now(timezone.utc)
        for s in db.list_schedules():
            if not s["enabled"]:
                continue
            if not self._is_due(s, now):
                continue
            await self.run_now(s["id"], reason="schedule")
        # 自动同步（仅 push——拉取通常是用户手动触发或远端主动拉）
        await self._auto_sync_if_due(now)

    async def _auto_sync_if_due(self, now: datetime) -> None:
        try:
            from .security import load_settings
            settings = load_settings()
        except Exception:
            return
        if not (settings.get("auto_sync") and settings.get("sync_remote_url")
                and settings.get("sync_remote_token")):
            return
        interval = SYNC_INTERVAL_MINUTES
        last = _last_sync.get("push")
        if last and (now - last).total_seconds() < interval * 60:
            return
        try:
            from .sync import push_to_remote
            result = await push_to_remote()
            _last_sync["push"] = now
            log.info("自动同步推送成功: %s", result)
        except Exception:
            log.warning("自动同步推送失败", exc_info=True)

    @staticmethod
    def _is_due(s: dict, now: datetime) -> bool:
        last = None
        if s.get("last_run_at"):
            try:
                last = datetime.fromisoformat(s["last_run_at"])
            except ValueError:
                last = None
        if last is not None and last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if s["kind"] == "interval":
            interval = int(s.get("interval_minutes") or 0)
            if interval <= 0:
                return False
            reference = last or (now - timedelta(minutes=interval))
            return (now - reference).total_seconds() >= interval * 60 - CHECK_INTERVAL
        # daily：计划时区的固定时刻 + 周几过滤 + 当日错过补跑
        if not s.get("daily_time"):
            return False
        due, today_run_utc = _daily_due_local(s, now)
        if not due:
            return False
        already_ran_today = last is not None and last >= today_run_utc
        return not already_ran_today

    async def run_now(self, schedule_id: str, reason: str = "manual") -> int:
        """立即执行计划（手动按钮或调度触发共用）。返回提交的运行数。"""
        s = db.get_schedule(schedule_id)
        if not s:
            return 0
        task = db.get_task(s["task_id"])
        if not task:
            log.warning("调度 %s 引用的任务不存在", schedule_id)
            return 0
        profiles = []
        for pid in s["profile_ids"]:
            p = db.get_profile(pid)
            if p:
                profiles.append(p)
        db.update_schedule(schedule_id, {"last_run_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        if not profiles:
            log.warning("调度 %s 无可用环境", schedule_id)
            return 0
        run_ids = await self.engine.run(
            task, profiles, headless=s["headless"], auto_close=s["auto_close"]
        )
        log.info("调度 %s（%s）触发任务 %s：%d 个运行", schedule_id, reason, task["name"], len(run_ids))
        return len(run_ids)

    @staticmethod
    def describe(s: dict) -> str:
        """调度的人类可读描述（UI 列表用）。"""
        days = _weekdays(s)
        day_txt = "每天" if not days else "周" + "、周".join(
            _WEEKDAY_NAMES[d] for d in sorted(days) if 0 <= d <= 6)
        tz = s.get("timezone") or "本地时区"
        if s["kind"] == "interval":
            return f"每 {s.get('interval_minutes')} 分钟"
        return f"{day_txt} {s.get('daily_time')}（{tz}）"

    @staticmethod
    def next_run_at(s: dict) -> Optional[str]:
        if not s.get("enabled"):
            return None
        now = datetime.now(timezone.utc)
        try:
            if s["kind"] == "interval":
                interval = int(s.get("interval_minutes") or 0)
                if interval <= 0:
                    return None
                last = datetime.fromisoformat(s["last_run_at"]) if s.get("last_run_at") else now
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                nxt = last + timedelta(minutes=interval)
                return max(nxt, now).astimezone(timezone.utc).isoformat(timespec="seconds")
            if not s.get("daily_time"):
                return None
            # daily：在计划时区内逐日找下一个「生效日 + 时刻」
            tz = _schedule_tz(s)
            days = _weekdays(s)
            hh, mm = s["daily_time"].split(":")[:2]
            local = now.astimezone(tz)
            candidate = local.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
            for _ in range(8):  # 最多看 8 天（周几全禁的脏数据兜底）
                if (not days or candidate.weekday() in days) and candidate > local:
                    return candidate.astimezone(timezone.utc).isoformat(timespec="seconds")
                candidate += timedelta(days=1)
            return None
        except Exception:
            return None
