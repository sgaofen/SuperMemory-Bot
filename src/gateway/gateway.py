"""
Gateway — the central orchestrator that ties together:
  - Memory retrieval (what do we know about this user?)
  - Context building (assemble soul + memories + history)
  - LLM routing (via Antigravity proxy or direct API through LiteLLM)
  - Memory extraction (learn new facts from the conversation)

Supports two LLM access modes:
  1. Antigravity Proxy (free) — antigravity-claude-proxy on localhost:8080
  2. Direct API — OpenAI/Anthropic/Gemini/Ollama via LiteLLM
"""
from __future__ import annotations

import asyncio
import json
import base64
import html
import hashlib
import logging
import math
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import litellm

from src.memory.memory_manager import MemoryManager
from src.memory.memory_extractor import MemoryExtractor
from src.memory.soul_manager import SoulManager
from src.memory.archive_manager import ArchiveManager
from src.gateway.context_builder import ContextBuilder

logger = logging.getLogger(__name__)

CATEGORY_LABELS = {
    "identity": "身份",
    "profile": "个人背景",
    "education": "学习",
    "work": "工作",
    "project": "项目",
    "goal": "目标",
    "routine": "作息习惯",
    "health": "健康",
    "finance": "财务",
    "relationship": "关系",
    "preference": "偏好",
    "emotion": "情绪",
    "location": "地点",
    "event": "事件",
    "misc": "其他",
}
IMPORTANCE_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "unknown": 4}


class Gateway:
    """Central AI gateway with memory-augmented LLM calls."""

    def __init__(
        self,
        default_model: Optional[str] = None,
        memory_manager: Optional[MemoryManager] = None,
        soul_manager: Optional[SoulManager] = None,
    ):
        self.default_model = default_model or os.getenv(
            "DEFAULT_MODEL", "claude-opus-4-6-thinking"
        )
        self.memory = memory_manager or MemoryManager()
        self.soul = soul_manager or SoulManager()

        # Antigravity proxy config (read early, needed for extractor)
        self.proxy_enabled = os.getenv("ANTIGRAVITY_PROXY_ENABLED", "").lower() == "true"
        self.proxy_url = os.getenv("ANTIGRAVITY_PROXY_URL", "http://localhost:8080")

        self.extractor = MemoryExtractor(
            model="anthropic/gemini-3-flash",  # User requested; fast + high limits
            proxy_url=self.proxy_url,
            proxy_enabled=self.proxy_enabled,
        )

        self.memory_retrieval_limit = int(os.getenv("MEMORY_RETRIEVAL_LIMIT", "120"))
        self.memory_extraction_context_turns = int(
            os.getenv("MEMORY_EXTRACTION_CONTEXT_TURNS", "8")
        )
        self.memory_extraction_context_max_chars = int(
            os.getenv("MEMORY_EXTRACTION_CONTEXT_MAX_CHARS", "2200")
        )
        self.detail_context_hint_turns = int(
            os.getenv("DETAIL_CONTEXT_HINT_TURNS", "3")
        )
        self.detail_context_hint_max_chars = int(
            os.getenv("DETAIL_CONTEXT_HINT_MAX_CHARS", "220")
        )
        self.max_output_tokens = int(os.getenv("MAX_OUTPUT_TOKENS", "4096"))
        self.max_history_turns = int(os.getenv("MAX_HISTORY_TURNS", "2000"))
        self.soul_sync_every_n_adds = int(os.getenv("SOUL_SYNC_EVERY_N_ADDS", "6"))
        self.soul_sync_min_interval_sec = float(
            os.getenv("SOUL_SYNC_MIN_INTERVAL_SEC", "90")
        )
        self.soul_sync_max_memories = int(os.getenv("SOUL_SYNC_MAX_MEMORIES", "6000"))
        self._pending_soul_sync_adds = 0
        self._last_soul_sync_ts = 0.0
        self.auto_recover_on_start = os.getenv("AUTO_RECOVER_MEMORY_ON_START", "true").lower() == "true"
        self.auto_recover_threshold = int(os.getenv("AUTO_RECOVER_MIN_COUNT", "5"))
        # Safety default: do NOT allow model outputs to directly rewrite soul.md.
        self.allow_llm_soul_edit = os.getenv("ALLOW_LLM_SOUL_EDIT", "false").lower() == "true"
        # Safety default: do NOT overwrite a meaningful soul summary with an empty DB snapshot.
        self.allow_empty_soul_summary_sync = os.getenv(
            "ALLOW_EMPTY_SOUL_SUMMARY_SYNC", "false"
        ).lower() == "true"
        self._soul_sync_lock = threading.Lock()
        # ---- Proactive Soul Evolution ----
        self.soul_evolution_enabled = os.getenv("SOUL_EVOLUTION_ENABLED", "true").lower() == "true"
        self._soul_evolution_lock = threading.Lock()
        # Long message preprocessing threshold (in approx tokens)
        # Default 80K — Opus/Gemini models have 200K+ context, so only
        # compress truly huge messages. This accounts for system prompt,
        # memories, and history overhead.
        self.long_message_threshold = int(os.getenv("LONG_MESSAGE_THRESHOLD", "80000"))
        self.long_message_keep_chars = int(os.getenv("LONG_MESSAGE_KEEP_CHARS", "8000"))
        self.voice_transcribe_model = os.getenv(
            "VOICE_TRANSCRIBE_MODEL",
            "anthropic/gemini-3-flash",  # Gemini natively supports audio input
        )
        self.voice_transcribe_max_tokens = int(
            os.getenv("VOICE_TRANSCRIBE_MAX_TOKENS", "3000")
        )
        self.user_timezone = os.getenv("USER_TIMEZONE", "America/Los_Angeles")
        self.web_search_enabled = os.getenv("WEB_SEARCH_ENABLED", "true").lower() == "true"
        self.web_search_max_results = int(os.getenv("WEB_SEARCH_MAX_RESULTS", "8"))
        self.web_search_timeout_sec = float(os.getenv("WEB_SEARCH_TIMEOUT_SEC", "12"))
        self.zai_api_key = os.getenv("ZAI_API_KEY", "").strip()
        self.web_search_engine = os.getenv("WEB_SEARCH_ENGINE", "auto").strip().lower()  # auto / zai / duckduckgo
        self.scoped_memory_enabled = os.getenv("SCOPED_MEMORY_ENABLED", "true").lower() == "true"
        self.scoped_memory_max_chars = int(os.getenv("SCOPED_MEMORY_MAX_CHARS", "1600"))
        self.scoped_memory_session_lines = int(os.getenv("SCOPED_MEMORY_SESSION_LINES", "4"))
        self.scoped_memory_daily_lines = int(os.getenv("SCOPED_MEMORY_DAILY_LINES", "4"))
        self.scoped_memory_weekly_lines = int(os.getenv("SCOPED_MEMORY_WEEKLY_LINES", "5"))
        self.scoped_memory_monthly_lines = int(os.getenv("SCOPED_MEMORY_MONTHLY_LINES", "6"))
        self.scoped_memory_cache_ttl_sec = float(
            os.getenv("SCOPED_MEMORY_CACHE_TTL_SEC", "120")
        )
        self.scoped_score_relevance_weight = float(
            os.getenv("SCOPED_SCORE_RELEVANCE_WEIGHT", "0.55")
        )
        self.scoped_score_importance_weight = float(
            os.getenv("SCOPED_SCORE_IMPORTANCE_WEIGHT", "0.20")
        )
        self.scoped_score_overlap_weight = float(
            os.getenv("SCOPED_SCORE_OVERLAP_WEIGHT", "0.15")
        )
        self.scoped_score_recency_weight = float(
            os.getenv("SCOPED_SCORE_RECENCY_WEIGHT", "0.10")
        )
        self._scoped_cache_lock = threading.Lock()
        self._scoped_cached_all_memories: list[dict] = []
        self._scoped_cached_at: float = 0.0
        self.archive_search_enabled = os.getenv("ARCHIVE_SEARCH_ENABLED", "true").lower() == "true"
        self.archive_search_max_results = int(os.getenv("ARCHIVE_SEARCH_MAX_RESULTS", "4"))
        self.archive_search_max_scan_events = int(
            os.getenv("ARCHIVE_SEARCH_MAX_SCAN_EVENTS", "25000")
        )
        self.archive_context_max_chars = int(os.getenv("ARCHIVE_CONTEXT_MAX_CHARS", "1200"))
        self.archive_rollup_enabled = os.getenv("ARCHIVE_ROLLUP_ENABLED", "true").lower() == "true"
        self.archive_rollup_every_n_turns = int(
            os.getenv("ARCHIVE_ROLLUP_EVERY_N_TURNS", "12")
        )
        self.archive_rollup_min_messages = int(
            os.getenv("ARCHIVE_ROLLUP_MIN_MESSAGES", "80")
        )
        self.archive_rollup_min_delta = int(os.getenv("ARCHIVE_ROLLUP_MIN_DELTA", "40"))
        self.archive_rollup_scan_limit = int(
            os.getenv("ARCHIVE_ROLLUP_SCAN_LIMIT", "250000")
        )
        self.archive_daily_rollup_min_messages = int(
            os.getenv("ARCHIVE_DAILY_ROLLUP_MIN_MESSAGES", "24")
        )
        self.archive_daily_rollup_min_delta = int(
            os.getenv("ARCHIVE_DAILY_ROLLUP_MIN_DELTA", "12")
        )
        self.archive_weekly_rollup_min_messages = int(
            os.getenv("ARCHIVE_WEEKLY_ROLLUP_MIN_MESSAGES", "48")
        )
        self.archive_weekly_rollup_min_delta = int(
            os.getenv("ARCHIVE_WEEKLY_ROLLUP_MIN_DELTA", "24")
        )
        self.auto_detail_cleanup_enabled = os.getenv(
            "AUTO_DETAIL_CLEANUP_ENABLED", "true"
        ).lower() == "true"
        self.auto_detail_cleanup_every_n_turns = int(
            os.getenv("AUTO_DETAIL_CLEANUP_EVERY_N_TURNS", "60")
        )
        self.auto_detail_cleanup_max_delete = int(
            os.getenv("AUTO_DETAIL_CLEANUP_MAX_DELETE", "30")
        )
        self._pending_detail_cleanup_turns = 0
        self._pending_archive_rollup_turns = 0
        self._archive_rollup_lock = threading.Lock()
        self._detail_cleanup_lock = threading.Lock()

        if self.proxy_enabled:
            logger.info(f"🔗 Antigravity proxy enabled at {self.proxy_url}")

        # Load soul once at startup
        self._soul_text = self.soul.get_system_prompt_section()
        self.context_builder = ContextBuilder(
            soul_text=self._soul_text,
            memory_token_budget=int(os.getenv("MEMORY_TOKEN_BUDGET", "18000")),
            history_token_budget=int(os.getenv("HISTORY_TOKEN_BUDGET", "120000")),
            system_prompt_token_budget=int(os.getenv("SYSTEM_PROMPT_TOKEN_BUDGET", "30000")),
            total_context_token_budget=int(os.getenv("TOTAL_CONTEXT_TOKEN_BUDGET", "160000")),
            response_reserve_tokens=int(os.getenv("RESPONSE_RESERVE_TOKENS", "8192")),
        )

        # Per-channel conversation history.
        self._histories: dict[str, list[dict]] = {}
        self.archive = ArchiveManager()

        self._recover_memory_if_needed()
        self._sync_soul_summary(force=True)



    def _prepare_completion_kwargs(
        self, model: str, messages: list[dict], **extra
    ) -> dict:
        """
        Build kwargs for litellm.acompletion, routing through Antigravity
        proxy when enabled and model is compatible.
        """
        kwargs = {
            "model": model,
            "messages": messages,
            **extra,
        }

        # Route through Antigravity proxy for Claude/Gemini models
        if self.proxy_enabled and self._is_proxy_model(model):
            # antigravity-claude-proxy exposes an Anthropic-compatible API
            # LiteLLM can talk to it by setting api_base and using anthropic/ prefix
            kwargs["api_base"] = self.proxy_url
            kwargs["api_key"] = "test"  # Proxy doesn't need a real key

            # If model doesn't have anthropic/ prefix, LiteLLM needs it
            # to know which SDK to use
            if not model.startswith("anthropic/") and "claude" in model.lower():
                kwargs["model"] = model  # LiteLLM auto-detects claude models
            elif "gemini" in model.lower() and not model.startswith("gemini/"):
                # For Gemini through the proxy, it's still Anthropic-format
                kwargs["model"] = model
                kwargs["custom_llm_provider"] = "anthropic"

            logger.debug(f"Routing through Antigravity proxy: {model}")
        else:
            logger.debug(f"Direct API call: {model}")

        return kwargs

    def _is_proxy_model(self, model: str) -> bool:
        """Check if this model should be routed through Antigravity proxy."""
        proxy_patterns = [
            "claude-sonnet", "claude-opus", "claude-haiku",
            "gemini-3", "gemini-2",
        ]
        model_lower = model.lower()
        return any(pattern in model_lower for pattern in proxy_patterns)

    @staticmethod
    def _extract_text_from_response_content(content) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            chunks: list[str] = []
            for part in content:
                if isinstance(part, dict):
                    text = part.get("text")
                    if text:
                        chunks.append(str(text))
                        continue
                    # Some providers nest text under value/content.
                    value = part.get("value")
                    if isinstance(value, str) and value.strip():
                        chunks.append(value)
                elif part is not None:
                    chunks.append(str(part))
            return "".join(chunks)
        if content is None:
            return ""
        return str(content)

    @staticmethod
    def _normalize_audio_media_type(mime_type: str, filename: str) -> str:
        mime = str(mime_type or "").strip().lower()
        if mime.startswith("audio/"):
            return mime

        suffix = str(filename or "").lower()
        if suffix.endswith(".ogg") or suffix.endswith(".opus"):
            return "audio/ogg"
        if suffix.endswith(".mp3"):
            return "audio/mpeg"
        if suffix.endswith(".wav"):
            return "audio/wav"
        if suffix.endswith(".m4a"):
            return "audio/mp4"
        if suffix.endswith(".flac"):
            return "audio/flac"
        if suffix.endswith(".webm"):
            return "audio/webm"
        return "audio/ogg"

    @staticmethod
    def _audio_format_from_media_type(media_type: str) -> str:
        mt = str(media_type or "").lower()
        if "mpeg" in mt or mt.endswith("/mp3"):
            return "mp3"
        if "wav" in mt:
            return "wav"
        if "flac" in mt:
            return "flac"
        if "mp4" in mt or "m4a" in mt:
            return "m4a"
        if "webm" in mt:
            return "webm"
        return "ogg"

    @staticmethod
    def _strip_html_tags(raw: str) -> str:
        text = re.sub(r"<[^>]+>", " ", str(raw or ""))
        text = html.unescape(text)
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _clip_text(text: str, max_chars: int) -> str:
        value = str(text or "").strip()
        if len(value) <= max_chars:
            return value
        if max_chars <= 3:
            return value[:max_chars]
        return value[: max_chars - 3].rstrip() + "..."

    @staticmethod
    def _normalize_duckduckgo_url(raw_url: str) -> str:
        href = str(raw_url or "").strip()
        if not href:
            return ""

        if href.startswith("//"):
            href = "https:" + href
        elif href.startswith("/"):
            href = "https://duckduckgo.com" + href

        parsed = urlparse(href)
        if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
            qs = parse_qs(parsed.query)
            redirect = qs.get("uddg", [])
            if redirect:
                return redirect[0]
        return href

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

    @staticmethod
    def _memory_identity(item: dict) -> str:
        memory_id = str(item.get("id", "")).strip()
        if memory_id:
            return f"id:{memory_id}"
        text = str(item.get("memory", item.get("text", ""))).strip().lower()
        text = re.sub(r"\s+", " ", text)
        if text:
            return f"text:{text}"
        return ""

    @staticmethod
    def _clean_memory_text(text: str) -> str:
        cleaned = str(text or "").strip()
        cleaned = re.sub(r"\s*（分类:.*?）\s*$", "", cleaned).strip()
        if cleaned.startswith("用户原话片段:"):
            cleaned = cleaned[len("用户原话片段:") :].strip()
        return re.sub(r"\s+", " ", cleaned)

    def _memory_datetime(self, item: dict) -> Optional[datetime]:
        metadata = item.get("metadata", {}) if isinstance(item, dict) else {}
        for value in (
            item.get("updated_at"),
            item.get("created_at"),
            metadata.get("updated_at"),
            metadata.get("created_at"),
        ):
            dt = self._parse_iso_datetime(str(value or ""))
            if dt:
                return dt
        return None

    def _memory_local_datetime(self, item: dict, zone: ZoneInfo) -> Optional[datetime]:
        dt = self._memory_datetime(item)
        if not dt:
            return None
        return dt.astimezone(zone)

    def _effective_zone(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.user_timezone)
        except Exception:
            return datetime.now().astimezone().tzinfo or ZoneInfo("UTC")

    async def _get_all_memories_cached(self) -> list[dict]:
        now_ts = time.time()
        with self._scoped_cache_lock:
            age = now_ts - self._scoped_cached_at
            if self._scoped_cached_all_memories and age < self.scoped_memory_cache_ttl_sec:
                return list(self._scoped_cached_all_memories)

        all_memories = await asyncio.to_thread(self.memory.get_all)
        with self._scoped_cache_lock:
            self._scoped_cached_all_memories = list(all_memories)
            self._scoped_cached_at = time.time()
        return list(all_memories)

    @staticmethod
    def _tokenize_for_overlap(text: str) -> set[str]:
        chunks = re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]{2,}", str(text or ""))
        return {c.lower() for c in chunks if c}

    def _memory_scope_score(
        self,
        item: dict,
        relevant_scores: dict[str, float],
        query_tokens: set[str],
    ) -> tuple[float, float, float, int, float]:
        identity = self._memory_identity(item)
        rel = float(relevant_scores.get(identity, 0.0))

        metadata = item.get("metadata", {}) if isinstance(item, dict) else {}
        importance = str(metadata.get("importance", "unknown")).strip().lower()
        importance_rank = IMPORTANCE_RANK.get(importance, IMPORTANCE_RANK["unknown"])
        importance_score = max(0.0, 1.0 - (importance_rank / 4.0))

        text = self._clean_memory_text(item.get("memory", item.get("text", "")))
        overlap = len(self._tokenize_for_overlap(text) & query_tokens)
        overlap_score = min(1.0, overlap / 3.0)
        dt = self._memory_datetime(item)
        dt_ts = dt.timestamp() if dt else 0.0
        if dt:
            age_days = max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 86_400.0)
            recency_score = math.exp(-age_days / 30.0)
        else:
            recency_score = 0.0

        base = (
            rel * self.scoped_score_relevance_weight
            + importance_score * self.scoped_score_importance_weight
            + overlap_score * self.scoped_score_overlap_weight
            + recency_score * self.scoped_score_recency_weight
        )
        if rel > 0:
            base += 0.12
        # Bigger is better for reverse sort.
        return (base, rel, recency_score, overlap, dt_ts)

    def _format_scope_line(self, item: dict, zone: ZoneInfo) -> str:
        metadata = item.get("metadata", {}) if isinstance(item, dict) else {}
        category = str(metadata.get("category", "misc")).strip().lower()
        category_label = CATEGORY_LABELS.get(category, "其他")
        importance = str(metadata.get("importance", "unknown")).strip().lower()
        time_scope = str(metadata.get("time_scope", "unknown")).strip().lower()
        text = self._clean_memory_text(item.get("memory", item.get("text", "")))
        text = self._clip_text(text, 88)
        dt_local = self._memory_local_datetime(item, zone)
        date_part = dt_local.strftime("%Y-%m-%d %H:%M") if dt_local else "未知"
        return f"- [{category_label}/{importance}/{time_scope}] {text} _(更新:{date_part})_"

    def _select_scope_items(
        self,
        items: list[dict],
        *,
        limit: int,
        relevant_scores: dict[str, float],
        query_tokens: set[str],
        exclude_ids: Optional[set[str]] = None,
    ) -> list[dict]:
        ranked = sorted(
            items,
            key=lambda x: self._memory_scope_score(x, relevant_scores, query_tokens),
            reverse=True,
        )
        out: list[dict] = []
        seen: set[str] = set()
        excluded = exclude_ids or set()
        for item in ranked:
            identity = self._memory_identity(item)
            if not identity or identity in seen or identity in excluded:
                continue
            text = self._clean_memory_text(item.get("memory", item.get("text", "")))
            if not text:
                continue
            mtype = str(item.get("metadata", {}).get("type", "note")).strip().lower()
            if mtype == "detail":
                continue
            seen.add(identity)
            out.append(item)
            if len(out) >= max(1, limit):
                break
        return out

    def _zai_mcp_post(self, payload: dict, session_id: str = "") -> tuple[str, str]:
        """Send a JSON-RPC request to Z.AI MCP server. Returns (body, session_id)."""
        headers = {
            "Authorization": f"Bearer {self.zai_api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if session_id:
            headers["Mcp-Session-Id"] = session_id

        req = Request(
            "https://api.z.ai/api/mcp/web_search_prime/mcp",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urlopen(req, timeout=self.web_search_timeout_sec) as resp:
            sid = resp.headers.get("Mcp-Session-Id", session_id)
            body = resp.read().decode("utf-8", errors="replace")
            return body, sid

    @staticmethod
    def _parse_mcp_sse_json(raw: str) -> dict:
        """Extract the first JSON-RPC result from an SSE stream."""
        for line in raw.split("\n"):
            stripped = line.strip()
            if stripped.startswith("data:"):
                data_str = stripped[5:].strip()
                try:
                    return json.loads(data_str)
                except json.JSONDecodeError:
                    continue
        # Fallback: try entire body as JSON
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    def _search_zai_blocking(self, query: str, limit: int) -> list[dict]:
        """Search using Z.AI MCP Web Search Prime (Streamable HTTP)."""
        q = str(query or "").strip()
        if not q or not self.zai_api_key:
            return []

        # Step 1: Initialize MCP session
        init_body, session_id = self._zai_mcp_post({
            "jsonrpc": "2.0",
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "ai-brain", "version": "0.1.0"},
            },
            "id": 1,
        })

        # Step 2: Send initialized notification (server may return 202/204)
        try:
            self._zai_mcp_post(
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                session_id,
            )
        except Exception:
            pass  # Notifications may not get a response body

        # Step 3: Call webSearchPrime tool
        tool_body, _ = self._zai_mcp_post(
            {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {
                    "name": "webSearchPrime",
                    "arguments": {
                        "search_query": q,
                        "count": str(min(max(1, limit), 50)),
                    },
                },
                "id": 2,
            },
            session_id,
        )

        # Step 4: Parse SSE → JSON-RPC → MCP content → search results
        rpc_result = self._parse_mcp_sse_json(tool_body)
        content_list = rpc_result.get("result", {}).get("content", [])

        results: list[dict] = []
        for content_item in content_list:
            if content_item.get("type") != "text":
                continue
            raw_text = content_item.get("text", "")
            # Z.AI MCP wraps JSON in double string-escape layers:
            # json.loads once → str, json.loads twice → list[dict]
            try:
                parsed = json.loads(raw_text)
                # If still a string, parse one more level
                if isinstance(parsed, str):
                    parsed = json.loads(parsed)
            except (json.JSONDecodeError, TypeError):
                if raw_text.strip():
                    results.append({"title": "", "url": "", "snippet": self._clip_text(raw_text, 600)})
                continue

            items = parsed if isinstance(parsed, list) else [parsed]
            for item in items:
                title = str(item.get("title", "")).strip()
                link = str(item.get("link", "")).strip()
                content = str(item.get("content", "")).strip()
                media = str(item.get("media", "")).strip()
                publish_date = str(item.get("publish_date", "")).strip()
                if not title and not content:
                    continue
                results.append({
                    "title": self._clip_text(title, 200),
                    "url": self._clip_text(link, 400),
                    "snippet": self._clip_text(content, 600),
                    "media": media,
                    "publish_date": publish_date,
                })
        return results

    def _search_duckduckgo_blocking(self, query: str, limit: int) -> list[dict]:
        """Fallback search using DuckDuckGo HTML scraping."""
        q = str(query or "").strip()
        if not q:
            return []

        params = urlencode({"q": q})
        url = f"https://html.duckduckgo.com/html/?{params}"
        req = Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36"
                )
            },
        )
        with urlopen(req, timeout=self.web_search_timeout_sec) as resp:
            html_doc = resp.read(1_500_000).decode("utf-8", errors="replace")

        anchor_pattern = re.compile(
            r'<a[^>]*class="result__a"[^>]*href="(.*?)"[^>]*>(.*?)</a>',
            re.IGNORECASE | re.DOTALL,
        )
        snippet_pattern = re.compile(
            r'class="result__snippet"[^>]*>(.*?)</(?:a|div)>',
            re.IGNORECASE | re.DOTALL,
        )

        results: list[dict] = []
        seen_urls: set[str] = set()
        for match in anchor_pattern.finditer(html_doc):
            raw_href = match.group(1)
            title_html = match.group(2)
            title = self._strip_html_tags(title_html)
            if not title:
                continue

            url_norm = self._normalize_duckduckgo_url(raw_href)
            if not url_norm or url_norm in seen_urls:
                continue

            tail = html_doc[match.end() : match.end() + 2400]
            snippet_match = snippet_pattern.search(tail)
            snippet = ""
            if snippet_match:
                snippet = self._strip_html_tags(snippet_match.group(1))

            results.append(
                {
                    "title": self._clip_text(title, 180),
                    "url": self._clip_text(url_norm, 400),
                    "snippet": self._clip_text(snippet, 280),
                }
            )
            seen_urls.add(url_norm)
            if len(results) >= max(1, limit):
                break
        return results

    def _search_web_blocking(self, query: str, limit: int) -> tuple[list[dict], str]:
        """
        Unified web search dispatcher.
        Returns (results, engine_name) tuple.
        Uses Z.AI as primary engine, DuckDuckGo as fallback.
        """
        engine = self.web_search_engine

        # "auto" mode: prefer Z.AI if API key is available, else DuckDuckGo
        if engine == "auto":
            engine = "zai" if self.zai_api_key else "duckduckgo"

        # Try primary engine
        if engine == "zai" and self.zai_api_key:
            try:
                results = self._search_zai_blocking(query, limit)
                if results:
                    return results, "Z.AI"
                logger.warning("Z.AI search returned empty results, trying DuckDuckGo fallback")
            except Exception as e:
                logger.warning(f"Z.AI search failed: {e}, trying DuckDuckGo fallback")

            # Fallback to DuckDuckGo
            try:
                return self._search_duckduckgo_blocking(query, limit), "DuckDuckGo"
            except Exception as e2:
                logger.warning(f"DuckDuckGo fallback also failed: {e2}")
                return [], "none"

        # DuckDuckGo as primary
        try:
            return self._search_duckduckgo_blocking(query, limit), "DuckDuckGo"
        except Exception as e:
            logger.warning(f"DuckDuckGo search failed: {e}")
            # Try Z.AI as fallback if key available
            if self.zai_api_key:
                try:
                    return self._search_zai_blocking(query, limit), "Z.AI"
                except Exception as e2:
                    logger.warning(f"Z.AI fallback also failed: {e2}")
            return [], "none"

    def _should_web_search(self, message: str) -> bool:
        if not self.web_search_enabled:
            return False
        text = str(message or "").strip().lower()
        if not text:
            return False
        # Explicit search prefixes
        if text.startswith(("联网:", "联网：", "/web ", "web:", "search:")):
            return True
        # URLs in message
        if re.search(r"https?://", text):
            return True

        # ---- Chinese keyword triggers ----
        cn_keywords = [
            # 搜索意图
            "联网", "上网", "搜索", "搜一下", "搜一搜", "查一下", "查一查",
            "查查", "帮我查", "帮我搜", "帮我看看", "帮我找", "帮查",
            "去查", "查下", "搜下",
            # 实时信息
            "最新", "最近", "现在", "目前", "当前", "如今", "今天的",
            # 新闻与事件
            "新闻", "热搜", "热点", "头条", "大事", "事件",
            # 金融与价格
            "价格", "多少钱", "售价", "报价", "定价",
            "汇率", "股价", "股票", "涨了", "跌了", "行情",
            "市值", "市场", "纳斯达克", "道琼斯", "标普",
            "比特币", "以太坊", "加密货币", "币价",
            # 天气
            "天气", "气温", "多少度", "下雨", "下雪", "温度",
            # 体育
            "比分", "比赛", "赛果", "谁赢", "冠军", "决赛",
            "超级碗", "世界杯", "奥运", "nba", "nfl", "mlb",
            # 信息查询
            "官网", "官方", "文档", "教程", "下载",
            "发布", "上市", "开售", "什么时候出",
            "怎么买", "哪里买", "在哪买",
            "评测", "评价", "口碑", "排名", "排行",
            # 影视娱乐
            "好看的电影", "好看的剧", "上映", "票房",
            "综艺", "演唱会", "音乐节",
            # 时间与日期相关
            "几点开门", "几点关门", "营业时间", "开放时间",
            "什么时候", "哪天", "多久",
            # 地点与服务
            "附近", "地址", "怎么去", "路线", "地图",
            "电话", "客服", "联系方式",
        ]
        if any(k in text for k in cn_keywords):
            return True

        # ---- Chinese pattern triggers (regex) ----
        cn_patterns = [
            r"(今[年天]|去年|明[年天]|上个?月|这个?月|本周|上周|这周).{0,15}(谁|什么|怎么|多少|哪|几)",
            r"(谁是|谁当).{0,10}(总统|总理|主席|冠军|第一)",
            r".{0,6}(出了吗|上线了吗|发售了吗|发布了吗|上映了吗)",
            r"(现在|目前|当前).{0,10}(多少|几|怎么样|如何)",
            r"(有没有|有什么).{0,8}(新|好|推荐)",
            r"(.{1,6})(几点|什么时候)(开|关|结束|开始)",
        ]
        for pattern in cn_patterns:
            if re.search(pattern, text):
                return True

        # ---- English keyword triggers ----
        en_keywords = [
            "latest", "newest", "recent", "current", "today's",
            "news", "headlines", "trending",
            "search", "look up", "check online", "google",
            "official docs", "documentation",
            "price", "cost", "how much",
            "stock", "crypto", "bitcoin", "ethereum",
            "weather", "temperature", "forecast",
            "score", "game result", "who won", "champion",
            "release", "launch", "available",
            "where to buy", "how to get",
            "review", "rating", "ranking",
            "schedule", "hours", "open",
            "download", "install",
            "what time", "when does", "when is", "when will",
            "directions", "address", "location",
        ]
        if any(k in text for k in en_keywords):
            return True

        return False

    def _extract_search_query(self, message: str) -> str:
        """
        Extract the actual search intent from a user message.
        For example, from "你好，我回来了，我刚上完生物课...搜一搜今天的ai新闻"
        it should extract "今天的ai新闻" (not the whole message).
        """
        text = str(message or "").strip()
        if not text:
            return text

        # Remove explicit search prefixes
        for prefix in ("联网:", "联网：", "/web ", "web:", "search:"):
            if text.lower().startswith(prefix):
                return text[len(prefix):].strip()[:120]

        # ---- Strategy: Split into clauses and find the search-intent one ----
        # Split by Chinese/English punctuation into clauses
        clauses = re.split(r"[，,。.！!？?；;、\n]+", text)
        clauses = [c.strip() for c in clauses if c.strip()]

        if not clauses:
            return text[:120]

        # Search-intent indicators: words that mark a search request
        intent_markers = [
            "搜", "查", "找", "看看", "搜索", "检索",
            "search", "look up", "find",
        ]
        # Topic indicators: words that signal information-seeking
        topic_markers = [
            "新闻", "天气", "价格", "多少", "最新", "最近", "今天",
            "怎么样", "如何", "什么", "谁", "哪", "多少",
            "news", "price", "weather", "latest", "today",
            "比特币", "股票", "汇率", "比分", "比赛",
            "发布", "上市", "上映", "排名", "评价",
        ]

        # Find the clause with search intent
        best_clause = None
        best_score = -1
        for clause in clauses:
            cl = clause.lower()
            score = 0
            # Check for intent markers (e.g. "搜一搜", "帮我查")
            for marker in intent_markers:
                if marker in cl:
                    score += 3
                    break
            # Check for topic markers
            for marker in topic_markers:
                if marker in cl:
                    score += 2
            if score > best_score:
                best_score = score
                best_clause = clause

        # If we found a clear intent clause, extract the query from it
        if best_clause and best_score >= 2:
            query = best_clause
            # Strip the action verbs to get just the topic
            action_prefixes = [
                r"^.*?搜一搜\s*",
                r"^.*?搜一下\s*",
                r"^.*?搜搜\s*",
                r"^.*?查一下\s*",
                r"^.*?查查\s*",
                r"^.*?帮我[搜查找看]\s*",
                r"^.*?帮[搜查找]\s*",
                r"^.*?看看\s*",
                r"^.*?找一下\s*",
                r"^(请问|问一下|想[问知]道?)\s*",
            ]
            for pat in action_prefixes:
                cleaned = re.sub(pat, "", query).strip()
                if cleaned and len(cleaned) >= 2:
                    query = cleaned
                    break
            return query.strip()[:120]

        # Fallback: use the last meaningful clause (often the actual question)
        for clause in reversed(clauses):
            if len(clause) >= 4:  # Skip very short fragments
                return clause[:120]

        return text[:120]

    @staticmethod
    def _sanitize_context_text(text: str) -> str:
        """
        Keep extraction context concise and readable, avoid injecting huge blobs.
        """
        raw = str(text or "")
        if not raw.strip():
            return ""
        raw = re.sub(r"```(?:[^\n]*\n)?(.*?)```", " [结构化内容已省略] ", raw, flags=re.DOTALL)
        raw = re.sub(r"\s*\[用户发送了以下文件\]\s*", " ", raw)
        raw = re.sub(r"\s+", " ", raw).strip()
        return raw

    @staticmethod
    def _tokenize_semantic_text(text: str) -> set[str]:
        raw = str(text or "").lower()
        tokens: set[str] = set(re.findall(r"[a-z0-9_]+", raw))
        for seq in re.findall(r"[\u4e00-\u9fff]+", raw):
            seq = seq.strip()
            if not seq:
                continue
            if len(seq) == 1:
                tokens.add(seq)
                continue
            for i in range(len(seq) - 1):
                tokens.add(seq[i : i + 2])
        return {t for t in tokens if t}

    def _semantic_text_similarity(self, a: str, b: str) -> float:
        ta = self._tokenize_semantic_text(a)
        tb = self._tokenize_semantic_text(b)
        if not ta or not tb:
            return 0.0
        return len(ta & tb) / max(1, len(ta | tb))

    @staticmethod
    def _normalize_evidence_text(text: str) -> str:
        return re.sub(r"\s+", " ", str(text or "").strip()).lower()

    @staticmethod
    def _extracted_memory_score(item: dict) -> float:
        text = str(item.get("text", "")).strip()
        category = str(item.get("category", "misc")).strip().lower()
        importance = str(item.get("importance", "unknown")).strip().lower()
        keywords = item.get("keywords", [])
        kw_count = len(keywords) if isinstance(keywords, list) else 0
        score = float(min(260, len(text))) + kw_count * 6
        if category != "misc":
            score += 12
        if importance in {"critical", "high"}:
            score += 10
        elif importance == "medium":
            score += 4
        return score

    def _dedupe_extracted_memories(self, memories: list[dict]) -> list[dict]:
        """
        Deduplicate extractor outputs within the same turn/evidence.
        Prevents near-duplicate structured memories from one utterance.
        """
        def evidence_root(text: str) -> str:
            raw = self._normalize_evidence_text(text)
            if not raw:
                return ""
            head = re.split(r"[，。,.!?；;]", raw, maxsplit=1)[0].strip()
            return head[:36]

        def mentions_user_side(text: str) -> bool:
            t = str(text or "")
            return any(k in t for k in ["用户", "我", "自己", "本人"])

        def mentions_other_side(text: str) -> bool:
            t = str(text or "")
            return any(k in t for k in ["互动对象", "对方", "女生", "她", "他"])

        def dual_subject_phrase(root: str) -> str:
            body = root
            for marker in ["我们两个都", "我们都", "双方都", "两个人都", "我们两个", "双方"]:
                body = body.replace(marker, "")
            body = body.strip(" ，。,.!?；;")
            if not body:
                body = root.strip(" ，。,.!?；;")
            if not body:
                return ""
            return f"用户提到他与互动对象都{body}。"

        importance_rank = {"critical": 4, "high": 3, "medium": 2, "low": 1}

        if not memories:
            return memories
        accepted: list[dict] = []
        for item in memories:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text", "")).strip()
            if not text:
                continue
            mtype = str(item.get("type", "note")).strip().lower()
            action = str(item.get("action", "add")).strip().lower()
            evidence = self._normalize_evidence_text(item.get("evidence", ""))

            duplicate_idx = -1
            merged_pair = False
            for idx, prev in enumerate(accepted):
                if str(prev.get("action", "add")).strip().lower() != action:
                    continue
                if str(prev.get("type", "note")).strip().lower() != mtype:
                    continue

                prev_text = str(prev.get("text", "")).strip()
                if not prev_text:
                    continue

                # Same evidence + high semantic overlap -> keep one.
                prev_evidence = self._normalize_evidence_text(prev.get("evidence", ""))
                prev_root = evidence_root(prev.get("evidence", ""))
                root = evidence_root(item.get("evidence", ""))
                dual_markers = ("我们两个", "我们都", "双方", "两个人")
                if (
                    mtype in {"fact", "relationship", "note"}
                    and root
                    and prev_root
                    and root == prev_root
                    and any(mark in root for mark in dual_markers)
                    and (
                        (mentions_user_side(text) and mentions_other_side(prev_text))
                        or (mentions_other_side(text) and mentions_user_side(prev_text))
                    )
                ):
                    merged = dict(prev)
                    merged_text = dual_subject_phrase(root)
                    if merged_text:
                        merged["text"] = merged_text
                    merged["category"] = "relationship"
                    prev_imp = str(prev.get("importance", "medium")).strip().lower()
                    now_imp = str(item.get("importance", "medium")).strip().lower()
                    merged["importance"] = (
                        prev_imp
                        if importance_rank.get(prev_imp, 0) >= importance_rank.get(now_imp, 0)
                        else now_imp
                    )
                    kw_prev = prev.get("keywords", []) if isinstance(prev.get("keywords", []), list) else []
                    kw_now = item.get("keywords", []) if isinstance(item.get("keywords", []), list) else []
                    merged["keywords"] = list(dict.fromkeys([*(kw_prev or []), *(kw_now or [])]))[:8]
                    merged["evidence"] = prev.get("evidence") or item.get("evidence") or root
                    accepted[idx] = merged
                    duplicate_idx = idx
                    merged_pair = True
                    break

                sim = self._semantic_text_similarity(text, prev_text)
                if evidence and prev_evidence and evidence == prev_evidence and sim >= 0.72:
                    duplicate_idx = idx
                    break

                # Hard duplicate on text.
                if self._normalize_evidence_text(text) == self._normalize_evidence_text(prev_text):
                    duplicate_idx = idx
                    break

            if duplicate_idx >= 0:
                if merged_pair:
                    continue
                prev = accepted[duplicate_idx]
                if self._extracted_memory_score(item) > self._extracted_memory_score(prev):
                    accepted[duplicate_idx] = item
                continue

            accepted.append(item)
        return accepted

    def _build_recent_conversation_context(
        self,
        channel_id: str,
        *,
        max_turns: int,
        max_chars: int,
    ) -> str:
        history = self._get_history(channel_id)
        if not history:
            return ""

        max_messages = max(2, max_turns * 2)
        recent = history[-max_messages:]
        lines: list[str] = []
        for msg in recent:
            role = str(msg.get("role", "")).strip().lower()
            if role not in {"user", "assistant"}:
                continue
            role_label = "用户" if role == "user" else "AI"
            content = self._sanitize_context_text(str(msg.get("content", "")))
            if not content:
                continue
            content = self._clip_text(content, 220)
            lines.append(f"[{role_label}] {content}")

        if not lines:
            return ""
        rendered = "\n".join(lines)
        return self._clip_text(rendered, max(200, max_chars))

    def _build_detail_context_hint(self, channel_id: str, current_message: str) -> str:
        """
        Build compact hint from recent turns for ambiguous short detail chunks.
        """
        history = self._get_history(channel_id)
        if not history:
            return ""

        hint_parts: list[str] = []
        # History usually ends with current user + current assistant in this flow.
        base = history[:-2] if len(history) >= 2 else history[:]

        # Add last assistant prompt right before current user turn when available.
        if len(history) >= 3 and str(history[-3].get("role", "")).lower() == "assistant":
            prompt = self._sanitize_context_text(str(history[-3].get("content", "")))
            prompt = self._clip_text(prompt, 90)
            if prompt:
                hint_parts.append(f"上轮AI: {prompt}")

        user_snippets: list[str] = []
        norm_current = re.sub(r"\s+", " ", str(current_message or "").strip()).lower()
        for msg in reversed(base):
            if str(msg.get("role", "")).lower() != "user":
                continue
            content = self._sanitize_context_text(str(msg.get("content", "")))
            if not content:
                continue
            norm_content = re.sub(r"\s+", " ", content.strip()).lower()
            if norm_current and norm_content == norm_current:
                continue
            user_snippets.append(self._clip_text(content, 90))
            if len(user_snippets) >= max(1, self.detail_context_hint_turns):
                break
        if user_snippets:
            hint_parts.append("用户此前: " + " | ".join(reversed(user_snippets)))

        if not hint_parts:
            return ""
        return self._clip_text("；".join(hint_parts), max(120, self.detail_context_hint_max_chars))

    def _build_time_context(self) -> str:
        now_utc = datetime.now(timezone.utc)
        try:
            zone = ZoneInfo(self.user_timezone)
            now_local = now_utc.astimezone(zone)
            zone_label = self.user_timezone
        except Exception:
            now_local = now_utc.astimezone()
            zone_label = now_local.tzname() or "local"

        today = now_local.date()
        yesterday = today - timedelta(days=1)
        tomorrow = today + timedelta(days=1)
        weekday_map = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        weekday = weekday_map[now_local.weekday()]

        return (
            "[时间基准]\n"
            f"- 当前本地时间({zone_label}): {now_local.strftime('%Y-%m-%d %H:%M:%S')} {weekday}\n"
            f"- 当前UTC时间: {now_utc.strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
            f"- 相对日期锚点: 今天={today.isoformat()}，昨天={yesterday.isoformat()}，明天={tomorrow.isoformat()}\n"
            "- 遇到“今天/明天/昨天/本周”等表述时，请严格按上述日期解释并在需要时给出绝对日期。"
        )

    async def _build_scoped_memory_context(
        self,
        channel_id: str,
        user_message: str,
        relevant_memories: list[dict],
    ) -> str:
        """
        OpenClaw-style scoped memory layers:
        session + today + this week + this month, with strict char budget.
        """
        if not self.scoped_memory_enabled:
            return ""

        zone = self._effective_zone()
        now_local = datetime.now(timezone.utc).astimezone(zone)
        today = now_local.date()
        month_key = (today.year, today.month)
        week_key = now_local.isocalendar()[:2]

        relevant_scores: dict[str, float] = {}
        for item in relevant_memories or []:
            identity = self._memory_identity(item)
            if not identity:
                continue
            score = item.get("score")
            score_v = float(score) if isinstance(score, (int, float)) else 0.0
            if score_v > relevant_scores.get(identity, 0.0):
                relevant_scores[identity] = score_v

        query_tokens = self._tokenize_for_overlap(user_message)
        lines = [
            "[分层记忆索引]",
            "- 采用 Session/今日/本月三级记忆摘要（控制 token，完整细节仍在向量库）。",
            "",
            "### Session 记忆",
        ]

        history = self._get_history(channel_id)
        session_lines = 0
        if history:
            for msg in reversed(history):
                role = msg.get("role")
                if role not in {"user", "assistant"}:
                    continue
                role_label = "用户" if role == "user" else "AI"
                content = self._clip_text(str(msg.get("content", "")).strip(), 110)
                if not content:
                    continue
                lines.append(f"- [{role_label}] {content}")
                session_lines += 1
                if session_lines >= max(1, self.scoped_memory_session_lines):
                    break
        if session_lines == 0:
            lines.append("- 暂无 session 摘要")

        all_memories = await self._get_all_memories_cached()
        daily_candidates: list[dict] = []
        weekly_candidates: list[dict] = []
        monthly_candidates: list[dict] = []
        for mem in all_memories:
            dt_local = self._memory_local_datetime(mem, zone)
            if not dt_local:
                continue
            if dt_local.date() == today:
                daily_candidates.append(mem)
            if dt_local.isocalendar()[:2] == week_key:
                weekly_candidates.append(mem)
            if (dt_local.year, dt_local.month) == month_key:
                monthly_candidates.append(mem)

        def non_detail_count(items: list[dict]) -> int:
            count = 0
            for item in items:
                mtype = str(item.get("metadata", {}).get("type", "note")).strip().lower()
                if mtype == "detail":
                    continue
                text = self._clean_memory_text(item.get("memory", item.get("text", "")))
                if text:
                    count += 1
            return count

        daily_top = self._select_scope_items(
            daily_candidates,
            limit=self.scoped_memory_daily_lines,
            relevant_scores=relevant_scores,
            query_tokens=query_tokens,
        )
        daily_total_non_detail = non_detail_count(daily_candidates)
        daily_ids = {self._memory_identity(item) for item in daily_top if self._memory_identity(item)}
        weekly_top = self._select_scope_items(
            weekly_candidates,
            limit=self.scoped_memory_weekly_lines,
            relevant_scores=relevant_scores,
            query_tokens=query_tokens,
            exclude_ids=daily_ids,
        )
        weekly_total_non_detail = non_detail_count(weekly_candidates)
        weekly_ids = {self._memory_identity(item) for item in weekly_top if self._memory_identity(item)}
        monthly_top = self._select_scope_items(
            monthly_candidates,
            limit=self.scoped_memory_monthly_lines,
            relevant_scores=relevant_scores,
            query_tokens=query_tokens,
            exclude_ids=daily_ids | weekly_ids,
        )
        monthly_total_non_detail = non_detail_count(monthly_candidates)

        lines.append("")
        lines.append("### 今日记忆")
        if daily_top:
            for item in daily_top:
                lines.append(self._format_scope_line(item, zone))
            if daily_total_non_detail > len(daily_top):
                lines.append(
                    f"- _(今日还有 {daily_total_non_detail - len(daily_top)} 条记忆未展开)_"
                )
        else:
            lines.append("- 今日暂无可用摘要记忆")

        lines.append("")
        lines.append("### 本周记忆")
        if weekly_top:
            for item in weekly_top:
                lines.append(self._format_scope_line(item, zone))
            if weekly_total_non_detail > len(weekly_top):
                lines.append(
                    f"- _(本周还有 {weekly_total_non_detail - len(weekly_top)} 条记忆未展开)_"
                )
        else:
            lines.append("- 本周暂无可用摘要记忆")

        lines.append("")
        lines.append("### 本月记忆")
        if monthly_top:
            for item in monthly_top:
                lines.append(self._format_scope_line(item, zone))
            if monthly_total_non_detail > len(monthly_top):
                lines.append(
                    f"- _(本月还有 {monthly_total_non_detail - len(monthly_top)} 条记忆未展开)_"
                )
        else:
            lines.append("- 本月暂无可用摘要记忆")

        rendered = "\n".join(lines).strip()
        if len(rendered) > self.scoped_memory_max_chars:
            rendered = (
                rendered[: max(200, self.scoped_memory_max_chars - 30)].rstrip()
                + "\n...(分层记忆已按 token 预算截断)"
            )
        return rendered

    async def _build_archive_context(self, query: str, channel_id: str) -> str:
        """
        Lexical fallback from raw archive (non-vector), keeping strict budget.
        """
        if not self.archive_search_enabled:
            return ""
        q = str(query or "").strip()
        if len(q) < 2:
            return ""

        try:
            channel_results = await asyncio.to_thread(
                self.archive.lexical_search,
                q,
                limit=max(1, self.archive_search_max_results),
                max_scan_events=max(100, self.archive_search_max_scan_events),
                channel_id=channel_id,
            )
        except Exception as e:
            logger.warning(f"Archive lexical search failed (channel): {e}")
            channel_results = []

        merged: list[dict] = []
        seen: set[str] = set()
        for item in channel_results:
            key = str(item.get("event_id", "")).strip() or (
                f"{item.get('timestamp','')}|{item.get('role','')}|{item.get('content','')[:40]}"
            )
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)

        if len(merged) < self.archive_search_max_results:
            try:
                global_results = await asyncio.to_thread(
                    self.archive.lexical_search,
                    q,
                    limit=max(1, self.archive_search_max_results),
                    max_scan_events=max(100, self.archive_search_max_scan_events),
                    channel_id=None,
                )
            except Exception as e:
                logger.warning(f"Archive lexical search failed (global): {e}")
                global_results = []
            for item in global_results:
                key = str(item.get("event_id", "")).strip() or (
                    f"{item.get('timestamp','')}|{item.get('role','')}|{item.get('content','')[:40]}"
                )
                if key in seen:
                    continue
                seen.add(key)
                merged.append(item)
                if len(merged) >= self.archive_search_max_results:
                    break

        if not merged:
            return ""

        lines = [
            "[原文归档检索]",
            "- 以下为原始聊天文本命中的片段（用于补充向量检索盲区）：",
        ]
        for idx, item in enumerate(merged[: self.archive_search_max_results], start=1):
            ts = str(item.get("timestamp", "")).replace("T", " ")[:19]
            role = "用户" if str(item.get("role", "")).lower() == "user" else "AI"
            ch = str(item.get("channel_id", "")).strip() or "default"
            score = item.get("score", 0)
            content = self._clip_text(str(item.get("content", "")).strip(), 180)
            lines.append(
                f"{idx}. [{ts}/{role}/ch={ch}] {content} _(lexical={float(score):.2f})_"
            )
        lines.append("- 需要原文上下文时可继续追问，我会扩大归档检索范围。")

        block = "\n".join(lines).strip()
        if len(block) > self.archive_context_max_chars:
            block = (
                block[: max(180, self.archive_context_max_chars - 24)].rstrip()
                + "\n...(归档片段已按预算截断)"
            )
        return block

    async def _archive_turn(
        self,
        channel_id: str,
        user_message: str,
        assistant_reply: str,
        model: str,
    ):
        """Persist raw turn to archive and optionally sync monthly rollup memory."""
        try:
            await asyncio.to_thread(
                self.archive.append_record,
                channel_id,
                "user",
                user_message,
                metadata={"kind": "chat_user"},
            )
            await asyncio.to_thread(
                self.archive.append_record,
                channel_id,
                "assistant",
                assistant_reply,
                metadata={"kind": "chat_assistant", "model": model},
            )
            if self.archive_rollup_enabled:
                self._pending_archive_rollup_turns += 1
                if self._pending_archive_rollup_turns >= max(1, self.archive_rollup_every_n_turns):
                    self._pending_archive_rollup_turns = 0
                    await self._sync_archive_rollups()
        except Exception as e:
            logger.error(f"Archive turn persistence failed: {e}")

    async def _sync_archive_rollups(self):
        """Build daily/weekly/monthly archive rollups and store compact vector memories."""
        if not self.archive_rollup_enabled:
            return
        if not self._archive_rollup_lock.acquire(blocking=False):
            return
        try:
            now_local = datetime.now(timezone.utc).astimezone(self._effective_zone())
            month_key = now_local.strftime("%Y-%m")
            day_key = now_local.strftime("%Y-%m-%d")
            iso_year, iso_week, _ = now_local.isocalendar()
            week_key = f"{iso_year:04d}-W{iso_week:02d}"

            daily_rollup, weekly_rollup, monthly_rollup = await asyncio.gather(
                asyncio.to_thread(
                    lambda: self.archive.build_daily_rollup_if_needed(
                        day_key=day_key,
                        min_messages=self.archive_daily_rollup_min_messages,
                        min_delta=self.archive_daily_rollup_min_delta,
                        scan_limit=self.archive_rollup_scan_limit,
                    )
                ),
                asyncio.to_thread(
                    lambda: self.archive.build_weekly_rollup_if_needed(
                        week_key=week_key,
                        min_messages=self.archive_weekly_rollup_min_messages,
                        min_delta=self.archive_weekly_rollup_min_delta,
                        scan_limit=self.archive_rollup_scan_limit,
                    )
                ),
                asyncio.to_thread(
                    lambda: self.archive.build_monthly_rollup_if_needed(
                        month_key=month_key,
                        min_messages=self.archive_rollup_min_messages,
                        min_delta=self.archive_rollup_min_delta,
                        scan_limit=self.archive_rollup_scan_limit,
                    )
                ),
            )

            rollup_specs = [
                ("daily_rollup", "archive_daily_rollup", "day", daily_rollup),
                ("weekly_rollup", "archive_weekly_rollup", "week", weekly_rollup),
                ("monthly_rollup", "archive_monthly_rollup", "month", monthly_rollup),
            ]
            synced = 0
            for scope, source_name, key_name, rollup in rollup_specs:
                if not rollup:
                    continue

                summary_text = str(rollup.get("summary", "")).strip()
                if not summary_text:
                    continue

                structured = self._format_structured_memory_text(
                    text=summary_text,
                    category="event",
                    importance="medium",
                    time_scope="ongoing",
                )
                metadata = {
                    "category": "event",
                    "category_label": CATEGORY_LABELS.get("event", "事件"),
                    "importance": "medium",
                    "time_scope": "ongoing",
                    "scope": scope,
                    "archive_message_count": int(rollup.get("message_count", 0)),
                    "archive_user_count": int(rollup.get("user_count", 0)),
                    "archive_assistant_count": int(rollup.get("assistant_count", 0)),
                    "archive_channel_count": int(rollup.get("channel_count", 0)),
                    "archive_summary_hash": str(rollup.get("summary_hash", "")),
                    "archive_first_ts": str(rollup.get("first_ts", "")),
                    "archive_last_ts": str(rollup.get("last_ts", "")),
                }
                metadata[key_name] = str(rollup.get(key_name, ""))

                result = await asyncio.to_thread(
                    self.memory.add,
                    structured,
                    memory_type="note",
                    source=source_name,
                    metadata=metadata,
                )
                if result.get("ok"):
                    synced += 1
                    logger.info(
                        "Archive %s synced into vector memory: %s=%s count=%s",
                        scope,
                        key_name,
                        rollup.get(key_name, ""),
                        rollup.get("message_count", 0),
                    )

            if synced > 0:
                self._pending_soul_sync_adds += synced
                await asyncio.to_thread(self._sync_soul_summary, False)
        except Exception as e:
            logger.error(f"Archive rollup sync failed: {e}")
        finally:
            self._archive_rollup_lock.release()

    async def _build_runtime_context(self, message: str) -> str:
        sections = [self._build_time_context()]
        if not self._should_web_search(message):
            return "\n\n".join(sections)

        # Clean up the query for better search results
        search_query = self._extract_search_query(message)
        if not search_query or len(search_query) < 4:
            search_query = message[:120]
        logger.info("Web search triggered — query=%r (from message=%r)", search_query, message[:60])

        try:
            results, engine_name = await asyncio.to_thread(
                self._search_web_blocking,
                search_query,
                self.web_search_max_results,
            )
            logger.info("Web search done — engine=%s results=%d", engine_name, len(results))
        except Exception as e:
            logger.warning(f"Web search failed: {e}")
            results = []
            engine_name = "none"

        if not results:
            sections.append(
                "[联网检索]\n"
                "- 已尝试联网搜索，但本轮未获取到可用结果。\n"
                "- 如问题强依赖实时信息，请明确要求我重试联网检索。"
            )
            return "\n\n".join(sections)

        lines = [
            "[联网检索]",
            f"- 以下为本轮自动检索到的网页线索（{engine_name}）：",
        ]
        for idx, item in enumerate(results, start=1):
            title = item.get("title", "")
            url = item.get("url", "")
            snippet = item.get("snippet", "")
            media = item.get("media", "")
            publish_date = item.get("publish_date", "")
            header = f"{idx}. {title}"
            if media:
                header += f" ({media})"
            lines.append(header)
            lines.append(f"   链接: {url}")
            if publish_date:
                lines.append(f"   发布日期: {publish_date}")
            if snippet:
                lines.append(f"   摘要: {snippet}")
        lines.append("- 回答时优先基于这些来源；若信息仍不确定，请明确说明不确定性。")
        sections.append("\n".join(lines))
        return "\n\n".join(sections)

    async def _maybe_cleanup_low_value_details(self):
        """
        Periodic high-confidence cleanup for raw detail noise.
        Runs infrequently to avoid overhead.
        """
        if not self.auto_detail_cleanup_enabled:
            return
        self._pending_detail_cleanup_turns += 1
        if self._pending_detail_cleanup_turns < max(1, self.auto_detail_cleanup_every_n_turns):
            return
        self._pending_detail_cleanup_turns = 0

        if not self._detail_cleanup_lock.acquire(blocking=False):
            return
        try:
            result = await asyncio.to_thread(
                self.memory.cleanup_low_value_details,
                dry_run=False,
                max_delete=max(1, self.auto_detail_cleanup_max_delete),
            )
            deleted = int(result.get("deleted", 0))
            if deleted > 0:
                logger.info(
                    "Auto detail cleanup deleted %s low-value memory chunks",
                    deleted,
                )
                await asyncio.to_thread(self._sync_soul_summary, True)
        except Exception as e:
            logger.error(f"Auto detail cleanup failed: {e}")
        finally:
            self._detail_cleanup_lock.release()

    # Patterns that indicate the model didn't actually hear the audio
    _FAKE_TRANSCRIPTION_PATTERNS = [
        "i'd be happy to",
        "i can't listen",
        "i cannot listen",
        "i'm unable to",
        "i don't have the ability",
        "no audio",
        "cannot process audio",
        "can't process audio",
        "unable to transcribe",
        "no voice",
        "please provide",
        "i can't hear",
        "cannot hear",
    ]

    def _is_fake_transcription(self, text: str) -> bool:
        """Detect if the model just replied to the instruction instead of
        actually transcribing the audio."""
        lower = text.lower()
        return any(pat in lower for pat in self._FAKE_TRANSCRIPTION_PATTERNS)

    async def transcribe_audio(
        self,
        audio_bytes: bytes,
        *,
        mime_type: str = "audio/ogg",
        filename: str = "voice.ogg",
        model: Optional[str] = None,
    ) -> str:
        """
        Transcribe a Discord voice/audio attachment into plain text.
        Uses Gemini Flash by default (native audio support).
        Tries multiple payload formats for compatibility.
        """
        if not audio_bytes:
            return ""

        media_type = self._normalize_audio_media_type(mime_type, filename)
        audio_format = self._audio_format_from_media_type(media_type)
        b64_audio = base64.b64encode(audio_bytes).decode("utf-8")
        target_model = model or self.voice_transcribe_model or self.default_model

        logger.info(
            "Transcribing audio: %s (%s, %d bytes) with model %s",
            filename, media_type, len(audio_bytes), target_model,
        )

        instruction = (
            "请将这段语音准确转写为文字。\n"
            "要求：\n"
            "- 输出简体中文；若原文不是中文，则保留原语言。\n"
            "- 保留数字、人名、地名、时间等关键信息。\n"
            "- 只输出转写文本，不要解释。"
        )

        # Build payload formats to try in order of preference
        payloads: list[tuple[str, list]] = [
            # Format 1: data URI via image_url-style block
            # LiteLLM and most providers translate this correctly
            (
                "data_uri",
                [
                    {"type": "text", "text": instruction},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{media_type};base64,{b64_audio}",
                        },
                    },
                ],
            ),
            # Format 2: Anthropic native audio block
            (
                "anthropic_audio",
                [
                    {"type": "text", "text": instruction},
                    {
                        "type": "audio",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": b64_audio,
                        },
                    },
                ],
            ),
            # Format 3: OpenAI-style input_audio block
            (
                "openai_input_audio",
                [
                    {"type": "text", "text": instruction},
                    {
                        "type": "input_audio",
                        "input_audio": {
                            "data": b64_audio,
                            "format": audio_format,
                        },
                    },
                ],
            ),
        ]

        last_error: Optional[Exception] = None
        for fmt_name, content_blocks in payloads:
            try:
                kwargs = self._prepare_completion_kwargs(
                    model=target_model,
                    messages=[{"role": "user", "content": content_blocks}],
                    temperature=0,
                    max_tokens=self.voice_transcribe_max_tokens,
                )
                response = await litellm.acompletion(**kwargs)
                content = response.choices[0].message.content
                text = self._extract_text_from_response_content(content).strip()

                if not text:
                    logger.warning("Format %s returned empty response", fmt_name)
                    continue

                # Check if the model faked a response instead of transcribing
                if self._is_fake_transcription(text):
                    logger.warning(
                        "Format %s returned fake transcription: %s...",
                        fmt_name, text[:80],
                    )
                    continue

                logger.info(
                    "Audio transcribed successfully via %s (%d chars)",
                    fmt_name, len(text),
                )
                return text

            except Exception as e:
                logger.warning("Format %s failed: %s", fmt_name, e)
                last_error = e

        if last_error:
            logger.error(
                "All audio transcription formats failed (%s, %s): %s",
                target_model, filename, last_error,
            )
        else:
            logger.error(
                "Audio transcription returned no valid result (%s, %s)",
                target_model, filename,
            )
        return ""

    async def chat(
        self,
        message: str,
        channel_id: str = "default",
        model: Optional[str] = None,
        auto_learn: bool = True,
    ) -> str:
        """
        Full pipeline: memory → context → LLM → learn → response.

        Args:
            message: User's message text.
            channel_id: Conversation context boundary.
            model: Override model for this request.
            auto_learn: Whether to extract memories from this exchange.

        Returns:
            AI response text.
        """
        target_model = model or self.default_model

        # 0. Pre-process: compress overly long messages to avoid context overflow
        original_message = message  # keep full text for memory extraction
        message = await self._preprocess_long_message(message)
        runtime_context_task = asyncio.create_task(self._build_runtime_context(message))
        archive_context_task = asyncio.create_task(
            self._build_archive_context(original_message, channel_id)
        )

        # 1. Retrieve relevant memories (mem0 search is blocking → run in thread)
        relevant_memories = await asyncio.to_thread(
            self.memory.search_with_details,
            message,
            limit=self.memory_retrieval_limit,
        )
        logger.debug(f"Found {len(relevant_memories)} relevant memories")
        runtime_context = await runtime_context_task
        scoped_memory_context = await self._build_scoped_memory_context(
            channel_id=channel_id,
            user_message=message,
            relevant_memories=relevant_memories,
        )
        archive_context = await archive_context_task
        extra_parts = [runtime_context]
        if scoped_memory_context:
            extra_parts.append(scoped_memory_context)
        if archive_context:
            extra_parts.append(archive_context)
        extra_context = "\n\n".join(part for part in extra_parts if part)

        # 2. Build context-enriched prompt
        system_prompt = self.context_builder.build_system_prompt(
            relevant_memories=relevant_memories,
            extra_instructions=extra_context,
        )

        # 3. Get conversation history for this channel
        history = self._get_history(channel_id)

        # 4. Build full message array
        messages = self.context_builder.build_messages(
            system_prompt=system_prompt,
            conversation_history=history,
            user_message=message,
        )

        # 5. Call LLM (via proxy or direct API)
        try:
            completion_kwargs = self._prepare_completion_kwargs(
                model=target_model,
                messages=messages,
                temperature=0.7,
                max_tokens=self.max_output_tokens,
            )
            response = await litellm.acompletion(**completion_kwargs)
            content = response.choices[0].message.content
            if isinstance(content, list):
                content = "".join(
                    part.get("text", "") if isinstance(part, dict) else str(part)
                    for part in content
                )
            reply = str(content or "").strip()
        except Exception as e:
            logger.error(f"LLM call failed: {e}")
            if self.proxy_enabled:
                reply = (
                    f"⚠️ 模型调用失败: {str(e)}\n\n"
                    f"请确认:\n"
                    f"1. `antigravity-claude-proxy` 正在运行 (`npx antigravity-claude-proxy@latest start`)\n"
                    f"2. 已在 http://localhost:8080 添加了 Google 账号\n"
                    f"3. 运行 `curl http://localhost:8080/health` 检查状态"
                )
            else:
                reply = f"⚠️ 模型调用失败: {str(e)}\n请检查 API key 和模型配置。"
            return reply

        # 6. Optionally process soul edits embedded in the reply (disabled by default).
        if self.allow_llm_soul_edit:
            reply = self._apply_soul_edits(reply)

        # 7. Update conversation history (store compressed version to save space)
        self._add_to_history(channel_id, "user", message)
        self._add_to_history(channel_id, "assistant", reply)
        asyncio.create_task(
            self._archive_turn(
                channel_id=channel_id,
                user_message=original_message,
                assistant_reply=reply,
                model=target_model,
            )
        )

        # 8. Background: extract and store new memories
        #    Use original_message (full text) so no information is lost.
        if auto_learn:
            asyncio.create_task(
                self._background_learn(
                    original_message,
                    reply,
                    channel_id,
                    relevant_memories,
                )
            )

        return reply

    async def _background_soul_evolution(self, channel_id: str):
        """
        Proactive soul evolution: reflect on recent conversation and
        autonomously update the handwritten sections of soul.md.
        Uses a lightweight LLM to analyze what changed and propose edits.
        """
        if not self._soul_evolution_lock.acquire(blocking=False):
            return
        try:
            # Get recent conversation context
            recent = self._build_recent_conversation_context(
                channel_id=channel_id,
                max_turns=max(3, self.soul_evolution_every_n_turns),
                max_chars=4000,
            )
            if not recent or len(recent.strip()) < 20:
                return

            # Read current soul.md (only the handwritten part, before AUTO_SUMMARY)
            current_soul = self.soul.read()
            auto_marker = "<!-- AUTO_USER_SUMMARY_START -->"
            hand_section = current_soul.split(auto_marker)[0] if auto_marker in current_soul else current_soul

            reflection_prompt = (
                "你是一个 AI 人格管理系统。你的任务是分析最近的对话，判断 soul.md（AI的人格定义文件）是否需要更新。\n\n"
                "## 当前 soul.md 的手写部分:\n"
                f"```\n{hand_section}\n```\n\n"
                "## 最近对话:\n"
                f"```\n{recent}\n```\n\n"
                "## 你的任务:\n"
                "判断对话是否包含**必须更新到 soul.md 的关键信息**。\n\n"
                "### ✅ 只有以下情况才需要更新 soul.md:\n"
                "- 用户的**核心身份信息**变化（换学校、换专业、搬家、换工作）\n"
                "- 用户**明确纠正**了 soul.md 中的错误事实\n"
                "- 用户要求**修改 AI 的名字或性格**\n"
                "- 用户的**重要关系状态**发生变化（开始/结束恋爱关系等）\n"
                "- 用户有了**全新的长期目标或重大项目**\n\n"
                "### ❌ 以下情况绝对不更新:\n"
                "- 日常闲聊、问候、情绪波动\n"
                "- 已经在 soul.md 中存在的信息\n"
                "- 临时事件（今天做了什么、天气、临时计划）\n"
                "- 细节信息（这些由向量数据库记录，不需要写进 soul.md）\n"
                "- 不确定是否长期有效的信息\n\n"
                "**大部分对话都不需要更新 soul.md。宁可漏掉也不要添加噪音。**\n\n"
                "请以 JSON 格式返回。如果不需要更新（绝大多数情况），返回 `{\"updates\": []}`。\n"
                "如果确实需要更新，返回:\n"
                "```json\n"
                "{\n"
                "  \"updates\": [\n"
                "    {\n"
                "      \"section\": \"要修改的章节名（如 '关于我', '兴趣', '项目'）\",\n"
                "      \"action\": \"add 或 replace\",\n"
                "      \"find\": \"要替换的旧文本（action=replace时需要，必须精确匹配）\",\n"
                "      \"content\": \"简洁的新内容（一行，markdown列表格式）\",\n"
                "      \"reason\": \"简短说明为什么这条信息必须写进 soul.md\"\n"
                "    }\n"
                "  ]\n"
                "}\n"
                "```\n"
                "注意：只返回 JSON。content 必须简洁（一行内）。find 必须精确匹配 soul.md 原文。"
            )

            completion_kwargs = self._prepare_completion_kwargs(
                model="anthropic/gemini-3-flash",
                messages=[
                    {"role": "system", "content": "你是 soul.md 管理系统。soul.md 只存关键身份信息，不是垃圾桶。大部分对话不需要更新。只输出 JSON。"},
                    {"role": "user", "content": reflection_prompt},
                ],
                temperature=0.1,
                max_tokens=1000,
            )
            response = await litellm.acompletion(**completion_kwargs)
            raw = str(response.choices[0].message.content or "").strip()

            # Parse JSON from response (strip markdown fences if present)
            json_str = raw
            if "```" in json_str:
                json_str = re.sub(r"```(?:json)?\s*", "", json_str)
                json_str = json_str.replace("```", "").strip()

            data = json.loads(json_str)
            updates = data.get("updates", [])
            if not updates:
                logger.info("Soul evolution: no updates needed this cycle")
                return

            applied = 0
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

            for upd in updates:
                action = str(upd.get("action", "")).lower()
                section = str(upd.get("section", ""))
                content = str(upd.get("content", "")).strip()
                reason = str(upd.get("reason", ""))
                find_text = str(upd.get("find", "")).strip()

                if not content:
                    continue

                try:
                    current_soul = self.soul.read()

                    if action == "replace" and find_text and find_text in current_soul:
                        new_soul = current_soul.replace(find_text, content, 1)
                        with open(self.soul.path, "w", encoding="utf-8") as f:
                            f.write(new_soul)
                        applied += 1
                        logger.info(f"Soul evolution [replace]: '{find_text[:40]}' → '{content[:40]}' | {reason}")

                    elif action == "add" and content:
                        # Find the section and append content after it
                        # Try to find by section header
                        section_patterns = [
                            f"### {section}",
                            f"## {section}",
                        ]
                        inserted = False
                        for pat in section_patterns:
                            if pat in current_soul:
                                # Find the end of the section's existing content
                                idx = current_soul.index(pat) + len(pat)
                                # Find the next line break after the header
                                nl_idx = current_soul.find("\n", idx)
                                if nl_idx >= 0:
                                    new_soul = current_soul[:nl_idx] + "\n" + content + current_soul[nl_idx:]
                                    with open(self.soul.path, "w", encoding="utf-8") as f:
                                        f.write(new_soul)
                                    inserted = True
                                    applied += 1
                                    logger.info(f"Soul evolution [add to {section}]: '{content[:50]}' | {reason}")
                                break

                        if not inserted:
                            # Fallback: append before auto summary
                            if auto_marker in current_soul:
                                new_soul = current_soul.replace(
                                    auto_marker,
                                    f"- {content}\n\n{auto_marker}",
                                )
                            else:
                                new_soul = current_soul + f"\n- {content}\n"
                            with open(self.soul.path, "w", encoding="utf-8") as f:
                                f.write(new_soul)
                            applied += 1
                            logger.info(f"Soul evolution [append]: '{content[:50]}' | {reason}")

                    # Add to update log table
                    log_entry = f"| {now_str} | {content[:60]} | 自动进化: {reason[:40]} |"
                    current_soul = self.soul.read()
                    if "| 日期 | 变更内容 | 原因 |" in current_soul:
                        # Append after the last table row
                        last_pipe = current_soul.rfind("|")
                        if last_pipe >= 0:
                            end_of_line = current_soul.find("\n", last_pipe)
                            if end_of_line >= 0:
                                new_soul = current_soul[:end_of_line + 1] + log_entry + "\n" + current_soul[end_of_line + 1:]
                            else:
                                new_soul = current_soul + "\n" + log_entry + "\n"
                            with open(self.soul.path, "w", encoding="utf-8") as f:
                                f.write(new_soul)

                except Exception as e:
                    logger.error(f"Soul evolution edit failed: {e}")

            if applied > 0:
                self.reload_soul()
                logger.info(f"Soul evolution complete: {applied} updates applied, soul reloaded")

        except json.JSONDecodeError as e:
            logger.warning(f"Soul evolution: failed to parse LLM response as JSON: {e}")
        except Exception as e:
            logger.error(f"Soul evolution failed: {e}")
        finally:
            self._soul_evolution_lock.release()

    async def _preprocess_long_message(self, message: str) -> str:
        """
        If a message exceeds the dynamic token threshold, compress it
        intelligently to fit within the available context budget.

        Strategy:
          - Only triggers when the message alone would eat most of the
            available context (threshold = total budget - system/memory/history
            overhead - output reserve).
          - Keeps ~40% of original text verbatim (head + tail).
          - Summarizes the middle in sections for better detail retention.
          - Tells the AI that full text is being stored in memory so no
            information is truly lost.
        """
        approx_tokens = self.context_builder._approx_tokens(message)
        if approx_tokens <= self.long_message_threshold:
            return message

        # Dynamically calculate how many chars to keep based on message size.
        # Keep 40% of the original (split evenly head/tail), capped at a
        # reasonable maximum so the compressed version is still useful.
        total_chars = len(message)
        keep_ratio = 0.4
        max_keep_per_side = 50_000  # ~20K tokens per side max
        keep_per_side = min(int(total_chars * keep_ratio / 2), max_keep_per_side)
        keep_per_side = max(keep_per_side, self.long_message_keep_chars)

        head = message[:keep_per_side]
        tail = message[-keep_per_side:]
        middle = message[keep_per_side:-keep_per_side]

        if not middle.strip():
            return message

        middle_tokens = self.context_builder._approx_tokens(middle)
        logger.info(
            f"Long message detected: ~{approx_tokens} tokens. "
            f"Keeping {keep_per_side} chars head + tail, "
            f"summarizing middle (~{middle_tokens} tokens) in sections..."
        )

        # Sectioned summarization: split middle into chunks and summarize
        # each one to preserve more detail than a single coarse summary.
        summary = await self._sectioned_summarize(middle, middle_tokens)

        compressed = (
            f"{head}\n\n"
            f"{'=' * 40}\n"
            f"📋 [以下是中间部分的分段详细摘要，原文约 {middle_tokens} tokens]\n"
            f"[完整原文已自动存入记忆系统，不会丢失任何信息]\n"
            f"{'=' * 40}\n\n"
            f"{summary}\n\n"
            f"{'=' * 40}\n"
            f"[摘要结束，以下是原文末尾部分]\n"
            f"{'=' * 40}\n\n"
            f"{tail}"
        )

        new_tokens = self.context_builder._approx_tokens(compressed)
        logger.info(
            f"Message compressed: {approx_tokens} → {new_tokens} tokens "
            f"(saved ~{approx_tokens - new_tokens})"
        )
        return compressed

    async def _sectioned_summarize(self, text: str, total_tokens: int) -> str:
        """
        Split long text into sections and summarize each one separately.
        This preserves much more detail than a single coarse summary.

        Each section gets its own LLM call with a generous token budget,
        and sections are summarized in parallel for speed.
        """
        # Target ~5000 tokens per chunk → ~13,000 chars
        chunk_size = 13_000
        chunks = []
        for i in range(0, len(text), chunk_size):
            chunk = text[i:i + chunk_size].strip()
            if chunk:
                chunks.append(chunk)

        if not chunks:
            return "[空内容]"

        logger.info(f"Sectioned summarize: {len(chunks)} sections from ~{total_tokens} tokens")

        # Summarize all chunks in parallel
        async def summarize_chunk(idx: int, chunk_text: str) -> str:
            try:
                kwargs = self._prepare_completion_kwargs(
                    model="anthropic/gemini-3-flash",
                    messages=[
                        {
                            "role": "user",
                            "content": (
                                "你是一个文本压缩助手。请将以下文本压缩成详细摘要。\n"
                                "要求：\n"
                                "- 保留所有关键信息：人名、地名、数字、日期、事件、观点\n"
                                "- 保留对话中每个人说了什么（如果是聊天记录）\n"
                                "- 保留情感、态度、语气等细节\n"
                                "- 不要遗漏任何实质性内容\n"
                                "- 用中文输出，只输出摘要，不加前缀\n\n"
                                f"第 {idx + 1}/{len(chunks)} 段原文：\n{chunk_text}"
                            ),
                        },
                    ],
                    temperature=0.1,
                    max_tokens=1500,  # generous budget per section
                )
                response = await litellm.acompletion(**kwargs)
                content = response.choices[0].message.content
                if isinstance(content, list):
                    content = "".join(
                        part.get("text", "") if isinstance(part, dict) else str(part)
                        for part in content
                    )
                return str(content or "").strip()
            except Exception as e:
                logger.error(f"Section {idx + 1} summarization failed: {e}")
                # Fallback: keep first 500 chars of the chunk
                return f"[第{idx+1}段摘要失败，原文片段] {chunk_text[:500]}..."

        tasks = [
            summarize_chunk(i, chunk) for i, chunk in enumerate(chunks)
        ]
        summaries = await asyncio.gather(*tasks)

        # Assemble sectioned summary
        parts = []
        for i, summary in enumerate(summaries):
            parts.append(f"### 第 {i + 1}/{len(chunks)} 段\n{summary}")

        return "\n\n".join(parts)

    async def _background_learn(
        self,
        message: str,
        reply: str,
        channel_id: str,
        relevant_memories: Optional[list[dict]] = None,
    ):
        """
        Extract and store memories in the background.
        All blocking mem0 calls are offloaded to threads so the
        Discord heartbeat is never blocked.
        """
        added_count = 0
        try:
            extraction_context = self._build_recent_conversation_context(
                channel_id=channel_id,
                max_turns=max(2, self.memory_extraction_context_turns),
                max_chars=max(300, self.memory_extraction_context_max_chars),
            )
            new_memories = await self.extractor.extract_memories(
                message,
                reply,
                relevant_memories=relevant_memories,
                conversation_context=extraction_context,
            )
            new_memories = self._dedupe_extracted_memories(new_memories)
            high_importance_found = False
            soul_worthy_categories = {
                "identity", "profile", "relationship", "preference",
                "goal", "project", "work", "education",
            }
            for mem in new_memories:
                text = mem.get("text", "")
                mtype = mem.get("type", "note")
                action = str(mem.get("action", "add")).lower()
                category = str(mem.get("category", "misc")).lower()
                importance = str(mem.get("importance", "medium")).lower()
                time_scope = str(mem.get("time_scope", "unknown")).lower()
                evidence = str(mem.get("evidence", "")).strip()
                keywords = mem.get("keywords", [])

                if action == "ignore":
                    continue
                if importance == "high" and category in soul_worthy_categories:
                    high_importance_found = True
                if text:
                    existing = await asyncio.to_thread(
                        self.memory.search, text, limit=3,
                    )
                    top_score = 0.0
                    if existing:
                            first_score = existing[0].get("score")
                            if isinstance(first_score, (int, float)):
                                top_score = float(first_score)

                    if action == "update" and existing:
                        old = existing[0]
                        old_id = old.get("id")
                        old_cat = old.get("metadata", {}).get("category", "")
                        if old_id and old_cat == category and top_score >= 0.86:
                            try:
                                await asyncio.to_thread(self.memory.delete, old_id)
                                logger.info(f"Extractor requested update: replaced old memory {old_id}")
                            except Exception as e:
                                logger.error(f"Failed replacing old memory {old_id}: {e}")

                    should_check_conflict = (
                        mtype in {"fact", "plan", "relationship"}
                        and bool(existing)
                        and top_score >= 0.72
                    )
                    conflict = None
                    if should_check_conflict:
                        conflict = await self.extractor.check_conflict(text, existing)

                    if conflict and conflict.get("has_conflict"):
                        action = conflict.get("suggested_action")
                        if action == "ignore_new":
                            logger.info(
                                f"Memory conflict detected (ignore_new), skipping: {text}"
                            )
                            continue
                        if action == "replace_old" and existing:
                            old = existing[0]
                            old_id = old.get("id")
                            old_cat = old.get("metadata", {}).get("category", "")
                            if (
                                old_id
                                and old_cat == category
                                and top_score >= 0.86
                            ):
                                try:
                                    await asyncio.to_thread(self.memory.delete, old_id)
                                    logger.info(f"Replaced old memory {old_id} with updated fact")
                                except Exception as e:
                                    logger.error(f"Failed to replace old memory {old_id}: {e}")

                    structured_text = self._format_structured_memory_text(
                        text=text,
                        category=category,
                        importance=importance,
                        time_scope=time_scope,
                    )
                    turn_hash = hashlib.sha1(
                        f"{channel_id}:{message[:500]}".encode("utf-8")
                    ).hexdigest()[:16]

                    result = await asyncio.to_thread(
                        self.memory.add,
                        structured_text,
                        memory_type=mtype,
                        source="auto_extract",
                        metadata={
                            "category": category,
                            "category_label": CATEGORY_LABELS.get(category, "其他"),
                            "importance": importance,
                            "time_scope": time_scope,
                            "evidence": evidence[:180],
                            "keywords": keywords if isinstance(keywords, list) else [],
                            "turn_hash": turn_hash,
                        },
                    )
                    if result.get("ok"):
                        added_count += 1
                    logger.info(
                        f"Auto-learned: [{mtype}/{category}/{importance}/{time_scope}] {text}"
                    )

            # Fallback: ensure at least one organized memory for this turn.
            if added_count == 0:
                fallback = self._build_fallback_memory(message)
                if fallback:
                    fallback_text = self._format_structured_memory_text(
                        text=fallback,
                        category="event",
                        importance="low",
                        time_scope="unknown",
                    )
                    result = await asyncio.to_thread(
                        self.memory.add,
                        fallback_text,
                        memory_type="note",
                        source="auto_fallback",
                        metadata={
                            "category": "event",
                            "category_label": CATEGORY_LABELS.get("event", "事件"),
                            "importance": "low",
                            "time_scope": "unknown",
                            "evidence": fallback[:120],
                            "keywords": [],
                        },
                    )
                    if result.get("ok"):
                        added_count += 1
                        logger.info(f"Fallback memory added: {fallback}")
        except Exception as e:
            logger.error(f"Memory extraction failed (non-fatal): {e}")
        finally:
            # Persist raw user details to vector memory even when extraction fails.
            detail_count = 0
            try:
                detail_context_hint = self._build_detail_context_hint(
                    channel_id=channel_id,
                    current_message=message,
                )
                detail_count = await asyncio.to_thread(
                    self.memory.add_user_message_details,
                    message=message,
                    source="raw_message",
                    metadata={"channel_id": channel_id},
                    context_hint=detail_context_hint,
                )
                if detail_count:
                    logger.info(f"Stored {detail_count} raw detail chunks")
            except Exception as e:
                logger.error(f"Raw detail storage failed (non-fatal): {e}")

            total_new = added_count + detail_count
            if total_new:
                self._pending_soul_sync_adds += total_new
                await asyncio.to_thread(self._sync_soul_summary, force=False)
            await self._maybe_cleanup_low_value_details()

            # Trigger soul evolution only when high-importance memories were found
            if self.soul_evolution_enabled and high_importance_found:
                asyncio.create_task(self._background_soul_evolution(channel_id))

    @staticmethod
    def _format_structured_memory_text(
        text: str,
        category: str,
        importance: str,
        time_scope: str,
    ) -> str:
        """
        Keep a natural-language core sentence while appending structured tags.
        This keeps retrieval quality and improves readability in DB.
        """
        core = str(text).strip()
        if not core:
            return core
        cat_label = CATEGORY_LABELS.get(category, "其他")
        return (
            f"{core} "
            f"（分类:{cat_label}/{category}; 重要度:{importance}; 时态:{time_scope}）"
        )

    @staticmethod
    def _build_fallback_memory(message: str) -> str:
        """Build a minimal, readable fallback memory when extractor returns none."""
        text = str(message or "").strip()
        if len(text) < 12:
            return ""
        # Use the first sentence-like segment as fallback.
        segment = re.split(r"[。！？!?;\n]+", text, maxsplit=1)[0].strip()
        segment = re.sub(r"\s+", " ", segment)
        if len(segment) > 180:
            segment = segment[:180].rstrip() + "..."
        if not segment:
            return ""
        if not segment.startswith("用户"):
            segment = f"用户提到：{segment}"
        return segment

    # ── Soul editing from AI responses ────────────────────────────

    def _apply_soul_edits(self, reply: str) -> str:
        """
        Parse and apply soul.md edit markers from the AI's response.
        Returns the reply with edit markers stripped out.

        Supported markers:
          <soul_patch find="old text" replace="new text"/>
          <soul_write>full new content</soul_write>
        """
        edited = False
        clean_reply = reply

        # 1. Handle <soul_write>...</soul_write> (full rewrite)
        write_pattern = re.compile(
            r'<soul_write>(.*?)</soul_write>',
            re.DOTALL,
        )
        write_match = write_pattern.search(clean_reply)
        if write_match:
            new_content = write_match.group(1).strip()
            if new_content and len(new_content) > 50:
                try:
                    soul_path = self.soul.path
                    with open(soul_path, 'w', encoding='utf-8') as f:
                        f.write(new_content)
                    edited = True
                    logger.info(f"Soul full rewrite applied ({len(new_content)} chars)")
                except Exception as e:
                    logger.error(f"Soul full rewrite failed: {e}")
            clean_reply = write_pattern.sub('', clean_reply)

        # 2. Handle <soul_patch find="..." replace="..."/> (find & replace)
        patch_pattern = re.compile(
            r'<soul_patch\s+find="(.*?)"\s+replace="(.*?)"\s*/?>',
            re.DOTALL,
        )
        patches = patch_pattern.findall(clean_reply)
        if patches:
            try:
                current_soul = self.soul.read()
                for find_text, replace_text in patches:
                    find_text = find_text.strip()
                    replace_text = replace_text.strip()
                    if find_text and find_text in current_soul:
                        current_soul = current_soul.replace(find_text, replace_text, 1)
                        logger.info(f"Soul patch applied: '{find_text[:50]}' → '{replace_text[:50]}'")
                        edited = True
                    else:
                        logger.warning(f"Soul patch target not found: '{find_text[:80]}'")

                if edited:
                    soul_path = self.soul.path
                    with open(soul_path, 'w', encoding='utf-8') as f:
                        f.write(current_soul)
            except Exception as e:
                logger.error(f"Soul patch failed: {e}")

            clean_reply = patch_pattern.sub('', clean_reply)

        # 3. Reload soul if any edits were applied
        if edited:
            self.reload_soul()
            logger.info("Soul reloaded after AI edit")

        # Clean up extra whitespace from removed markers
        clean_reply = re.sub(r'\n{3,}', '\n\n', clean_reply).strip()
        return clean_reply

    def reload_soul(self):
        """Reload soul.md (e.g., after an update)."""
        self._soul_text = self.soul.get_system_prompt_section()
        self.context_builder = ContextBuilder(
            soul_text=self._soul_text,
            memory_token_budget=int(os.getenv("MEMORY_TOKEN_BUDGET", "18000")),
            history_token_budget=int(os.getenv("HISTORY_TOKEN_BUDGET", "120000")),
            system_prompt_token_budget=int(os.getenv("SYSTEM_PROMPT_TOKEN_BUDGET", "30000")),
            total_context_token_budget=int(os.getenv("TOTAL_CONTEXT_TOKEN_BUDGET", "160000")),
            response_reserve_tokens=int(os.getenv("RESPONSE_RESERVE_TOKENS", "8192")),
        )
        logger.info("Soul reloaded")

    def refresh_soul_summary(self):
        """Force a full soul auto-summary refresh from current memories."""
        self._sync_soul_summary(force=True)

    def _sync_soul_summary(self, force: bool = False):
        """
        Sync the soul.md auto summary periodically.
        Keeps soul concise while vector DB stores all raw details.
        """
        if not force and self._pending_soul_sync_adds < self.soul_sync_every_n_adds:
            return
        now_ts = time.time()
        if (
            not force
            and self._last_soul_sync_ts > 0
            and now_ts - self._last_soul_sync_ts < max(1.0, self.soul_sync_min_interval_sec)
        ):
            return
        try:
            with self._soul_sync_lock:
                all_memories = self.memory.get_all(
                    limit=max(200, self.soul_sync_max_memories)
                    if self.soul_sync_max_memories > 0
                    else None
                )

                if (
                    not all_memories
                    and not self.allow_empty_soul_summary_sync
                    and self.soul.has_meaningful_auto_summary()
                ):
                    logger.warning(
                        "Skip soul summary sync: memory DB returned empty, "
                        "but existing soul summary has meaningful content."
                    )
                    return

                changed = self.soul.sync_auto_summary(all_memories)
                if changed:
                    self.reload_soul()
                self._pending_soul_sync_adds = 0
                self._last_soul_sync_ts = time.time()
        except Exception as e:
            logger.error(f"Soul summary sync failed: {e}")

    def _recover_memory_if_needed(self):
        """Recover vector memories from history when DB looks unexpectedly empty."""
        if not self.auto_recover_on_start:
            return
        try:
            current = self.memory.get_all(limit=max(32, self.auto_recover_threshold + 12))
        except Exception as e:
            logger.error(f"Memory pre-check failed: {e}")
            return

        if len(current) >= self.auto_recover_threshold:
            return

        try:
            stats = self.memory.recover_from_history()
            if stats.get("recovered", 0) > 0:
                logger.warning(f"Recovered memories from history: {stats}")
            else:
                logger.info(f"Memory recovery skipped/empty: {stats}")
        except Exception as e:
            logger.error(f"Memory auto-recovery failed: {e}")

    def _get_history(self, channel_id: str) -> list[dict]:
        return self._histories.get(channel_id, [])

    def _add_to_history(self, channel_id: str, role: str, content: str):
        if channel_id not in self._histories:
            self._histories[channel_id] = []
        self._histories[channel_id].append({"role": role, "content": content})
        max_messages = max(8, self.max_history_turns * 2)
        if len(self._histories[channel_id]) > max_messages:
            self._histories[channel_id] = self._histories[channel_id][-max_messages:]

    def clear_history(self, channel_id: str):
        self._histories.pop(channel_id, None)
        logger.info(f"History cleared for channel {channel_id}")

    def get_context_status(self, channel_id: str) -> dict:
        """
        Calculate token usage for each context component.
        Returns a detailed breakdown for the /status command.
        """
        approx = self.context_builder._approx_tokens

        # Soul / system prompt base
        soul_tokens = approx(self._soul_text)

        # History for this channel
        history = self._get_history(channel_id)
        history_tokens = sum(
            approx(str(m.get("content", ""))) + 6 for m in history
        )
        history_turns = len(history) // 2  # each turn = user + assistant

        # Estimate memory tokens (do a sample search)
        try:
            sample_memories = self.memory.search("用户信息", limit=self.memory_retrieval_limit)
            memory_lines = []
            for mem in sample_memories:
                text = str(mem.get("memory", mem.get("text", "")))[:600]
                mtype = mem.get("metadata", {}).get("type", "note")
                memory_lines.append(f"- [{mtype}] {text}")
            memory_text = "\n".join(memory_lines)
            memory_tokens = approx(memory_text)
            memory_count = len(sample_memories)
        except Exception:
            memory_tokens = 0
            memory_count = 0

        # Total stored memories
        try:
            total_memories = int(self.memory.get_total_count())
        except Exception:
            total_memories = 0

        # Budgets
        system_budget = self.context_builder.system_prompt_token_budget
        history_budget = self.context_builder.history_token_budget
        memory_budget = self.context_builder.memory_token_budget

        total_used = soul_tokens + history_tokens + memory_tokens
        total_budget = system_budget + history_budget + memory_budget

        return {
            "soul_tokens": soul_tokens,
            "memory_tokens": memory_tokens,
            "memory_count": memory_count,
            "total_memories": total_memories,
            "history_tokens": history_tokens,
            "history_turns": history_turns,
            "history_messages": len(history),
            "total_used": total_used,
            "system_budget": system_budget,
            "history_budget": history_budget,
            "memory_budget": memory_budget,
            "total_budget": total_budget,
            "usage_pct": round(total_used / max(1, total_budget) * 100, 1),
        }

    async def compact_history(self, channel_id: str) -> dict:
        """
        Compress conversation history by summarizing older turns into a
        condensed context message, keeping only recent turns verbatim.

        Returns stats about the compaction.
        """
        history = self._get_history(channel_id)
        if len(history) < 8:
            return {"action": "skip", "reason": "history_too_short", "messages": len(history)}

        # Keep the last 6 messages verbatim, summarize the rest
        keep_recent = 6
        to_summarize = history[:-keep_recent]
        kept = history[-keep_recent:]

        # Build the old conversation text for summarization
        old_lines = []
        for msg in to_summarize:
            role = "用户" if msg["role"] == "user" else "AI"
            content = str(msg.get("content", ""))[:500]
            old_lines.append(f"{role}: {content}")
        old_text = "\n".join(old_lines)

        # Use LLM to create a summary
        try:
            summary_kwargs = self._prepare_completion_kwargs(
                model="anthropic/gemini-3-flash",
                messages=[
                    {
                        "role": "user",
                        "content": (
                            "请用中文将以下对话历史压缩成一段简洁的摘要，"
                            "保留所有关键信息（话题、决定、待办事项、重要细节）。\n"
                            "只输出摘要文本，不要其他内容。\n\n"
                            f"对话历史：\n{old_text}"
                        ),
                    },
                ],
                temperature=0.2,
                max_tokens=800,
            )
            response = await litellm.acompletion(**summary_kwargs)
            content = response.choices[0].message.content
            if isinstance(content, list):
                content = "".join(
                    part.get("text", "") if isinstance(part, dict) else str(part)
                    for part in content
                )
            summary = str(content or "").strip()
        except Exception as e:
            logger.error(f"History compaction LLM call failed: {e}")
            # Fallback: just trim without summary
            self._histories[channel_id] = kept
            return {
                "action": "trimmed",
                "removed": len(to_summarize),
                "kept": len(kept),
                "summary": None,
            }

        # Replace history with: [summary message] + [recent messages]
        summary_msg = {
            "role": "system",
            "content": f"[之前的对话摘要]\n{summary}",
        }
        self._histories[channel_id] = [summary_msg] + kept

        before_tokens = self.context_builder._approx_tokens(old_text)
        after_tokens = self.context_builder._approx_tokens(summary)
        saved = before_tokens - after_tokens

        logger.info(
            f"History compacted for {channel_id}: "
            f"{len(to_summarize)} msgs → summary, kept {len(kept)} recent, "
            f"saved ~{saved} tokens"
        )

        return {
            "action": "compacted",
            "summarized_messages": len(to_summarize),
            "kept_messages": len(kept),
            "tokens_saved": saved,
            "summary_preview": summary[:200],
        }
