"""Prometheus text-exposition rendering for the /metrics endpoint (feature SUPPORT_METRICS).

Dependency-free: the relay is lean, so we emit the simple text format (version 0.0.4) by hand from the
store's point-in-time counts rather than pulling in prometheus_client. All series are gauges.
"""
from __future__ import annotations

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def _block(name: str, help_text: str, samples: list[tuple[str, float]]) -> list[str]:
    out = [f"# HELP {name} {help_text}", f"# TYPE {name} gauge"]
    out += [f"{name}{labels} {value}" for labels, value in samples]
    return out


def render(counts: dict, version: str = "0.1.0") -> str:
    """Render the store's counts dict (see Store.metrics) as Prometheus text. Trailing newline included."""
    lines: list[str] = []
    lines += _block("supportal_tickets", "Tickets by status.", [
        ('{status="open"}', counts.get("tickets_open", 0)),
        ('{status="closed"}', counts.get("tickets_closed", 0)),
    ])
    lines += _block("supportal_messages", "Feed messages by direction (excluding deleted).", [
        ('{direction="in"}', counts.get("messages_in", 0)),
        ('{direction="out"}', counts.get("messages_out", 0)),
    ])
    lines += _block("supportal_attachments", "Stored attachments.", [("", counts.get("attachments", 0))])
    lines += _block("supportal_outbox_pending", "Operator replies awaiting delivery retry.",
                    [("", counts.get("outbox_pending", 0))])
    lines += _block("supportal_build_info", "Build info (value is always 1).",
                    [(f'{{version="{version}"}}', 1)])
    return "\n".join(lines) + "\n"
