"""
core/llm.py
─────────────────────────────────────────────────────────────────────────────
LLM factory — returns either AzureChatOpenAI or ChatOpenAI based on the
LLM_PROVIDER environment variable.

Usage:
    from core.llm import get_llm
    llm = get_llm()  # temperature 0.2 by default
    result = llm.invoke("Hello")
"""

from __future__ import annotations

import logging
from functools import lru_cache

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import AzureChatOpenAI, ChatOpenAI

from .config import get_settings

logger = logging.getLogger(__name__)


@lru_cache(maxsize=8)
def get_llm(temperature: float = 0.2) -> BaseChatModel:
    """
    Return a configured chat LLM instance.

    - If LLM_PROVIDER=azure (default), returns AzureChatOpenAI.
    - If LLM_PROVIDER=openai, returns ChatOpenAI.

    Raises RuntimeError with a helpful message if the required credentials
    are missing, so failures surface clearly instead of deep in graph nodes.
    """
    settings = get_settings()
    provider = settings.llm_provider.lower()

    if provider == "azure":
        if not settings.azure_openai_endpoint or not settings.azure_openai_api_key:
            raise RuntimeError(
                "LLM_PROVIDER=azure but AZURE_OPENAI_ENDPOINT / AZURE_OPENAI_API_KEY "
                "are not set. Add them to .env or set LLM_PROVIDER=openai."
            )
        logger.debug(
            "Using Azure OpenAI: endpoint=%s deployment=%s",
            settings.azure_openai_endpoint,
            settings.azure_openai_deployment,
        )
        return AzureChatOpenAI(
            azure_endpoint=settings.azure_openai_endpoint,
            api_key=settings.azure_openai_api_key,  # type: ignore[arg-type]
            azure_deployment=settings.azure_openai_deployment,
            api_version=settings.azure_openai_api_version,
            temperature=temperature,
        )

    if provider == "openai":
        if not settings.openai_api_key:
            raise RuntimeError("LLM_PROVIDER=openai but OPENAI_API_KEY is not set. Add it to .env.")
        logger.debug("Using OpenAI: model=%s", settings.openai_model)
        return ChatOpenAI(
            model=settings.openai_model,
            api_key=settings.openai_api_key,  # type: ignore[arg-type]
            temperature=temperature,
        )

    raise RuntimeError(f"Unknown LLM_PROVIDER='{provider}'. Must be 'azure' or 'openai'.")


def reset_llm_cache() -> None:
    """Clear the LLM singleton cache. Useful in tests."""
    get_llm.cache_clear()
