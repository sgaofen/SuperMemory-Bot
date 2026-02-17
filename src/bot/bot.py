"""
Discord Bot — the primary conversational interface for the AI Brain.

Handles:
  - Message events → forward to Gateway → reply
  - Slash commands (/remember, /recall, /whoami, /model, /forget, /clear)
  - Per-channel conversation boundaries
"""

from __future__ import annotations

import asyncio
import logging
import os

import discord
from discord import app_commands

from src.gateway.gateway import Gateway

logger = logging.getLogger(__name__)


class BrainBot(discord.Client):
    """The AI Brain Discord bot."""

    def __init__(self, gateway: Gateway):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)

        self.gateway = gateway
        self.voice_auto_transcribe = (
            os.getenv("VOICE_AUTO_TRANSCRIBE", "true").lower() == "true"
        )
        self.voice_transcribe_max_file_size = int(
            os.getenv("VOICE_TRANSCRIBE_MAX_FILE_SIZE", "15000000")
        )
        self.tree = app_commands.CommandTree(self)
        self._processed_messages: set[int] = set()  # dedup message IDs
        self._setup_commands()

    @staticmethod
    def _is_audio_attachment(att: discord.Attachment) -> bool:
        name = str(att.filename or "")
        ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
        audio_exts = {".ogg", ".opus", ".mp3", ".wav", ".m4a", ".flac", ".webm", ".aac"}
        content_type = str(att.content_type or "").lower()
        return (
            getattr(att, "is_voice_message", False)
            or ext in audio_exts
            or "audio" in content_type
        )

    def _setup_commands(self):
        """Register all slash commands."""

        @self.tree.command(name="remember", description="手动添加一条记忆")
        @app_commands.describe(
            text="要记住的内容",
            memory_type="记忆类型: fact / preference / plan / note / relationship / detail",
        )
        async def remember(
            interaction: discord.Interaction,
            text: str,
            memory_type: str = "note",
        ):
            await interaction.response.defer(thinking=True)
            self.gateway.memory.add(text, memory_type=memory_type, source="manual")
            await interaction.followup.send(f"✅ 已记住: {text}\n类型: `{memory_type}`")

        @self.tree.command(name="recall", description="搜索记忆")
        @app_commands.describe(query="搜索关键词")
        async def recall(interaction: discord.Interaction, query: str):
            await interaction.response.defer(thinking=True)
            results = self.gateway.memory.search(query, limit=10)
            if not results:
                await interaction.followup.send("🔍 没有找到相关记忆。")
                return

            lines = [f"🔍 **搜索「{query}」的结果：**\n"]
            for i, mem in enumerate(results, 1):
                text = mem.get("memory", mem.get("text", ""))
                metadata = mem.get("metadata", {})
                mtype = metadata.get("type", "?")
                category = metadata.get("category", "misc")
                importance = metadata.get("importance", "unknown")
                time_scope = metadata.get("time_scope", "unknown")
                score = mem.get("score", 0)
                memory_id = mem.get("id", "?")
                lines.append(
                    f"{i}. [{mtype}/{category}/{importance}/{time_scope}] {text} "
                    f"_(相关度: {score:.2f}, ID: `{memory_id}`)_"
                )

            await interaction.followup.send("\n".join(lines))

        @self.tree.command(name="whoami", description="查看 AI 对你的记忆档案")
        async def whoami(interaction: discord.Interaction):
            await interaction.response.defer(thinking=True)
            profile = self.gateway.memory.get_profile_summary()
            # Discord has a 2000 char limit
            if len(profile) > 1900:
                profile = profile[:1900] + "\n\n_(记忆太多，已截断)_"
            await interaction.followup.send(profile)

        @self.tree.command(name="model", description="切换当前使用的 AI 模型")
        @app_commands.describe(
            name="模型名称，如 opus, sonnet, gemini-pro, gemini-flash"
        )
        async def switch_model(interaction: discord.Interaction, name: str):
            # Map friendly names to proxy model strings
            model_aliases = {
                "opus": "anthropic/claude-opus-4-6-thinking",
                "opus-4.6": "anthropic/claude-opus-4-6-thinking",
                "opus-4.5": "anthropic/claude-opus-4-5-thinking",
                "sonnet": "anthropic/claude-sonnet-4-5",
                "sonnet-thinking": "anthropic/claude-sonnet-4-5-thinking",
                "gemini-pro": "anthropic/gemini-3-pro-high",
                "gemini-flash": "anthropic/gemini-2.5-flash",
                "gemini": "anthropic/gemini-2.5-flash",
            }
            resolved = model_aliases.get(name.lower(), name)
            self.gateway.default_model = resolved
            await interaction.response.send_message(f"🔄 模型已切换为: `{resolved}`")

        @self.tree.command(name="forget", description="删除一条记忆（需要记忆ID）")
        @app_commands.describe(memory_id="要删除的记忆 ID")
        async def forget(interaction: discord.Interaction, memory_id: str):
            await interaction.response.defer(thinking=True)
            try:
                self.gateway.memory.delete(memory_id)
                await interaction.followup.send(f"🗑️ 已删除记忆: `{memory_id}`")
            except Exception as e:
                await interaction.followup.send(f"❌ 删除失败: {e}")

        @self.tree.command(name="correct", description="纠正一条记忆（替换旧内容）")
        @app_commands.describe(
            memory_id="要纠正的记忆 ID（可通过 /recall 查看）",
            new_text="新的正确内容",
        )
        async def correct(
            interaction: discord.Interaction,
            memory_id: str,
            new_text: str,
        ):
            await interaction.response.defer(thinking=True)
            try:
                result = self.gateway.memory.correct(
                    memory_id=memory_id,
                    new_text=new_text,
                )
                if result.get("ok"):
                    await interaction.followup.send(
                        f"✅ 已纠正记忆 `{memory_id}`\n新内容: {new_text}"
                    )
                else:
                    await interaction.followup.send(
                        f"❌ 纠正失败: {result.get('error', 'unknown error')}"
                    )
            except Exception as e:
                await interaction.followup.send(f"❌ 纠正失败: {e}")

        @self.tree.command(
            name="repair_memory", description="修复向量库索引并从历史日志恢复记忆"
        )
        async def repair_memory(interaction: discord.Interaction):
            await interaction.response.defer(thinking=True)
            try:
                stats = self.gateway.memory.repair_vector_store(
                    reason="manual_discord_command"
                )
                if stats.get("ok"):
                    recovered = stats.get("recovered", 0)
                    backup = stats.get("backup_path", "(none)")
                    await interaction.followup.send(
                        "✅ 向量库修复完成\n"
                        f"- 恢复记忆: {recovered}\n"
                        f"- 损坏库备份: `{backup}`"
                    )
                else:
                    await interaction.followup.send(
                        f"❌ 修复失败: {stats.get('error', stats.get('reason', 'unknown error'))}"
                    )
            except Exception as e:
                await interaction.followup.send(f"❌ 修复失败: {e}")

        @self.tree.command(name="clear", description="清除当前频道的对话历史")
        async def clear(interaction: discord.Interaction):
            channel_id = str(interaction.channel_id)
            self.gateway.clear_history(channel_id)
            await interaction.response.send_message("🧹 对话历史已清除。记忆仍然保留。")

        @self.tree.command(
            name="soul", description="查看或刷新 AI 的人格定义与自动总结"
        )
        @app_commands.describe(action="view=查看；refresh=强制刷新自动总结")
        @app_commands.choices(
            action=[
                app_commands.Choice(name="查看", value="view"),
                app_commands.Choice(name="刷新自动总结", value="refresh"),
            ]
        )
        async def soul(interaction: discord.Interaction, action: str = "view"):
            await interaction.response.defer(thinking=True)
            if action == "refresh":
                self.gateway.refresh_soul_summary()
            soul_text = self.gateway.soul.read()
            if len(soul_text) > 1900:
                soul_text = soul_text[:1900] + "\n\n_(已截断)_"
            if action == "refresh":
                await interaction.followup.send(
                    f"✅ 已刷新自动总结。\n\n```markdown\n{soul_text}\n```"
                )
            else:
                await interaction.followup.send(f"```markdown\n{soul_text}\n```")

        @self.tree.command(name="status", description="查看当前上下文窗口使用情况")
        async def status(interaction: discord.Interaction):
            await interaction.response.defer(thinking=True)
            channel_id = str(interaction.channel_id)
            stats = self.gateway.get_context_status(channel_id)

            def bar(used: int, budget: int, width: int = 16) -> str:
                pct = min(used / max(1, budget), 1.0)
                filled = int(pct * width)
                empty = width - filled
                if pct >= 0.85:
                    emoji = "🔴"
                elif pct >= 0.6:
                    emoji = "🟡"
                else:
                    emoji = "🟢"
                return f"{emoji} `{'█' * filled}{'░' * empty}` {used:,}/{budget:,} tokens ({pct:.0%})"

            lines = [
                "## 📊 上下文窗口状态\n",
                f"**🧠 Soul 人格**",
                f"   {bar(stats['soul_tokens'], stats['system_budget'])}",
                "",
                f"**💾 记忆检索** ({stats['memory_count']} 条检索 / {stats['total_memories']} 条总计)",
                f"   {bar(stats['memory_tokens'], stats['memory_budget'])}",
                "",
                f"**💬 对话历史** ({stats['history_turns']} 轮 / {stats['history_messages']} 条消息)",
                f"   {bar(stats['history_tokens'], stats['history_budget'])}",
                "",
                "---",
                f"**📦 总计**: {bar(stats['total_used'], stats['total_budget'])}",
            ]

            # Recommendations
            if stats["usage_pct"] >= 85:
                lines.append(
                    "\n⚠️ **上下文接近上限！** 建议执行 `/compact` 压缩对话历史。"
                )
            elif stats["usage_pct"] >= 60:
                lines.append("\n💡 上下文使用适中，可以考虑 `/compact` 释放空间。")
            else:
                lines.append("\n✅ 上下文空间充裕。")

            await interaction.followup.send("\n".join(lines))

        @self.tree.command(
            name="memory_health", description="查看记忆库健康状态（总量/噪声候选/分布）"
        )
        async def memory_health(interaction: discord.Interaction):
            await interaction.response.defer(thinking=True)
            try:
                stats = await asyncio.to_thread(
                    self.gateway.memory.get_health_snapshot,
                    scan_noise=True,
                )
            except Exception as e:
                await interaction.followup.send(f"❌ 获取记忆健康状态失败: {e}")
                return

            def top_items(d: dict, n: int = 6) -> str:
                if not d:
                    return "无"
                pairs = sorted(d.items(), key=lambda x: x[1], reverse=True)[:n]
                return " / ".join(f"{k}:{v}" for k, v in pairs)

            total = int(stats.get("total", 0))
            collection_total = int(stats.get("collection_total", total))
            raw_detail_count = int(stats.get("raw_detail_count", 0))
            raw_detail_ratio = float(stats.get("raw_detail_ratio", 0.0))
            noise_candidates = int(stats.get("noise_candidates", 0))

            lines = [
                "## 🩺 记忆库健康状态",
                f"- 可读记忆总量: {total}",
                f"- 向量库总行数: {collection_total}",
                f"- 原始细节占比: {raw_detail_count} ({raw_detail_ratio:.1%})",
                f"- 低价值候选(可清理): {noise_candidates}",
                "",
                f"- 类型分布: {top_items(stats.get('by_type', {}))}",
                f"- 来源分布: {top_items(stats.get('by_source', {}))}",
                f"- 分类分布: {top_items(stats.get('by_category', {}))}",
            ]

            sample = stats.get("noise_sample", []) or []
            if sample:
                lines.append("")
                lines.append("### 噪声样例预览")
                for item in sample[:3]:
                    sid = str(item.get("id", ""))[:8]
                    txt = str(item.get("text", "")).strip()
                    if len(txt) > 80:
                        txt = txt[:80] + "..."
                    lines.append(f"- `{sid}` {txt}")

            msg = "\n".join(lines)
            if len(msg) > 1900:
                msg = msg[:1900] + "\n\n_(已截断)_"
            await interaction.followup.send(msg)

        @self.tree.command(
            name="compact", description="压缩当前频道的对话历史，释放上下文空间"
        )
        async def compact(interaction: discord.Interaction):
            await interaction.response.defer(thinking=True)
            channel_id = str(interaction.channel_id)

            # Show before stats
            before = self.gateway.get_context_status(channel_id)

            result = await self.gateway.compact_history(channel_id)

            if result["action"] == "skip":
                await interaction.followup.send(
                    f"ℹ️ 对话历史太短（{result['messages']} 条消息），不需要压缩。"
                )
                return

            # Show after stats
            after = self.gateway.get_context_status(channel_id)

            if result["action"] == "compacted":
                lines = [
                    "✅ **对话历史已压缩！**\n",
                    f"📝 **压缩了** {result['summarized_messages']} 条旧消息 → 1 条摘要",
                    f"💬 **保留了** {result['kept_messages']} 条最近消息",
                    f"💾 **节省了** ~{result['tokens_saved']:,} tokens",
                    "",
                    f"**摘要预览**: _{result['summary_preview']}..._"
                    if result.get("summary_preview")
                    else "",
                    "",
                    f"历史 tokens: {before['history_tokens']:,} → {after['history_tokens']:,}",
                    f"总使用率: {before['usage_pct']}% → {after['usage_pct']}%",
                ]
            else:
                lines = [
                    "⚠️ **对话历史已裁剪**（LLM 摘要失败，直接截断）\n",
                    f"移除了 {result['removed']} 条旧消息",
                    f"保留了 {result['kept']} 条最近消息",
                ]

            await interaction.followup.send("\n".join(lines))

        # ==================== Enhanced Gateway Commands ====================

        @self.tree.command(name="remind", description="创建一个提醒")
        @app_commands.describe(
            title="提醒内容",
            when="提醒时间（如：明天下午3点、周五、2024-03-15 10:00）",
            priority="优先级: critical / high / medium / low",
        )
        @app_commands.choices(
            priority=[
                app_commands.Choice(name="高 (critical)", value="critical"),
                app_commands.Choice(name="中高 (high)", value="high"),
                app_commands.Choice(name="中等 (medium)", value="medium"),
                app_commands.Choice(name="低 (low)", value="low"),
            ]
        )
        async def remind(
            interaction: discord.Interaction,
            title: str,
            when: str,
            priority: str = "medium",
        ):
            await interaction.response.defer(thinking=True)

            from src.gateway.enhanced_gateway import EnhancedGateway

            if not isinstance(self.gateway, EnhancedGateway):
                await interaction.followup.send(
                    "⚠️ 增强功能未启用，请设置 USE_ENHANCED_GATEWAY=true"
                )
                return

            from src.memory.event_scheduler import _parse_relative_time
            from datetime import datetime, timezone
            from zoneinfo import ZoneInfo

            zone = ZoneInfo(os.getenv("USER_TIMEZONE", "America/Los_Angeles"))
            trigger_time = _parse_relative_time(when, datetime.now(timezone.utc), zone)

            if not trigger_time:
                await interaction.followup.send(
                    f"❌ 无法解析时间: {when}\n请使用如: 明天下午3点、周五、2024-03-15 10:00"
                )
                return

            event = self.gateway.create_event(
                title=title,
                trigger_time=trigger_time,
                priority=priority,
            )

            local_time = trigger_time.astimezone(zone)
            await interaction.followup.send(
                f"✅ 已创建提醒\n"
                f"📌 **{title}**\n"
                f"⏰ {local_time.strftime('%Y-%m-%d %H:%M')}\n"
                f"🎯 优先级: {priority}"
            )

        @self.tree.command(name="reminders", description="查看所有提醒")
        @app_commands.describe(include_completed="是否包含已完成的提醒")
        async def reminders_cmd(
            interaction: discord.Interaction, include_completed: bool = False
        ):
            await interaction.response.defer(thinking=True)

            from src.gateway.enhanced_gateway import EnhancedGateway

            if not isinstance(self.gateway, EnhancedGateway):
                await interaction.followup.send("⚠️ 增强功能未启用")
                return

            events = self.gateway.get_all_events(include_completed=include_completed)
            if not events:
                await interaction.followup.send("📭 暂无提醒")
                return

            lines = [f"## 📋 提醒列表 ({len(events)}个)\n"]
            for ev in events[:15]:
                lines.append(self.gateway.events.format_event_for_display(ev))

            if len(events) > 15:
                lines.append(f"\n_...还有 {len(events) - 15} 个提醒未显示_")

            msg = "\n".join(lines)
            if len(msg) > 1900:
                msg = msg[:1900] + "\n\n_(已截断)_"
            await interaction.followup.send(msg)

        @self.tree.command(name="complete_reminder", description="完成一个提醒")
        @app_commands.describe(event_id="提醒ID（可通过 /reminders 查看）")
        async def complete_reminder(interaction: discord.Interaction, event_id: str):
            await interaction.response.defer(thinking=True)

            from src.gateway.enhanced_gateway import EnhancedGateway

            if not isinstance(self.gateway, EnhancedGateway):
                await interaction.followup.send("⚠️ 增强功能未启用")
                return

            if self.gateway.complete_event(event_id):
                await interaction.followup.send(f"✅ 已完成提醒: `{event_id}`")
            else:
                await interaction.followup.send(f"❌ 未找到提醒: `{event_id}`")

        @self.tree.command(name="followups", description="查看需要跟进的话题")
        async def followups(interaction: discord.Interaction):
            await interaction.response.defer(thinking=True)

            from src.gateway.enhanced_gateway import EnhancedGateway

            if not isinstance(self.gateway, EnhancedGateway):
                await interaction.followup.send("⚠️ 增强功能未启用")
                return

            topics = self.gateway.get_all_followups()
            if not topics:
                await interaction.followup.send("📭 暂无需要跟进的话题")
                return

            lines = [f"## 📌 跟进话题 ({len(topics)}个)\n"]
            for t in topics[:10]:
                days = t.days_since_last_mention()
                status_emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(
                    t.priority, "⚪"
                )
                lines.append(f"{status_emoji} **{t.title}** ({t.category})")
                lines.append(
                    f"   上次提及: {int(days)}天前 | 提及次数: {t.mention_count}"
                )
                lines.append(f"   ID: `{t.topic_id}`\n")

            msg = "\n".join(lines)
            if len(msg) > 1900:
                msg = msg[:1900] + "\n\n_(已截断)_"
            await interaction.followup.send(msg)

        @self.tree.command(name="complete_followup", description="标记话题为已完成")
        @app_commands.describe(topic_id="话题ID（可通过 /followups 查看）")
        async def complete_followup(interaction: discord.Interaction, topic_id: str):
            await interaction.response.defer(thinking=True)

            from src.gateway.enhanced_gateway import EnhancedGateway

            if not isinstance(self.gateway, EnhancedGateway):
                await interaction.followup.send("⚠️ 增强功能未启用")
                return

            if self.gateway.complete_followup(topic_id):
                await interaction.followup.send(f"✅ 已完成跟进话题: `{topic_id}`")
            else:
                await interaction.followup.send(f"❌ 未找到话题: `{topic_id}`")

        @self.tree.command(name="emotion", description="查看当前情绪状态和趋势")
        async def emotion(interaction: discord.Interaction):
            await interaction.response.defer(thinking=True)

            from src.gateway.enhanced_gateway import EnhancedGateway

            if not isinstance(self.gateway, EnhancedGateway):
                await interaction.followup.send("⚠️ 增强功能未启用")
                return

            state = self.gateway.get_emotion_state()
            trend = self.gateway.get_emotion_trend(hours=24)

            emotion_emoji = {
                "happy": "😊",
                "excited": "🤩",
                "calm": "😌",
                "neutral": "😐",
                "stressed": "😰",
                "anxious": "😟",
                "sad": "😢",
                "angry": "😤",
                "tired": "😴",
                "frustrated": "😒",
                "hopeful": "🌟",
                "confused": "🤔",
            }.get(state.emotion, "❓")

            energy_emoji = {
                "high": "⚡",
                "medium": "🔋",
                "low": "🪫",
                "exhausted": "💀",
            }

            lines = [
                "## 💭 情绪状态",
                f"当前情绪: {emotion_emoji} **{state.emotion}** (置信度: {state.confidence:.0%})",
                f"能量水平: {energy_emoji.get(state.energy_level, '❓')} **{state.energy_level}**",
                f"深夜模式: {'🌙 是' if state.is_late_night else '☀️ 否'}",
                "",
                "## 📈 24小时趋势",
                f"主导情绪: {emotion_emoji} **{trend.get('dominant_emotion', 'neutral')}**",
                f"趋势方向: {trend.get('trend_direction', 'stable')}",
                f"稳定性: {trend.get('stability', 1.0):.0%}",
            ]

            await interaction.followup.send("\n".join(lines))

        @self.tree.command(name="session", description="查看会话状态")
        async def session_cmd(interaction: discord.Interaction):
            await interaction.response.defer(thinking=True)

            from src.gateway.enhanced_gateway import EnhancedGateway

            if not isinstance(self.gateway, EnhancedGateway):
                await interaction.followup.send("⚠️ 增强功能未启用")
                return

            summary = self.gateway.get_session_summary()
            is_returning = summary.get("is_returning", False)
            welcome = self.gateway.get_welcome_back_context()

            lines = [
                "## 📊 会话状态",
                f"总会话数: {summary.get('total_sessions', 0)}",
                f"用户回归: {'是' if is_returning else '否'}",
            ]

            if welcome:
                lines.append(f"\n**欢迎回来上下文:**\n```\n{welcome}\n```")

            await interaction.followup.send("\n".join(lines))

        # ==================== Diary Commands ====================

        @self.tree.command(name="diary", description="生成或查看今天的日记")
        @app_commands.describe(
            action="操作类型",
            date="日期 (YYYY-MM-DD)，默认今天",
        )
        @app_commands.choices(
            action=[
                app_commands.Choice(name="查看/生成今天", value="today"),
                app_commands.Choice(name="查看指定日期", value="view"),
                app_commands.Choice(name="列出最近日记", value="list"),
                app_commands.Choice(name="重新生成今天", value="regenerate"),
            ]
        )
        async def diary_cmd(
            interaction: discord.Interaction,
            action: str = "today",
            date: str = "",
        ):
            await interaction.response.defer(thinking=True)

            from src.gateway.enhanced_gateway import EnhancedGateway

            if not isinstance(self.gateway, EnhancedGateway):
                await interaction.followup.send("⚠️ 增强功能未启用")
                return

            if not self.gateway.diary.enabled:
                await interaction.followup.send("⚠️ 日记功能未启用，请设置 DIARY_ENABLED=true")
                return

            if action == "today":
                # Generate or show today's diary
                content = await self.gateway.generate_diary()
                if content:
                    # Truncate for Discord
                    if len(content) > 1900:
                        content = content[:1900] + "\n\n_(日记太长，已截断。完整内容请查看本地文件)_"
                    await interaction.followup.send(content)
                else:
                    await interaction.followup.send("📝 今天还没有足够的对话来生成日记。")

            elif action == "view":
                if not date:
                    await interaction.followup.send("❌ 请指定日期，格式: YYYY-MM-DD")
                    return
                content = self.gateway.get_diary(date)
                if content:
                    if len(content) > 1900:
                        content = content[:1900] + "\n\n_(已截断)_"
                    await interaction.followup.send(content)
                else:
                    await interaction.followup.send(f"📭 {date} 没有日记记录")

            elif action == "list":
                entries = self.gateway.list_diaries(limit=15)
                if not entries:
                    await interaction.followup.send("📭 还没有任何日记")
                    return
                lines = ["## 📔 日记列表\n"]
                for entry in entries:
                    d = entry.get("date", "?")
                    preview = entry.get("preview", "")
                    chars = entry.get("size_chars", 0)
                    lines.append(f"📅 **{d}** ({chars}字)")
                    if preview:
                        lines.append(f"   _{preview}_\n")
                msg = "\n".join(lines)
                if len(msg) > 1900:
                    msg = msg[:1900] + "\n\n_(已截断)_"
                await interaction.followup.send(msg)

            elif action == "regenerate":
                target = date or None
                content = await self.gateway.generate_diary(date=target, force=True)
                if content:
                    header = f"✅ 已重新生成日记\n\n"
                    if len(header + content) > 1900:
                        content = content[:1900 - len(header)] + "\n\n_(已截断)_"
                    await interaction.followup.send(header + content)
                else:
                    await interaction.followup.send("❌ 日记生成失败")

    async def setup_hook(self):
        """Sync slash commands when the bot starts."""
        await self.tree.sync()
        logger.info("Slash commands synced")

    async def on_ready(self):
        """Called when the bot is connected and ready."""
        logger.info(f"🧠 Brain is online as {self.user} (ID: {self.user.id})")
        logger.info(f"   Default model: {self.gateway.default_model}")
        logger.info(f"   Guilds: {len(self.guilds)}")

        # Set status
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.listening,
                name="你的想法 💭",
            )
        )

    async def on_message(self, message: discord.Message):
        """Handle incoming messages."""
        # Ignore own messages
        if message.author == self.user:
            return

        # Ignore messages from other bots
        if message.author.bot:
            return

        # Dedup: prevent processing the same message twice
        if message.id in self._processed_messages:
            return
        self._processed_messages.add(message.id)
        # Keep set bounded to avoid memory leak
        if len(self._processed_messages) > 500:
            # Remove oldest entries (set is unordered, but this is good enough)
            to_remove = list(self._processed_messages)[:250]
            for mid in to_remove:
                self._processed_messages.discard(mid)

        # Check if the bot is mentioned or if it's a DM
        is_dm = isinstance(message.channel, discord.DMChannel)
        is_mentioned = self.user in message.mentions
        has_audio_attachment = any(
            self._is_audio_attachment(att) for att in message.attachments
        )

        # Debug logging for message routing
        logger.debug(
            "Message: dm=%s, mentioned=%s, audio=%s, content_len=%d, "
            "author=%s, channel=%s",
            is_dm,
            is_mentioned,
            has_audio_attachment,
            len(message.content),
            message.author,
            message.channel,
        )

        # Respond in guild when mentioned, and also allow direct voice messages.
        if (
            not is_dm
            and not is_mentioned
            and not (self.voice_auto_transcribe and has_audio_attachment)
        ):
            return

        # Clean up the mention from the message text
        content = message.content
        if is_mentioned:
            content = content.replace(f"<@{self.user.id}>", "").strip()
            content = content.replace(f"<@!{self.user.id}>", "").strip()

        # Process attachments
        attachment_texts = await self._process_attachments(message.attachments)
        if attachment_texts:
            attachment_block = "\n\n".join(attachment_texts)
            if content:
                content = f"{content}\n\n[用户发送了以下文件]\n{attachment_block}"
            else:
                content = f"[用户发送了以下文件]\n{attachment_block}"

        # If bot was mentioned but content is empty, MESSAGE CONTENT INTENT
        # may not be enabled in the Discord Developer Portal.
        if not content and is_mentioned:
            await message.reply(
                "⚠️ 我收到了你的 @提及，但无法读取消息内容。\n"
                "请在 [Discord Developer Portal](https://discord.com/developers/applications) "
                "中开启 **MESSAGE CONTENT INTENT**。\n\n"
                "路径: Bot → Privileged Gateway Intents → MESSAGE CONTENT INTENT ✅",
                mention_author=False,
            )
            return

        if not content:
            return

        # Show typing indicator
        async with message.channel.typing():
            channel_id = str(message.channel.id)

            logger.info(
                "Processing msg_id=%s from=%s content=%r",
                message.id,
                message.author,
                content[:80],
            )

            # Get response from gateway
            reply = await self.gateway.chat(
                message=content,
                channel_id=channel_id,
            )

            logger.info(
                "Reply for msg_id=%s len=%d preview=%r",
                message.id,
                len(reply),
                reply[:100],
            )

            # Discord has a 2000 char limit — split if needed
            if len(reply) <= 2000:
                await message.reply(reply, mention_author=False)
            else:
                # Split into chunks
                chunks = [reply[i : i + 1990] for i in range(0, len(reply), 1990)]
                for i, chunk in enumerate(chunks):
                    if i == 0:
                        await message.reply(chunk, mention_author=False)
                    else:
                        await message.channel.send(chunk)

    async def _process_attachments(
        self, attachments: list[discord.Attachment]
    ) -> list[str]:
        """
        Download and process attachments (text/image/audio).
        Returns a list of formatted strings describing each attachment.
        """
        if not attachments:
            return []

        TEXT_EXTENSIONS = {
            ".txt",
            ".md",
            ".py",
            ".js",
            ".ts",
            ".json",
            ".csv",
            ".html",
            ".css",
            ".xml",
            ".yaml",
            ".yml",
            ".toml",
            ".sh",
            ".bash",
            ".c",
            ".cpp",
            ".h",
            ".java",
            ".go",
            ".rs",
            ".rb",
            ".php",
            ".sql",
            ".r",
            ".log",
            ".ini",
            ".cfg",
            ".conf",
            ".env",
            ".gitignore",
            ".dockerfile",
        }
        IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
        MAX_FILE_SIZE = 5_000_000  # 5MB max per file

        results = []
        for att in attachments:
            name = att.filename
            ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
            content_type = str(att.content_type or "").lower()
            is_audio = self._is_audio_attachment(att)

            if (
                ext in TEXT_EXTENSIONS
                or att.content_type
                and "text" in att.content_type
            ):
                if att.size > MAX_FILE_SIZE:
                    results.append(
                        f"📄 **{name}** ({att.size:,} bytes — 文件太大，已跳过)"
                    )
                    continue
                try:
                    data = await att.read()
                    text = data.decode("utf-8", errors="replace")
                    results.append(f"📄 **{name}** 内容:\n```\n{text}\n```")
                except Exception as e:
                    logger.error(f"Failed to read attachment {name}: {e}")
                    results.append(f"📄 **{name}** (读取失败: {e})")

            elif (
                ext in IMAGE_EXTENSIONS
                or att.content_type
                and "image" in att.content_type
            ):
                results.append(
                    f"🖼️ **{name}** (图片, {att.size:,} bytes, URL: {att.url})"
                )

            elif self.voice_auto_transcribe and is_audio:
                if att.size > self.voice_transcribe_max_file_size:
                    results.append(
                        f"🎙️ **{name}** ({att.size:,} bytes — 语音文件太大，已跳过转写)"
                    )
                    continue
                try:
                    audio_bytes = await att.read()
                    transcript = await self.gateway.transcribe_audio(
                        audio_bytes=audio_bytes,
                        mime_type=att.content_type or "",
                        filename=name,
                    )
                    if transcript:
                        if len(transcript) > 8_000:
                            transcript = transcript[:8_000] + "...(转写已截断)"
                        results.append(f"🎙️ **{name}** 语音转写:\n{transcript}")
                    else:
                        results.append(
                            f"🎙️ **{name}** ({att.size:,} bytes — 语音转写失败)"
                        )
                except Exception as e:
                    logger.error(f"Failed to transcribe audio attachment {name}: {e}")
                    results.append(f"🎙️ **{name}** (语音转写失败: {e})")

            else:
                results.append(
                    f"📎 **{name}** ({att.content_type or '未知类型'}, "
                    f"{att.size:,} bytes — 暂不支持此格式)"
                )

        return results


def create_bot(gateway: Gateway) -> BrainBot:
    """Create and return a configured BrainBot instance."""
    return BrainBot(gateway=gateway)
