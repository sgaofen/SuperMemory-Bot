"""
SessionContext — 跨会话连贯性管理

功能:
  - 维护全局会话状态，不依赖单一频道
  - 记录上次交互摘要，支持"我回来了"场景
  - 跨频道的用户状态共享
  - 会话间隔感知（长时间未对话时的重新连接）
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

SESSION_FILE = Path(
    os.getenv(
        "SESSION_FILE",
        Path(__file__).parent.parent.parent / "data" / "session_context.json",
    )
)
DEFAULT_TIMEZONE = os.getenv("USER_TIMEZONE", "America/Los_Angeles")


class SessionSummary:
    """单次会话摘要"""

    def __init__(
        self,
        session_id: str,
        started_at: datetime,
        *,
        ended_at: Optional[datetime] = None,
        channel_id: str = "",
        topic_summary: str = "",
        key_points: Optional[list[str]] = None,
        emotion_snapshot: str = "neutral",
        unfinished_business: Optional[list[str]] = None,
        message_count: int = 0,
    ):
        self.session_id = session_id
        self.started_at = started_at
        self.ended_at = ended_at
        self.channel_id = channel_id
        self.topic_summary = topic_summary
        self.key_points = key_points or []
        self.emotion_snapshot = emotion_snapshot
        self.unfinished_business = unfinished_business or []
        self.message_count = message_count

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "started_at": self.started_at.isoformat(),
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "channel_id": self.channel_id,
            "topic_summary": self.topic_summary,
            "key_points": self.key_points,
            "emotion_snapshot": self.emotion_snapshot,
            "unfinished_business": self.unfinished_business,
            "message_count": self.message_count,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SessionSummary":
        return cls(
            session_id=data["session_id"],
            started_at=datetime.fromisoformat(data["started_at"]),
            ended_at=datetime.fromisoformat(data["ended_at"])
            if data.get("ended_at")
            else None,
            channel_id=data.get("channel_id", ""),
            topic_summary=data.get("topic_summary", ""),
            key_points=data.get("key_points", []),
            emotion_snapshot=data.get("emotion_snapshot", "neutral"),
            unfinished_business=data.get("unfinished_business", []),
            message_count=data.get("message_count", 0),
        )

    @property
    def duration_minutes(self) -> float:
        end = self.ended_at or datetime.now(timezone.utc)
        return (end - self.started_at).total_seconds() / 60


class GlobalSessionContext:
    """全局会话上下文管理器"""

    def __init__(self, timezone_str: str = DEFAULT_TIMEZONE):
        self.timezone_str = timezone_str
        try:
            self.zone = ZoneInfo(timezone_str)
        except Exception:
            self.zone = ZoneInfo("America/Los_Angeles")

        self.sessions: list[SessionSummary] = []
        self._current_session: Optional[SessionSummary] = None
        self._last_interaction: Optional[datetime] = None
        self._global_state: dict = {}
        self._lock = threading.RLock()
        self._file_path = SESSION_FILE
        self._session_timeout_minutes = int(os.getenv("SESSION_TIMEOUT_MINUTES", "30"))
        self._ensure_file()
        self._load()

    def _ensure_file(self):
        self._file_path.parent.mkdir(parents=True, exist_ok=True)
        if not self._file_path.exists():
            self._file_path.write_text("{}", encoding="utf-8")

    def _load(self):
        try:
            data = json.loads(self._file_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                sessions_data = data.get("sessions", [])
                if isinstance(sessions_data, list):
                    self.sessions = [
                        SessionSummary.from_dict(s)
                        for s in sessions_data
                        if isinstance(s, dict)
                    ]
                last_str = data.get("last_interaction")
                if last_str:
                    self._last_interaction = datetime.fromisoformat(last_str)
                self._global_state = data.get("global_state", {})
            logger.info(f"Loaded {len(self.sessions)} session records")
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Failed to load session context: {e}")

    def _save(self):
        with self._lock:
            try:
                data = {
                    "sessions": [s.to_dict() for s in self.sessions[-100:]],
                    "last_interaction": self._last_interaction.isoformat()
                    if self._last_interaction
                    else None,
                    "global_state": self._global_state,
                }
                self._file_path.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            except OSError as e:
                logger.error(f"Failed to save session context: {e}")

    def start_session(self, channel_id: str = "") -> SessionSummary:
        """开始新会话"""
        import uuid

        now = datetime.now(timezone.utc)

        if self._current_session and not self._current_session.ended_at:
            self.end_session()

        session = SessionSummary(
            session_id=f"{uuid.uuid4().hex[:12]}",
            started_at=now,
            channel_id=channel_id,
        )

        with self._lock:
            self._current_session = session
            self._last_interaction = now
            self.sessions.append(session)
            self._save()

        logger.info(f"Started new session: {session.session_id}")
        return session

    def end_session(self, summary: str = "", key_points: Optional[list[str]] = None):
        """结束当前会话"""
        with self._lock:
            if not self._current_session:
                return

            self._current_session.ended_at = datetime.now(timezone.utc)
            if summary:
                self._current_session.topic_summary = summary
            if key_points:
                self._current_session.key_points = key_points
            self._save()
            logger.info(f"Ended session: {self._current_session.session_id}")
            self._current_session = None

    def record_interaction(self, message_count: int = 1):
        """记录一次交互"""
        now = datetime.now(timezone.utc)
        with self._lock:
            self._last_interaction = now
            if self._current_session:
                self._current_session.message_count += message_count

    def get_time_since_last_interaction(self) -> Optional[timedelta]:
        """获取距离上次交互的时间"""
        if not self._last_interaction:
            return None
        return datetime.now(timezone.utc) - self._last_interaction

    def is_returning_user(self) -> tuple[bool, str]:
        """
        检查用户是否"回来了"（长时间未交互）。
        返回 (是否回归, 问候建议)
        """
        delta = self.get_time_since_last_interaction()
        if delta is None:
            return False, ""

        hours = delta.total_seconds() / 3600
        days = hours / 24

        if days >= 7:
            return True, "好久不见"
        elif days >= 3:
            return True, "几天没聊了"
        elif hours >= 12:
            return True, "回来了"
        elif hours >= 4:
            return True, ""

        return False, ""

    def get_last_session_summary(self) -> Optional[str]:
        """获取上次会话的摘要，用于接续对话"""
        with self._lock:
            completed = [s for s in self.sessions if s.ended_at]
            if not completed:
                return None

            last = completed[-1]
            if not last.topic_summary:
                return None

            return last.topic_summary

    def get_welcome_back_context(self) -> str:
        """
        生成"欢迎回来"的上下文信息。
        供 AI 在用户说"我回来了"时参考。
        """
        is_returning, greeting_hint = self.is_returning_user()
        parts: list[str] = []

        if is_returning:
            delta = self.get_time_since_last_interaction()
            if delta:
                hours = delta.total_seconds() / 3600
                if hours >= 24:
                    parts.append(f"用户已经 {int(hours / 24)} 天没有对话了")
                else:
                    parts.append(f"用户已经 {int(hours)} 小时没有对话了")

        last_summary = self.get_last_session_summary()
        if last_summary:
            parts.append(f"上次聊天主要话题: {last_summary}")

        if self._global_state.get("pending_topics"):
            pending = self._global_state["pending_topics"][:3]
            parts.append(f"待跟进话题: {', '.join(pending)}")

        if not parts:
            return ""

        return "[会话上下文: " + "; ".join(parts) + "]"

    def update_global_state(self, key: str, value):
        """更新全局状态"""
        with self._lock:
            self._global_state[key] = value
            self._save()

    def get_global_state(self, key: str, default=None):
        """获取全局状态"""
        with self._lock:
            return self._global_state.get(key, default)

    def add_pending_topic(self, topic: str):
        """添加待跟进话题"""
        with self._lock:
            topics = self._global_state.get("pending_topics", [])
            if topic not in topics:
                topics.append(topic)
                self._global_state["pending_topics"] = topics[-20:]
                self._save()

    def remove_pending_topic(self, topic: str):
        """移除待跟进话题"""
        with self._lock:
            topics = self._global_state.get("pending_topics", [])
            if topic in topics:
                topics.remove(topic)
                self._global_state["pending_topics"] = topics
                self._save()

    def get_recent_sessions(self, limit: int = 5) -> list[SessionSummary]:
        """获取最近的会话"""
        with self._lock:
            completed = [s for s in self.sessions if s.ended_at]
            completed.sort(
                key=lambda x: x.ended_at or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True,
            )
            return completed[:limit]

    def check_and_auto_end_session(self):
        """检查会话是否超时，自动结束"""
        delta = self.get_time_since_last_interaction()
        if delta and delta.total_seconds() > self._session_timeout_minutes * 60:
            if self._current_session and not self._current_session.ended_at:
                self.end_session()
                logger.info("Session auto-ended due to timeout")

    def format_session_for_display(self, session: SessionSummary) -> str:
        """格式化会话用于显示"""
        local_start = session.started_at.astimezone(self.zone)
        start_str = local_start.strftime("%Y-%m-%d %H:%M")

        lines = [f"📅 **{start_str}** ({session.message_count}条消息)"]
        if session.topic_summary:
            lines.append(f"   话题: {session.topic_summary}")
        if session.key_points:
            lines.append(f"   要点: {'; '.join(session.key_points[:3])}")

        return "\n".join(lines)

    def cleanup_old_sessions(self, days: int = 30):
        """清理旧会话"""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        with self._lock:
            before = len(self.sessions)
            self.sessions = [s for s in self.sessions if s.started_at > cutoff]
            after = len(self.sessions)
            if before != after:
                self._save()
                logger.info(f"Cleaned up {before - after} old sessions")
