"""supportal HTTP-contract DTOs. This is the embedding boundary — the product only knows these shapes."""
from __future__ import annotations

from pydantic import BaseModel, Field


class IngressMessage(BaseModel):
    """Product → relay: a user's message to support."""

    user_ref: str = Field(..., description="Stable opaque user id (e.g. a normalized login). The ticket key.")
    text: str = Field(..., description="User text.")
    display_name: str = Field("", description="Human-readable name — goes into the topic title/header (not a secret).")
    meta: dict = Field(default_factory=dict, description="Extra context for the operator. `product` and `page` are surfaced in the topic header; any other keys are carried but not interpreted.")
    context: dict = Field(default_factory=dict, description="Visitor context (url/title/custom) posted once into the topic on creation; display-only.")


class IngressResult(BaseModel):
    """Relay → product: acknowledgement of an ingress message."""

    ok: bool = True
    ticket_id: str
    thread_id: int
    message_id: int = 0   # id of the recorded feed row (the widget matches its own optimistic bubble)
    attachment_id: int = 0  # set when the message carries a file (feature SUPPORT_ATTACHMENTS)


class WidgetMessage(BaseModel):
    """A feed row for the browser widget (GET /v1/widget/messages). deleted=true → the widget removes the bubble."""

    id: int
    direction: str          # in | out
    author: str = ""
    text: str = ""
    ts: float = 0.0
    deleted: bool = False
    kind: str = "msg"       # csat_request → the widget shows a rating prompt below this row (feature SUPPORT_CSAT)
    read: bool = False      # an operator (out) message the user has seen (feature SUPPORT_RECEIPTS)
    attachment_id: int = 0  # >0 → fetch bytes from /v1/widget/attachment/{id}
    attachment_name: str = ""
    attachment_mime: str = ""


class WidgetPoll(BaseModel):
    """Widget poll response: changes (new/edited/deleted) + the seq cursor for the next request."""

    ok: bool = True
    messages: list[WidgetMessage] = Field(default_factory=list)
    cursor: int = 0         # max seq of the response → the client sends it in ?since= on the next poll


class OperatorEvent(BaseModel):
    """Relay → product (callback): an operator reply or a system ticket event."""

    event: str = Field("message", description="message | ticket_closed")
    user_ref: str
    ticket_id: str
    text: str = ""
    operator: str = Field("", description="Who replied (for display/audit).")
