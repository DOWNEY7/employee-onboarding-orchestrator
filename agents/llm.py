"""
agents/llm.py
─────────────────────────────────────────────────────────────────────────────
Shared LLM factory — returns a LangChain chat model backed by either
Azure OpenAI or OpenAI direct, controlled by the LLM_PROVIDER env var.

Usage:
    from agents.llm import get_llm
    llm = get_llm()
    response = llm.invoke([HumanMessage(content="Hello")])
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache

from langchain_core.language_models.chat_models import BaseChatModel

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_llm() -> BaseChatModel:
    """
    Return a cached LangChain chat model.

    Provider resolution order:
    1. LLM_PROVIDER env var ("azure" | "openai")
    2. If AZURE_OPENAI_ENDPOINT is set → azure
    3. Otherwise → openai

    All config is read from environment variables to avoid hard-coding credentials.
    """
    provider = os.getenv("LLM_PROVIDER", "").lower()

    # Auto-detect if not explicitly set
    if not provider:
        provider = "azure" if os.getenv("AZURE_OPENAI_ENDPOINT") else "openai"

    if provider == "azure":
        return _build_azure_llm()
    elif provider == "openai":
        return _build_openai_llm()
    else:
        raise ValueError(
            f"Unknown LLM_PROVIDER='{provider}'. "
            "Set LLM_PROVIDER to 'azure' or 'openai'."
        )


def _build_azure_llm() -> BaseChatModel:
    """Build an AzureChatOpenAI instance from environment variables."""
    from langchain_openai import AzureChatOpenAI

    endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
    api_key = os.getenv("AZURE_OPENAI_API_KEY")
    deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")
    api_version = os.getenv("AZURE_OPENAI_API_VERSION", "2024-08-01-preview")

    if not endpoint:
        raise OSError(
            "AZURE_OPENAI_ENDPOINT is not set. "
            "Set it to your Azure OpenAI resource URL or set LLM_PROVIDER=openai."
        )

    logger.info("LLM: Using Azure OpenAI — deployment=%s api_version=%s", deployment, api_version)

    return AzureChatOpenAI(
        azure_endpoint=endpoint,
        api_key=api_key,
        azure_deployment=deployment,
        api_version=api_version,
        temperature=0.2,
        max_tokens=4096,
        timeout=60,
        max_retries=3,
    )


def _build_openai_llm() -> BaseChatModel:
    """Build a ChatOpenAI instance from environment variables."""
    from langchain_openai import ChatOpenAI

    api_key = os.getenv("OPENAI_API_KEY")
    model = os.getenv("OPENAI_MODEL", "gpt-4o")

    if not api_key:
        raise OSError(
            "OPENAI_API_KEY is not set. "
            "Set it or configure Azure OpenAI with LLM_PROVIDER=azure."
        )

    logger.info("LLM: Using OpenAI direct — model=%s", model)

    return ChatOpenAI(
        api_key=api_key,
        model=model,
        temperature=0.2,
        max_tokens=4096,
        timeout=60,
        max_retries=3,
    )


def reset_llm_cache() -> None:
    """Clear the LLM singleton cache. Useful in tests."""
    get_llm.cache_clear()
