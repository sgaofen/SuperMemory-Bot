"""
Mem0 configuration — uses ChromaDB + local multilingual embeddings.
All local, no API keys needed for storage or embedding.
LLM calls route through Antigravity proxy via environment variables.
"""
import os
from pathlib import Path

import litellm

# Data directory for persistent storage
DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

CHROMA_DB_PATH = str(DATA_DIR / "chromadb")
MEMORY_HISTORY_PATH = str(DATA_DIR / "memory_history.json")

# The Mem0 internal LLM model — used for memory processing (function calling)
MEM0_LLM_MODEL = os.getenv("MEM0_LLM_MODEL", "anthropic/gemini-3-flash")


def setup_proxy_env():
    """
    Set environment variables so LiteLLM (inside Mem0) automatically
    routes Anthropic/Claude calls through the Antigravity proxy.
    """
    proxy_enabled = os.getenv("ANTIGRAVITY_PROXY_ENABLED", "").lower() == "true"
    proxy_url = os.getenv("ANTIGRAVITY_PROXY_URL", "http://localhost:8080")

    if proxy_enabled:
        if not os.getenv("ANTHROPIC_API_KEY"):
            os.environ["ANTHROPIC_API_KEY"] = "test"
        if not os.getenv("ANTHROPIC_BASE_URL"):
            os.environ["ANTHROPIC_BASE_URL"] = proxy_url


def _register_proxy_models():
    """
    Register Antigravity proxy models in LiteLLM's model_cost dict
    so it knows they support function calling.

    The proxy is an Anthropic-compatible API that forwards to real
    Claude/Gemini backends which DO support tools/function calling.
    """
    # Get a reference model entry to copy from
    try:
        ref = litellm.get_model_info("anthropic/claude-sonnet-4-5")
    except Exception:
        ref = {}

    proxy_models = [
        "gemini-3-flash",
        "gemini-3-pro-high",
        "gemini-3-pro-low",
        "gemini-2.5-flash",
        "gemini-2.5-pro",
        "anthropic/gemini-3-flash",
        "anthropic/gemini-3-pro-high",
        "anthropic/gemini-3-pro-low",
        "anthropic/gemini-2.5-flash",
        "anthropic/gemini-2.5-pro",
    ]

    for model_name in proxy_models:
        if model_name not in litellm.model_cost:
            litellm.model_cost[model_name] = {
                **ref,
                "supports_function_calling": True,
                "supports_tool_choice": True,
                "litellm_provider": "anthropic",
            }


def get_mem0_config() -> dict:
    """
    Return Mem0 configuration dict.
    - Vector store: ChromaDB (local, no Docker)
    - Embeddings: multilingual sentence-transformers (local, free, 支持中文)
    - LLM: Gemini 3 Flash via proxy (fast, high rate limits)
    """
    setup_proxy_env()
    _register_proxy_models()

    config = {
        "vector_store": {
            "provider": "chroma",
            "config": {
                "collection_name": "ai_brain_memories",
                "path": CHROMA_DB_PATH,
            },
        },
        # 多语言嵌入模型 — 支持中文！本地运行，免费
        "embedder": {
            "provider": "huggingface",
            "config": {
                "model": "paraphrase-multilingual-MiniLM-L12-v2",
            },
        },
        # Mem0 内部 LLM — Gemini 3 Flash (fast, generous limits)
        "llm": {
            "provider": "litellm",
            "config": {
                "model": MEM0_LLM_MODEL,
                "temperature": 0.1,
            },
        },
    }

    return config
