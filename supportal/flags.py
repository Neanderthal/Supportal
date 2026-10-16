"""Feature-flag / setting NAMES as constants — so a typo is an AttributeError, not a silent fall-back.

These are the keys passed to `supportal.settings.get*` (which resolves settings.yaml → env → the default
given at the call site — the call site stays the single source of truth for default VALUES). Only behaviour
flags live here; infrastructure/secret env names (tokens, DB URL, log level, ports) stay in `config.py`.
"""
from __future__ import annotations


class Flags:
    # Toggles (read with settings.get_bool), all default off unless noted.
    RATE_LIMIT = "SUPPORT_RATE_LIMIT"
    ATTACHMENTS = "SUPPORT_ATTACHMENTS"
    VISITOR_CONTEXT = "SUPPORT_VISITOR_CONTEXT"
    CSAT = "SUPPORT_CSAT"
    CANNED = "SUPPORT_CANNED"
    BUSINESS_HOURS = "SUPPORT_BUSINESS_HOURS"
    RECEIPTS = "SUPPORT_RECEIPTS"
    OPENAPI = "SUPPORT_OPENAPI"       # read at startup (app.py), not hot
    GDPR = "SUPPORT_GDPR"
    METRICS = "SUPPORT_METRICS"
    AI = "SUPPORT_AI"                 # LLM auto-answer from a small md knowledge base before human handoff

    # Values (read with settings.get_int / get / get_dict).
    AI_MODEL = "SUPPORT_AI_MODEL"           # OpenAI-compatible chat model (e.g. deepseek-chat)
    AI_KB_DIR = "SUPPORT_AI_KB_DIR"         # directory of short .md knowledge-base docs
    AI_KB_MAX_CHARS = "SUPPORT_AI_KB_MAX_CHARS"   # cap on the KB text stuffed into the prompt
    RATE_LIMIT_MAX = "SUPPORT_RATE_LIMIT_MAX"
    RATE_LIMIT_WINDOW = "SUPPORT_RATE_LIMIT_WINDOW"
    ATTACHMENTS_MAX_MB = "SUPPORT_ATTACHMENTS_MAX_MB"
    CANNED_REPLIES = "SUPPORT_CANNED_REPLIES"
    HOURS = "SUPPORT_HOURS"
    HOURS_TZ = "SUPPORT_HOURS_TZ"
    AWAY_MESSAGE = "SUPPORT_AWAY_MESSAGE"
    CLOSED_NOTICE = "SUPPORT_CLOSED_NOTICE"
