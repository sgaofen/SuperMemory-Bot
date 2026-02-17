"""
DiaryManager — AI 自动日记生成系统

功能:
  - 每天自动从 chat_archive.jsonl 中提取当天对话
  - 调用 LLM 生成温暖、有文学感的叙事日记
  - 按年/月/日组织存储为 Markdown 文件
  - 支持手动触发、重新生成、查看历史
  - 自动触发机制（消息条数 + 定时）
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import litellm

logger = logging.getLogger(__name__)

BOT_NAME = os.getenv("BOT_NAME", "AI")

# ── Defaults ──────────────────────────────────────────────────────────
DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
DIARY_DIR = DATA_DIR / "diary"
ARCHIVE_PATH = DATA_DIR / "chat_archive.jsonl"
DIARY_STATE_PATH = DATA_DIR / "diary_state.json"

DIARY_PROMPT = f"""\
你是一位温暖而细腻的日记作家。你的任务是根据我今天和 AI 伙伴 {BOT_NAME} 的对话，为我写一篇 **完整的、叙事风格的私人日记**。

## 写作要求

### 风格
- **第一人称**视角，像是我自己在写日记
- **温暖、细腻、有文学感**，像日系手账，但不做作
- **完整的叙事段落**，不是一条条的子弹列表
- 用**自然的时间线**串联一天的经历，让读者能感受到时间的流动
- 适当加入**内心独白和感受**，让日记有温度
- 如果一天中有快乐、焦虑、疲惫等不同情绪，自然地体现出来

### 结构
- 开头用一句话capture今天整体的感觉/氛围
- 按照时间线自然展开，分成几个自然段
- 每个段落有明确的主题（比如上午的课、午后的编程、晚上的放松）
- 用 `##` 二级标题分段，标题要简短有意境（如 "晨光"、"午后代码"、"深夜的小确幸"）
- 结尾有一两句对今天的总结或对明天的期待

### 内容
- 提到的具体事情要**准确**，不要编造
- 如果对话中提到了具体的课程、项目、人物，如实记录
- 记录学到的东西、做了什么决定、有什么计划
- 保留有趣的对话细节，但不是逐字复述
- 忽略纯粹的系统消息或测试消息

### 格式
- 输出纯 Markdown
- 开头一行用 `# 📔 YYYY年M月D日 星期X` 作为标题
- 然后一行日期的英文格式和情绪 emoji（例如 `*February 11, 2026 · 😊 愉快*`）
- 正文用 `##` 分段，段落之间留空行
- 不要输出多余的说明或元信息

## 今天的日期
{date_str}

## 今天的对话记录
{conversations}
"""

DIARY_PROMPT_NO_DATA = f"""\
今天（{{date_str}}）没有和 {BOT_NAME} 进行对话记录。

写一篇简短的日记，可以是:
- 做轻描淡写的一天，安静地度过
- 或者提到这是平静的一天
- 保持温暖的语气，一两段就好

格式要求同上：`# 📔 YYYY年M月D日 星期X` 标题 + 简短正文。
"""

WEEKDAY_CN = {
    0: "星期一",
    1: "星期二",
    2: "星期三",
    3: "星期四",
    4: "星期五",
    5: "星期六",
    6: "星期日",
}


class DiaryManager:
    """AI 自动日记管理器"""

    def __init__(
        self,
        timezone_str: str = "America/Los_Angeles",
        proxy_enabled: bool = False,
        proxy_url: str = "http://localhost:8080",
    ):
        self.timezone_str = timezone_str
        self.proxy_enabled = proxy_enabled
        self.proxy_url = proxy_url
        self.diary_dir = DIARY_DIR
        self.archive_path = ARCHIVE_PATH
        self.state_path = DIARY_STATE_PATH

        # Config from env
        self.enabled = os.getenv("DIARY_ENABLED", "true").lower() == "true"
        self.auto_generate = os.getenv("DIARY_AUTO_GENERATE", "true").lower() == "true"
        self.auto_generate_time = os.getenv("DIARY_AUTO_GENERATE_TIME", "23:30")
        self.auto_generate_after_messages = int(
            os.getenv("DIARY_AUTO_GENERATE_AFTER_MESSAGES", "30")
        )
        self.diary_model = os.getenv(
            "DIARY_MODEL", "anthropic/claude-opus-4-6-thinking"
        )
        self.diary_max_tokens = int(os.getenv("DIARY_MAX_TOKENS", "4096"))

        # Internal state
        self._message_count_today = 0
        self._messages_at_last_gen = 0  # messages when diary was last generated
        self._last_auto_check: Optional[datetime] = None
        self._generation_lock = threading.Lock()

        # Ensure dirs
        self.diary_dir.mkdir(parents=True, exist_ok=True)
        self._load_state()

        logger.info(
            f"DiaryManager initialized: enabled={self.enabled}, "
            f"auto={self.auto_generate}, time={self.auto_generate_time}, "
            f"model={self.diary_model}"
        )

    # ── Zone helpers ──────────────────────────────────────────────────

    def _tz(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.timezone_str)
        except Exception:
            return ZoneInfo("America/Los_Angeles")

    def _now_local(self) -> datetime:
        return datetime.now(self._tz())

    def _today_str(self) -> str:
        return self._now_local().strftime("%Y-%m-%d")

    # ── State persistence ─────────────────────────────────────────────

    def _load_state(self):
        """Load diary generation state."""
        if self.state_path.exists():
            try:
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
                self._last_generated_date = data.get("last_generated_date", "")
                self._message_count_today = data.get("message_count_today", 0)
                self._messages_at_last_gen = data.get("messages_at_last_gen", 0)
                self._count_date = data.get("count_date", "")
            except Exception:
                self._last_generated_date = ""
                self._message_count_today = 0
                self._messages_at_last_gen = 0
                self._count_date = ""
        else:
            self._last_generated_date = ""
            self._message_count_today = 0
            self._messages_at_last_gen = 0
            self._count_date = ""

    def _save_state(self):
        """Persist diary generation state."""
        data = {
            "last_generated_date": self._last_generated_date,
            "message_count_today": self._message_count_today,
            "messages_at_last_gen": self._messages_at_last_gen,
            "count_date": self._count_date,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            self.state_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as e:
            logger.error(f"Failed to save diary state: {e}")

    # ── Message counting + auto trigger ───────────────────────────────

    def record_message(self):
        """Called after each user message to track daily count."""
        today = self._today_str()
        if self._count_date != today:
            self._count_date = today
            self._message_count_today = 0
        self._message_count_today += 1
        self._save_state()

    def should_auto_generate(self) -> bool:
        """
        Check if diary should be auto-generated or updated.
        Conditions:
          1. Feature is enabled
          2. Auto-generate is enabled
          3. Either:
             a. Haven't generated today yet + (time or message threshold hit)
             b. Already generated today but enough NEW messages since last gen
        """
        if not self.enabled or not self.auto_generate:
            return False

        today = self._today_str()
        already_generated_today = (self._last_generated_date == today)
        now = self._now_local()

        # ── Case B: already generated, check for update ──
        if already_generated_today:
            if self._count_date == today:
                new_messages = self._message_count_today - self._messages_at_last_gen
                if new_messages >= self.auto_generate_after_messages:
                    logger.info(
                        f"Diary update triggered: {new_messages} new messages "
                        f"since last generation"
                    )
                    return True
            return False

        # ── Case A: first generation today ──
        # Time-based trigger: after configured time
        try:
            h, m = map(int, self.auto_generate_time.split(":"))
            trigger_time = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if now >= trigger_time:
                # Check yesterday — if we missed yesterday's diary, generate it
                yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
                if self._last_generated_date != yesterday:
                    yesterday_path = self._diary_path(yesterday)
                    if not yesterday_path.exists():
                        return True
                return True
        except (ValueError, TypeError):
            pass

        # Message count trigger
        if self._count_date == today:
            if self._message_count_today >= self.auto_generate_after_messages:
                return True

        return False

    async def maybe_auto_generate(self) -> Optional[str]:
        """
        Check and generate diary if conditions are met.
        Returns the diary content if generated, None otherwise.
        Called from enhanced_gateway after each chat.
        """
        if not self.should_auto_generate():
            return None

        # Avoid generating multiple times if called rapidly
        now = self._now_local()
        if self._last_auto_check and (now - self._last_auto_check).total_seconds() < 60:
            return None
        self._last_auto_check = now

        today = self._today_str()
        is_update = (self._last_generated_date == today)

        # Also check if yesterday's diary is missing
        if not is_update:
            yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
            if self._last_generated_date != yesterday and not self._diary_path(yesterday).exists():
                logger.info(f"Auto-generating missed diary for {yesterday}")
                try:
                    await self.generate_diary(yesterday)
                except Exception as e:
                    logger.error(f"Failed to auto-generate yesterday's diary: {e}")

        # Generate or update today's diary
        action = "Updating" if is_update else "Auto-generating"
        logger.info(f"{action} diary for {today}")
        try:
            return await self.generate_diary(today, force=is_update)
        except Exception as e:
            logger.error(f"Failed to {action.lower()} diary: {e}")
            return None

    # ── Core diary generation ─────────────────────────────────────────

    async def generate_diary(self, date: Optional[str] = None, force: bool = False) -> str:
        """
        Generate a diary entry for the given date.

        Args:
            date: YYYY-MM-DD string, defaults to today
            force: If True, regenerate even if one already exists

        Returns:
            The generated diary content (Markdown string)
        """
        if not self.enabled:
            return "⚠️ 日记功能未启用"

        target_date = date or self._today_str()

        # Check if already exists
        if not force and self._diary_path(target_date).exists():
            return self._diary_path(target_date).read_text(encoding="utf-8")

        # Get conversations for the date
        conversations = self._get_conversations_for_date(target_date)

        # Build the prompt
        dt = datetime.strptime(target_date, "%Y-%m-%d")
        tz = self._tz()
        dt_local = dt.replace(tzinfo=tz)
        weekday = WEEKDAY_CN.get(dt_local.weekday(), "")
        date_str = f"{dt_local.year}年{dt_local.month}月{dt_local.day}日 {weekday}"

        if conversations:
            formatted = self._format_conversations(conversations)
            prompt = DIARY_PROMPT.format(
                date_str=date_str,
                conversations=formatted,
            )
        else:
            prompt = DIARY_PROMPT_NO_DATA.format(date_str=date_str)

        # Call LLM
        diary_content = await self._call_llm(prompt)

        # Save
        self._save_diary(target_date, diary_content)

        # Update state
        self._last_generated_date = target_date
        self._messages_at_last_gen = self._message_count_today
        self._save_state()

        logger.info(
            f"Diary generated for {target_date}: {len(diary_content)} chars, "
            f"{len(conversations)} messages"
        )
        return diary_content

    def get_diary(self, date: Optional[str] = None) -> Optional[str]:
        """Read a diary entry for the given date."""
        target_date = date or self._today_str()
        path = self._diary_path(target_date)
        if path.exists():
            return path.read_text(encoding="utf-8")
        return None

    def list_diaries(
        self, month: Optional[str] = None, limit: int = 30
    ) -> list[dict]:
        """
        List available diary entries.

        Args:
            month: Optional YYYY-MM filter
            limit: Max entries to return

        Returns:
            List of dicts with date, path, size_chars, preview
        """
        results: list[dict] = []

        if month:
            try:
                year, mon = month.split("-")
                search_dir = self.diary_dir / year / mon
                if not search_dir.is_dir():
                    return []
                md_files = sorted(search_dir.glob("diary_*.md"), reverse=True)
            except (ValueError, OSError):
                return []
        else:
            md_files = sorted(self.diary_dir.rglob("diary_*.md"), reverse=True)

        for path in md_files[:limit]:
            fname = path.stem  # diary_YYYY-MM-DD
            date_str = fname.replace("diary_", "")
            try:
                content = path.read_text(encoding="utf-8")
                # Get first non-empty, non-heading line as preview
                preview = ""
                for line in content.split("\n"):
                    stripped = line.strip()
                    if stripped and not stripped.startswith("#") and not stripped.startswith("*"):
                        preview = stripped[:80]
                        break
                results.append(
                    {
                        "date": date_str,
                        "path": str(path),
                        "size_chars": len(content),
                        "preview": preview,
                    }
                )
            except Exception:
                results.append({"date": date_str, "path": str(path)})

        return results

    def get_diary_dates_for_month(self, year: int, month: int) -> list[int]:
        """Get list of days that have diary entries for a given month."""
        month_dir = self.diary_dir / str(year) / f"{month:02d}"
        if not month_dir.is_dir():
            return []
        days = []
        for f in month_dir.glob("diary_*.md"):
            try:
                date_str = f.stem.replace("diary_", "")
                d = datetime.strptime(date_str, "%Y-%m-%d")
                days.append(d.day)
            except ValueError:
                pass
        return sorted(days)

    # ── Data retrieval ────────────────────────────────────────────────

    def _get_conversations_for_date(self, date_str: str) -> list[dict]:
        """
        Read chat_archive.jsonl and extract all messages for a given date
        in the user's local timezone.
        """
        tz = self._tz()
        try:
            target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            logger.error(f"Invalid date format: {date_str}")
            return []

        messages: list[dict] = []
        if not self.archive_path.exists():
            return []

        try:
            with open(self.archive_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    ts_str = event.get("timestamp", "")
                    if not ts_str:
                        continue

                    try:
                        ts = datetime.fromisoformat(ts_str)
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=timezone.utc)
                        local_ts = ts.astimezone(tz)
                    except (ValueError, TypeError):
                        continue

                    if local_ts.date() != target_date:
                        continue

                    role = event.get("role", "")
                    content = event.get("content", "")
                    channel = event.get("channel_id", "")
                    kind = event.get("metadata", {}).get("kind", "")

                    # Skip test messages
                    if kind == "test" or channel == "test-channel":
                        continue

                    if role in ("user", "assistant") and content.strip():
                        messages.append(
                            {
                                "time": local_ts.strftime("%H:%M"),
                                "role": "我" if role == "user" else BOT_NAME,
                                "content": content.strip(),
                            }
                        )
        except Exception as e:
            logger.error(f"Error reading archive for diary: {e}")

        return messages

    def _format_conversations(self, messages: list[dict]) -> str:
        """Format conversation messages for the LLM prompt."""
        lines: list[str] = []
        for msg in messages:
            lines.append(f"[{msg['time']}] {msg['role']}: {msg['content']}")
        return "\n".join(lines)

    # ── LLM call ──────────────────────────────────────────────────────

    async def _call_llm(self, prompt: str) -> str:
        """Call LLM to generate diary content."""
        messages = [
            {"role": "user", "content": prompt},
        ]

        kwargs: dict = {
            "model": self.diary_model,
            "messages": messages,
            "temperature": 0.8,
            "max_tokens": self.diary_max_tokens,
        }

        # Route through proxy if enabled
        if self.proxy_enabled and self._is_proxy_model(self.diary_model):
            kwargs["api_base"] = self.proxy_url
            kwargs["api_key"] = "test"
            if "gemini" in self.diary_model.lower() and not self.diary_model.startswith("gemini/"):
                kwargs["custom_llm_provider"] = "anthropic"

        try:
            response = await litellm.acompletion(**kwargs)
            content = response.choices[0].message.content
            if isinstance(content, list):
                content = "".join(
                    part.get("text", "") if isinstance(part, dict) else str(part)
                    for part in content
                )
            return str(content or "").strip()
        except Exception as e:
            logger.error(f"Diary LLM call failed: {e}")
            raise

    @staticmethod
    def _is_proxy_model(model: str) -> bool:
        proxy_patterns = [
            "claude-sonnet", "claude-opus", "claude-haiku",
            "gemini-3", "gemini-2",
        ]
        return any(p in model.lower() for p in proxy_patterns)

    # ── File I/O ──────────────────────────────────────────────────────

    def _diary_path(self, date_str: str) -> Path:
        """Get the file path for a diary entry: data/diary/YYYY/MM/diary_YYYY-MM-DD.md"""
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            dt = self._now_local()
        year = str(dt.year)
        month = f"{dt.month:02d}"
        return self.diary_dir / year / month / f"diary_{date_str}.md"

    def _save_diary(self, date_str: str, content: str):
        """Save diary content to file."""
        path = self._diary_path(date_str)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        logger.info(f"Diary saved: {path}")

    # ── API for Web Viewer ────────────────────────────────────────────

    def get_all_diary_data(self) -> list[dict]:
        """
        Return all diary entries as a list of dicts for the web viewer API.
        Sorted by date descending.
        """
        entries: list[dict] = []
        for md_file in sorted(self.diary_dir.rglob("diary_*.md"), reverse=True):
            date_str = md_file.stem.replace("diary_", "")
            try:
                content = md_file.read_text(encoding="utf-8")
                entries.append({"date": date_str, "content": content})
            except Exception:
                pass
        return entries
