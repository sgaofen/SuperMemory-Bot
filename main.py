"""
AI Brain — Entry Point

Initializes all components and starts the Discord bot.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root
PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("ai-brain")


def main():
    """Initialize and start the AI Brain."""
    # Check for Discord token
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        logger.error("❌ DISCORD_BOT_TOKEN not found in .env")
        logger.error("   Please copy .env.example to .env and add your token")
        sys.exit(1)

    # Check LLM access
    proxy_enabled = os.getenv("ANTIGRAVITY_PROXY_ENABLED", "").lower() == "true"
    has_llm = any(
        [
            os.getenv("OPENAI_API_KEY"),
            os.getenv("ANTHROPIC_API_KEY"),
            os.getenv("GEMINI_API_KEY"),
        ]
    )
    if proxy_enabled:
        proxy_url = os.getenv("ANTIGRAVITY_PROXY_URL", "http://localhost:8080")
        logger.info(f"🔗 Antigravity 反代已启用: {proxy_url}")
    elif not has_llm:
        logger.warning("⚠️  No LLM API keys found and proxy not enabled")
        logger.warning("   Either set ANTIGRAVITY_PROXY_ENABLED=true or add API keys")

    default_model = os.getenv("DEFAULT_MODEL", "claude-opus-4-6-thinking")
    logger.info(f"🧠 AI Brain starting...")
    logger.info(f"   Model: {default_model}")

    # Auto-create soul.md from template on first run
    soul_path = PROJECT_ROOT / "soul.md"
    soul_example = PROJECT_ROOT / "soul.example.md"
    if not soul_path.exists() and soul_example.exists():
        import shutil
        shutil.copy2(soul_example, soul_path)
        logger.info(f"📝 Created soul.md from template — customize it to define your AI's personality!")
    logger.info(f"   Soul: {soul_path}")

    # Initialize components
    from src.memory.memory_manager import MemoryManager
    from src.memory.soul_manager import SoulManager
    from src.gateway.enhanced_gateway import EnhancedGateway
    from src.bot.bot import create_bot

    use_enhanced = os.getenv("USE_ENHANCED_GATEWAY", "true").lower() != "false"

    memory = MemoryManager()
    soul = SoulManager()

    if use_enhanced:
        gateway = EnhancedGateway(
            default_model=default_model,
            memory_manager=memory,
            soul_manager=soul,
        )
        logger.info("   Enhanced features: events, followups, emotion, session")
    else:
        from src.gateway.gateway import Gateway

        gateway = Gateway(
            default_model=default_model,
            memory_manager=memory,
            soul_manager=soul,
        )

    bot = create_bot(gateway)

    # Start the bot
    logger.info("🚀 Connecting to Discord...")
    bot.run(token, log_handler=None)  # log_handler=None to avoid duplicate logs


if __name__ == "__main__":
    main()
