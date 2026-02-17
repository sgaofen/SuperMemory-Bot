"""
MemoryExtractor — understand conversation context and emit structured memories.

The extractor is designed to avoid fragmented memory text by requiring
full-sentence, context-rich facts before storage.

Enhanced with confidence scoring for memory verification.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional

import litellm

logger = logging.getLogger(__name__)

MEMORY_TYPES = {"fact", "preference", "plan", "relationship", "note", "detail"}
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
IMPORTANCE_LEVELS = {"critical", "high", "medium", "low"}
TIME_SCOPES = {"current", "past", "future", "ongoing", "unknown"}
MEMORY_ACTIONS = {"add", "update", "ignore"}
CONFIDENCE_LEVELS = {"high", "medium", "low"}

EXTRACTION_PROMPT = """你是"长期记忆知识工程师"。
你的任务是：基于用户本轮输入 + 已有记忆上下文，提取**可长期使用、语义完整、可检索**的记忆条目。

核心原则：
1. 记忆必须是"完整句"，不要碎片（例如不要只写"中午上学"）。
2. 必须保留关键限定信息：时间、地点、对象、频率、数量、约束条件。
3. 如果是旧信息的变更（例如年龄、学校、计划发生变化），action 用 "update"。
4. 没有价值或只是寒暄内容时，action 用 "ignore"。
5. 只提取用户说的内容，不根据 AI 回复脑补。
6. 若同一句明确表达"我们两个/我们都/双方"等双主体信息，优先合并为一条关系记忆，避免仅主语不同的重复两条。
7. **置信度(confidence)**: 根据信息来源判断可信程度：
   - high (0.9): 用户直接明确陈述的事实（如"我今年19岁"）
   - medium (0.7): 用户提及但需推理的信息（如"我明天考试"→推断有考试）
   - low (0.5): AI从上下文推断但用户未明确说出的信息

输出 JSON 对象（不要代码块）：
{{
  "items": [
    {{
      "text": "用户目前在某大学读书，主修计算机相关方向。",
      "type": "fact",
      "category": "education",
      "importance": "high",
      "time_scope": "current",
      "action": "add",
      "evidence": "我现在在XX大学读CS",
      "keywords": ["大学", "CS", "计算机"],
      "confidence": 0.9
    }}
  ]
}}

字段约束：
- type: fact/preference/plan/relationship/note/detail
- category: identity/profile/education/work/project/goal/routine/health/finance/relationship/preference/emotion/location/event/misc
- importance: critical/high/medium/low
- time_scope: current/past/future/ongoing/unknown
- action: add/update/ignore
- evidence: 引用用户原话中的关键片段（简短）
- confidence: 0.0-1.0，表示记忆可信程度

已有记忆上下文（用于判断是否重复或更新）：
{known_context}

最近对话上下文（用于补全代词、省略和多轮含义）：
{conversation_context}

用户输入：
{user_message}

AI 回复（仅作语境参考，不可作为事实来源）：
{bot_response}
"""

CONFLICT_CHECK_PROMPT = """请判断新记忆是否与已有记忆冲突。
只返回 JSON：
{{"has_conflict": true, "conflict_type": "update", "suggested_action": "replace_old", "explanation": "一句话说明"}}

新记忆：
{new_memory}

已有记忆：
{existing_memories}
"""

VERIFICATION_PROMPT = """请评估以下记忆是否需要用户确认。
如果记忆涉及重要个人事实但置信度不高，或者可能是暂时性信息，建议需要验证。

记忆内容：
{memory_text}

证据来源：{evidence}

只返回 JSON：
{{"needs_verification": true, "reason": "原因说明", "suggested_question": "可以问用户确认的问题"}}
"""


def _extract_json(text: str):
    """Extract JSON from model output with multiple fallbacks."""
    if not text:
        return None
    text = text.strip()

    def _try_parse(candidate: str):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            return None

    direct = _try_parse(text)
    if direct is not None:
        return direct

    code_block = re.search(
        r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE
    )
    if code_block:
        parsed = _try_parse(code_block.group(1).strip())
        if parsed is not None:
            return parsed

    decoder = json.JSONDecoder()
    for idx, ch in enumerate(text):
        if ch not in "[{":
            continue
        try:
            parsed, _ = decoder.raw_decode(text[idx:])
            return parsed
        except json.JSONDecodeError:
            continue
    return None


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip())


def _normalize_category(category: str) -> str:
    c = (category or "").strip().lower()
    alias = {
        "study": "education",
        "school": "education",
        "career": "work",
        "job": "work",
        "habit": "routine",
        "hobby": "preference",
        "feeling": "emotion",
        "mood": "emotion",
        "social": "relationship",
    }
    c = alias.get(c, c)
    return c if c in MEMORY_CATEGORIES else "misc"


def _is_too_fragmented(text: str) -> bool:
    if len(text) < 8:
        return True
    bad_patterns = [
        r"^用户[是有在想要爱喜欢讨厌]\s*$",
        r"^(中午|下午|晚上|今天|明天)$",
        r"^(喜欢|讨厌|在|是|有).{0,2}$",
    ]
    return any(re.search(p, text) for p in bad_patterns)


def _ensure_user_subject(text: str) -> str:
    t = text.strip()
    if not t:
        return t
    if t.startswith("用户"):
        return t
    if t.startswith(("我", "目前", "现在", "正在")):
        return f"用户{t}"
    if t.startswith(("是", "有", "在", "喜欢", "计划", "每天", "通常", "希望", "想")):
        return f"用户{t}"
    return f"用户{t}"


def _infer_confidence(text: str, evidence: str, memory_type: str) -> float:
    """
    Infer confidence level based on memory characteristics.
    High confidence: direct user statement with clear evidence
    Medium confidence: inferred from user message
    Low confidence: AI speculation or weak evidence
    """
    combined = f"{text} {evidence}".lower()

    direct_indicators = [
        "我是",
        "我叫",
        "我在",
        "我有",
        "我喜欢",
        "我讨厌",
        "我打算",
        "i am",
        "i'm",
        "my",
        "i have",
        "i like",
        "i want",
    ]
    for ind in direct_indicators:
        if ind in combined:
            return 0.9

    inferred_indicators = [
        "可能",
        "也许",
        "应该",
        "大概",
        "感觉",
        "觉得",
        "maybe",
        "might",
        "probably",
        "think",
        "seems",
    ]
    for ind in inferred_indicators:
        if ind in combined:
            return 0.6

    if memory_type == "fact":
        return 0.8
    elif memory_type == "preference":
        return 0.85
    elif memory_type == "plan":
        return 0.75
    elif memory_type == "relationship":
        return 0.7
    elif memory_type == "detail":
        return 0.5

    return 0.7


def _infer_decay_rate(category: str, time_scope: str) -> str:
    """
    Infer memory decay rate based on category and time scope.
    slow: stable facts (name, birthday, family)
    normal: typical information
    fast: temporary states, moods
    """
    slow_categories = {"identity", "profile", "relationship", "location"}
    fast_categories = {"emotion", "event"}

    if category in slow_categories:
        return "slow"
    if category in fast_categories:
        return "fast"
    if time_scope == "current":
        return "normal"
    if time_scope in {"past", "future"}:
        return "normal"

    return "normal"


def _sanitize_memories(parsed, user_message: str = "") -> list[dict]:
    """Validate and normalize extraction output into structured items."""
    if isinstance(parsed, dict):
        for key in ("items", "memories", "results", "data"):
            if key in parsed:
                parsed = parsed[key]
                break
        else:
            parsed = []

    if not isinstance(parsed, list):
        return []

    seen = set()
    items: list[dict] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue

        text = _normalize_text(str(item.get("text", "")))
        text = _ensure_user_subject(text)
        if _is_too_fragmented(text):
            continue
        if len(text) > 420:
            text = text[:420].rstrip() + "..."

        memory_type = str(item.get("type", "detail")).strip().lower()
        if memory_type not in MEMORY_TYPES:
            memory_type = "detail"

        category = _normalize_category(str(item.get("category", "misc")))

        importance = str(item.get("importance", "medium")).strip().lower()
        if importance not in IMPORTANCE_LEVELS:
            importance = "medium"

        time_scope = str(item.get("time_scope", "unknown")).strip().lower()
        if time_scope not in TIME_SCOPES:
            time_scope = "unknown"

        action = str(item.get("action", "add")).strip().lower()
        if action not in MEMORY_ACTIONS:
            action = "add"

        evidence = _normalize_text(str(item.get("evidence", "")))
        if len(evidence) > 160:
            evidence = evidence[:160] + "..."

        keywords: list[str] = []
        raw_keywords = item.get("keywords")
        if isinstance(raw_keywords, list):
            for kw in raw_keywords:
                kw_s = _normalize_text(str(kw))
                if kw_s and len(kw_s) <= 32:
                    keywords.append(kw_s)

        confidence = item.get("confidence")
        if isinstance(confidence, (int, float)):
            confidence = min(1.0, max(0.0, float(confidence)))
        else:
            confidence = _infer_confidence(text, evidence, memory_type)

        decay_rate = _infer_decay_rate(category, time_scope)

        needs_verification = confidence < 0.7 and importance in {"high", "critical"}

        key = (text.lower(), memory_type, category, action)
        if key in seen:
            continue
        seen.add(key)
        items.append(
            {
                "text": text,
                "type": memory_type,
                "category": category,
                "importance": importance,
                "time_scope": time_scope,
                "action": action,
                "evidence": evidence,
                "keywords": keywords,
                "confidence": round(confidence, 2),
                "decay_rate": decay_rate,
                "needs_verification": needs_verification,
                "extracted_at": datetime.now(timezone.utc).isoformat(),
                "source_message": user_message[:200] if user_message else "",
            }
        )
    return items


class MemoryExtractor:
    """Extract and classify memories from conversation text."""

    def __init__(
        self,
        model: str = "anthropic/gemini-3-flash",
        proxy_url: Optional[str] = None,
        proxy_enabled: bool = False,
    ):
        self.model = model
        self.proxy_url = proxy_url
        self.proxy_enabled = proxy_enabled

    def _llm_kwargs(self) -> dict:
        if self.proxy_enabled and self.proxy_url:
            return {"api_base": self.proxy_url, "api_key": "test"}
        return {}

    @staticmethod
    def _build_known_context(relevant_memories: Optional[list[dict]]) -> str:
        if not relevant_memories:
            return "（暂无）"
        lines = []
        for mem in relevant_memories[:12]:
            text = str(mem.get("memory", mem.get("text", ""))).strip()
            if not text:
                continue
            mtype = mem.get("metadata", {}).get("type", "note")
            cat = mem.get("metadata", {}).get("category", "misc")
            conf = mem.get("metadata", {}).get("confidence", "?")
            lines.append(f"- [{mtype}/{cat}] {text} (置信度:{conf})")
        return "\n".join(lines) if lines else "（暂无）"

    async def extract_memories(
        self,
        user_message: str,
        bot_response: str = "",
        relevant_memories: Optional[list[dict]] = None,
        conversation_context: str = "",
    ) -> list[dict]:
        """Analyze a turn and extract structured memory candidates."""
        known_context = self._build_known_context(relevant_memories)
        convo = str(conversation_context or "").strip()
        if not convo:
            convo = "（暂无）"
        try:
            response = await litellm.acompletion(
                model=self.model,
                messages=[
                    {
                        "role": "user",
                        "content": EXTRACTION_PROMPT.format(
                            known_context=known_context,
                            conversation_context=convo,
                            user_message=user_message,
                            bot_response=bot_response or "(空)",
                        ),
                    },
                ],
                temperature=0.1,
                max_tokens=2200,
                **self._llm_kwargs(),
            )
            content = response.choices[0].message.content
            if isinstance(content, list):
                content = "".join(
                    part.get("text", "") if isinstance(part, dict) else str(part)
                    for part in content
                )
            if not content:
                return []

            parsed = _extract_json(content)
            if parsed is None:
                logger.warning(f"Could not parse extractor output: {content[:260]}")
                return []

            items = _sanitize_memories(parsed, user_message)
            if items:
                logger.info(f"Extractor produced {len(items)} structured memories")
            return items
        except Exception as e:
            logger.error(f"Memory extraction failed: {e}")
            return []

    async def check_conflict(
        self,
        new_memory: str,
        existing_memories: list[dict],
    ) -> Optional[dict]:
        """Secondary conflict checker (fallback)."""
        if not existing_memories:
            return {
                "has_conflict": False,
                "conflict_type": "none",
                "suggested_action": "keep_both",
            }

        existing_text = "\n".join(
            f"- {m.get('memory', m.get('text', ''))}" for m in existing_memories[:8]
        )
        try:
            response = await litellm.acompletion(
                model=self.model,
                messages=[
                    {
                        "role": "user",
                        "content": CONFLICT_CHECK_PROMPT.format(
                            new_memory=new_memory,
                            existing_memories=existing_text,
                        ),
                    },
                ],
                temperature=0.1,
                max_tokens=300,
                **self._llm_kwargs(),
            )
            content = response.choices[0].message.content
            if isinstance(content, list):
                content = "".join(
                    part.get("text", "") if isinstance(part, dict) else str(part)
                    for part in content
                )
            parsed = _extract_json(content)
            if not isinstance(parsed, dict):
                return None
            action = str(parsed.get("suggested_action", "keep_both")).strip().lower()
            if action not in {"keep_both", "ignore_new", "replace_old"}:
                parsed["suggested_action"] = "keep_both"
            return parsed
        except Exception as e:
            logger.error(f"Conflict check failed: {e}")
            return None

    async def check_verification_needed(
        self,
        memory_text: str,
        evidence: str,
    ) -> Optional[dict]:
        """Check if a memory needs user verification."""
        try:
            response = await litellm.acompletion(
                model=self.model,
                messages=[
                    {
                        "role": "user",
                        "content": VERIFICATION_PROMPT.format(
                            memory_text=memory_text,
                            evidence=evidence or "(无)",
                        ),
                    },
                ],
                temperature=0.1,
                max_tokens=300,
                **self._llm_kwargs(),
            )
            content = response.choices[0].message.content
            if isinstance(content, list):
                content = "".join(
                    part.get("text", "") if isinstance(part, dict) else str(part)
                    for part in content
                )
            parsed = _extract_json(content)
            if not isinstance(parsed, dict):
                return None
            return parsed
        except Exception as e:
            logger.error(f"Verification check failed: {e}")
            return None
