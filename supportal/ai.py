"""LLM auto-answer from a small markdown knowledge base (feature SUPPORT_AI).

Deliberately NOT a RAG pipeline — no chunking, embeddings or a vector store. We just read a handful of short
`.md` docs, stuff them into the system prompt of an OpenAI-compatible chat model (e.g. DeepSeek), and let the
model answer from them. If the KB doesn't cover the question the model replies `HANDOFF` and a human takes over.

Config: behaviour via settings (`SUPPORT_AI` / `SUPPORT_AI_MODEL` / `SUPPORT_AI_KB_DIR` / `SUPPORT_AI_KB_MAX_CHARS`);
endpoint + secret via the environment (`SUPPORT_AI_API_BASE`, `SUPPORT_AI_API_KEY`, `SUPPORT_AI_TIMEOUT`) — secrets
never live in settings.yaml. Fail-soft everywhere: no KB / no endpoint / any error → None (hand off to a human).
"""
from __future__ import annotations

import glob
import logging
import os

from .flags import Flags
from .settings import get, get_int

_LOG = logging.getLogger("supportal.ai")

_HANDOFF = "HANDOFF"

SYSTEM_PROMPT = (
    "You are a helpful support assistant. Answer the user's question using ONLY the knowledge base below. "
    "Be concise and friendly, and answer in the user's language. If the knowledge base does not clearly "
    f"contain the answer, reply with exactly the single word {_HANDOFF} (nothing else) so a human can take over.\n\n"
    "=== KNOWLEDGE BASE ===\n{kb}\n=== END KNOWLEDGE BASE ==="
)


def load_kb(kb_dir: str, max_chars: int) -> str:
    """Concatenate the `.md` files in `kb_dir` (sorted, each under a `## <filename>` header), capped at
    `max_chars`. Missing dir / no docs / unreadable files → "" (→ the caller hands off)."""
    if not kb_dir or not os.path.isdir(kb_dir):
        return ""
    parts: list[str] = []
    total = 0
    for path in sorted(glob.glob(os.path.join(kb_dir, "*.md"))):
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read().strip()
        except OSError:
            continue
        if not text:
            continue
        block = f"## {os.path.basename(path)}\n{text}"
        parts.append(block)
        total += len(block)
        if total >= max_chars:
            break
    return "\n\n".join(parts)[:max_chars]


async def auto_answer(question: str) -> str | None:
    """An answer drawn from the KB, or None to hand off to a human (HANDOFF / empty / no KB / not configured / error)."""
    kb = load_kb(get(Flags.AI_KB_DIR, "kb"), get_int(Flags.AI_KB_MAX_CHARS, 8000))
    if not kb or not (question or "").strip():
        return None
    base = os.environ.get("SUPPORT_AI_API_BASE", "").rstrip("/")
    key = os.environ.get("SUPPORT_AI_API_KEY", "")
    if not base or not key:
        _LOG.warning("[ai] SUPPORT_AI is on but SUPPORT_AI_API_BASE/KEY are not set — handing off")
        return None
    body = {
        "model": get(Flags.AI_MODEL, "deepseek-chat"),
        "messages": [{"role": "system", "content": SYSTEM_PROMPT.format(kb=kb)},
                     {"role": "user", "content": question}],
        "temperature": 0.2, "max_tokens": 500,
    }
    import httpx
    try:
        async with httpx.AsyncClient(timeout=get_int("SUPPORT_AI_TIMEOUT", 30)) as client:
            resp = await client.post(base + "/chat/completions", json=body,
                                     headers={"Authorization": f"Bearer {key}"})
        resp.raise_for_status()
        text = ((resp.json().get("choices") or [{}])[0].get("message", {}).get("content") or "").strip()
    except Exception:
        _LOG.warning("[ai] auto-answer call failed — handing off", exc_info=True)
        return None
    if not text or text.strip().upper().startswith(_HANDOFF):
        _LOG.info("[ai] model handed off (q=%dchars)", len(question))
        return None
    _LOG.info("[ai] auto-answered (q=%dchars a=%dchars)", len(question), len(text))
    return text
