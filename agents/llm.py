"""
agents/llm.py
─────────────────────────────────────────────────────────────────────────────
Backward-compatible re-export of the LLM factory from core.llm.

The single source-of-truth implementation now lives in core/llm.py (cached,
validated, provider-agnostic). This module is retained so that existing test
patches targeting ``agents.llm.get_llm`` continue to work without change:

    from unittest.mock import patch
    with patch("agents.llm.get_llm") as mock_llm:
        ...

All new code should import directly from core.llm.
"""

from core.llm import get_llm, reset_llm_cache  # noqa: F401

__all__ = ["get_llm", "reset_llm_cache"]
