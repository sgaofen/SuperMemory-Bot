"""
EmotionAnalyzer — 情绪/状态感知模块

功能:
  - 从对话中推断用户当前情绪状态
  - 检测能量水平（深夜、疲惫等）
  - 为 AI 回复提供情绪上下文
  - 追踪情绪变化趋势
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

EMOTION_FILE = Path(
    os.getenv(
        "EMOTION_FILE",
        Path(__file__).parent.parent.parent / "data" / "emotion_history.json",
    )
)
DEFAULT_TIMEZONE = os.getenv("USER_TIMEZONE", "America/Los_Angeles")

EMOTION_ORDER = [
    "happy",
    "excited",
    "hopeful",
    "calm",
    "neutral",
    "confused",
    "stressed",
    "anxious",
    "frustrated",
    "sad",
    "angry",
    "tired",
]
EMOTION_CATEGORIES = set(EMOTION_ORDER)

ENERGY_LEVELS = {"high", "medium", "low", "exhausted"}

EMOTION_INDICATORS = {
    "happy": [
        "哈哈",
        "好开心",
        "太棒了",
        "太好了",
        "喜欢",
        "开心",
        "快乐",
        "awesome",
        "great",
        "happy",
        "love",
        "yay",
        "haha",
        "lol",
        "😄",
        "😊",
        "🎉",
        "❤️",
        "🥰",
    ],
    "excited": [
        "期待",
        "兴奋",
        "迫不及待",
        "终于",
        "wow",
        "omg",
        "激动",
        "太期待",
        "好期待",
        "excited",
        "cant wait",
        "🔥",
        "⭐",
        "✨",
        "🤩",
    ],
    "calm": [
        "还好",
        "不错",
        "挺好的",
        "fine",
        "okay",
        "good",
        "平静",
        "放松",
        "peaceful",
        "chill",
    ],
    "stressed": [
        "压力大",
        "好累",
        "累死",
        "烦",
        "烦死",
        "压力",
        "stressed",
        "overwhelmed",
        "pressure",
        "busy",
        "💀",
        "🤯",
        "😰",
    ],
    "anxious": [
        "担心",
        "焦虑",
        "紧张",
        "怕",
        "害怕",
        "不安",
        "worried",
        "anxious",
        "nervous",
        "scared",
        "😰",
        "😟",
        "🥺",
    ],
    "sad": [
        "难过",
        "伤心",
        "失落",
        "郁闷",
        "不开心",
        "沮丧",
        "sad",
        "upset",
        "depressed",
        "down",
        "disappointed",
        "😢",
        "😭",
        "💔",
        "😞",
    ],
    "angry": [
        "生气",
        "愤怒",
        "烦死了",
        "讨厌",
        "气死",
        "angry",
        "mad",
        "frustrated",
        "hate",
        "😤",
        "😡",
        "🤬",
    ],
    "tired": [
        "好困",
        "困死",
        "没精神",
        "乏力",
        "疲惫",
        "tired",
        "exhausted",
        "sleepy",
        "drained",
        "😴",
        "🥱",
        "😪",
    ],
    "frustrated": [
        "无语",
        "服了",
        "算了",
        "搞不定",
        "太难了",
        "frustrated",
        "stuck",
        "give up",
        "ugh",
        "😑",
        "😒",
        "🙄",
    ],
    "hopeful": [
        "希望",
        "期待",
        "应该可以",
        "说不定",
        "也许",
        "hope",
        "hopefully",
        "wish",
        "fingers crossed",
        "🤞",
        "💪",
        "🌟",
    ],
    "confused": [
        "不懂",
        "不明白",
        "什么意思",
        "搞不懂",
        "困惑",
        "confused",
        "dont understand",
        "what",
        "??",
        "🤔",
        "❓",
        "😕",
    ],
}


class EmotionState:
    """情绪状态快照"""

    def __init__(
        self,
        emotion: str = "neutral",
        confidence: float = 0.5,
        energy_level: str = "medium",
        is_late_night: bool = False,
        indicators: Optional[list[str]] = None,
        timestamp: Optional[datetime] = None,
        source_message: str = "",
    ):
        self.emotion = emotion if emotion in EMOTION_CATEGORIES else "neutral"
        self.confidence = min(1.0, max(0.0, confidence))
        self.energy_level = energy_level if energy_level in ENERGY_LEVELS else "medium"
        self.is_late_night = is_late_night
        self.indicators = indicators or []
        self.timestamp = timestamp or datetime.now(timezone.utc)
        self.source_message = source_message

    def to_dict(self) -> dict:
        return {
            "emotion": self.emotion,
            "confidence": self.confidence,
            "energy_level": self.energy_level,
            "is_late_night": self.is_late_night,
            "indicators": self.indicators,
            "timestamp": self.timestamp.isoformat(),
            "source_message": self.source_message[:200] if self.source_message else "",
        }

    @classmethod
    def from_dict(cls, data: dict) -> "EmotionState":
        return cls(
            emotion=data.get("emotion", "neutral"),
            confidence=data.get("confidence", 0.5),
            energy_level=data.get("energy_level", "medium"),
            is_late_night=data.get("is_late_night", False),
            indicators=data.get("indicators", []),
            timestamp=datetime.fromisoformat(data["timestamp"])
            if data.get("timestamp")
            else None,
            source_message=data.get("source_message", ""),
        )


class EmotionAnalyzer:
    """情绪分析器"""

    def __init__(self, timezone_str: str = DEFAULT_TIMEZONE):
        self.timezone_str = timezone_str
        try:
            self.zone = ZoneInfo(timezone_str)
        except Exception:
            self.zone = ZoneInfo("America/Los_Angeles")

        self.history: list[EmotionState] = []
        self._lock = threading.RLock()
        self._file_path = EMOTION_FILE
        self._current_state = EmotionState()
        self._ensure_file()
        self._load()

    def _ensure_file(self):
        self._file_path.parent.mkdir(parents=True, exist_ok=True)
        if not self._file_path.exists():
            self._file_path.write_text("[]", encoding="utf-8")

    def _load(self):
        try:
            data = json.loads(self._file_path.read_text(encoding="utf-8"))
            if isinstance(data, list) and data:
                self.history = [
                    EmotionState.from_dict(e) for e in data if isinstance(e, dict)
                ]
                recent = [
                    e
                    for e in self.history
                    if (datetime.now(timezone.utc) - e.timestamp).total_seconds()
                    < 86400
                ]
                if recent:
                    self._current_state = recent[-1]
            logger.info(f"Loaded {len(self.history)} emotion records")
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Failed to load emotion history: {e}")

    def _save(self):
        with self._lock:
            try:
                data = [e.to_dict() for e in self.history[-500:]]
                self._file_path.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            except OSError as e:
                logger.error(f"Failed to save emotion history: {e}")

    def analyze(self, message: str) -> EmotionState:
        """
        分析消息的情绪状态。
        返回 EmotionState 对象。
        """
        text = str(message or "").lower()
        now = datetime.now(timezone.utc)
        local_time = now.astimezone(self.zone)
        hour = local_time.hour

        is_late_night = hour >= 23 or hour <= 5

        emotion_scores: dict[str, float] = {e: 0.0 for e in EMOTION_CATEGORIES}
        found_indicators: list[str] = []

        for emotion, indicators in EMOTION_INDICATORS.items():
            for indicator in indicators:
                if indicator.lower() in text:
                    emotion_scores[emotion] += 1.0
                    found_indicators.append(indicator)

        total_score = sum(emotion_scores.values())
        if total_score > 0:
            for emotion in emotion_scores:
                emotion_scores[emotion] /= total_score

        best_emotion = max(emotion_scores.items(), key=lambda x: x[1])
        detected_emotion = best_emotion[0] if best_emotion[1] > 0.1 else "neutral"
        confidence = min(1.0, best_emotion[1] * 2)

        energy_level = self._infer_energy_level(text, is_late_night, hour)

        state = EmotionState(
            emotion=detected_emotion,
            confidence=confidence,
            energy_level=energy_level,
            is_late_night=is_late_night,
            indicators=found_indicators[:10],
            timestamp=now,
            source_message=message[:200],
        )

        with self._lock:
            self._current_state = state
            self.history.append(state)
            if len(self.history) > 500:
                self.history = self.history[-500:]
            self._save()

        return state

    def _infer_energy_level(self, text: str, is_late_night: bool, hour: int) -> str:
        if any(
            w in text
            for w in ["累死", "困死", "没力气", "筋疲力尽", "exhausted", "drained"]
        ):
            return "exhausted"
        if any(w in text for w in ["好困", "困", "困了", "sleepy", "tired"]):
            return "low"
        if any(
            w in text for w in ["精力充沛", "精神很好", "活力", "energetic", "fresh"]
        ):
            return "high"
        if is_late_night:
            return "low"
        if 6 <= hour <= 9:
            return "medium"
        if 14 <= hour <= 16:
            return "low"
        return "medium"

    def get_current_state(self) -> EmotionState:
        return self._current_state

    def get_recent_emotions(self, hours: int = 24) -> list[EmotionState]:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        with self._lock:
            return [e for e in self.history if e.timestamp > cutoff]

    def get_emotion_trend(self, hours: int = 24) -> dict:
        """
        分析近期情绪趋势。
        返回 {dominant_emotion, trend_direction, stability}
        """
        recent = self.get_recent_emotions(hours)
        if not recent:
            return {
                "dominant_emotion": "neutral",
                "trend_direction": "stable",
                "stability": 1.0,
            }

        emotion_counts: dict[str, int] = {}
        for state in recent:
            emotion_counts[state.emotion] = emotion_counts.get(state.emotion, 0) + 1

        dominant = (
            max(emotion_counts.items(), key=lambda x: x[1])[0]
            if emotion_counts
            else "neutral"
        )

        if len(recent) >= 3:
            early_avg = sum(
                EMOTION_ORDER.index(e.emotion) if e.emotion in EMOTION_ORDER else 3
                for e in recent[: len(recent) // 2]
            ) / max(1, len(recent) // 2)
            late_avg = sum(
                EMOTION_ORDER.index(e.emotion) if e.emotion in EMOTION_ORDER else 3
                for e in recent[len(recent) // 2 :]
            ) / max(1, len(recent) - len(recent) // 2)

            if late_avg < early_avg - 1:
                trend = "improving"
            elif late_avg > early_avg + 1:
                trend = "declining"
            else:
                trend = "stable"
        else:
            trend = "stable"

        unique_emotions = len(set(e.emotion for e in recent))
        stability = 1.0 - (unique_emotions - 1) / max(1, len(recent))

        return {
            "dominant_emotion": dominant,
            "trend_direction": trend,
            "stability": round(stability, 2),
        }

    def get_response_guidance(self) -> str:
        """
        根据当前情绪状态生成回复指导。
        供 AI 参考调整回复风格。
        """
        state = self._current_state
        guidance_parts: list[str] = []

        if state.is_late_night:
            guidance_parts.append("现在是深夜，用户可能需要更温和简洁的回复")

        if state.energy_level in {"low", "exhausted"}:
            guidance_parts.append("用户能量较低，回复宜简洁，避免长篇大论")

        emotion = state.emotion
        if emotion == "stressed":
            guidance_parts.append("用户有压力感，可以适当表示理解和支持")
        elif emotion == "anxious":
            guidance_parts.append("用户有些焦虑，回复宜安抚和鼓励")
        elif emotion == "sad":
            guidance_parts.append("用户情绪低落，可以温暖陪伴，不要过度说教")
        elif emotion == "happy" or emotion == "excited":
            guidance_parts.append("用户情绪积极，可以分享这份喜悦")
        elif emotion == "angry" or emotion == "frustrated":
            guidance_parts.append("用户有些烦躁，先倾听理解，避免争辩")
        elif emotion == "confused":
            guidance_parts.append("用户有些困惑，可以更清晰地解释")

        if not guidance_parts:
            return ""

        return f"[情绪感知: {'; '.join(guidance_parts)}]"

    def cleanup_old_records(self, days: int = 7):
        """清理旧记录"""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        with self._lock:
            before = len(self.history)
            self.history = [e for e in self.history if e.timestamp > cutoff]
            after = len(self.history)
            if before != after:
                self._save()
                logger.info(f"Cleaned up {before - after} old emotion records")
