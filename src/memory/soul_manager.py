"""
SoulManager — reads and manages the soul.md personality file.

The soul.md is loaded into every system prompt to give the AI
a consistent personality and knowledge of the user.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

SOUL_PATH = Path(__file__).resolve().parent.parent.parent / "soul.md"
SOUL_BACKUP_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "soul_backups"
AUTO_SUMMARY_START = "<!-- AUTO_USER_SUMMARY_START -->"
AUTO_SUMMARY_END = "<!-- AUTO_USER_SUMMARY_END -->"
SOUL_PROMPT_MAX_CHARS = int(os.getenv("SOUL_PROMPT_MAX_CHARS", "12000"))
SOUL_SUMMARY_MAX_ITEMS = int(os.getenv("SOUL_SUMMARY_MAX_ITEMS", "15"))
SOUL_KEY_MEMORY_MAX_ITEMS = int(os.getenv("SOUL_KEY_MEMORY_MAX_ITEMS", "15"))
SOUL_TOTAL_SUMMARY_MAX = int(os.getenv("SOUL_TOTAL_SUMMARY_MAX", "50"))
SOUL_BACKUP_KEEP = int(os.getenv("SOUL_BACKUP_KEEP", "20"))
SOUL_OVERVIEW_MAX_PER_SECTION = int(os.getenv("SOUL_OVERVIEW_MAX_PER_SECTION", "3"))
SOUL_OVERVIEW_TEXT_MAX_CHARS = int(os.getenv("SOUL_OVERVIEW_TEXT_MAX_CHARS", "68"))
SOUL_OVERVIEW_MAX_KEYWORDS = int(os.getenv("SOUL_OVERVIEW_MAX_KEYWORDS", "4"))

CATEGORY_ORDER = [
    "identity",
    "profile",
    "education",
    "work",
    "project",
    "goal",
    "routine",
    "health",
    "finance",
    "relationship",
    "preference",
    "emotion",
    "location",
    "event",
    "misc",
]
CATEGORY_LABELS = {
    "identity": "身份信息",
    "profile": "个人背景",
    "education": "学习",
    "work": "工作",
    "project": "项目",
    "goal": "目标计划",
    "routine": "作息习惯",
    "health": "健康",
    "finance": "财务",
    "relationship": "关系网络",
    "preference": "偏好兴趣",
    "emotion": "情绪状态",
    "location": "地点信息",
    "event": "事件经历",
    "misc": "其他",
}
IMPORTANCE_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "unknown": 4}
SPECIAL_PERSON_NAME = os.getenv("SPECIAL_PERSON_NAME", "").strip().lower()
SPECIAL_PERSON_LABEL = os.getenv("SPECIAL_PERSON_LABEL", "Special Person")

SUMMARY_SECTION_ORDER = [
    "profile",
    "context",
    "special_person",
    "relationships",
    "boundaries",
    "other",
]

def _build_summary_section_titles() -> dict:
    return {
        "profile": "### 👤 USER Snapshot",
        "context": "### 🧩 Context & Projects",
        "special_person": f"### 💘 {SPECIAL_PERSON_LABEL}",
        "relationships": "### ❤️ Relationships",
        "boundaries": "### 🚧 Boundaries",
        "other": "### 🗂 Other Signals",
    }

SUMMARY_SECTION_TITLES = _build_summary_section_titles()


class SoulManager:
    """Read and dynamically update the AI's soul/personality file."""

    def __init__(self, path: Path = SOUL_PATH):
        self.path = path
        SOUL_BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    def read(self) -> str:
        """Read the full soul.md content."""
        if not self.path.exists():
            logger.warning(f"soul.md not found at {self.path}")
            return ""
        return self.path.read_text(encoding="utf-8")

    def get_system_prompt_section(self) -> str:
        """
        Return a condensed version of soul.md suitable for injection
        into the system prompt (to save tokens).
        """
        content = self.read()
        if not content:
            return "你是一个友好的AI伙伴。"

        content_for_prompt = content
        if len(content_for_prompt) > SOUL_PROMPT_MAX_CHARS:
            auto_summary = self._extract_auto_summary_block(content_for_prompt)
            keep_head = max(1000, SOUL_PROMPT_MAX_CHARS - len(auto_summary) - 120)
            content_for_prompt = content_for_prompt[:keep_head]
            if auto_summary:
                content_for_prompt = (
                    f"{content_for_prompt}\n\n{auto_summary}\n\n"
                    "_(soul.md 已按上下文预算截断，保留了自动用户总结)_"
                )

        return (
            "以下是你的人格定义和对用户的了解（来自 soul.md）：\n"
            "---\n"
            f"{content_for_prompt}\n"
            "---\n"
            "请始终遵循上述人格定义来回复。"
        )

    def _extract_auto_summary_block(self, content: str) -> str:
        pattern = re.compile(
            rf"{re.escape(AUTO_SUMMARY_START)}(.*?){re.escape(AUTO_SUMMARY_END)}",
            re.DOTALL,
        )
        match = pattern.search(content)
        if not match:
            return ""
        return f"{AUTO_SUMMARY_START}{match.group(1)}{AUTO_SUMMARY_END}"

    def has_meaningful_auto_summary(self) -> bool:
        """Check if soul.md currently contains a non-empty auto summary block."""
        content = self.read()
        if not content:
            return False
        block = self._extract_auto_summary_block(content)
        if not block:
            return False

        normalized = block.strip()
        if "暂无可总结的用户信息" in normalized:
            return False

        plain = (
            normalized.replace(AUTO_SUMMARY_START, "")
            .replace(AUTO_SUMMARY_END, "")
            .strip()
        )
        # Require enough content length to avoid treating marker-only blocks as meaningful.
        return len(plain) >= 60

    def _backup_current_file(self, content: str):
        """Create a timestamped soul.md backup before replacing auto summary."""
        if not content.strip():
            return

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        backup_path = SOUL_BACKUP_DIR / f"soul-{timestamp}.md"
        backup_path.write_text(content, encoding="utf-8")

        backups = sorted(SOUL_BACKUP_DIR.glob("soul-*.md"))
        if len(backups) > SOUL_BACKUP_KEEP:
            for old in backups[: len(backups) - SOUL_BACKUP_KEEP]:
                try:
                    old.unlink()
                except OSError:
                    logger.warning(f"Failed to prune old soul backup: {old}")

    @staticmethod
    def _compress_items(items: list[str], max_items: int) -> list[str]:
        """
        Deduplicate and merge similar memory items.
        Keeps the most informative version when entries overlap.
        """
        if not items:
            return []

        # Normalize and dedupe
        seen_lower: set[str] = set()
        unique: list[str] = []
        for item in items:
            lower = item.strip().lower()
            if not lower or lower in seen_lower:
                continue
            seen_lower.add(lower)
            unique.append(item.strip())

        # Merge by containment: if A is contained in B, drop A
        merged: list[str] = []
        for i, a in enumerate(unique):
            a_lower = a.lower()
            is_subset = False
            for j, b in enumerate(unique):
                if i == j:
                    continue
                if a_lower in b.lower() and len(b) > len(a):
                    is_subset = True
                    break
            if not is_subset:
                merged.append(a)

        # Merge by high token overlap (>80%)
        final: list[str] = []
        used: set[int] = set()
        for i, a in enumerate(merged):
            if i in used:
                continue
            tokens_a = set(a.lower().split())
            for j in range(i + 1, len(merged)):
                if j in used:
                    continue
                tokens_b = set(merged[j].lower().split())
                if tokens_a and tokens_b:
                    overlap = len(tokens_a & tokens_b) / max(
                        1, len(tokens_a | tokens_b)
                    )
                    if overlap >= 0.80:
                        # Keep the longer (more informative) one
                        if len(merged[j]) > len(a):
                            a = merged[j]
                            tokens_a = set(a.lower().split())
                        used.add(j)
            final.append(a)
            used.add(i)

        return final[:max_items]

    @staticmethod
    def _infer_category(text: str, mtype: str) -> str:
        t = text.lower()
        if any(k in t for k in ["名字", "生日", "年龄", "国籍", "语言", "identity"]):
            return "identity"
        if any(k in t for k in ["学校", "大学", "university", "college", "major", "课程", "读书", "专业"]):
            return "education"
        if any(k in t for k in ["工作", "实习", "公司", "office", "law office", "job"]):
            return "work"
        if any(k in t for k in ["项目", "plugin", "agent", "程序", "开发", "canvas"]):
            return "project"
        if any(k in t for k in ["计划", "目标", "打算", "希望", "未来", "完成"]):
            return "goal"
        if any(k in t for k in ["每周", "每天", "作息", "习惯", "通勤", "跑步", "游泳", "健身"]):
            return "routine"
        if any(k in t for k in ["妈妈", "父亲", "朋友", "同学", "关系", "女友", "男友", "女朋友", "男朋友"]):
            return "relationship"
        if any(k in t for k in ["喜欢", "偏好", "爱好", "兴趣", "讨厌"]):
            return "preference"
        if any(k in t for k in ["心情", "压力", "焦虑", "开心", "情绪"]):
            return "emotion"
        if any(k in t for k in ["住", "地址", "城市", "搬家", "住址", "address", "city"]):
            return "location"
        if any(k in t for k in ["钱", "薪资", "花费", "账单", "财务"]):
            return "finance"
        if any(k in t for k in ["病", "phlebotomy", "健康", "身体", "中医", "西医"]):
            return "health"
        if mtype == "preference":
            return "preference"
        if mtype == "relationship":
            return "relationship"
        if mtype == "plan":
            return "goal"
        return "misc"

    @staticmethod
    def _is_special_person_memory(text: str) -> bool:
        """Check if memory relates to the configured special person."""
        if not SPECIAL_PERSON_NAME:
            return False
        t = text.lower()
        return SPECIAL_PERSON_NAME in t

    @staticmethod
    def _is_boundary_memory(text: str) -> bool:
        t = text.lower()
        return any(
            k in t
            for k in [
                "法律",
                "违法",
                "红线",
                "底线",
                "边界",
                "不碰",
                "禁止",
                "合规",
                "grey area",
                "灰色地带",
            ]
        )

    def _summary_section_for_memory(self, category: str, mtype: str, text: str) -> str:
        if self._is_boundary_memory(text):
            return "boundaries"
        if self._is_special_person_memory(text):
            return "special_person"
        if category in {"relationship", "emotion"} or mtype == "relationship":
            return "relationships"
        if category in {"project", "goal", "work", "education", "routine", "preference", "event"}:
            return "context"
        if category in {"identity", "profile", "location", "health", "finance"}:
            return "profile"
        return "other"

    @staticmethod
    def _parse_iso_datetime(value: str) -> Optional[datetime]:
        raw = str(value or "").strip()
        if not raw:
            return None
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            try:
                dt = datetime.fromisoformat(raw[:19])
            except ValueError:
                return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    def _memory_datetime(self, memory: dict) -> Optional[datetime]:
        metadata = memory.get("metadata", {}) if isinstance(memory, dict) else {}
        for value in (
            memory.get("updated_at"),
            memory.get("created_at"),
            metadata.get("updated_at"),
            metadata.get("created_at"),
        ):
            dt = self._parse_iso_datetime(str(value or ""))
            if dt:
                return dt
        return None

    @staticmethod
    def _clean_memory_text(text: str) -> str:
        cleaned = str(text or "").strip()
        cleaned = re.sub(r"\s*（分类:.*?）\s*$", "", cleaned).strip()
        if cleaned.startswith("用户原话片段:"):
            cleaned = cleaned[len("用户原话片段:") :].strip()
        cleaned = re.sub(r"\s+", " ", cleaned)
        return cleaned

    @staticmethod
    def _shorten(text: str, max_chars: int) -> str:
        if len(text) <= max_chars:
            return text
        if max_chars <= 3:
            return text[:max_chars]
        return text[: max_chars - 3].rstrip() + "..."

    @staticmethod
    def _normalize_overview_text(text: str) -> str:
        t = str(text or "").strip()
        if not t:
            return t
        t = re.sub(r"^用户(目前|现在|正在|提到|表示)?", "", t).strip(" ：:，,")
        if not t:
            return "有新的变化"
        return t

    @staticmethod
    def _is_noise_memory(
        text: str,
        mtype: str,
        category: str,
        importance: str,
        time_scope: str,
    ) -> bool:
        t = str(text or "").strip().lower()
        if not t:
            return True
        noisy_markers = [
            "[用户发送了以下文件]",
            "这是我俩的聊天记录",
            "1保持正常",
            "啊哦，我以为你能联网的",
        ]
        if any(marker in t for marker in noisy_markers):
            return True
        if (
            mtype == "note"
            and importance in {"low", "unknown"}
            and category in {"event", "misc"}
            and time_scope == "unknown"
            and len(t) <= 80
        ):
            return True
        return False

    def _extract_keywords(self, text: str, metadata: dict) -> list[str]:
        picked: list[str] = []
        seen: set[str] = set()

        raw_keywords = metadata.get("keywords", [])
        if isinstance(raw_keywords, list):
            for kw in raw_keywords:
                kw_s = str(kw).strip()
                kw_norm = kw_s.lower()
                if not kw_s or kw_norm in seen:
                    continue
                seen.add(kw_norm)
                picked.append(kw_s)
                if len(picked) >= SOUL_OVERVIEW_MAX_KEYWORDS:
                    return picked

        for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9\-\+\.]{1,20}", text):
            token_l = token.lower()
            if token_l in {"user", "users", "unknown", "current", "future", "past"}:
                continue
            if token_l in seen:
                continue
            seen.add(token_l)
            picked.append(token)
            if len(picked) >= SOUL_OVERVIEW_MAX_KEYWORDS:
                break
        return picked

    def build_auto_summary(self, memories: list[dict]) -> str:
        """
        Build a concise, timestamped overview from vector memories.
        soul.md keeps high-level map only; complete details stay in vector DB.
        """
        if not memories:
            return (
                "## 🧠 自动用户总结（概览模式）\n\n"
                "_自动更新于 "
                f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_\n\n"
                "- 暂无可总结的用户信息。"
            )

        grouped: dict[str, dict[str, list[dict]]] = {}
        detail_count = 0
        all_timestamps: list[datetime] = []
        non_detail_count = 0

        for mem in memories:
            metadata = mem.get("metadata", {})
            mtype = str(metadata.get("type", "detail")).strip().lower()
            if mtype == "detail":
                detail_count += 1
                continue

            category = str(metadata.get("category", "misc")).strip().lower()
            if category not in CATEGORY_LABELS:
                category = "misc"

            importance = str(metadata.get("importance", "unknown")).strip().lower()
            rank = IMPORTANCE_RANK.get(importance, IMPORTANCE_RANK["unknown"])
            time_scope = str(metadata.get("time_scope", "unknown")).strip().lower()
            text = self._clean_memory_text(mem.get("memory", mem.get("text", "")))
            if not text:
                continue
            if self._is_noise_memory(text, mtype, category, importance, time_scope):
                continue

            if category == "misc":
                category = self._infer_category(text, mtype)

            dt = self._memory_datetime(mem)
            if dt:
                all_timestamps.append(dt)

            section = self._summary_section_for_memory(category, mtype, text)
            grouped.setdefault(section, {}).setdefault(category, []).append(
                {
                    "rank": rank,
                    "importance": importance,
                    "category": category,
                    "mtype": mtype,
                    "time_scope": time_scope,
                    "text": text,
                    "dt": dt,
                    "keywords": self._extract_keywords(text, metadata),
                }
            )
            non_detail_count += 1

        lines = [
            "## 🧠 自动用户总结（概览模式）",
            "",
            f"_自动更新于 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_",
            "_策略：这里只保留大纲索引，完整细节请以向量数据库检索结果为准。_",
            "",
            "### 🗺 记忆地图",
        ]

        total_topics = sum(len(v) for v in grouped.values())
        if total_topics > SOUL_TOTAL_SUMMARY_MAX:
            per_section_cap = max(1, SOUL_TOTAL_SUMMARY_MAX // max(1, len(grouped)))
        else:
            per_section_cap = min(SOUL_SUMMARY_MAX_ITEMS, SOUL_OVERVIEW_MAX_PER_SECTION)

        total_added = 0
        for section in SUMMARY_SECTION_ORDER:
            lines.append(SUMMARY_SECTION_TITLES.get(section, f"### 📚 {section}"))
            categories = grouped.get(section, {})
            if not categories:
                lines.append("- 暂无记录")
                lines.append("")
                continue

            section_topics: list[tuple[int, Optional[datetime], str]] = []
            rendered: dict[str, str] = {}
            for category, items in categories.items():
                if not items:
                    continue
                ranked = sorted(
                    items,
                    key=lambda x: (
                        x["rank"],
                        -(x["dt"].timestamp() if x["dt"] else 0),
                        -len(x["text"]),
                    ),
                )
                best = ranked[0]
                latest_dt = max(
                    (x["dt"] for x in items if x["dt"] is not None),
                    default=None,
                )
                examples: list[str] = []
                seen_example: set[str] = set()
                for entry in ranked:
                    normalized = entry["text"].lower()
                    if normalized in seen_example:
                        continue
                    seen_example.add(normalized)
                    examples.append(self._normalize_overview_text(entry["text"]))
                    if len(examples) >= 1:
                        break

                if examples:
                    overview = self._shorten(examples[0], SOUL_OVERVIEW_TEXT_MAX_CHARS)
                else:
                    overview = "有新的变化"
                keywords: list[str] = []
                seen_kw: set[str] = set()
                for entry in items:
                    for kw in entry.get("keywords", []):
                        kw_norm = str(kw).strip().lower()
                        if not kw_norm or kw_norm in seen_kw:
                            continue
                        seen_kw.add(kw_norm)
                        keywords.append(str(kw).strip())
                        if len(keywords) >= SOUL_OVERVIEW_MAX_KEYWORDS:
                            break
                    if len(keywords) >= SOUL_OVERVIEW_MAX_KEYWORDS:
                        break

                date_part = latest_dt.strftime("%Y-%m-%d") if latest_dt else "未知"
                keyword_part = "、".join(keywords) if keywords else "可用分类词检索"
                rendered[category] = (
                    f"- [{CATEGORY_LABELS.get(category, category)}/{best['importance']}/{best['time_scope']}] "
                    f"{overview} "
                    f"_(最近更新:{date_part}; 相关记忆:{len(items)}; 检索词:{keyword_part})_"
                )
                section_topics.append((best["rank"], latest_dt, category))

            section_topics.sort(
                key=lambda x: (
                    x[0],
                    -(x[1].timestamp() if x[1] else 0),
                )
            )
            shown = section_topics[:per_section_cap]
            for _, _, category in shown:
                lines.append(rendered[category])
                total_added += 1
            remaining = len(section_topics) - len(shown)
            if remaining > 0:
                lines.append(
                    f"- _(该分区还有 {remaining} 个主题在向量数据库中)_"
                )
            lines.append("")

        if total_added == 0:
            lines.append("- 暂无可总结的用户信息。")

        if all_timestamps:
            time_range = (
                f"{min(all_timestamps).strftime('%Y-%m-%d')} ~ "
                f"{max(all_timestamps).strftime('%Y-%m-%d')}"
            )
        else:
            time_range = "未知"
        lines.extend(
            [
                "### ⏱ 时间与规模",
                f"- 非细节记忆: {non_detail_count} 条",
                f"- 细节记忆: {detail_count} 条",
                f"- 时间覆盖: {time_range}",
                "",
                "_需要精确信息时，请优先依赖向量数据库检索结果，不要只依据 soul.md 概览。_",
            ]
        )

        return "\n".join(lines).strip()

    def sync_auto_summary(self, memories: list[dict]) -> bool:
        """Upsert the auto summary block inside soul.md."""
        try:
            current = self.read()
            summary = self.build_auto_summary(memories)
            block = f"{AUTO_SUMMARY_START}\n{summary}\n{AUTO_SUMMARY_END}"

            pattern = re.compile(
                rf"{re.escape(AUTO_SUMMARY_START)}.*?{re.escape(AUTO_SUMMARY_END)}",
                re.DOTALL,
            )

            if pattern.search(current):
                updated = pattern.sub(block, current)
            else:
                anchor = "## 🔧 动态更新日志"
                if anchor in current:
                    updated = current.replace(anchor, f"{block}\n\n---\n\n{anchor}", 1)
                else:
                    updated = f"{current.rstrip()}\n\n---\n\n{block}\n"

            changed = updated != current
            if updated != current:
                self._backup_current_file(current)
                self.path.write_text(updated, encoding="utf-8")
                logger.info("Soul auto summary synced")
            return changed
        except Exception as e:
            logger.error(f"Failed to sync soul auto summary: {e}")
            return False

    def propose_update(self, section: str, new_content: str) -> str:
        """
        Generate a proposed update to soul.md for user review.
        Returns a formatted proposal string (not yet applied).
        """
        return (
            f"🔄 **Soul 更新提议**\n\n"
            f"**部分**: {section}\n"
            f"**新内容**:\n{new_content}\n\n"
            f"回复「确认」来应用这个更新。"
        )

    def apply_update(self, section: str, new_content: str) -> bool:
        """
        Append a change to soul.md and log it in the update table.
        """
        try:
            current = self.read()
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            log_entry = f"| {now} | {section}: {new_content[:50]} | AI 提议，用户确认 |"

            # Append the log entry to the update table
            if "动态更新日志" in current:
                # Find the last line of the table and append
                lines = current.split("\n")
                for i in range(len(lines) - 1, -1, -1):
                    if lines[i].startswith("|") and "日期" not in lines[i] and "---" not in lines[i]:
                        lines.insert(i + 1, log_entry)
                        break
                current = "\n".join(lines)

            self.path.write_text(current, encoding="utf-8")
            logger.info(f"Soul updated: {section}")
            return True
        except Exception as e:
            logger.error(f"Failed to update soul: {e}")
            return False
