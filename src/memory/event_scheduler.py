"""
EventScheduler — 智能事件提醒系统

功能:
  - 从对话中自动识别时间承诺并创建提醒
  - 支持一次性事件和周期性事件
  - 启动时检查到期提醒
  - 事件完成后可标记状态
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

EVENTS_FILE = Path(
    os.getenv(
        "EVENTS_FILE",
        Path(__file__).parent.parent.parent / "data" / "scheduled_events.json",
    )
)
DEFAULT_TIMEZONE = os.getenv("USER_TIMEZONE", "America/Los_Angeles")

EVENT_STATUS = {"pending", "triggered", "completed", "cancelled", "snoozed"}
EVENT_PRIORITY = {"critical", "high", "medium", "low"}
RECURRENCE_TYPES = {"none", "daily", "weekly", "monthly", "yearly"}


def _parse_relative_time(
    text: str, base_time: datetime, zone: ZoneInfo
) -> Optional[datetime]:
    """
    解析相对时间表达式，返回绝对时间。
    支持: "明天", "后天", "下周一", "周五", "3天后", "下周三下午3点" 等
    """
    t = str(text or "").strip().lower()
    if not t:
        return None

    now = base_time.astimezone(zone)
    today = now.date()

    weekday_map = {
        "周一": 0,
        "星期一": 0,
        "monday": 0,
        "mon": 0,
        "周二": 1,
        "星期二": 1,
        "tuesday": 1,
        "tue": 1,
        "周三": 2,
        "星期三": 2,
        "wednesday": 2,
        "wed": 2,
        "周四": 3,
        "星期四": 3,
        "thursday": 3,
        "thu": 3,
        "周五": 4,
        "星期五": 4,
        "friday": 4,
        "fri": 4,
        "周六": 5,
        "星期六": 5,
        "saturday": 5,
        "sat": 5,
        "周日": 6,
        "星期日": 6,
        "星期天": 6,
        "sunday": 6,
        "sun": 6,
    }

    def set_time(dt: datetime, hour: int, minute: int = 0) -> datetime:
        return dt.replace(hour=hour, minute=minute, second=0, microsecond=0)

    if "现在" in t or "立刻" in t or "马上" in t:
        return now + timedelta(minutes=5)

    if "今天" in t:
        result = now
        if "早上" in t or "上午" in t:
            result = set_time(result, 9)
        elif "中午" in t:
            result = set_time(result, 12)
        elif "下午" in t:
            result = set_time(result, 15)
        elif "晚上" in t or "傍晚" in t:
            result = set_time(result, 19)
        elif "睡前" in t or "睡前" in t:
            result = set_time(result, 22)
        hour_match = re.search(r"(\d{1,2})[点时:：]", t)
        if hour_match:
            hour = int(hour_match.group(1))
            if "下午" in t or "晚上" in t or "pm" in t:
                if hour < 12:
                    hour += 12
            result = set_time(result, hour)
            minute_match = re.search(r"(\d{1,2})[分]", t)
            if minute_match:
                result = result.replace(minute=int(minute_match.group(1)))
        return result

    if "明天" in t:
        result = now + timedelta(days=1)
        if "早上" in t or "上午" in t:
            result = set_time(result, 9)
        elif "中午" in t:
            result = set_time(result, 12)
        elif "下午" in t:
            result = set_time(result, 15)
        elif "晚上" in t:
            result = set_time(result, 19)
        hour_match = re.search(r"(\d{1,2})[点时:：]", t)
        if hour_match:
            hour = int(hour_match.group(1))
            if "下午" in t or "晚上" in t or hour < 8:
                if hour < 12 and ("下午" in t or "晚上" in t or "pm" in t):
                    hour += 12
            result = set_time(result, hour)
        return result

    if "后天" in t:
        return now + timedelta(days=2)

    days_match = re.search(r"(\d+)\s*[天日][之以]?[前后]", t)
    if days_match:
        days = int(days_match.group(1))
        if "后" in t:
            return now + timedelta(days=days)
        elif "前" in t:
            return now - timedelta(days=days)

    hours_match = re.search(r"(\d+)\s*[个小]?[时][之以]?[前后]", t)
    if hours_match:
        hours = int(hours_match.group(1))
        if "后" in t:
            return now + timedelta(hours=hours)
        elif "前" in t:
            return now - timedelta(hours=hours)

    minutes_match = re.search(r"(\d+)\s*分[之以]?[前后]", t)
    if minutes_match:
        mins = int(minutes_match.group(1))
        if "后" in t:
            return now + timedelta(minutes=mins)
        elif "前" in t:
            return now - timedelta(minutes=mins)

    for name, wd in weekday_map.items():
        if name in t:
            current_wd = now.weekday()
            days_ahead = wd - current_wd
            if "下" in t:
                if days_ahead <= 0:
                    days_ahead += 7
                else:
                    pass
            elif days_ahead <= 0:
                days_ahead += 7
            result = now + timedelta(days=days_ahead)
            result = result.replace(hour=9, minute=0)
            hour_match = re.search(r"(\d{1,2})[点时:：]", t)
            if hour_match:
                hour = int(hour_match.group(1))
                if "下午" in t or "晚上" in t:
                    if hour < 12:
                        hour += 12
                result = result.replace(hour=hour)
            return result

    if "下周" in t:
        week_offset = 7
        for name, wd in weekday_map.items():
            if name in t:
                current_wd = now.weekday()
                days_ahead = wd - current_wd + week_offset
                result = now + timedelta(days=days_ahead)
                return result.replace(hour=9, minute=0)

    next_week_match = re.search(r"下周\s*(\d+)", t)
    if next_week_match:
        week_num = int(next_week_match.group(1))
        current_week = now.isocalendar()[1]
        weeks_ahead = week_num - current_week
        if weeks_ahead <= 0:
            weeks_ahead += 52
        result = now + timedelta(weeks=weeks_ahead)
        return result.replace(hour=9, minute=0)

    date_match = re.search(r"(\d{1,2})[月/](\d{1,2})", t)
    if date_match:
        month = int(date_match.group(1))
        day = int(date_match.group(2))
        year = now.year
        if month < now.month or (month == now.month and day < now.day):
            year += 1
        try:
            result = datetime(year, month, day, 9, 0, tzinfo=zone)
            return result
        except ValueError:
            pass

    full_date_match = re.search(r"(\d{4})[年/-](\d{1,2})[月/-](\d{1,2})", t)
    if full_date_match:
        year = int(full_date_match.group(1))
        month = int(full_date_match.group(2))
        day = int(full_date_match.group(3))
        try:
            result = datetime(year, month, day, 9, 0, tzinfo=zone)
            return result
        except ValueError:
            pass

    return None


class ScheduledEvent:
    """单个提醒事件"""

    def __init__(
        self,
        event_id: str,
        title: str,
        trigger_time: datetime,
        *,
        description: str = "",
        status: str = "pending",
        priority: str = "medium",
        recurrence: str = "none",
        source: str = "user",
        source_message: str = "",
        created_at: Optional[datetime] = None,
        snooze_until: Optional[datetime] = None,
        metadata: Optional[dict] = None,
    ):
        self.event_id = event_id
        self.title = title
        self.trigger_time = trigger_time
        self.description = description
        self.status = status if status in EVENT_STATUS else "pending"
        self.priority = priority if priority in EVENT_PRIORITY else "medium"
        self.recurrence = recurrence if recurrence in RECURRENCE_TYPES else "none"
        self.source = source
        self.source_message = source_message
        self.created_at = created_at or datetime.now(timezone.utc)
        self.snooze_until = snooze_until
        self.metadata = metadata or {}

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "title": self.title,
            "trigger_time": self.trigger_time.isoformat(),
            "description": self.description,
            "status": self.status,
            "priority": self.priority,
            "recurrence": self.recurrence,
            "source": self.source,
            "source_message": self.source_message,
            "created_at": self.created_at.isoformat(),
            "snooze_until": self.snooze_until.isoformat()
            if self.snooze_until
            else None,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ScheduledEvent":
        return cls(
            event_id=data["event_id"],
            title=data["title"],
            trigger_time=datetime.fromisoformat(data["trigger_time"]),
            description=data.get("description", ""),
            status=data.get("status", "pending"),
            priority=data.get("priority", "medium"),
            recurrence=data.get("recurrence", "none"),
            source=data.get("source", "user"),
            source_message=data.get("source_message", ""),
            created_at=datetime.fromisoformat(data["created_at"])
            if data.get("created_at")
            else None,
            snooze_until=datetime.fromisoformat(data["snooze_until"])
            if data.get("snooze_until")
            else None,
            metadata=data.get("metadata", {}),
        )

    def is_due(self, now: Optional[datetime] = None) -> bool:
        if self.status != "pending":
            return False
        if self.snooze_until and self.snooze_until > (
            now or datetime.now(timezone.utc)
        ):
            return False
        return self.trigger_time <= (now or datetime.now(timezone.utc))

    def mark_triggered(self):
        self.status = "triggered"

    def mark_completed(self):
        self.status = "completed"

    def snooze(self, duration: timedelta):
        self.snooze_until = datetime.now(timezone.utc) + duration
        self.status = "snoozed"

    def create_next_recurrence(self) -> Optional["ScheduledEvent"]:
        if self.recurrence == "none":
            return None

        next_time = self.trigger_time
        if self.recurrence == "daily":
            next_time += timedelta(days=1)
        elif self.recurrence == "weekly":
            next_time += timedelta(weeks=1)
        elif self.recurrence == "monthly":
            month = next_time.month + 1
            year = next_time.year
            if month > 12:
                month = 1
                year += 1
            next_time = next_time.replace(year=year, month=month)
        elif self.recurrence == "yearly":
            next_time = next_time.replace(year=next_time.year + 1)

        import uuid

        return ScheduledEvent(
            event_id=f"{uuid.uuid4().hex[:12]}",
            title=self.title,
            trigger_time=next_time,
            description=self.description,
            priority=self.priority,
            recurrence=self.recurrence,
            source="recurrence",
            metadata=self.metadata,
        )


class EventScheduler:
    """事件提醒调度器"""

    def __init__(self, timezone_str: str = DEFAULT_TIMEZONE):
        self.timezone_str = timezone_str
        try:
            self.zone = ZoneInfo(timezone_str)
        except Exception:
            self.zone = ZoneInfo("America/Los_Angeles")

        self.events: list[ScheduledEvent] = []
        self._lock = threading.RLock()
        self._file_path = EVENTS_FILE
        self._ensure_file()
        self._load()

    def _ensure_file(self):
        self._file_path.parent.mkdir(parents=True, exist_ok=True)
        if not self._file_path.exists():
            self._file_path.write_text("[]", encoding="utf-8")

    def _load(self):
        try:
            data = json.loads(self._file_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                self.events = [
                    ScheduledEvent.from_dict(e) for e in data if isinstance(e, dict)
                ]
            logger.info(f"Loaded {len(self.events)} scheduled events")
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Failed to load events: {e}")
            self.events = []

    def _save(self):
        with self._lock:
            try:
                data = [e.to_dict() for e in self.events]
                self._file_path.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            except OSError as e:
                logger.error(f"Failed to save events: {e}")

    def add_event(
        self,
        title: str,
        trigger_time: datetime,
        *,
        description: str = "",
        priority: str = "medium",
        recurrence: str = "none",
        source: str = "user",
        source_message: str = "",
        metadata: Optional[dict] = None,
    ) -> ScheduledEvent:
        import uuid

        event = ScheduledEvent(
            event_id=f"{uuid.uuid4().hex[:12]}",
            title=title,
            trigger_time=trigger_time,
            description=description,
            priority=priority,
            recurrence=recurrence,
            source=source,
            source_message=source_message,
            metadata=metadata,
        )
        with self._lock:
            self.events.append(event)
            self._save()
        logger.info(f"Added event: {title} at {trigger_time}")
        return event

    def add_from_message(
        self,
        message: str,
        context: str = "",
    ) -> Optional[ScheduledEvent]:
        """
        从用户消息中提取事件并创建提醒。
        返回创建的事件，如果未检测到时间承诺则返回 None。
        """
        time_indicators = [
            r"提醒我(.{0,30})(记住|别忘了|记得)",
            r"别忘了(.{0,30})",
            r"记得(.{0,30})",
            r"记住(.{0,30})",
            r"(考试|面试|约会|会议|appointment|exam|meeting|interview).{0,10}(是|在|on)",
            r"(周五|周[一二三四五六日]|明天|后天|下周|今天).{0,20}(考试|交|due|deadline|开会)",
            r"(考试|期中|final|midterm).{0,5}(在|是|on)",
        ]

        combined = f"{context} {message}".strip() if context else message

        for pattern in time_indicators:
            match = re.search(pattern, combined, re.IGNORECASE)
            if match:
                matched_text = match.group(0)
                trigger_time = _parse_relative_time(
                    matched_text, datetime.now(timezone.utc), self.zone
                )
                if trigger_time:
                    title = self._extract_event_title(matched_text, message)
                    event = self.add_event(
                        title=title,
                        trigger_time=trigger_time,
                        source="auto_extracted",
                        source_message=message,
                        metadata={"context": context, "matched_pattern": pattern},
                    )
                    return event

        time_explicit = re.search(
            r"(提醒我|记着|别忘了)[,:：]?\s*(.+?)(?=提醒|记住|$)",
            message,
            re.IGNORECASE,
        )
        if time_explicit:
            reminder_content = time_explicit.group(2).strip()
            default_trigger = datetime.now(self.zone) + timedelta(hours=2)
            title = reminder_content[:80]
            event = self.add_event(
                title=title,
                trigger_time=default_trigger,
                source="user_explicit",
                source_message=message,
            )
            return event

        return None

    def _extract_event_title(self, matched_text: str, original_message: str) -> str:
        clean = re.sub(
            r"^(提醒我|记得|记住|别忘了|不要忘记)\s*",
            "",
            matched_text,
            flags=re.IGNORECASE,
        )
        clean = re.sub(r"\s*(在|是|on)\s*$", "", clean, flags=re.IGNORECASE)
        if len(clean) < 5:
            clean = original_message[:60]
        return clean.strip()[:100]

    def get_due_events(self) -> list[ScheduledEvent]:
        """获取所有到期未触发的事件"""
        now = datetime.now(timezone.utc)
        with self._lock:
            due = [e for e in self.events if e.is_due(now)]
        return due

    def get_upcoming_events(
        self, days: int = 7, limit: int = 10
    ) -> list[ScheduledEvent]:
        """获取即将到来的事件（用于主动提醒）"""
        now = datetime.now(timezone.utc)
        end = now + timedelta(days=days)
        with self._lock:
            upcoming = [
                e
                for e in self.events
                if e.status == "pending"
                and now < e.trigger_time <= end
                and (e.snooze_until is None or e.snooze_until <= now)
            ]
        upcoming.sort(key=lambda x: x.trigger_time)
        return upcoming[:limit]

    def mark_triggered(self, event_id: str):
        with self._lock:
            for event in self.events:
                if event.event_id == event_id:
                    event.mark_triggered()
                    if event.recurrence != "none":
                        next_event = event.create_next_recurrence()
                        if next_event:
                            self.events.append(next_event)
                    self._save()
                    return True
        return False

    def mark_completed(self, event_id: str):
        with self._lock:
            for event in self.events:
                if event.event_id == event_id:
                    event.mark_completed()
                    self._save()
                    return True
        return False

    def snooze_event(self, event_id: str, duration: timedelta) -> bool:
        with self._lock:
            for event in self.events:
                if event.event_id == event_id:
                    event.snooze(duration)
                    self._save()
                    return True
        return False

    def cancel_event(self, event_id: str) -> bool:
        with self._lock:
            for i, event in enumerate(self.events):
                if event.event_id == event_id:
                    event.status = "cancelled"
                    self._save()
                    return True
        return False

    def delete_event(self, event_id: str) -> bool:
        with self._lock:
            for i, event in enumerate(self.events):
                if event.event_id == event_id:
                    del self.events[i]
                    self._save()
                    return True
        return False

    def get_all_events(self, include_completed: bool = False) -> list[ScheduledEvent]:
        with self._lock:
            if include_completed:
                return list(self.events)
            return [
                e for e in self.events if e.status not in {"completed", "cancelled"}
            ]

    def format_event_for_display(
        self, event: ScheduledEvent, now: Optional[datetime] = None
    ) -> str:
        now = now or datetime.now(timezone.utc)
        local_time = event.trigger_time.astimezone(self.zone)
        time_str = local_time.strftime("%Y-%m-%d %H:%M")

        if event.trigger_time <= now:
            status_emoji = "🔔"
            time_prefix = "已到期"
        else:
            delta = event.trigger_time - now
            if delta.total_seconds() < 3600:
                time_prefix = f"{int(delta.total_seconds() // 60)}分钟后"
            elif delta.total_seconds() < 86400:
                time_prefix = f"{int(delta.total_seconds() // 3600)}小时后"
            else:
                time_prefix = f"{int(delta.total_seconds() // 86400)}天后"
            status_emoji = "📅"

        priority_emoji = {
            "critical": "🔴",
            "high": "🟠",
            "medium": "🟡",
            "low": "🟢",
        }.get(event.priority, "⚪")

        return f"{status_emoji} {priority_emoji} **{event.title}** ({time_prefix}, {time_str})"

    def cleanup_old_events(self, days: int = 30):
        """清理旧的已完成/取消的事件"""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        with self._lock:
            before = len(self.events)
            self.events = [
                e
                for e in self.events
                if e.status not in {"completed", "cancelled"} or e.trigger_time > cutoff
            ]
            after = len(self.events)
            if before != after:
                self._save()
                logger.info(f"Cleaned up {before - after} old events")
