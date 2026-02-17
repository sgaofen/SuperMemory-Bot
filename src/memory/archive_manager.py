"""
ArchiveManager — append-only raw chat archive + lightweight lexical retrieval.

Design goals:
1) Keep every message forever in cheap text storage (JSONL).
2) Provide non-vector lexical backfill when vector retrieval misses details.
3) Generate bounded daily/weekly/monthly rollup text so old data does not bloat prompts.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .memory_config import DATA_DIR

logger = logging.getLogger(__name__)

RAW_ARCHIVE_PATH = Path(DATA_DIR) / "chat_archive.jsonl"
ARCHIVE_STATE_PATH = Path(DATA_DIR) / "chat_archive_state.json"

STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "that",
    "this",
    "from",
    "have",
    "has",
    "are",
    "was",
    "were",
    "you",
    "your",
    "can",
    "will",
    "just",
    "then",
    "they",
    "them",
    "about",
    "please",
    "今天",
    "昨天",
    "明天",
    "然后",
    "这个",
    "那个",
    "我们",
    "你们",
    "他们",
    "就是",
    "一下",
    "一个",
    "已经",
    "现在",
    "还是",
    "如果",
    "因为",
    "所以",
    "但是",
    "可以",
    "需要",
    "帮我",
}


class ArchiveManager:
    """Raw archive manager with simple lexical search and time-scoped rollups."""

    def __init__(
        self,
        archive_path: Path = RAW_ARCHIVE_PATH,
        state_path: Path = ARCHIVE_STATE_PATH,
    ):
        self.archive_path = archive_path
        self.state_path = state_path
        self.archive_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_files()

    def _ensure_files(self):
        if not self.archive_path.exists():
            self.archive_path.write_text("", encoding="utf-8")
        if not self.state_path.exists():
            self.state_path.write_text("{}", encoding="utf-8")

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _normalize_text(value: str) -> str:
        return re.sub(r"\s+", " ", str(value or "").strip())

    @staticmethod
    def _clip_text(value: str, max_chars: int) -> str:
        text = str(value or "").strip()
        if len(text) <= max_chars:
            return text
        if max_chars <= 3:
            return text[:max_chars]
        return text[: max_chars - 3].rstrip() + "..."

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        parts = re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]{2,}", str(text or ""))
        return {p.lower() for p in parts if p}

    def _load_state(self) -> dict:
        try:
            obj = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
        return {}

    def _save_state(self, state: dict):
        self.state_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def append_record(
        self,
        channel_id: str,
        role: str,
        content: str,
        *,
        metadata: Optional[dict] = None,
        timestamp: Optional[str] = None,
    ) -> bool:
        text = self._normalize_text(content)
        if not text:
            return False
        ts = str(timestamp or self._now_iso())
        event = {
            "event_id": hashlib.sha1(
                f"{ts}|{channel_id}|{role}|{text[:300]}".encode("utf-8")
            ).hexdigest()[:20],
            "timestamp": ts,
            "channel_id": str(channel_id or "default"),
            "role": str(role or "user"),
            "content": text,
            "metadata": metadata or {},
        }
        with self.archive_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
        return True

    def _iter_recent_events(self, max_scan_events: int) -> list[dict]:
        maxlen = max(100, int(max_scan_events))
        ring: deque[dict] = deque(maxlen=maxlen)
        with self.archive_path.open("r", encoding="utf-8") as f:
            for line in f:
                raw = line.strip()
                if not raw:
                    continue
                try:
                    item = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    ring.append(item)
        return list(ring)

    def lexical_search(
        self,
        query: str,
        *,
        limit: int = 6,
        max_scan_events: int = 25_000,
        channel_id: Optional[str] = None,
    ) -> list[dict]:
        q = self._normalize_text(query).lower()
        if not q:
            return []
        q_tokens = self._tokenize(q)
        if not q_tokens and not q:
            return []

        events = self._iter_recent_events(max_scan_events=max_scan_events)
        if not events:
            return []

        out: list[tuple[float, int, dict]] = []
        for idx, ev in enumerate(reversed(events)):  # newest first
            if channel_id and str(ev.get("channel_id", "")) != str(channel_id):
                continue
            text = self._normalize_text(ev.get("content", ""))
            if not text:
                continue

            text_lower = text.lower()
            overlap = len(self._tokenize(text_lower) & q_tokens)
            if overlap <= 0 and q not in text_lower:
                continue

            role = str(ev.get("role", "user")).strip().lower()
            recency_bonus = 1.0 / (1.0 + idx / 220.0)
            role_bonus = 0.05 if role == "user" else 0.0
            exact_bonus = 0.25 if q and q in text_lower else 0.0
            score = float(overlap) + recency_bonus + role_bonus + exact_bonus
            out.append((score, -idx, ev))

        out.sort(key=lambda x: (x[0], x[1]), reverse=True)
        results: list[dict] = []
        seen: set[str] = set()
        for score, _, ev in out:
            event_id = str(ev.get("event_id", "")).strip()
            if event_id and event_id in seen:
                continue
            if event_id:
                seen.add(event_id)
            results.append(
                {
                    "event_id": event_id,
                    "timestamp": str(ev.get("timestamp", "")),
                    "channel_id": str(ev.get("channel_id", "")),
                    "role": str(ev.get("role", "")),
                    "content": self._clip_text(self._normalize_text(ev.get("content", "")), 260),
                    "score": round(float(score), 4),
                }
            )
            if len(results) >= max(1, int(limit)):
                break
        return results

    def _build_rollup_if_needed(
        self,
        *,
        state_bucket: str,
        state_key: str,
        events: list[dict],
        title: str,
        min_messages: int,
        min_delta: int,
    ) -> Optional[dict]:
        """Build/update one rollup from filtered events when new data is enough."""
        bucket = str(state_bucket or "").strip()
        key = str(state_key or "").strip()
        if not bucket or not key:
            return None
        if len(events) < max(1, int(min_messages)):
            return None

        state = self._load_state()
        rollups = state.setdefault(bucket, {})
        prev = rollups.get(key, {}) if isinstance(rollups, dict) else {}
        prev_count = int(prev.get("message_count", 0)) if isinstance(prev, dict) else 0
        delta = len(events) - prev_count
        if prev_count > 0 and delta < max(1, int(min_delta)):
            return None

        user_count = sum(1 for ev in events if str(ev.get("role", "")).lower() == "user")
        assistant_count = sum(1 for ev in events if str(ev.get("role", "")).lower() == "assistant")
        channels = {
            str(ev.get("channel_id", "")).strip()
            for ev in events
            if ev.get("channel_id")
        }
        first_ts = str(events[0].get("timestamp", ""))
        last_ts = str(events[-1].get("timestamp", ""))

        keyword_counter: Counter[str] = Counter()
        highlights: list[str] = []
        seen_h: set[str] = set()
        for ev in events:
            role = str(ev.get("role", "")).lower()
            text = self._normalize_text(ev.get("content", ""))
            if not text:
                continue
            if role == "user":
                for tok in self._tokenize(text):
                    if len(tok) < 2 or tok in STOPWORDS:
                        continue
                    keyword_counter[tok] += 1
                clipped = self._clip_text(text, 92)
                key_h = clipped.lower()
                if key_h not in seen_h and len(highlights) < 5:
                    seen_h.add(key_h)
                    highlights.append(clipped)

        top_topics = [k for k, _ in keyword_counter.most_common(8)]
        topic_part = "、".join(top_topics) if top_topics else "暂无明显高频主题"
        highlights_part = "；".join(highlights[:3]) if highlights else "暂无代表原话"
        summary = (
            f"【{title}归档 {key}】共 {len(events)} 条消息（用户 {user_count} / AI {assistant_count}），"
            f"活跃频道 {max(1, len(channels))} 个。高频主题：{topic_part}。"
            f"代表原话：{highlights_part}。"
        )

        summary_hash = hashlib.sha1(summary.encode("utf-8")).hexdigest()
        if (
            isinstance(prev, dict)
            and prev.get("summary_hash") == summary_hash
            and prev_count == len(events)
        ):
            return None

        rollups[key] = {
            "message_count": len(events),
            "summary_hash": summary_hash,
            "updated_at": self._now_iso(),
            "first_ts": first_ts,
            "last_ts": last_ts,
        }
        state[bucket] = rollups
        self._save_state(state)

        return {
            "key": key,
            "summary": summary,
            "message_count": len(events),
            "user_count": user_count,
            "assistant_count": assistant_count,
            "channel_count": max(1, len(channels)),
            "first_ts": first_ts,
            "last_ts": last_ts,
            "summary_hash": summary_hash,
        }

    def build_daily_rollup_if_needed(
        self,
        day_key: str,
        *,
        min_messages: int = 24,
        min_delta: int = 12,
        scan_limit: int = 250_000,
    ) -> Optional[dict]:
        """
        Build/update one day rollup text from archive when enough new events arrived.
        day_key format: YYYY-MM-DD (UTC/local normalized timestamp prefix).
        """
        day = str(day_key or "").strip()
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", day):
            return None

        events = self._iter_recent_events(max_scan_events=scan_limit)
        day_events = [
            ev
            for ev in events
            if str(ev.get("timestamp", "")).strip().startswith(day)
        ]
        out = self._build_rollup_if_needed(
            state_bucket="daily_rollups",
            state_key=day,
            events=day_events,
            title="日度",
            min_messages=min_messages,
            min_delta=min_delta,
        )
        if not out:
            return None
        return {"day": day, **out}

    def build_weekly_rollup_if_needed(
        self,
        week_key: str,
        *,
        min_messages: int = 48,
        min_delta: int = 24,
        scan_limit: int = 250_000,
    ) -> Optional[dict]:
        """
        Build/update one week rollup text from archive when enough new events arrived.
        week_key format: YYYY-Www (ISO week, e.g. 2026-W07).
        """
        week = str(week_key or "").strip()
        if not re.match(r"^\d{4}-W\d{2}$", week):
            return None

        week_events: list[dict] = []
        events = self._iter_recent_events(max_scan_events=scan_limit)
        for ev in events:
            dt_raw = str(ev.get("timestamp", "")).strip()
            if not dt_raw:
                continue
            dt = None
            try:
                value = dt_raw[:-1] + "+00:00" if dt_raw.endswith("Z") else dt_raw
                dt = datetime.fromisoformat(value)
            except Exception:
                continue
            iso_year, iso_week, _ = dt.isocalendar()
            if f"{iso_year:04d}-W{iso_week:02d}" == week:
                week_events.append(ev)

        out = self._build_rollup_if_needed(
            state_bucket="weekly_rollups",
            state_key=week,
            events=week_events,
            title="周度",
            min_messages=min_messages,
            min_delta=min_delta,
        )
        if not out:
            return None
        return {"week": week, **out}

    def build_monthly_rollup_if_needed(
        self,
        month_key: str,
        *,
        min_messages: int = 80,
        min_delta: int = 40,
        scan_limit: int = 250_000,
    ) -> Optional[dict]:
        """
        Build/update one month rollup text from archive when enough new events arrived.
        month_key format: YYYY-MM (UTC timestamp prefix).
        """
        month = str(month_key or "").strip()
        if not re.match(r"^\d{4}-\d{2}$", month):
            return None

        events = self._iter_recent_events(max_scan_events=scan_limit)
        month_events = [
            ev
            for ev in events
            if str(ev.get("timestamp", "")).strip().startswith(month)
        ]
        out = self._build_rollup_if_needed(
            state_bucket="monthly_rollups",
            state_key=month,
            events=month_events,
            title="月度",
            min_messages=min_messages,
            min_delta=min_delta,
        )
        if not out:
            return None
        return {"month": month, **out}
