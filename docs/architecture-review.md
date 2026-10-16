> **`supportal` architecture review.** An independent assessment of the service's structure, risks,
> and recommendations — the critique companion to [architecture.md](architecture.md) (the design).
> Reviewed state: `main` @ `18f199b` (attachments, widget i18n, visitor-context). **Audience:
> contributors/maintainers.** Date: 2026-10-16. **Status: all findings resolved — see
> [§Resolution](#resolution--2026-10-17) (the original review below is kept as the historical record).**

# Supportal — architecture review

**Severity legend:** 🔴 high · 🟡 medium · ⚪ low.

## Resolution — 2026-10-17

Every finding below was fixed, one PR each, each merged independently. Line numbers in the original
findings are from `18f199b` and have since shifted.

| # | Finding | Sev | PR | What shipped |
|---|---|---|---|---|
| 3 | Single-process invariant vs. scale-out docs | 🟡 | [#17](https://github.com/Neanderthal/supportal/pull/17) | Stated loudly at startup (`app.py` docstring + lifespan log) and in README / `architecture.md` §Scaling / `integration.md`; corrected the "Postgres = scalable" claim. |
| 5 | `Content-Disposition` filename not sanitized | 🟡 | [#18](https://github.com/Neanderthal/supportal/pull/18) | `_content_disposition()` strips quotes/CR-LF for the ASCII fallback + RFC 5987 `filename*=UTF-8''…`. |
| 4 | Upload buffers whole body before the size check | 🟡 | [#20](https://github.com/Neanderthal/supportal/pull/20) | `Content-Length` pre-check + streaming hard cap (missing/understated length can't OOM). |
| 6 | Operator-media failures vanish silently | 🟡 | [#21](https://github.com/Neanderthal/supportal/pull/21) | `_handle_media` wraps the download; logs + posts `MEDIA_FETCH_FAILED` into the topic. |
| 1 + 2 | `seq` written single-threaded; sync DB/BLOB on the loop | 🔴 / 🟡 | [#22](https://github.com/Neanderthal/supportal/pull/22) | Atomic `seq` (`seq_counter` + `UPDATE … RETURNING`), WAL + `busy_timeout`, comment fixed. Off-loop/pooled connections deferred per the review ("when attachments/throughput justify it") — the foundation is now safe. |
| — | Testing: `app.py` wiring + `telegram_ops` media path | — | [#24](https://github.com/Neanderthal/supportal/pull/24) | New `test_app_unit.py` + media/bot tests: `app.py` 28%→95%, `telegram_ops` ~71%→100%, total →96%. |
| 7 | Operator-callback signature sprawl | 🟡 | [#26](https://github.com/Neanderthal/supportal/pull/26) | `OperatorHandler` Protocol + structured event dataclasses; `start_polling(handler)` replaces 5 positional callbacks + the 7-tuple alias. |
| 8 | Smaller items | ⚪ | [#27](https://github.com/Neanderthal/supportal/pull/27) | `Flags` constants module; `meta` docstring/`api.md` aligned; ingress-ordering + outbox-default source-of-truth comments. |

**One carry-over (deferred, not a bug):** finding #2's *off-loop* move (threadpool / pooled or per-thread
connections for heavy attachment BLOBs) is intentionally left for when attachment volume justifies it — the
review itself scoped it that way. WAL + `busy_timeout` + atomic `seq` (PR #22) make that move safe when it's
warranted.

---

## Verdict

A genuinely well-designed small service. "Boundary A" (the relay knows only an opaque `user_ref` and an
HTTP contract) is real and upheld; fail-soft is applied consistently; token/HMAC/XSS handling is careful.
The one architecturally weak area is the **storage/concurrency model** — fine at the current single-embed
scale, but weak in a *latent* way that quietly blocks the horizontal scale-out path the docs advertise.

## Strengths (preserve these)

- **Boundary discipline.** `relay.py` imports no product/web concepts; `OperatorEvent` carries only
  `user_ref/ticket_id/text/operator`. The product side is reduced to "mint a token + accept a callback".
- **Fail-soft, no silent excepts.** `_crash_logger` (`app.py:32`) + a wrapped `_outbox_loop` (`app.py:47`);
  every `except` logs (`relay.py:154/213/216`). Matches the project's logging convention.
- **Widget XSS is correct by construction.** The only `innerHTML` (`widget.js:286`) is a static template;
  all dynamic content — operator text, author, localized strings, attachment links — goes through
  `textContent`/`setAttribute`; images render from blob `createObjectURL`. Product-supplied `strings`
  only overwrite known keys and only when `typeof === "string"` (`widget.js:343`).
- **Visitor-context is well-contained.** `_context_block` (`relay.py:166`) caps 12 fields, truncates
  keys/values to 40/300 chars, strips newlines, uses no Telegram `parse_mode`, and is off by default.

## Findings (prioritized)

### 🔴 1. Storage/concurrency is written single-threaded but sold as scalable

The headline issue — stated precisely, because it is usually mis-diagnosed:

- **There is no active `seq` race today.** Everything — FastAPI `async` routes, the aiogram polling task,
  the outbox loop — runs on the **one asyncio event loop**, and every `Store` method is fully synchronous
  with no `await` inside. So `_next_seq()`'s `SELECT MAX(seq)` and the following `INSERT`
  (`store.py:243`→`249`) are not interleaved with other coroutines. The comment "single writer … no races"
  (`store.py:242`) reaches the right conclusion for the wrong reason: it's cooperative single-threaded
  scheduling, not a single writer (several code paths write).
- **But it is a loaded gun.** One shared `sqlite3` connection opened with `check_same_thread=False`
  (`store.py:77`) + the non-atomic read-then-insert means the instant any DB work moves off the loop
  (a threadpool, or `asyncio.to_thread` — the standard remedy for finding #2), the race becomes real and
  can duplicate `seq`, which makes the widget's poll cursor silently step over a message.
- **Therefore #1 and #2 must be solved together:** make `seq` atomic (`INSERT … RETURNING`, or a counter
  bumped in the same statement) *before* introducing real concurrency, and move to WAL + `busy_timeout`
  + pooled/per-thread connections. Fix the misleading `store.py:242` comment regardless.

### 🟡 2. Synchronous DB/BLOB calls block the event loop

All `Store` calls run on the loop thread (routes are `async def`, so FastAPI never offloads them to a
threadpool). For text this is sub-millisecond and irrelevant at support-chat concurrency. The realistic
cost is **attachments**: a 10 MB BLOB `INSERT` (`store.py:260`) and the `bytes(row["data"])` read-back
(`store.py:273`) stall polling and every other request while they run. Low urgency at current scale;
real once attachments see volume.

### 🟡 3. The single-process invariant is implicit and contradicts the scale-out docs

The long-poller, the outbox loop, and the in-memory rate limiter are all per-process (`app.py:54-59`,
`ratelimit.py`). Running `uvicorn --workers>1` or replicas →two `getUpdates` consumers (Telegram returns
409 Conflict; one silently wins), double outbox delivery, and per-worker rate limits. Yet
`architecture.md` / `integration.md` invite a "swap in Postgres for multi-user" path — which Postgres
alone does **not** enable; the poller/outbox need a singleton/leader first. Make this a loud, explicit
constraint at the `app` level and correct the docs.

### 🟡 4. Upload buffers the whole body before the size check (memory DoS)

`widget_api.py:94` does `data = await request.body()` — the entire upload is read into memory **before**
the `SUPPORT_ATTACHMENTS_MAX_MB` check at line 98. An authenticated user (feature on) can post an
arbitrarily large body and OOM the process. Reject early on `Content-Length`, and/or stream with a hard
cap.

### 🟡 5. Attachment `Content-Disposition` filename is not sanitized

`widget_api.py:116`: `f'attachment; filename="{att.filename}"'` — the filename comes from the browser
(`?name=`) or Telegram (`document.file_name`). A `"` or CR/LF breaks or injects the header. Strip
quotes/newlines, or use RFC 5987 `filename*=`. (Inline-image SVG XSS is **not** exploitable here: the
endpoint is `Authorization`-header-gated, so it can't be top-level-navigated or loaded via `<img src>`;
it only ever renders as a blob in `<img>`, which does not execute SVG script.)

### 🟡 6. Operator-media failures vanish silently

`telegram_ops._handle_media` → `await self._download(file_id)` (`telegram_ops.py:147`) has no try/except.
aiogram keeps the poller alive per-update, but a failed download (e.g. a file above the Bot API's 20 MB
`getFile` limit) drops the operator's file with no feed note and no operator-visible signal — unlike the
user path, which surfaces `uploadFail`. Wrap it: log + optionally post an operator-facing note into the
topic.

### 🟡 7. Operator-callback signature sprawl (the one worthwhile refactor)

Five positional callbacks thread through `app.py:54` → `start_polling` → `_register_handlers`, with
hand-written type aliases including the 7-tuple `OnMedia` (`telegram_ops.py:17-22`). Every new operator
capability edits three files and relies on positional/registration ordering (tests even index into the
registered handler list). Collapse into a single handler object (`Protocol`/dataclass) passing
**structured events** (`OperatorReply`, `OperatorMediaReply`). The Rule of Three is already met
(message/media/edit/delete/command). This is the one change that materially improves changeability.

### ⚪ 8. Smaller items

- `_header` (`relay.py:163`) parses `meta["product"|"page"]` though `models.py` says "meta is displayed,
  not parsed". Harmless — align the docstring or the code.
- Default duplication: the outbox defaults in `config.py` are mirrored by `Relay.__init__` kwargs
  (`relay.py:33`). Those kwargs are **not** dead (tests construct `Relay(store, tg, "", "")` and rely on
  them) — just don't treat the numbers as a second source of truth.
- Feature-flag names are read as bare strings in 2–3 places each; a small `Flags` constants module would
  make them typo-proof (a typo silently yields the default today).
- `ingress` writes the feed row *after* `send_to_topic` (`relay.py:56-60`); a crash in between shows the
  operator a message the user's feed never receives. `on_operator_message` uses the safer order.
  Edge-case, low.

### Testing — the two weakest-covered modules are the two riskiest

The 85% coverage gate passes, but it passes *over* the danger zones:

- **`app.py` wiring** (lifespan, `_crash_logger`, `_outbox_loop`, task cancel/shutdown) has no direct
  tests — the concurrency-critical assembly is untested.
- **`telegram_ops` media path** (`_handle_media` file-id/mime extraction, `send_media`
  photo-vs-document/SVG branching, `_download`) is untested — relay-level media is tested only against a
  *fake* `TelegramOps`, so the real aiogram-touching logic is the blind spot.

## Recommended order of work

1. **Document the single-process invariant** (`app.py` + `architecture.md`/`integration.md`) and correct
   the "Postgres = scalable" claim — zero-risk, prevents an ops foot-gun. *(🟡 #3)*
2. **Two cheap security fixes:** sanitize the `Content-Disposition` filename; move the upload size check
   to `Content-Length` before buffering. *(🟡 #4, #5)*
3. **Harden operator-media** with try/except + an operator-visible signal. *(🟡 #6)*
4. **Fix the `_next_seq` comment now; make `seq` atomic before any off-loop DB move** — and pair that with
   WAL + pooled connections when attachments/throughput justify it. *(🔴 #1, 🟡 #2)*
5. **Add tests for `app.py` wiring and the `telegram_ops` media path.** *(testing)*
6. **Refactor the operator callbacks into a handler object + structured events** when that area is next
   touched. *(🟡 #7)*

## Method

Reviewed the full `supportal/` package (`api`, `app`, `config`, `models`, `relay`, `store`,
`telegram_ops`, `tokens`, `widget_api`, `settings`, `ratelimit`, `widget/widget.js`, `widget/widget.css`)
plus `docs/` and `tests/`, cross-checked by an independent reviewer pass. Findings and severities were
verified against the source; the concurrency finding in particular was corrected from an initial
"active race" claim to the latent-risk framing above after confirming the single-event-loop execution
model.

---

*Living document: update as the findings are addressed or the architecture changes.*
