"""
FollowupManager — 主动跟进机制

功能:
  - 从对话中识别需要跟进的话题（计划、承诺、重要事件）
  - 追踪话题状态，在适当时机主动询问
  - 避免生硬追问，自然融入对话
  - 支持手动标记话题完成/放弃
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

FOLLOWUPS_FILE = Path(
    os.getenv(
        "FOLLOWUPS_FILE",
        Path(__file__).parent.parent.parent / "data" / "followup_topics.json",
    )
)
DEFAULT_TIMEZONE = os.getenv("USER_TIMEZONE", "America/Los_Angeles")

TOPIC_STATUS = {"active", "completed", "abandoned", "paused"}
TOPIC_PRIORITY = {"high", "medium", "low"}

FOLLOWUP_PATTERNS = [
    (r"我打算(.{3,50})", "plan"),
    (r"我准备(.{3,50})", "plan"),
    (r"我计划(.{3,50})", "plan"),
    (r"我想要(.{3,50})", "goal"),
    (r"我希望能(.{3,50})", "goal"),
    (r"我得(.{3,30})", "obligation"),
    (r"我需要(.{3,50})", "need"),
    (r"我要去(.{3,30})", "event"),
    (r"我和(.{3,30})约了", "appointment"),
    (r"正在(.{3,30})", "ongoing"),
    (r"开始(.{3,30})了", "ongoing"),
    (r"试试(.{3,30})", "experiment"),
    (r"考虑(.{3,30})", "consideration"),
    (r"想想(.{3,30})", "consideration"),
]


class FollowupTopic:
    """一个需要跟进的话题"""

    def __init__(
        self,
        topic_id: str,
        title: str,
        category: str,
        *,
        description: str = "",
        status: str = "active",
        priority: str = "medium",
        source_message: str = "",
        created_at: Optional[datetime] = None,
        last_mentioned: Optional[datetime] = None,
        last_followed_up: Optional[datetime] = None,
        followup_count: int = 0,
        mention_count: int = 1,
        related_entities: Optional[list[str]] = None,
        metadata: Optional[dict] = None,
    ):
        self.topic_id = topic_id
        self.title = title
        self.category = category
        self.description = description
        self.status = status if status in TOPIC_STATUS else "active"
        self.priority = priority if priority in TOPIC_PRIORITY else "medium"
        self.source_message = source_message
        self.created_at = created_at or datetime.now(timezone.utc)
        self.last_mentioned = last_mentioned or self.created_at
        self.last_followed_up = last_followed_up
        self.followup_count = followup_count
        self.mention_count = mention_count
        self.related_entities = related_entities or []
        self.metadata = metadata or {}

    def to_dict(self) -> dict:
        return {
            "topic_id": self.topic_id,
            "title": self.title,
            "category": self.category,
            "description": self.description,
            "status": self.status,
            "priority": self.priority,
            "source_message": self.source_message,
            "created_at": self.created_at.isoformat(),
            "last_mentioned": self.last_mentioned.isoformat(),
            "last_followed_up": self.last_followed_up.isoformat()
            if self.last_followed_up
            else None,
            "followup_count": self.followup_count,
            "mention_count": self.mention_count,
            "related_entities": self.related_entities,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "FollowupTopic":
        return cls(
            topic_id=data["topic_id"],
            title=data["title"],
            category=data["category"],
            description=data.get("description", ""),
            status=data.get("status", "active"),
            priority=data.get("priority", "medium"),
            source_message=data.get("source_message", ""),
            created_at=datetime.fromisoformat(data["created_at"])
            if data.get("created_at")
            else None,
            last_mentioned=datetime.fromisoformat(data["last_mentioned"])
            if data.get("last_mentioned")
            else None,
            last_followed_up=datetime.fromisoformat(data["last_followed_up"])
            if data.get("last_followed_up")
            else None,
            followup_count=data.get("followup_count", 0),
            mention_count=data.get("mention_count", 1),
            related_entities=data.get("related_entities", []),
            metadata=data.get("metadata", {}),
        )

    def days_since_created(self) -> float:
        delta = datetime.now(timezone.utc) - self.created_at
        return delta.total_seconds() / 86400

    def days_since_last_mention(self) -> float:
        delta = datetime.now(timezone.utc) - self.last_mentioned
        return delta.total_seconds() / 86400

    def days_since_last_followup(self) -> Optional[float]:
        if not self.last_followed_up:
            return None
        delta = datetime.now(timezone.utc) - self.last_followed_up
        return delta.total_seconds() / 86400

    def should_followup(self) -> bool:
        if self.status != "active":
            return False

        days_mention = self.days_since_last_mention()
        days_followup = self.days_since_last_followup()

        if self.priority == "high":
            min_gap = 1.0
        elif self.priority == "medium":
            min_gap = 2.0
        else:
            min_gap = 4.0

        if days_followup is not None and days_followup < min_gap:
            return False

        return days_mention >= min_gap


class FollowupManager:
    """主动跟进管理器"""

    def __init__(self, timezone_str: str = DEFAULT_TIMEZONE):
        self.timezone_str = timezone_str
        try:
            self.zone = ZoneInfo(timezone_str)
        except Exception:
            self.zone = ZoneInfo("America/Los_Angeles")

        self.topics: list[FollowupTopic] = []
        self._lock = threading.RLock()
        self._file_path = FOLLOWUPS_FILE
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
                self.topics = [
                    FollowupTopic.from_dict(t) for t in data if isinstance(t, dict)
                ]
            logger.info(f"Loaded {len(self.topics)} followup topics")
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Failed to load followup topics: {e}")
            self.topics = []

    def _save(self):
        with self._lock:
            try:
                data = [t.to_dict() for t in self.topics]
                self._file_path.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            except OSError as e:
                logger.error(f"Failed to save followup topics: {e}")

    def add_topic(
        self,
        title: str,
        category: str,
        *,
        description: str = "",
        priority: str = "medium",
        source_message: str = "",
        related_entities: Optional[list[str]] = None,
        metadata: Optional[dict] = None,
    ) -> FollowupTopic:
        import uuid

        topic = FollowupTopic(
            topic_id=f"{uuid.uuid4().hex[:12]}",
            title=title,
            category=category,
            description=description,
            priority=priority,
            source_message=source_message,
            related_entities=related_entities,
            metadata=metadata,
        )
        with self._lock:
            self.topics.append(topic)
            self._save()
        logger.info(f"Added followup topic: {title}")
        return topic

    def extract_from_message(self, message: str) -> list[FollowupTopic]:
        """
        从消息中提取可能需要跟进的话题。
        返回新创建的话题列表。
        """
        extracted: list[FollowupTopic] = []

        for pattern, category in FOLLOWUP_PATTERNS:
            matches = re.finditer(pattern, message, re.IGNORECASE)
            for match in matches:
                content = match.group(1).strip()
                if len(content) < 3:
                    continue

                title = self._clean_topic_title(content)
                if not self._is_duplicate_topic(title):
                    priority = self._infer_priority(title, message)
                    related = self._extract_entities(message)

                    topic = self.add_topic(
                        title=title,
                        category=category,
                        priority=priority,
                        source_message=message,
                        related_entities=related,
                    )
                    extracted.append(topic)

        return extracted

    def _clean_topic_title(self, raw: str) -> str:
        clean = re.sub(r"^(一下|一些|点什么|什么)", "", raw)
        clean = re.sub(r"(看看|试试|想想|考虑)$", "", clean)
        clean = re.sub(r"\s+", " ", clean).strip()
        return clean[:100]

    def _is_duplicate_topic(self, title: str) -> bool:
        title_lower = title.lower()
        with self._lock:
            for topic in self.topics:
                if topic.status != "active":
                    continue
                if (
                    title_lower in topic.title.lower()
                    or topic.title.lower() in title_lower
                ):
                    return True
                if self._semantic_overlap(title, topic.title) > 0.7:
                    return True
        return False

    def _semantic_overlap(self, a: str, b: str) -> float:
        tokens_a = set(re.findall(r"[\u4e00-\u9fff]{2,}|[a-z]+", a.lower()))
        tokens_b = set(re.findall(r"[\u4e00-\u9fff]{2,}|[a-z]+", b.lower()))
        if not tokens_a or not tokens_b:
            return 0.0
        intersection = len(tokens_a & tokens_b)
        union = len(tokens_a | tokens_b)
        return intersection / union if union > 0 else 0.0

    def _infer_priority(self, title: str, context: str) -> str:
        combined = f"{title} {context}".lower()
        high_indicators = [
            "考试",
            "面试",
            "deadline",
            "重要",
            "紧急",
            "必须",
            "一定",
            "明天",
            "后天",
            "exam",
            "urgent",
        ]
        low_indicators = ["随便", "可能", "也许", "看看", "试试", "maybe", "might"]

        if any(ind in combined for ind in high_indicators):
            return "high"
        if any(ind in combined for ind in low_indicators):
            return "low"
        return "medium"

    def _extract_entities(self, message: str) -> list[str]:
        entities: list[str] = []
        name_patterns = [
            r"[A-Z][a-z]+",
            r"[\u4e00-\u9fff]{2,3}(?=[说告诉约见面])",
        ]
        for pattern in name_patterns:
            matches = re.findall(pattern, message)
            entities.extend(matches[:3])
        return list(dict.fromkeys(entities))[:5]

    def update_mention(self, topic_id: str):
        """更新话题的最后提及时间"""
        with self._lock:
            for topic in self.topics:
                if topic.topic_id == topic_id:
                    topic.last_mentioned = datetime.now(timezone.utc)
                    topic.mention_count += 1
                    self._save()
                    return True
        return False

    def record_followup(self, topic_id: str):
        """记录一次跟进"""
        with self._lock:
            for topic in self.topics:
                if topic.topic_id == topic_id:
                    topic.last_followed_up = datetime.now(timezone.utc)
                    topic.followup_count += 1
                    self._save()
                    return True
        return False

    def mark_completed(self, topic_id: str) -> bool:
        with self._lock:
            for topic in self.topics:
                if topic.topic_id == topic_id:
                    topic.status = "completed"
                    self._save()
                    return True
        return False

    def mark_abandoned(self, topic_id: str) -> bool:
        with self._lock:
            for topic in self.topics:
                if topic.topic_id == topic_id:
                    topic.status = "abandoned"
                    self._save()
                    return True
        return False

    def pause_topic(self, topic_id: str) -> bool:
        with self._lock:
            for topic in self.topics:
                if topic.topic_id == topic_id:
                    topic.status = "paused"
                    self._save()
                    return True
        return False

    def resume_topic(self, topic_id: str) -> bool:
        with self._lock:
            for topic in self.topics:
                if topic.topic_id == topic_id:
                    topic.status = "active"
                    self._save()
                    return True
        return False

    def get_topics_for_followup(self, limit: int = 3) -> list[FollowupTopic]:
        """获取应该跟进的话题"""
        with self._lock:
            candidates = [t for t in self.topics if t.should_followup()]
        candidates.sort(
            key=lambda x: (x.priority == "high", x.days_since_last_mention()),
            reverse=True,
        )
        return candidates[:limit]

    def get_all_active_topics(self) -> list[FollowupTopic]:
        with self._lock:
            return [t for t in self.topics if t.status == "active"]

    def generate_followup_prompt(self, topic: FollowupTopic) -> str:
        """
        生成跟进提示语，供 AI 自然融入对话。
        返回一个简短的提示，而不是完整的问句。
        """
        days = topic.days_since_last_mention()
        category = topic.category

        if category == "plan":
            if days < 2:
                return f"(可选跟进: 询问「{topic.title}」的准备进展)"
            else:
                return f"(建议跟进: 「{topic.title}」已经{int(days)}天没提了，可以自然问问进展)"

        elif category == "goal":
            return f"(可选跟进: 「{topic.title}」的目标进展如何)"

        elif category == "event":
            return f"(可选跟进: 关于「{topic.title}」的安排)"

        elif category == "ongoing":
            return f"(建议跟进: 「{topic.title}」现在怎么样了)"

        else:
            return f"(可选跟进: 之前提到的「{topic.title}」)"

    def check_message_for_topic_update(
        self, message: str
    ) -> list[tuple[FollowupTopic, str]]:
        """
        检查消息是否包含对已有话题的更新。
        返回 (话题, 更新类型) 列表。
        """
        message_lower = message.lower()
        updates: list[tuple[FollowupTopic, str]] = []

        completion_indicators = [
            "完成了",
            "做好了",
            "搞定",
            "成功",
            "达成了",
            "done",
            "finished",
            "completed",
            "achieved",
            "不用了",
            "放弃了",
            "算了",
        ]

        with self._lock:
            for topic in self.topics:
                if topic.status != "active":
                    continue

                title_lower = topic.title.lower()
                if (
                    title_lower not in message_lower
                    and self._semantic_overlap(title_lower, message_lower) < 0.5
                ):
                    continue

                self.update_mention(topic.topic_id)

                for indicator in completion_indicators:
                    if indicator in message_lower:
                        if (
                            "放弃" in indicator
                            or "算了" in indicator
                            or "不用" in indicator
                        ):
                            updates.append((topic, "abandoned"))
                        else:
                            updates.append((topic, "completed"))
                        break
                else:
                    updates.append((topic, "mentioned"))

        return updates

    def cleanup_old_topics(self, days: int = 60):
        """清理旧的非活跃话题"""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        with self._lock:
            before = len(self.topics)
            self.topics = [
                t
                for t in self.topics
                if t.status in {"active", "paused"} or t.last_mentioned > cutoff
            ]
            after = len(self.topics)
            if before != after:
                self._save()
                logger.info(f"Cleaned up {before - after} old followup topics")
