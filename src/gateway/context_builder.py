"""
ContextBuilder — assembles the final system prompt from:
  1. Soul personality (soul.md)
  2. Retrieved memories (from Mem0 search)
  3. Conversation history (sliding window)
"""
from __future__ import annotations

from datetime import datetime, timezone
import logging
import os
import re
from typing import Optional

logger = logging.getLogger(__name__)

CHARS_PER_TOKEN = 2.6  # rough estimate for mixed Chinese/English text
DEFAULT_MEMORY_TOKEN_BUDGET = int(os.getenv("MEMORY_TOKEN_BUDGET", "18000"))
DEFAULT_HISTORY_TOKEN_BUDGET = int(os.getenv("HISTORY_TOKEN_BUDGET", "120000"))
DEFAULT_SYSTEM_PROMPT_TOKEN_BUDGET = int(os.getenv("SYSTEM_PROMPT_TOKEN_BUDGET", "30000"))
DEFAULT_TOTAL_CONTEXT_TOKEN_BUDGET = int(os.getenv("TOTAL_CONTEXT_TOKEN_BUDGET", "160000"))
DEFAULT_RESPONSE_RESERVE_TOKENS = int(os.getenv("RESPONSE_RESERVE_TOKENS", "8192"))


class ContextBuilder:
    """Build enriched prompts with soul + memories + history."""

    def __init__(
        self,
        soul_text: str = "",
        memory_token_budget: int = DEFAULT_MEMORY_TOKEN_BUDGET,
        history_token_budget: int = DEFAULT_HISTORY_TOKEN_BUDGET,
        system_prompt_token_budget: int = DEFAULT_SYSTEM_PROMPT_TOKEN_BUDGET,
        total_context_token_budget: int = DEFAULT_TOTAL_CONTEXT_TOKEN_BUDGET,
        response_reserve_tokens: int = DEFAULT_RESPONSE_RESERVE_TOKENS,
    ):
        self.soul_text = soul_text
        self.memory_token_budget = max(400, memory_token_budget)
        self.history_token_budget = max(400, history_token_budget)
        self.system_prompt_token_budget = max(1500, system_prompt_token_budget)
        self.total_context_token_budget = max(4096, total_context_token_budget)
        self.response_reserve_tokens = max(512, response_reserve_tokens)

    @staticmethod
    def _approx_tokens(text: str) -> int:
        if not text:
            return 0
        return int(len(text) / CHARS_PER_TOKEN)

    def build_system_prompt(
        self,
        relevant_memories: list[dict],
        extra_instructions: Optional[str] = None,
    ) -> str:
        """
        Assemble the full system prompt.

        Args:
            relevant_memories: Results from MemoryManager.search().
            extra_instructions: Optional one-off instructions.

        Returns:
            Complete system prompt string.
        """
        sections = []
        memory_block = ""

        # 1. Soul / personality
        if self.soul_text:
            sections.append(self.soul_text)

        # 2. Retrieved memories
        if relevant_memories:
            memory_block = self._format_memories(
                relevant_memories,
                token_budget=self.memory_token_budget,
            )
            sections.append(memory_block)

        # 3. Extra instructions
        if extra_instructions:
            sections.append(f"\n**额外指示**: {extra_instructions}")

        prompt = "\n\n".join(sections)
        # Secondary guard: if system prompt still too long, shrink memory block further.
        if memory_block and self._approx_tokens(prompt) > self.system_prompt_token_budget:
            overflow = self._approx_tokens(prompt) - self.system_prompt_token_budget
            reduced_budget = max(300, self.memory_token_budget - overflow - 300)
            compact_memory = self._format_memories(relevant_memories, token_budget=reduced_budget)
            compact_sections = []
            if self.soul_text:
                compact_sections.append(self.soul_text)
            compact_sections.append(compact_memory)
            if extra_instructions:
                compact_sections.append(f"\n**额外指示**: {extra_instructions}")
            prompt = "\n\n".join(compact_sections)
        return prompt

    def build_messages(
        self,
        system_prompt: str,
        conversation_history: list[dict],
        user_message: str,
    ) -> list[dict]:
        """
        Build the full message array for the LLM API call.
        Uses a generous history budget while avoiding context overrun.
        """
        messages = [{"role": "system", "content": system_prompt}]

        # Add trimmed conversation history (from latest backwards), bounded by
        # both history budget and total context hard limit.
        system_tokens = self._approx_tokens(system_prompt) + 10
        user_tokens = self._approx_tokens(user_message) + 10
        hard_available = (
            self.total_context_token_budget
            - self.response_reserve_tokens
            - system_tokens
            - user_tokens
            - 60
        )
        history_budget = min(self.history_token_budget, max(0, hard_available))
        messages.extend(self._trim_history(conversation_history, budget_tokens=history_budget))

        # Add current user message
        messages.append({"role": "user", "content": user_message})

        return messages

    def _trim_history(self, history: list[dict], budget_tokens: Optional[int] = None) -> list[dict]:
        if not history:
            return []

        effective_budget = self.history_token_budget if budget_tokens is None else max(0, int(budget_tokens))
        if effective_budget <= 0:
            return []

        kept_reversed: list[dict] = []
        used_tokens = 0
        for msg in reversed(history):
            content = str(msg.get("content", ""))
            role = msg.get("role")
            if role not in {"user", "assistant", "system"}:
                continue
            msg_tokens = self._approx_tokens(content) + 6
            if used_tokens + msg_tokens > effective_budget:
                break
            kept_reversed.append({"role": role, "content": content})
            used_tokens += msg_tokens

        kept = list(reversed(kept_reversed))
        # Keep turn alignment: avoid starting with an assistant orphan message.
        if kept and kept[0]["role"] == "assistant":
            kept = kept[1:]
        return kept

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

    def _memory_time_part(self, memory: dict) -> str:
        dt = self._memory_datetime(memory)
        if not dt:
            return "📅时间未知"
        return f"📅{dt.strftime('%Y-%m-%d %H:%M')} UTC"

    def _format_memories(self, memories: list[dict], token_budget: int) -> str:
        """
        Format retrieved memories with a high but bounded budget.
        """
        char_budget = int(max(120, token_budget) * CHARS_PER_TOKEN)
        type_emoji = {
            "fact": "📋",
            "preference": "💜",
            "plan": "🎯",
            "note": "📝",
            "relationship": "🤝",
            "detail": "🔍",
        }

        lines = [
            f"**你对用户的记忆（检索到 {len(memories)} 条）：**",
            "_规则：soul.md 只提供概览；涉及事实细节时以下述检索记忆为准。_",
            "",
        ]
        used_chars = sum(len(line) for line in lines)
        seen: set[str] = set()

        structured: list[dict] = []
        details: list[dict] = []
        for mem in memories:
            mtype = str(mem.get("metadata", {}).get("type", "note")).strip().lower()
            if mtype == "detail":
                details.append(mem)
            else:
                structured.append(mem)

        details.sort(
            key=lambda x: (
                float(x.get("score", 0) or 0),
                self._memory_datetime(x) or datetime.min.replace(tzinfo=timezone.utc),
            ),
            reverse=True,
        )

        def append_bucket(title: str, items: list[dict]):
            nonlocal used_chars
            lines.append(title)
            used_chars += len(title) + 1
            if not items:
                lines.append("- 暂无")
                lines.append("")
                used_chars += len("- 暂无") + 2
                return

            omitted = 0
            for mem in items:
                text = str(mem.get("memory", mem.get("text", ""))).strip()
                if not text:
                    continue
                if text.startswith("用户原话片段:"):
                    text = text[len("用户原话片段:"):].strip()
                text = re.sub(r"\s*（分类:.*?）\s*$", "", text).strip()
                text = text[:950] + "...(已截断)" if len(text) > 950 else text

                metadata = mem.get("metadata", {})
                mtype = str(metadata.get("type", "note")).strip().lower() or "note"
                emoji = type_emoji.get(mtype, "📌")
                memory_id = str(mem.get("id", "")).strip()
                key = memory_id or f"{mtype}:{text.lower()}"
                if key in seen:
                    continue
                seen.add(key)

                category = str(metadata.get("category", "misc")).strip().lower()
                importance = str(metadata.get("importance", "unknown")).strip().lower()
                time_scope = str(metadata.get("time_scope", "unknown")).strip().lower()
                source = str(metadata.get("retrieval_source", "primary")).strip().lower()
                score = mem.get("score")
                score_part = f" score={float(score):.2f}" if isinstance(score, (float, int)) else ""
                line = (
                    f"- {emoji} [{mtype}/{category}/{importance}/{time_scope}] "
                    f"{self._memory_time_part(mem)} {text} "
                    f"_(source={source}{',' if score_part else ''}{score_part.strip()})_"
                )

                if used_chars + len(line) + 1 > char_budget:
                    omitted += 1
                    continue

                lines.append(line)
                used_chars += len(line) + 1

            if omitted > 0:
                omitted_line = f"- _(该分区还有 {omitted} 条记忆未展开，可继续追问细节)_"
                lines.append(omitted_line)
                used_chars += len(omitted_line) + 1
            lines.append("")
            used_chars += 1

        append_bucket("### 📌 结构化主记忆", structured)
        append_bucket("### 🔎 原始细节片段（向量库）", details)

        return "\n".join(lines)
