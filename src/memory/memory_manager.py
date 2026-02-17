"""
MemoryManager — wraps Mem0 with personal-brain-specific semantics.

Provides typed memory operations:
  - add_memory(text, metadata)  → store a new memory
  - search(query, limit)        → find relevant memories
  - get_all()                   → list all memories
  - delete(memory_id)           → remove a memory
"""
from __future__ import annotations

import json
import logging
import os
import re
import ast
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from threading import RLock

from mem0 import Memory

from .memory_config import get_mem0_config, MEMORY_HISTORY_PATH, CHROMA_DB_PATH, DATA_DIR

logger = logging.getLogger(__name__)

# Valid memory categories
MEMORY_TYPES = {"fact", "preference", "plan", "note", "relationship", "detail"}
MEMORY_CATEGORIES = {
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
}

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
IMPORTANCE_LEVELS = {"critical", "high", "medium", "low"}
TIME_SCOPES = {"current", "past", "future", "ongoing", "unknown"}
# Configurable "special person" tracking (set via .env)
SPECIAL_PERSON_NAME = os.getenv("SPECIAL_PERSON_NAME", "").strip().lower()
SPECIAL_PERSON_LABEL = os.getenv("SPECIAL_PERSON_LABEL", "Special Person")

PROFILE_SECTION_ORDER = [
    "profile",
    "context",
    "special_person",
    "relationships",
    "boundaries",
    "other",
]

def _build_profile_section_titles() -> dict:
    titles = {
        "profile": "### 👤 USER Snapshot",
        "context": "### 🧩 Context & Projects",
        "special_person": f"### 💘 {SPECIAL_PERSON_LABEL}",
        "relationships": "### ❤️ Relationships",
        "boundaries": "### 🚧 Boundaries",
        "other": "### 🗂 Other Signals",
    }
    return titles

PROFILE_SECTION_TITLES = _build_profile_section_titles()

# Default user ID (single-user system)
DEFAULT_USER = os.getenv("BOT_USER_ID", "user")

# Dedupe + detail storage tuning
SIMILAR_DUPLICATE_SCORE = float(os.getenv("MEMORY_DUPLICATE_SCORE", "0.92"))
# Lower default to keep short but potentially useful details (e.g. "周五考试").
DETAIL_MIN_CHARS = int(os.getenv("DETAIL_MIN_CHARS", "4"))
DETAIL_CHUNK_CHARS = int(os.getenv("DETAIL_CHUNK_CHARS", "260"))
DETAIL_MAX_CHUNKS_PER_TURN = int(os.getenv("DETAIL_MAX_CHUNKS_PER_TURN", "24"))
DETAIL_GET_ALL_PAGE_SIZE = int(os.getenv("MEMORY_GET_ALL_PAGE_SIZE", "500"))
RECOVER_INCLUDE_DETAILS = os.getenv("RECOVER_INCLUDE_DETAILS", "false").lower() == "true"
# Mem0 infer=True will rewrite/merge memories via LLM.
# We default to infer=False to preserve exact user facts.
MEM0_INFER = os.getenv("MEM0_INFER", "false").lower() == "true"
AUTO_REPAIR_ON_INDEX_ERROR = os.getenv("AUTO_REPAIR_ON_INDEX_ERROR", "true").lower() == "true"
RECOVER_MAX_ITEMS = int(os.getenv("RECOVER_MAX_ITEMS", "800"))
CHROMA_BACKUP_KEEP = int(os.getenv("CHROMA_BACKUP_KEEP", "8"))
DETAIL_EXPANSION_ENABLED = os.getenv(
    "MEMORY_DETAIL_EXPANSION_ENABLED", "true"
).lower() == "true"
DETAIL_EXPANSION_ANCHORS = int(os.getenv("MEMORY_DETAIL_EXPANSION_ANCHORS", "6"))
DETAIL_EXPANSION_PER_ANCHOR = int(
    os.getenv("MEMORY_DETAIL_EXPANSION_PER_ANCHOR", "3")
)
DETAIL_EXPANSION_QUERY_LIMIT = int(
    os.getenv("MEMORY_DETAIL_EXPANSION_QUERY_LIMIT", "14")
)
DETAIL_EXPANSION_MAX = int(os.getenv("MEMORY_DETAIL_EXPANSION_MAX", "28"))
DETAIL_EXPANSION_MIN_SCORE = float(
    os.getenv("MEMORY_DETAIL_EXPANSION_MIN_SCORE", "0.55")
)

DETAIL_JSON_NOISE_KEYS = {
    "localid",
    "createtime",
    "formattedtime",
    "localtype",
    "issend",
    "sender",
    "username",
    "displayname",
    "emojiinfo",
    "mediapath",
    "session",
    "statistics",
    "messagecount",
    "messages",
    "wxid",
    "local_id",
}
DETAIL_SHORT_GENERIC_PHRASES = {
    "用chatgpt",
    "不知道",
    "不知道啊",
    "哈哈",
    "哈哈哈",
    "ok",
    "okay",
    "yes",
    "no",
    "嗯",
    "嗯嗯",
    "好的",
    "行",
}
DETAIL_SHORT_SIGNAL_KEYWORDS = {
    "考试",
    "复习",
    "周",
    "月",
    "年",
    "明天",
    "今天",
    "昨天",
    "特别的人",
    "项目",
    "脚本",
    "化学",
    "学校",
    "大学",
    "课程",
    "计划",
    "约",
    "喜欢",
    "讨厌",
    "生病",
    "兼职",
    "工作",
    "八卦",
    "闺蜜",
    "朋友",
    "同学",
    "他",
    "她",
    "谁",
    "约会",
    "暧昧",
    "关系",
}
DETAIL_AMBIGUOUS_PRONOUNS = {
    "他",
    "她",
    "他们",
    "她们",
    "这个",
    "那个",
    "这里",
    "那里",
    "这次",
    "那次",
    "这个人",
    "那个人",
    "这样",
    "那样",
}


def _normalize_text(text: str) -> str:
    """Normalize memory text for lightweight duplicate checks."""
    return re.sub(r"\s+", " ", text.strip().lower())


def _tokenize_semantic(text: str) -> set[str]:
    """
    Semantic tokens robust for mixed Chinese/English:
    - latin words / numbers
    - Chinese bigrams (improves overlap matching for no-space text)
    """
    raw = str(text or "").lower()
    tokens: set[str] = set()
    for word in re.findall(r"[a-z0-9_]+", raw):
        if word:
            tokens.add(word)

    for seq in re.findall(r"[\u4e00-\u9fff]+", raw):
        seq = seq.strip()
        if not seq:
            continue
        if len(seq) == 1:
            tokens.add(seq)
            continue
        for i in range(len(seq) - 1):
            bg = seq[i : i + 2]
            if bg:
                tokens.add(bg)
    return tokens


def _semantic_similarity(a: str, b: str) -> float:
    ta = _tokenize_semantic(a)
    tb = _tokenize_semantic(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    if union <= 0:
        return 0.0
    return inter / union


def _strip_structured_suffix(text: str) -> str:
    """Remove appended structured tags from memory text for display."""
    t = text.strip()
    return re.sub(r"\s*（分类:.*?）\s*$", "", t)


def _detail_core_text(text: str) -> str:
    """Extract plain detail text from stored wrapper format."""
    t = _strip_structured_suffix(str(text or "").strip())
    t = re.sub(r"^\s*用户原话片段:\s*", "", t)
    return re.sub(r"\s+", " ", t).strip()


def _looks_structured_blob(text: str) -> bool:
    """Detect JSON/export-like snippets that should not be stored as raw detail."""
    t = str(text or "").strip().lower()
    if not t:
        return True

    key_hits = sum(1 for key in DETAIL_JSON_NOISE_KEYS if key in t)
    punct_count = sum(t.count(ch) for ch in ('{', '}', '[', ']', '"', ':', ','))
    punct_ratio = punct_count / max(1, len(t))

    if key_hits >= 2:
        return True
    if punct_ratio >= 0.14 and t.count(":") >= 3 and ("{" in t or "[" in t):
        return True
    if t.startswith(("{", "[", '",', '"}', "},", "],")) and t.count(":") >= 2:
        return True
    if "```" in t and ("{" in t or "[" in t):
        return True
    return False


def _is_low_value_short_phrase(text: str) -> bool:
    t = re.sub(r"\s+", "", str(text or "").strip().lower())
    if not t:
        return True
    if any(k in t for k in DETAIL_SHORT_SIGNAL_KEYWORDS):
        return False
    if any(ch.isdigit() for ch in t):
        return False
    if t in DETAIL_SHORT_GENERIC_PHRASES:
        return True
    if re.fullmatch(r"(哈){2,}", t):
        return True
    if re.fullmatch(r"(嗯){2,}", t):
        return True
    if re.fullmatch(r"[a-z]{1,7}", t):
        return True
    return False


def _sanitize_metadata(metadata: dict) -> dict:
    """
    Chroma metadata accepts scalar values and non-empty scalar lists.
    Drop empty / unsupported values to avoid insertion failures.
    """
    out: dict = {}
    for key, value in (metadata or {}).items():
        if value is None:
            continue
        if isinstance(value, list):
            cleaned = [v for v in value if isinstance(v, (str, int, float, bool)) and str(v).strip()]
            if cleaned:
                out[key] = cleaned
            continue
        if isinstance(value, (dict, tuple, set)):
            # Keep vector metadata simple/stable.
            continue
        if isinstance(value, (str, int, float, bool)):
            if isinstance(value, str) and not value.strip():
                continue
            out[key] = value
    return out


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


def _infer_importance(text: str, mtype: str) -> str:
    t = text.lower()
    if any(k in t for k in ["必须", "一定", "关键", "非常重要", "urgent", "deadline"]):
        return "high"
    if mtype in {"plan", "relationship"}:
        return "high"
    if mtype == "fact":
        return "medium"
    if mtype == "detail":
        return "low"
    return "medium"


def _infer_time_scope(text: str) -> str:
    t = text.lower()
    if any(k in t for k in ["计划", "打算", "将会", "会在", "明天", "下周", "下个月", "明年", "未来"]):
        return "future"
    if any(k in t for k in ["曾经", "以前", "过去", "之前", "去年", "小时候"]):
        return "past"
    if any(k in t for k in ["每周", "每天", "通常", "习惯", "长期", "一直"]):
        return "ongoing"
    if any(k in t for k in ["现在", "目前", "当前", "正在"]):
        return "current"
    return "unknown"


def _is_special_person_memory(text: str) -> bool:
    """Check if memory relates to the configured special person."""
    if not SPECIAL_PERSON_NAME:
        return False
    t = text.lower()
    return SPECIAL_PERSON_NAME in t


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
            "hacking",
            "server hacking",
            "med school",
            "plan a",
            "plan b",
            "职业路线",
        ]
    )


def _profile_section_for_memory(category: str, mtype: str, text: str) -> str:
    if _is_boundary_memory(text):
        return "boundaries"
    if _is_special_person_memory(text):
        return "special_person"
    if category in {"relationship", "emotion"} or mtype == "relationship":
        return "relationships"
    if category in {"project", "goal", "work", "education", "routine", "preference", "event"}:
        return "context"
    if category in {"identity", "profile", "location", "health", "finance"}:
        return "profile"
    return "other"


class MemoryManager:
    """Central memory interface for the AI Brain."""

    def __init__(self, user_id: str = DEFAULT_USER):
        self.user_id = user_id
        config = get_mem0_config()
        self.memory = Memory.from_config(config)
        self._mem_lock = RLock()
        self._repair_in_progress = False
        self._history_path = Path(MEMORY_HISTORY_PATH)
        self._history_jsonl_path = self._history_path.with_suffix(".jsonl")
        self._ensure_history_file()

    @staticmethod
    def _is_index_corruption_error(exc: Exception) -> bool:
        msg = str(exc or "").lower()
        return (
            "error finding id" in msg
            or ("internalerror" in msg and "id" in msg)
            or "no such table: embeddings" in msg
            or "database disk image is malformed" in msg
            or "sqlite_corrupt" in msg
        )

    def _prune_corrupt_backups(self, backup_root: Path):
        backups = sorted(backup_root.glob("chromadb-*"))
        if len(backups) <= CHROMA_BACKUP_KEEP:
            return
        for old in backups[: len(backups) - CHROMA_BACKUP_KEEP]:
            try:
                if old.is_dir():
                    shutil.rmtree(old, ignore_errors=True)
                else:
                    old.unlink(missing_ok=True)
            except Exception:
                logger.warning(f"Failed pruning old backup: {old}")

    def _repair_vector_store_locked(self, reason: str) -> dict:
        """
        Backup corrupted Chroma DB, recreate store, then recover memories
        from history logs. Must be called under self._mem_lock.
        """
        if self._repair_in_progress:
            return {"ok": False, "reason": "repair_in_progress"}

        self._repair_in_progress = True
        db_path = Path(CHROMA_DB_PATH)
        backup_root = Path(DATA_DIR) / "chromadb_corrupt_backups"
        backup_root.mkdir(parents=True, exist_ok=True)

        backup_path = None
        recovered_stats = {"recovered": 0, "candidates": 0, "events": 0}
        try:
            if db_path.exists():
                ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
                backup_path = backup_root / f"chromadb-{ts}"
                shutil.copytree(db_path, backup_path)
                shutil.rmtree(db_path, ignore_errors=True)

            # Recreate Mem0/Chroma store and replay from history.
            self.memory = Memory.from_config(get_mem0_config())
            recovered_stats = self.recover_from_history(max_recover=RECOVER_MAX_ITEMS)
            self._prune_corrupt_backups(backup_root)

            payload = {
                "reason": reason[:500],
                "backup_path": str(backup_path) if backup_path else "",
                **recovered_stats,
            }
            self._log_event("repair_store", payload)
            logger.warning(f"Vector store repaired: {payload}")
            return {"ok": True, **payload}
        except Exception as e:
            logger.error(f"Vector store repair failed: {e}")
            return {
                "ok": False,
                "reason": reason[:500],
                "error": str(e),
                "backup_path": str(backup_path) if backup_path else "",
            }
        finally:
            self._repair_in_progress = False

    def repair_vector_store(self, reason: str = "manual") -> dict:
        """Public repair entrypoint (manual or auto-heal)."""
        with self._mem_lock:
            return self._repair_vector_store_locked(reason)

    def _safe_mem_call(self, op_name: str, fn):
        """
        Serialize Mem0 operations and auto-repair corrupted indices once.
        """
        with self._mem_lock:
            try:
                return fn()
            except Exception as e:
                if not AUTO_REPAIR_ON_INDEX_ERROR or not self._is_index_corruption_error(e):
                    raise

                logger.error(f"Mem0 {op_name} failed with index error, attempting repair: {e}")
                repair = self._repair_vector_store_locked(reason=f"{op_name}: {e}")
                if not repair.get("ok"):
                    raise
                # Retry once after repair.
                return fn()

    def _ensure_history_file(self):
        """Create memory history logs if they don't exist."""
        if not self._history_path.exists():
            self._history_path.parent.mkdir(parents=True, exist_ok=True)
            self._history_path.write_text("[]", encoding="utf-8")
        if not self._history_jsonl_path.exists():
            self._history_jsonl_path.parent.mkdir(parents=True, exist_ok=True)
            self._history_jsonl_path.write_text("", encoding="utf-8")

    def _load_history_events(self) -> list[dict]:
        """Load history events from JSON array and JSONL logs."""
        events: list[dict] = []

        # Legacy JSON array log
        try:
            if self._history_path.exists():
                loaded = json.loads(self._history_path.read_text(encoding="utf-8"))
                if isinstance(loaded, list):
                    events.extend(item for item in loaded if isinstance(item, dict))
        except (json.JSONDecodeError, OSError):
            pass

        # JSONL append log
        try:
            if self._history_jsonl_path.exists():
                for line in self._history_jsonl_path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(obj, dict):
                        events.append(obj)
        except OSError:
            pass

        # Dedupe by stable tuple
        seen = set()
        unique: list[dict] = []
        for ev in events:
            key = (
                ev.get("timestamp", ""),
                ev.get("action", ""),
                json.dumps(ev.get("data", {}), ensure_ascii=False, sort_keys=True),
            )
            if key in seen:
                continue
            seen.add(key)
            unique.append(ev)
        unique.sort(key=lambda x: str(x.get("timestamp", "")))
        return unique

    @staticmethod
    def _extract_id_text_from_result(result_str: str) -> dict[str, str]:
        """
        Parse Mem0 result string and map memory id -> text.
        Example string:
        "{'results': [{'id': '...', 'memory': '用户喜欢蓝色', 'event': 'ADD'}]}"
        """
        if not result_str:
            return {}
        try:
            obj = ast.literal_eval(result_str)
        except Exception:
            return {}
        if not isinstance(obj, dict):
            return {}
        out: dict[str, str] = {}
        for item in obj.get("results", []):
            if not isinstance(item, dict):
                continue
            mid = str(item.get("id", "")).strip()
            text = str(item.get("memory", "")).strip()
            if mid and text:
                out[mid] = text
        return out

    def _log_event(self, action: str, data: dict):
        """Append an event to memory audit logs."""
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "data": data,
        }

        # Fast path: append JSONL line (scales for high write volume).
        with self._history_jsonl_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

        # Backward-compatible JSON array log; avoid expensive rewrites once it grows.
        try:
            if self._history_path.exists() and self._history_path.stat().st_size <= 512_000:
                history = json.loads(self._history_path.read_text(encoding="utf-8"))
                if not isinstance(history, list):
                    history = []
            else:
                return
        except (json.JSONDecodeError, FileNotFoundError):
            history = []

        history.append(event)
        self._history_path.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")

    def recover_from_history(self, max_recover: int = 500) -> dict:
        """
        Rebuild vector memories from history logs.
        Intended for accidental DB loss recovery.
        """
        events = self._load_history_events()
        if not events:
            return {"recovered": 0, "candidates": 0, "reason": "no_history"}

        id_to_text: dict[str, str] = {}
        deleted_ids: set[str] = set()
        candidates: list[tuple[str, str, dict]] = []

        for ev in events:
            action = str(ev.get("action", "")).strip()
            data = ev.get("data", {}) if isinstance(ev.get("data"), dict) else {}

            if action == "add":
                text = str(data.get("text", "")).strip()
                metadata = data.get("metadata", {}) if isinstance(data.get("metadata"), dict) else {}
                mtype = str(metadata.get("type", "note")).strip() or "note"
                source = str(metadata.get("source", "recovery")).strip() or "recovery"
                if mtype == "detail" and not RECOVER_INCLUDE_DETAILS:
                    continue
                if source == "raw_message" and not RECOVER_INCLUDE_DETAILS:
                    continue
                extra_meta = {k: v for k, v in metadata.items() if k not in {"type", "source", "created_at"}}
                if text:
                    candidates.append((text, mtype, {"source": source, **extra_meta}))

                parsed = self._extract_id_text_from_result(str(data.get("result", "")))
                id_to_text.update(parsed)

            elif action == "delete":
                memory_id = str(data.get("memory_id", "")).strip()
                if memory_id:
                    deleted_ids.add(memory_id)

        # Skip entries that were explicitly deleted later.
        deleted_texts = {id_to_text[mid] for mid in deleted_ids if mid in id_to_text}
        existing_texts = {
            _normalize_text(self._memory_text(item))
            for item in self.get_all()
            if self._memory_text(item)
        }
        restored_texts: set[str] = set()

        recovered = 0
        processed = 0
        for text, mtype, meta in candidates:
            if processed >= max_recover:
                break
            processed += 1
            if text in deleted_texts:
                continue
            norm = _normalize_text(text)
            if not norm:
                continue
            if norm in existing_texts or norm in restored_texts:
                continue
            result = self.add(
                text=text,
                memory_type=mtype,
                source=meta.get("source", "recovery"),
                metadata={
                    **meta,
                    "recovered_from_history": True,
                },
                allow_duplicate=True,
            )
            if result.get("ok"):
                recovered += 1
                restored_texts.add(norm)

        return {
            "recovered": recovered,
            "candidates": len(candidates),
            "events": len(events),
        }

    @staticmethod
    def _memory_text(item: dict) -> str:
        return str(item.get("memory", item.get("text", ""))).strip()

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

    def _memory_datetime(self, item: dict) -> Optional[datetime]:
        metadata = item.get("metadata", {}) if isinstance(item, dict) else {}
        candidates = [
            item.get("updated_at"),
            item.get("created_at"),
            metadata.get("updated_at"),
            metadata.get("created_at"),
        ]
        for value in candidates:
            dt = self._parse_iso_datetime(str(value or ""))
            if dt:
                return dt
        return None

    @staticmethod
    def _memory_score(item: dict) -> float:
        score = item.get("score")
        if isinstance(score, (int, float)):
            return float(score)
        return 0.0

    def _memory_identity(self, item: dict) -> str:
        memory_id = str(item.get("id", "")).strip()
        if memory_id:
            return f"id:{memory_id}"
        text = _normalize_text(self._memory_text(item))
        if text:
            return f"text:{text}"
        return ""

    def _enrich_memory_item(self, item: dict) -> dict:
        """
        Fill missing metadata for legacy memories at read time so downstream
        components (summary/profile/retrieval formatting) stay organized.
        """
        if not isinstance(item, dict):
            return item
        out = dict(item)
        metadata = dict(out.get("metadata") or {})
        text = self._memory_text(out)

        mtype = str(metadata.get("type", "note")).strip().lower()
        if mtype not in MEMORY_TYPES:
            if text.startswith("用户原话片段:"):
                mtype = "detail"
            else:
                mtype = "note"
            metadata["type"] = mtype

        category = str(metadata.get("category", "")).strip().lower()
        if category not in MEMORY_CATEGORIES:
            category = _infer_category(text, mtype)
            metadata["category"] = category
        metadata.setdefault("category_label", CATEGORY_LABELS.get(category, "其他"))

        importance = str(metadata.get("importance", "")).strip().lower()
        if importance not in IMPORTANCE_LEVELS:
            metadata["importance"] = _infer_importance(text, mtype)

        time_scope = str(metadata.get("time_scope", "")).strip().lower()
        if time_scope not in TIME_SCOPES:
            metadata["time_scope"] = _infer_time_scope(text)

        out["metadata"] = metadata
        return out

    def _is_duplicate(self, text: str, limit: int = 5) -> bool:
        """
        Check if a memory is already stored.

        Uses semantic search + normalized exact/containment match to avoid
        obvious duplicates while keeping semantically new details.
        """
        normalized = _normalize_text(text)
        if not normalized:
            return True

        try:
            candidates = self.search(text, limit=limit)
        except Exception:
            return False

        for item in candidates:
            existing = _normalize_text(self._memory_text(item))
            if not existing:
                continue
            if normalized == existing:
                return True

            # Containment match for near-identical phrases.
            if (
                min(len(normalized), len(existing)) >= 18
                and (normalized in existing or existing in normalized)
            ):
                return True

            score = item.get("score")
            if isinstance(score, (float, int)) and score >= SIMILAR_DUPLICATE_SCORE:
                # Keep this conservative: high score + significant overlap.
                overlap = _semantic_similarity(normalized, existing)
                if overlap >= 0.72:
                    return True

            # Secondary semantic guard for Chinese/no-space text.
            if _semantic_similarity(normalized, existing) >= 0.84:
                return True
        return False

    def _verify_stored(self, text: str, limit: int = 5) -> bool:
        """Best-effort verification that text exists in vector memory."""
        try:
            candidates = self.search(text, limit=limit)
        except Exception:
            return False
        normalized = _normalize_text(text)
        if not normalized:
            return False
        for item in candidates:
            existing = _normalize_text(self._memory_text(item))
            if not existing:
                continue
            if normalized == existing:
                return True
            if (
                min(len(normalized), len(existing)) >= 18
                and (normalized in existing or existing in normalized)
            ):
                return True
            score = item.get("score")
            if isinstance(score, (float, int)) and score >= 0.78:
                return True
            if _semantic_similarity(normalized, existing) >= 0.84:
                return True
        return False

    def add(
        self,
        text: str,
        memory_type: str = "note",
        source: str = "discord",
        metadata: Optional[dict] = None,
        allow_duplicate: bool = False,
    ) -> dict:
        """
        Store a new memory.

        Args:
            text: The memory content (natural language).
            memory_type: One of fact/preference/plan/note/relationship/detail.
            source: Where this memory came from.
            metadata: Additional key-value pairs.

        Returns:
            The Mem0 result dict.
        """
        text = text.strip()
        if not text:
            return {"results": [], "event": "SKIP_EMPTY", "ok": False}

        if memory_type not in MEMORY_TYPES:
            logger.warning(f"Unknown memory type '{memory_type}', defaulting to 'note'")
            memory_type = "note"

        if not allow_duplicate and self._is_duplicate(text):
            self._log_event(
                "skip_duplicate",
                {"text": text, "type": memory_type, "source": source},
            )
            return {"results": [], "event": "SKIP_DUPLICATE", "ok": False}

        meta = {
            "type": memory_type,
            "source": source,
            "created_at": datetime.now(timezone.utc).isoformat(),
            **(metadata or {}),
        }
        meta = _sanitize_metadata(meta)

        raw_result = self._safe_mem_call(
            "add",
            lambda: self.memory.add(
                text,
                user_id=self.user_id,
                metadata=meta,
                infer=MEM0_INFER,
            ),
        )
        if isinstance(raw_result, dict):
            result = dict(raw_result)
        else:
            result = {"raw_result": str(raw_result)}
        result.setdefault("results", [])

        inserted = bool(result.get("results"))
        if not inserted:
            inserted = self._verify_stored(text)

        # Retry once with clean core text if structured suffix may hurt insertion.
        if not inserted:
            retry_text = _strip_structured_suffix(text)
            if retry_text and retry_text != text:
                try:
                    retry_raw = self._safe_mem_call(
                        "add_retry",
                        lambda: self.memory.add(
                            retry_text,
                            user_id=self.user_id,
                            metadata=meta,
                            infer=MEM0_INFER,
                        ),
                    )
                    if isinstance(retry_raw, dict):
                        retry_results = retry_raw.get("results", [])
                    else:
                        retry_results = []
                    inserted = bool(retry_results) or self._verify_stored(retry_text)
                    if inserted:
                        result["retry_text"] = retry_text
                except Exception as e:
                    logger.error(f"Retry memory add failed: {e}")

        result["inserted"] = inserted
        result["ok"] = inserted

        self._log_event("add", {"text": text, "metadata": meta, "result": str(result)})
        if inserted:
            logger.info(f"Memory added: {text[:80]}...")
        else:
            logger.warning(f"Memory add unverified (will rely on history recovery): {text[:80]}...")
        return result

    def correct(
        self,
        memory_id: str,
        new_text: str,
        *,
        memory_type: Optional[str] = None,
        source: str = "manual_correction",
        metadata: Optional[dict] = None,
        keep_old: bool = False,
    ) -> dict:
        """
        Correct an existing memory by replacing (default) or forking it.
        This gives a real 'modify' path instead of delete+recreate manually.
        """
        memory_id = str(memory_id or "").strip()
        new_text = str(new_text or "").strip()
        if not memory_id:
            return {"ok": False, "error": "memory_id is required"}
        if not new_text:
            return {"ok": False, "error": "new_text is required"}

        current = None
        for item in self.get_all():
            if str(item.get("id", "")).strip() == memory_id:
                current = item
                break
        if not current:
            return {"ok": False, "error": f"memory {memory_id} not found"}

        old_meta = dict(current.get("metadata") or {})
        mtype = memory_type or old_meta.get("type", "note")
        merged_meta = {
            **old_meta,
            **(metadata or {}),
            "corrected_from_id": memory_id,
            "corrected_at": datetime.now(timezone.utc).isoformat(),
        }
        if not keep_old:
            try:
                self.delete(memory_id)
            except Exception as e:
                return {"ok": False, "error": f"delete failed: {e}"}

        add_result = self.add(
            text=new_text,
            memory_type=mtype,
            source=source,
            metadata=merged_meta,
            allow_duplicate=True,
        )
        self._log_event(
            "correct",
            {
                "old_memory_id": memory_id,
                "new_text": new_text,
                "keep_old": keep_old,
                "result": str(add_result),
            },
        )
        return {"ok": bool(add_result.get("ok")), "result": add_result}

    def _is_low_value_detail_chunk(self, text: str) -> bool:
        """
        Conservative noise filter:
        - Drop clearly structured export fragments (JSON/chat dump shards).
        - Drop ultra-short generic fillers with no temporal/topic signal.
        """
        core = _detail_core_text(text)
        if len(core) < DETAIL_MIN_CHARS:
            return True
        if _looks_structured_blob(core):
            return True
        if "[结构化内容已省略]" in core:
            residue = core.replace("[结构化内容已省略]", " ")
            residue = re.sub(r"\*\*[^*]+\*\*", " ", residue)
            residue = re.sub(
                r"(📄|📎|🎙️|🖼️|内容:|语音转写:|voice-message\.[a-z0-9]+)",
                " ",
                residue,
                flags=re.IGNORECASE,
            )
            residue = re.sub(r"\s+", " ", residue).strip()
            if len(residue) < DETAIL_MIN_CHARS:
                return True

        tight = re.sub(r"\s+", "", core)
        if len(tight) <= 14 and _is_low_value_short_phrase(core):
            return True
        return False

    def _sanitize_raw_detail_source(self, text: str) -> str:
        """
        Remove heavy structured blobs before chunking raw details,
        while keeping conversational/voice text.
        """
        raw = str(text or "")
        if not raw.strip():
            return ""

        # Replace large fenced blocks (usually file dumps) with a marker.
        fence_pattern = re.compile(r"```(?:[^\n]*\n)?(.*?)```", re.DOTALL)

        def _replace_fence(match):
            body = match.group(1).strip()
            if len(body) <= 200 and not _looks_structured_blob(body):
                return body
            return " [结构化内容已省略] "

        raw = fence_pattern.sub(_replace_fence, raw)
        raw = re.sub(r"\s*\[用户发送了以下文件\]\s*", " ", raw)
        raw = re.sub(r"📄\s*\*\*[^*]+\*\*\s*内容:\s*", " ", raw)
        raw = re.sub(r"🎙️\s*\*\*[^*]+\*\*\s*语音转写:\s*", " ", raw)
        raw = re.sub(r"\s+", " ", raw).strip()
        return raw

    def _is_ambiguous_detail_chunk(self, text: str) -> bool:
        """
        Decide whether a detail chunk likely depends on prior turns.
        Used to trigger context-enriched detail storage.
        """
        core = _detail_core_text(text)
        if not core:
            return False
        tight = re.sub(r"\s+", "", core)
        disambiguating_signals = DETAIL_SHORT_SIGNAL_KEYWORDS - {
            "他",
            "她",
            "他们",
            "她们",
            "谁",
            "关系",
        }
        if any(k in tight.lower() for k in disambiguating_signals):
            return False
        if len(tight) <= 14:
            return True
        if any(p in core for p in DETAIL_AMBIGUOUS_PRONOUNS):
            return True
        return False

    @staticmethod
    def _clip_text(value: str, max_chars: int) -> str:
        text = str(value or "").strip()
        if len(text) <= max_chars:
            return text
        if max_chars <= 3:
            return text[:max_chars]
        return text[: max_chars - 3].rstrip() + "..."

    def _split_detail_chunks(self, text: str) -> list[str]:
        """
        Split long raw user messages into searchable detail chunks.
        Keeps sentence-level granularity first, then falls back to fixed chunks.
        """
        cleaned = self._sanitize_raw_detail_source(text)
        if len(cleaned) < DETAIL_MIN_CHARS:
            return []

        chunks: list[str] = []
        parts = re.split(r"[。！？!?；;\n]+", cleaned)

        for part in parts:
            part = part.strip(" \t\r\n-•")
            if len(part) < DETAIL_MIN_CHARS:
                continue
            while len(part) > DETAIL_CHUNK_CHARS:
                head = part[:DETAIL_CHUNK_CHARS].strip()
                if not self._is_low_value_detail_chunk(head):
                    chunks.append(head)
                part = part[DETAIL_CHUNK_CHARS:].strip()
                if len(chunks) >= DETAIL_MAX_CHUNKS_PER_TURN:
                    return [c for c in chunks if c]
            if part and not self._is_low_value_detail_chunk(part):
                chunks.append(part)
            if len(chunks) >= DETAIL_MAX_CHUNKS_PER_TURN:
                return [c for c in chunks if c]

        # If no sentence split succeeded, fallback to fixed slices.
        if not chunks:
            cursor = 0
            while cursor < len(cleaned) and len(chunks) < DETAIL_MAX_CHUNKS_PER_TURN:
                chunk = cleaned[cursor : cursor + DETAIL_CHUNK_CHARS].strip()
                if len(chunk) >= DETAIL_MIN_CHARS and not self._is_low_value_detail_chunk(chunk):
                    chunks.append(chunk)
                cursor += DETAIL_CHUNK_CHARS

        return [c for c in chunks if c]

    def add_user_message_details(
        self,
        message: str,
        source: str = "raw_message",
        metadata: Optional[dict] = None,
        context_hint: str = "",
    ) -> int:
        """
        Persist the user's raw message details into vector memory.
        This preserves nuance that structured extraction may miss.
        """
        chunks = self._split_detail_chunks(message)
        hint = self._sanitize_raw_detail_source(context_hint or "")
        hint = self._clip_text(hint, 200) if hint else ""
        if not chunks:
            cleaned = self._sanitize_raw_detail_source(message)
            if cleaned and hint and self._is_ambiguous_detail_chunk(cleaned):
                chunks = [cleaned]
            else:
                return 0

        total = len(chunks)
        added = 0
        for idx, chunk in enumerate(chunks, start=1):
            chunk = chunk.replace("[结构化内容已省略]", " ").strip()
            chunk = re.sub(r"\s+", " ", chunk)
            if not chunk:
                continue
            enriched = chunk
            context_enriched = False
            if hint and self._is_ambiguous_detail_chunk(chunk):
                enriched = f"上下文: {hint}；当前补充: {chunk}"
                context_enriched = True

            if self._is_low_value_detail_chunk(enriched):
                continue
            category = _infer_category(enriched, "detail")
            category_label = CATEGORY_LABELS.get(category, "其他")
            structured_chunk = (
                f"用户原话片段: {enriched} "
                f"（分类:{category_label}/{category}; 重要度:low; 时态:unknown）"
            )
            result = self.add(
                text=structured_chunk,
                memory_type="detail",
                source=source,
                metadata={
                    "category": category,
                    "category_label": category_label,
                    "importance": "low",
                    "time_scope": "unknown",
                    "segment_index": idx,
                    "segment_total": total,
                    "context_enriched": context_enriched,
                    **(metadata or {}),
                },
                allow_duplicate=False,
            )
            if result.get("ok"):
                added += 1
        return added

    def search(self, query: str, limit: int = 5) -> list[dict]:
        """
        Search memories by semantic similarity.

        Args:
            query: Natural language search query.
            limit: Maximum results to return.

        Returns:
            List of memory dicts with id, text, metadata, score.
        """
        results = self._safe_mem_call(
            "search",
            lambda: self.memory.search(query, user_id=self.user_id, limit=limit),
        )
        logger.debug(f"Memory search '{query}' returned {len(results.get('results', []))} results")
        raw = results.get("results", [])
        return [self._enrich_memory_item(item) for item in raw]

    def search_with_details(self, query: str, limit: int = 5) -> list[dict]:
        """
        Two-stage retrieval:
        1) Semantic primary memories for intent understanding.
        2) Detail expansion from raw user chunks using primary memories as anchors.
        """
        primary = self.search(query, limit=limit)
        if not primary or not DETAIL_EXPANSION_ENABLED:
            return primary

        merged: list[dict] = []
        seen: set[str] = set()

        def push(item: dict, retrieval_source: str) -> bool:
            identity = self._memory_identity(item)
            if not identity or identity in seen:
                return False
            out = dict(item)
            metadata = dict(out.get("metadata") or {})
            metadata.setdefault("retrieval_source", retrieval_source)
            out["metadata"] = metadata
            merged.append(out)
            seen.add(identity)
            return True

        for item in primary:
            push(item, "primary")

        anchors: list[dict] = []
        for item in primary:
            mtype = str(item.get("metadata", {}).get("type", "note")).strip().lower()
            if mtype == "detail":
                continue
            anchors.append(item)
            if len(anchors) >= max(1, DETAIL_EXPANSION_ANCHORS):
                break

        searched_terms: set[str] = set()
        detail_added = 0
        for anchor in anchors:
            if detail_added >= max(0, DETAIL_EXPANSION_MAX):
                break

            anchor_meta = anchor.get("metadata", {}) if isinstance(anchor, dict) else {}
            anchor_category = str(anchor_meta.get("category", "misc")).strip().lower()
            anchor_text = _strip_structured_suffix(self._memory_text(anchor))
            if not anchor_text:
                continue

            query_terms = [anchor_text]
            keywords = anchor_meta.get("keywords", [])
            if isinstance(keywords, list):
                for kw in keywords:
                    kw_s = str(kw).strip()
                    if kw_s:
                        query_terms.append(kw_s)
                    if len(query_terms) >= 4:
                        break

            per_anchor_added = 0
            for term in query_terms:
                if (
                    detail_added >= max(0, DETAIL_EXPANSION_MAX)
                    or per_anchor_added >= max(1, DETAIL_EXPANSION_PER_ANCHOR)
                ):
                    break
                term_norm = _normalize_text(term)
                if not term_norm or term_norm in searched_terms:
                    continue
                searched_terms.add(term_norm)

                candidates = self.search(
                    term,
                    limit=max(DETAIL_EXPANSION_QUERY_LIMIT, DETAIL_EXPANSION_PER_ANCHOR),
                )
                candidates.sort(
                    key=lambda x: (
                        self._memory_score(x),
                        self._memory_datetime(x) or datetime.min.replace(tzinfo=timezone.utc),
                    ),
                    reverse=True,
                )

                for cand in candidates:
                    if (
                        detail_added >= max(0, DETAIL_EXPANSION_MAX)
                        or per_anchor_added >= max(1, DETAIL_EXPANSION_PER_ANCHOR)
                    ):
                        break

                    metadata = cand.get("metadata", {}) if isinstance(cand, dict) else {}
                    cand_type = str(metadata.get("type", "note")).strip().lower()
                    if cand_type != "detail":
                        continue

                    score = self._memory_score(cand)
                    if score > 0 and score < DETAIL_EXPANSION_MIN_SCORE:
                        continue

                    cand_category = str(metadata.get("category", "misc")).strip().lower()
                    if (
                        anchor_category
                        and anchor_category != "misc"
                        and cand_category
                        and cand_category != anchor_category
                    ):
                        continue

                    if push(cand, "detail_expand"):
                        detail_added += 1
                        per_anchor_added += 1

        if detail_added:
            logger.debug(
                "Memory detail expansion added %s items for query '%s'",
                detail_added,
                query[:80],
            )
        return merged

    def get_total_count(self) -> int:
        """Return total vector rows (all users in this collection)."""
        try:
            return int(self.memory.vector_store.collection.count())
        except Exception:
            return 0

    def _get_all_from_chroma(self, limit: Optional[int] = None) -> list[dict]:
        """
        Fetch full memories for this user by paging Chroma directly.
        Mem0's get_all has a hard default limit=100, which is not enough.
        """
        collection = self.memory.vector_store.collection
        page_size = max(50, int(DETAIL_GET_ALL_PAGE_SIZE))
        remaining = None if limit is None else max(0, int(limit))
        offset = 0
        out: list[dict] = []

        promoted_payload_keys = {"user_id", "agent_id", "run_id", "actor_id", "role"}
        core_keys = {"data", "hash", "created_at", "updated_at", *promoted_payload_keys}
        where = {"user_id": self.user_id}

        while True:
            fetch_limit = page_size if remaining is None else min(page_size, remaining)
            if fetch_limit <= 0:
                break

            batch = collection.get(where=where, limit=fetch_limit, offset=offset)
            ids = batch.get("ids", []) or []
            docs = batch.get("documents", []) or []
            metas = batch.get("metadatas", []) or []
            if not ids:
                break

            for idx, mem_id in enumerate(ids):
                meta = metas[idx] if idx < len(metas) and isinstance(metas[idx], dict) else {}
                doc_text = docs[idx] if idx < len(docs) else ""
                memory_text = str(doc_text or meta.get("data", "") or "").strip()
                item = {
                    "id": mem_id,
                    "memory": memory_text,
                    "hash": meta.get("hash"),
                    "created_at": meta.get("created_at"),
                    "updated_at": meta.get("updated_at"),
                }
                for key in promoted_payload_keys:
                    if key in meta:
                        item[key] = meta[key]

                additional = {k: v for k, v in meta.items() if k not in core_keys}
                if additional:
                    item["metadata"] = additional
                out.append(item)

            fetched = len(ids)
            offset += fetched
            if remaining is not None:
                remaining -= fetched
                if remaining <= 0:
                    break
            if fetched < fetch_limit:
                break
        return out

    def get_all(self, limit: Optional[int] = None) -> list[dict]:
        """
        Return stored memories for this user.
        - limit=None: full scan (paged, no 100-item truncation)
        - limit=N: at most N items
        """
        raw: list[dict] = []
        try:
            raw = self._safe_mem_call(
                "get_all_paged",
                lambda: self._get_all_from_chroma(limit=limit),
            )
        except Exception as e:
            logger.warning(f"Paged get_all fallback to mem0.get_all: {e}")
            fallback_limit = int(limit) if limit is not None else 5000
            results = self._safe_mem_call(
                "get_all",
                lambda: self.memory.get_all(user_id=self.user_id, limit=fallback_limit),
            )
            raw = results.get("results", [])
        return [self._enrich_memory_item(item) for item in raw]

    def cleanup_low_value_details(
        self,
        *,
        dry_run: bool = True,
        max_delete: int = 800,
    ) -> dict:
        """
        Remove clearly useless raw detail shards while preserving meaningful small details.
        """
        all_memories = self.get_all()
        candidates: list[dict] = []
        for item in all_memories:
            metadata = item.get("metadata", {}) if isinstance(item, dict) else {}
            mtype = str(metadata.get("type", "note")).strip().lower()
            source = str(metadata.get("source", "")).strip().lower()
            if mtype != "detail" or source != "raw_message":
                continue

            text = self._memory_text(item)
            if self._is_low_value_detail_chunk(text):
                candidates.append(item)

        deleted = 0
        deleted_ids: list[str] = []
        errors: list[str] = []
        if not dry_run:
            for item in candidates[: max(1, int(max_delete))]:
                memory_id = str(item.get("id", "")).strip()
                if not memory_id:
                    continue
                try:
                    self.delete(memory_id)
                    deleted += 1
                    deleted_ids.append(memory_id)
                except Exception as e:
                    errors.append(f"{memory_id}: {e}")

        sample = []
        for item in candidates[:10]:
            sample.append(
                {
                    "id": str(item.get("id", "")),
                    "text": _detail_core_text(self._memory_text(item))[:160],
                }
            )

        return {
            "ok": True,
            "total_memories": len(all_memories),
            "candidates": len(candidates),
            "deleted": deleted,
            "dry_run": dry_run,
            "sample": sample,
            "errors": errors,
            "deleted_ids": deleted_ids,
        }

    def _structured_specificity_score(self, item: dict) -> float:
        text = _detail_core_text(self._memory_text(item))
        md = item.get("metadata", {}) if isinstance(item, dict) else {}
        category = str(md.get("category", "misc")).strip().lower()
        importance = str(md.get("importance", "unknown")).strip().lower()
        evidence = str(md.get("evidence", "")).strip()
        keywords = md.get("keywords", [])
        kw_count = len(keywords) if isinstance(keywords, list) else 0
        bonus = 0.0
        if category != "misc":
            bonus += 12.0
        if importance in {"critical", "high"}:
            bonus += 10.0
        elif importance == "medium":
            bonus += 4.0
        if evidence:
            bonus += 6.0
        if "我们两个" in evidence or "我们都" in evidence:
            if category == "relationship":
                bonus += 8.0
        return float(min(240, len(text)) + kw_count * 6 + bonus)

    def cleanup_overlapping_structured_memories(
        self,
        *,
        dry_run: bool = True,
        max_delete: int = 300,
        similarity_threshold: float = 0.78,
    ) -> dict:
        """
        Remove overlapping structured memories from same turn/evidence.
        Keeps the most informative one per overlap cluster.
        """
        all_memories = self.get_all()
        groups: dict[tuple[str, str, str], list[dict]] = {}
        for item in all_memories:
            md = item.get("metadata", {}) if isinstance(item, dict) else {}
            mtype = str(md.get("type", "note")).strip().lower()
            if mtype == "detail":
                continue
            turn_hash = str(md.get("turn_hash", "")).strip()
            evidence = str(md.get("evidence", "")).strip().lower()
            if not turn_hash or not evidence:
                continue
            key = (turn_hash, evidence, mtype)
            groups.setdefault(key, []).append(item)

        candidates: list[dict] = []
        for _, items in groups.items():
            if len(items) <= 1:
                continue
            ranked = sorted(items, key=self._structured_specificity_score, reverse=True)
            keeper = ranked[0]
            keep_text = _detail_core_text(self._memory_text(keeper))
            for other in ranked[1:]:
                other_text = _detail_core_text(self._memory_text(other))
                if not other_text or not keep_text:
                    continue
                sim = _semantic_similarity(keep_text, other_text)
                if sim >= max(0.5, float(similarity_threshold)):
                    candidates.append(
                        {
                            "id": str(other.get("id", "")),
                            "sim": round(sim, 4),
                            "keep_id": str(keeper.get("id", "")),
                            "text": other_text[:180],
                        }
                    )

        deleted = 0
        deleted_ids: list[str] = []
        errors: list[str] = []
        if not dry_run:
            for item in candidates[: max(1, int(max_delete))]:
                mid = str(item.get("id", "")).strip()
                if not mid:
                    continue
                try:
                    self.delete(mid)
                    deleted += 1
                    deleted_ids.append(mid)
                except Exception as e:
                    errors.append(f"{mid}: {e}")

        return {
            "ok": True,
            "groups": len(groups),
            "candidates": len(candidates),
            "deleted": deleted,
            "dry_run": dry_run,
            "sample": candidates[:10],
            "errors": errors,
            "deleted_ids": deleted_ids,
        }

    def merge_dual_subject_structured_memories(
        self,
        *,
        dry_run: bool = True,
        max_groups: int = 120,
    ) -> dict:
        """
        Merge same-turn split memories like:
        - user side fact
        - counterpart side fact
        when evidence indicates "we both /双方".
        """
        def evidence_root(text: str) -> str:
            raw = re.sub(r"\s+", " ", str(text or "").strip()).lower()
            if not raw:
                return ""
            head = re.split(r"[，。,.!?；;]", raw, maxsplit=1)[0].strip()
            return head[:48]

        def dual_subject_phrase(root: str) -> str:
            body = str(root or "")
            for marker in ["我们两个都", "我们都", "双方都", "两个人都", "我们两个", "双方"]:
                body = body.replace(marker, "")
            body = body.strip(" ，。,.!?；;")
            if not body:
                body = str(root or "").strip(" ，。,.!?；;")
            if not body:
                return ""
            return f"用户提到他与互动对象都{body}。"

        def mentions_user_side(text: str) -> bool:
            t = str(text or "")
            return any(k in t for k in ["用户", "我", "自己", "本人"])

        def mentions_other_side(text: str) -> bool:
            t = str(text or "")
            return any(k in t for k in ["互动对象", "对方", "女生", "她", "他"])

        all_memories = self.get_all()
        grouped: dict[tuple[str, str, str], list[dict]] = {}
        dual_markers = ("我们两个", "我们都", "双方", "两个人")
        for item in all_memories:
            md = item.get("metadata", {}) if isinstance(item, dict) else {}
            mtype = str(md.get("type", "note")).strip().lower()
            if mtype == "detail":
                continue
            turn_hash = str(md.get("turn_hash", "")).strip()
            evidence = str(md.get("evidence", "")).strip()
            root = evidence_root(evidence)
            if not turn_hash or not root:
                continue
            if not any(mark in root for mark in dual_markers):
                continue
            grouped.setdefault((turn_hash, mtype, root), []).append(item)

        candidates: list[dict] = []
        for (turn_hash, mtype, root), items in grouped.items():
            if len(items) < 2:
                continue
            user_side = any(mentions_user_side(self._memory_text(x)) for x in items)
            other_side = any(mentions_other_side(self._memory_text(x)) for x in items)
            if not (user_side and other_side):
                continue
            merged_text = dual_subject_phrase(root)
            if not merged_text:
                continue
            ranked = sorted(items, key=self._structured_specificity_score, reverse=True)
            base = ranked[0]
            base_md = base.get("metadata", {}) if isinstance(base, dict) else {}
            candidates.append(
                {
                    "turn_hash": turn_hash,
                    "type": mtype,
                    "root": root,
                    "merged_text": merged_text,
                    "base_metadata": base_md,
                    "source_ids": [str(x.get("id", "")) for x in items if str(x.get("id", "")).strip()],
                }
            )
            if len(candidates) >= max(1, int(max_groups)):
                break

        merged = 0
        deleted = 0
        errors: list[str] = []
        if not dry_run:
            for cand in candidates:
                base_md = cand.get("base_metadata", {}) or {}
                merged_structured = (
                    f"{cand['merged_text']} "
                    f"（分类:{CATEGORY_LABELS.get('relationship', '关系网络')}/relationship; "
                    "重要度:medium; 时态:past）"
                )
                add_result = self.add(
                    text=merged_structured,
                    memory_type=str(cand.get("type", "fact")).strip().lower() or "fact",
                    source="auto_extract_merge",
                    metadata={
                        "category": "relationship",
                        "category_label": CATEGORY_LABELS.get("relationship", "关系网络"),
                        "importance": "medium",
                        "time_scope": str(base_md.get("time_scope", "unknown") or "unknown"),
                        "keywords": base_md.get("keywords", []) if isinstance(base_md.get("keywords"), list) else [],
                        "evidence": str(base_md.get("evidence", "") or cand.get("root", "")),
                        "turn_hash": str(cand.get("turn_hash", "")),
                        "merged_from_ids": cand.get("source_ids", []),
                    },
                    allow_duplicate=False,
                )
                if add_result.get("ok"):
                    merged += 1
                    for mid in cand.get("source_ids", []):
                        try:
                            self.delete(mid)
                            deleted += 1
                        except Exception as e:
                            errors.append(f"{mid}: {e}")

        return {
            "ok": True,
            "candidates": len(candidates),
            "merged": merged,
            "deleted": deleted,
            "dry_run": dry_run,
            "sample": candidates[:10],
            "errors": errors,
        }

    def get_health_snapshot(self, *, scan_noise: bool = True) -> dict:
        """
        Lightweight memory health metrics for long-running operations.
        """
        all_memories = self.get_all()
        total = len(all_memories)
        by_type: dict[str, int] = {}
        by_source: dict[str, int] = {}
        by_category: dict[str, int] = {}
        raw_detail = 0
        for item in all_memories:
            md = item.get("metadata", {}) if isinstance(item, dict) else {}
            mtype = str(md.get("type", "unknown"))
            source = str(md.get("source", "unknown"))
            category = str(md.get("category", "unknown"))
            by_type[mtype] = by_type.get(mtype, 0) + 1
            by_source[source] = by_source.get(source, 0) + 1
            by_category[category] = by_category.get(category, 0) + 1
            if mtype == "detail" and source == "raw_message":
                raw_detail += 1

        noise_candidates = 0
        noise_sample = []
        if scan_noise:
            clean = self.cleanup_low_value_details(dry_run=True)
            noise_candidates = int(clean.get("candidates", 0))
            noise_sample = list(clean.get("sample", []))[:5]

        return {
            "total": total,
            "collection_total": self.get_total_count(),
            "by_type": by_type,
            "by_source": by_source,
            "by_category": by_category,
            "raw_detail_count": raw_detail,
            "raw_detail_ratio": round(raw_detail / max(1, total), 4),
            "noise_candidates": noise_candidates,
            "noise_sample": noise_sample,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    def delete(self, memory_id: str) -> dict:
        """
        Delete a specific memory by ID.

        Args:
            memory_id: The Mem0 memory ID.

        Returns:
            Deletion result.
        """
        result = self._safe_mem_call(
            "delete",
            lambda: self.memory.delete(memory_id),
        )
        self._log_event("delete", {"memory_id": memory_id})
        logger.info(f"Memory deleted: {memory_id}")
        return result

    def get_profile_summary(self) -> str:
        """
        Build an organized profile with vibe-style sections.
        """
        all_memories = self.get_all()
        if not all_memories:
            return "暂时还没有关于你的记忆。多和我聊聊吧！"

        sectioned: dict[str, list[tuple[int, str, str, str, str]]] = {}
        detail_count = 0
        seen = set()
        for mem in all_memories:
            metadata = mem.get("metadata", {})
            mtype = metadata.get("type", "note")
            if mtype == "detail":
                detail_count += 1
                continue

            category = str(metadata.get("category", "misc")).strip().lower()
            if category not in MEMORY_CATEGORIES:
                category = "misc"

            importance = str(metadata.get("importance", "unknown")).strip().lower()
            rank = IMPORTANCE_RANK.get(importance, IMPORTANCE_RANK["unknown"])
            text = _strip_structured_suffix(str(mem.get("memory", mem.get("text", ""))).strip())
            if not text:
                continue
            if category == "misc":
                category = _infer_category(text, mtype)

            section = _profile_section_for_memory(category, mtype, text)
            dedupe_key = (section, text.lower())
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            sectioned.setdefault(section, []).append((rank, importance, category, mtype, text))

        lines = ["**关于你，我记得这些（按你要的分类整理）：**\n"]
        for section in PROFILE_SECTION_ORDER:
            items = sectioned.get(section)
            lines.append(PROFILE_SECTION_TITLES.get(section, f"### 📚 {section}"))
            if not items:
                lines.append("- 暂无记录")
                lines.append("")
                continue
            items.sort(key=lambda x: (x[0], x[3]))
            max_items = 12
            for _, importance, category, mtype, text in items[:max_items]:
                lines.append(
                    f"- [{CATEGORY_LABELS.get(category, category)}/{mtype}/{importance}] {text}"
                )
            if len(items) > max_items:
                lines.append(f"- _(还有 {len(items) - max_items} 条已归档)_")
            lines.append("")

        if detail_count > 0:
            lines.append(f"🔍 另有 {detail_count} 条原始细节片段保存在向量库中。")

        return "\n".join(lines)
