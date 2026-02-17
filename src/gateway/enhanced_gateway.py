"""
EnhancedGateway — 扩展 Gateway，集成所有新功能模块

新功能:
  - EventScheduler: 事件提醒系统
  - FollowupManager: 主动跟进机制
  - EmotionAnalyzer: 情绪感知
  - SessionContext: 跨会话连贯性
  - 记忆置信度和衰减
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from src.gateway.gateway import Gateway
from src.memory.event_scheduler import EventScheduler, ScheduledEvent
from src.memory.followup_manager import FollowupManager, FollowupTopic
from src.memory.emotion_analyzer import EmotionAnalyzer, EmotionState
from src.memory.session_context import GlobalSessionContext
from src.memory.diary_manager import DiaryManager

logger = logging.getLogger(__name__)


class EnhancedGateway(Gateway):
    """扩展版 Gateway，集成所有智能功能"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.events = EventScheduler(timezone_str=self.user_timezone)
        self.followups = FollowupManager(timezone_str=self.user_timezone)
        self.emotion = EmotionAnalyzer(timezone_str=self.user_timezone)
        self.session = GlobalSessionContext(timezone_str=self.user_timezone)
        self.diary = DiaryManager(
            timezone_str=self.user_timezone,
            proxy_enabled=self.proxy_enabled,
            proxy_url=self.proxy_url,
        )

        self._pending_event_check = False
        self._last_event_check: Optional[datetime] = None
        self.event_check_interval_minutes = 5

        self._check_due_events_on_startup()
        logger.info(
            "EnhancedGateway initialized with EventScheduler, FollowupManager, "
            "EmotionAnalyzer, SessionContext, DiaryManager"
        )

    def _check_due_events_on_startup(self):
        """启动时检查是否有到期的提醒"""
        due = self.events.get_due_events()
        if due:
            logger.info(f"Found {len(due)} due events on startup")
            for event in due:
                self.events.mark_triggered(event.event_id)

    async def chat(
        self,
        message: str,
        channel_id: str = "default",
        model: Optional[str] = None,
        auto_learn: bool = True,
    ) -> str:
        """
        增强版对话处理，集成所有新功能。
        """
        self.session.record_interaction()

        emotion_state = self.emotion.analyze(message)

        self.session.check_and_auto_end_session()
        if self.session._current_session is None:
            self.session.start_session(channel_id)

        followup_topics = self.followups.extract_from_message(message)
        if followup_topics:
            logger.info(
                f"Extracted {len(followup_topics)} followup topics from message"
            )

        topic_updates = self.followups.check_message_for_topic_update(message)
        for topic, update_type in topic_updates:
            if update_type == "completed":
                self.followups.mark_completed(topic.topic_id)
                logger.info(f"Followup topic completed: {topic.title}")
            elif update_type == "abandoned":
                self.followups.mark_abandoned(topic.topic_id)
                logger.info(f"Followup topic abandoned: {topic.title}")

        event = self.events.add_from_message(message)
        if event:
            logger.info(f"Auto-created event: {event.title} at {event.trigger_time}")

        due_events = self._should_check_events()
        if due_events:
            for ev in due_events:
                self.events.mark_triggered(ev.event_id)

        is_returning, greeting_hint = self.session.is_returning_user()

        extra_instructions = await self._build_enhanced_context(
            message=message,
            emotion_state=emotion_state,
            due_events=due_events,
            is_returning=is_returning,
            greeting_hint=greeting_hint,
        )

        reply = await self._enhanced_chat(
            message=message,
            channel_id=channel_id,
            model=model,
            auto_learn=auto_learn,
            extra_instructions=extra_instructions,
        )

        # Track message for diary auto-generation
        if self.diary.enabled:
            self.diary.record_message()
            asyncio.create_task(self._try_auto_diary())

        return reply

    async def _enhanced_chat(
        self,
        message: str,
        channel_id: str,
        model: Optional[str],
        auto_learn: bool,
        extra_instructions: str,
    ) -> str:
        """调用父类 chat 方法，添加额外的上下文指令"""
        target_model = model or self.default_model

        original_message = message
        message = await self._preprocess_long_message(message)

        runtime_context_task = asyncio.create_task(self._build_runtime_context(message))
        archive_context_task = asyncio.create_task(
            self._build_archive_context(original_message, channel_id)
        )

        relevant_memories = await asyncio.to_thread(
            self.memory.search_with_details,
            message,
            limit=self.memory_retrieval_limit,
        )

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
        if extra_instructions:
            extra_parts.append(extra_instructions)

        extra_context = "\n\n".join(part for part in extra_parts if part)

        import litellm

        system_prompt = self.context_builder.build_system_prompt(
            relevant_memories=relevant_memories,
            extra_instructions=extra_context,
        )

        history = self._get_history(channel_id)
        messages = self.context_builder.build_messages(
            system_prompt=system_prompt,
            conversation_history=history,
            user_message=message,
        )

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
                reply = f"⚠️ 模型调用失败: {str(e)}"
            else:
                reply = f"⚠️ 模型调用失败: {str(e)}"
            return reply

        if self.allow_llm_soul_edit:
            reply = self._apply_soul_edits(reply)

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

    async def _build_enhanced_context(
        self,
        message: str,
        emotion_state: EmotionState,
        due_events: list[ScheduledEvent],
        is_returning: bool,
        greeting_hint: str,
    ) -> str:
        """构建增强的上下文信息"""
        parts: list[str] = []

        emotion_guidance = self.emotion.get_response_guidance()
        if emotion_guidance:
            parts.append(emotion_guidance)

        if due_events:
            event_lines = ["[到期提醒]"]
            for ev in due_events[:3]:
                event_lines.append(f"- {self.events.format_event_for_display(ev)}")
            event_lines.append("- 请在回复中自然地提及这些提醒。")
            parts.append("\n".join(event_lines))

        followup_topics = self.followups.get_topics_for_followup(limit=2)
        if followup_topics:
            followup_lines = ["[主动跟进]"]
            for topic in followup_topics:
                prompt = self.followups.generate_followup_prompt(topic)
                followup_lines.append(f"- {prompt}")
                self.followups.record_followup(topic.topic_id)
            followup_lines.append("- 如果对话自然，可以顺势询问；否则不要强行追问。")
            parts.append("\n".join(followup_lines))

        if is_returning:
            welcome_context = self.session.get_welcome_back_context()
            if welcome_context:
                parts.append(welcome_context)
            if greeting_hint:
                parts.append(f"[用户回归提示: {greeting_hint}]")

        upcoming_events = self.events.get_upcoming_events(days=3, limit=3)
        if upcoming_events:
            upcoming_lines = ["[即将到来的事件]"]
            for ev in upcoming_events:
                upcoming_lines.append(f"- {self.events.format_event_for_display(ev)}")
            parts.append("\n".join(upcoming_lines))

        return "\n\n".join(parts) if parts else ""

    def _should_check_events(self) -> list[ScheduledEvent]:
        """检查是否需要触发事件提醒"""
        now = datetime.now(timezone.utc)

        if self._last_event_check:
            elapsed = (now - self._last_event_check).total_seconds() / 60
            if elapsed < self.event_check_interval_minutes:
                return []

        self._last_event_check = now
        return self.events.get_due_events()

    def create_event(
        self,
        title: str,
        trigger_time: datetime,
        description: str = "",
        priority: str = "medium",
        recurrence: str = "none",
    ) -> ScheduledEvent:
        """手动创建事件提醒"""
        return self.events.add_event(
            title=title,
            trigger_time=trigger_time,
            description=description,
            priority=priority,
            recurrence=recurrence,
            source="manual",
        )

    def get_all_events(self, include_completed: bool = False) -> list[ScheduledEvent]:
        """获取所有事件"""
        return self.events.get_all_events(include_completed=include_completed)

    def complete_event(self, event_id: str) -> bool:
        """标记事件完成"""
        return self.events.mark_completed(event_id)

    def cancel_event(self, event_id: str) -> bool:
        """取消事件"""
        return self.events.cancel_event(event_id)

    def get_all_followups(self) -> list[FollowupTopic]:
        """获取所有活跃的跟进话题"""
        return self.followups.get_all_active_topics()

    def complete_followup(self, topic_id: str) -> bool:
        """标记跟进话题完成"""
        return self.followups.mark_completed(topic_id)

    def get_emotion_state(self) -> EmotionState:
        """获取当前情绪状态"""
        return self.emotion.get_current_state()

    def get_emotion_trend(self, hours: int = 24) -> dict:
        """获取情绪趋势"""
        return self.emotion.get_emotion_trend(hours=hours)

    def get_session_summary(self) -> dict:
        """获取会话摘要"""
        recent = self.session.get_recent_sessions(limit=5)
        return {
            "total_sessions": len(self.session.sessions),
            "recent_sessions": [s.to_dict() for s in recent],
            "last_interaction": self.session._last_interaction.isoformat()
            if self.session._last_interaction
            else None,
            "is_returning": self.session.is_returning_user()[0],
        }

    def get_welcome_back_context(self) -> str:
        """获取欢迎回来的上下文"""
        return self.session.get_welcome_back_context()

    def end_current_session(self, summary: str = ""):
        """结束当前会话"""
        self.session.end_session(summary=summary)

    def cleanup_all(self, days: int = 30):
        """清理所有模块的旧数据"""
        self.events.cleanup_old_events(days=days)
        self.followups.cleanup_old_topics(days=days)
        self.emotion.cleanup_old_records(days=7)
        self.session.cleanup_old_sessions(days=days)
        logger.info(f"Cleaned up old data (older than {days} days)")

    # ── Diary ─────────────────────────────────────────────────────────

    async def _try_auto_diary(self):
        """Background task: auto-generate diary if conditions met."""
        try:
            result = await self.diary.maybe_auto_generate()
            if result:
                logger.info("Auto-diary generated successfully")
        except Exception as e:
            logger.error(f"Auto-diary generation failed: {e}")

    async def generate_diary(self, date: Optional[str] = None, force: bool = False) -> str:
        """Generate diary for a date. Returns diary content."""
        return await self.diary.generate_diary(date=date, force=force)

    def get_diary(self, date: Optional[str] = None) -> Optional[str]:
        """Get existing diary for a date."""
        return self.diary.get_diary(date=date)

    def list_diaries(self, month: Optional[str] = None, limit: int = 30) -> list[dict]:
        """List available diary entries."""
        return self.diary.list_diaries(month=month, limit=limit)
