"""Opsiyonel harici danisman yan paketi (yalniz `personal`, dlp_mode != block).

Cekirdek (`mem.vault`, `mem.search`, ...) bu paketi ASLA import etmez; baglanti yalnizca
`mem.cli` ve `mem.mcp_server` uzerinden, kullanici acikca etkinlestirdiginde kurulur.
"""

from __future__ import annotations

from .gemini_decisions import (
    DECISIONS_ENDPOINT,
    DEFAULT_MODEL,
    AdvisorCache,
    AdvisorError,
    GeminiDecisionAdvisor,
    GeminiDecisionsClient,
    GeminiSemanticReranker,
    advisor_status,
    build_for_profile,
    load_advisor_settings,
    resolve_api_key,
    save_advisor_settings,
)

__all__ = [
    "DECISIONS_ENDPOINT",
    "DEFAULT_MODEL",
    "AdvisorCache",
    "AdvisorError",
    "GeminiDecisionAdvisor",
    "GeminiDecisionsClient",
    "GeminiSemanticReranker",
    "advisor_status",
    "build_for_profile",
    "load_advisor_settings",
    "resolve_api_key",
    "save_advisor_settings",
]
