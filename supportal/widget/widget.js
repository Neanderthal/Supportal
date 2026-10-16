// supportal — embeddable support widget (vanilla JS, no dependencies).
//
// Self-contained: draws a floating "💬" button (bottom-left) + a slide-out panel on the right, keeps its own
// feed, talks DIRECTLY to the relay with a token. It embeds into a product with a single <script> tag (+ widget.css);
// all logic is here. The source of truth of the conversation is the relay (DB); the widget only polls.
//
// Contract with the host (product):
//   GET  {CONFIG_URL}            → {api_base, token, ttl}  (the product knows the logged-in user, mints the token)
//                                   no token / not ok → the widget silently does not start (self-gating)
//   POST {api_base}/v1/widget/send      Bearer token  {text}          → {ok, message_id}
//   GET  {api_base}/v1/widget/messages?since=<seq>  Bearer token      → {messages:[{id,direction,author,text,ts,deleted}], cursor}
//
// Operator replies are delivered via polling (visible tab ~4s, hidden less often). `direction`: in = user,
// out = operator/system. Reconcile by id: new → add; seen → update text; deleted → remove the bubble.
// Operator text comes from Telegram → we insert it ONLY as textContent (no innerHTML).
(function () {
  "use strict";
  if (window.__supportalInit) return;      // one instance per page
  window.__supportalInit = true;

  var CONFIG_URL = "/support/config";          // same-origin product endpoint (returns the token)
  var POLL_VISIBLE = 4000, POLL_HIDDEN = 20000;

  var cfg = null;          // {api_base, token, ttl}
  var lastSeq = 0;         // polling cursor by seq (insert/edit/delete)
  var unread = 0;          // unread operator replies while the panel is closed
  var firstSync = true;    // the first poll = restore (don't badge history)
  var isOpen = false;
  var seen = {};           // id → true (dedup between the optimistic echo and polling)
  var bubbles = {};        // id → DOM bubble (for edit-sync: update text; /del: remove)
  var els = {};            // DOM references

  // ── network ────────────────────────────────────────────────────────────────
  function apiUrl(path) { return ((cfg && cfg.api_base) || "") + path; }
  function authHeaders(extra) {
    var h = extra || {};
    h["Authorization"] = "Bearer " + ((cfg && cfg.token) || "");
    return h;
  }
  function fetchConfig() {
    // POST (not GET): in SPA products a catch-all on GET often intercepts any GET and returns index.html.
    return fetch(CONFIG_URL, { method: "POST", credentials: "same-origin", cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (j) { return (j && j.token) ? j : null; })
      .catch(function () { return null; });
  }
  function refreshToken() {
    return fetchConfig().then(function (j) { if (j) { cfg.token = j.token; if (j.api_base) cfg.api_base = j.api_base; } return !!j; });
  }

  function send(text, retried) {
    return fetch(apiUrl("/v1/widget/send"), {
      method: "POST", headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ text: text })
    }).then(function (r) {
      if (r.status === 401 && !retried) return refreshToken().then(function (ok) { return ok ? send(text, true) : null; });
      return r.ok ? r.json() : null;
    }).catch(function () { return null; });
  }

  function pollOnce(retried) {
    if (!cfg) return Promise.resolve();
    return fetch(apiUrl("/v1/widget/messages?since=" + lastSeq), { headers: authHeaders() })
      .then(function (r) {
        if (r.status === 401 && !retried) return refreshToken().then(function (ok) { return ok ? pollOnce(true) : null; });
        return r.ok ? r.json() : null;
      })
      .then(function (j) {
        if (!j) return;
        if (j.messages) ingest(j.messages);
        if (typeof j.cursor === "number" && j.cursor > lastSeq) lastSeq = j.cursor;
      })
      .catch(function () { /* network blipped — the next tick retries */ });
  }

  function pollLoop() {
    pollOnce().then(function () {
      var delay = (typeof document.hidden === "boolean" && document.hidden) ? POLL_HIDDEN : POLL_VISIBLE;
      setTimeout(pollLoop, delay);
    });
  }

  // ── feed ─────────────────────────────────────────────────────────────────────
  // Reconcile by id: new → add; already seen → update text (edit-sync); deleted → remove the bubble.
  function ingest(messages) {
    var changed = false;
    for (var i = 0; i < messages.length; i++) {
      var m = messages[i];
      if (m.deleted) {                       // recalled by the operator (/del) → remove, mark as seen
        if (removeBubble(m.id)) changed = true;
        seen[m.id] = true;
        continue;
      }
      if (seen[m.id]) {                      // already on screen → text edit (edited_message)
        if (updateBubble(m.id, m.text || "")) changed = true;
        continue;
      }
      seen[m.id] = true;
      renderBubble(m);
      changed = true;
      if (m.direction === "out" && !firstSync && !isOpen) unread++;
    }
    firstSync = false;
    if (changed) { updateBadge(); syncGreeting(); if (isOpen) scrollToBottom(); }
  }

  function renderBubble(m) {
    var row = document.createElement("div");
    row.className = "supportal-msg " + (m.direction === "in" ? "supportal-in" : "supportal-out");
    row.dataset.mid = m.id;
    if (m.direction === "out" && m.author) {
      var who = document.createElement("div");
      who.className = "supportal-msg-author";
      who.textContent = m.author;            // operator name — textContent (no HTML)
      row.appendChild(who);
    }
    var bub = document.createElement("div");
    bub.className = "supportal-bubble";
    bub.textContent = m.text || "";          // IMPORTANT: text from Telegram → textContent only
    row.appendChild(bub);
    els.list.appendChild(row);
    bubbles[m.id] = bub;                     // for later edit/del by id
  }

  function updateBubble(id, text) {
    var bub = bubbles[id];
    if (!bub) return false;
    bub.textContent = text;                  // an edit — also textContent only
    return true;
  }

  function removeBubble(id) {
    var bub = bubbles[id];
    if (!bub) return false;
    var row = bub.parentNode;                // .supportal-msg (the outer wrapper)
    if (row && row.parentNode) row.parentNode.removeChild(row);
    delete bubbles[id];
    return true;
  }

  function renderNote(text) {
    var n = document.createElement("div");
    n.className = "supportal-note";
    n.textContent = text;
    els.list.appendChild(n);
    if (isOpen) scrollToBottom();
  }

  // Show the greeting when the feed is empty (incl. after /clear), hide it when there are messages.
  function syncGreeting() {
    if (els.greeting) els.greeting.style.display = els.list.querySelector(".supportal-msg") ? "none" : "";
  }
  function scrollToBottom() { els.list.scrollTop = els.list.scrollHeight; }
  function updateBadge() {
    if (unread > 0) { els.badge.textContent = unread > 99 ? "99+" : String(unread); els.badge.style.display = ""; }
    else els.badge.style.display = "none";
  }

  // ── open/close ───────────────────────────────────────────────────────────────
  function setOpen(v) {
    isOpen = v;
    els.root.classList.toggle("open", v);
    els.fab.setAttribute("aria-expanded", v ? "true" : "false");
    if (v) { unread = 0; updateBadge(); scrollToBottom(); setTimeout(function () { els.input.focus(); }, 60); }
  }

  function submit() {
    var text = (els.input.value || "").trim();
    if (!text) return;
    els.input.value = "";
    autoGrow();
    send(text).then(function (res) {
      if (res && res.message_id) {
        if (!seen[res.message_id]) {   // it may have already arrived via polling — then don't duplicate
          seen[res.message_id] = true;
          renderBubble({ id: res.message_id, direction: "in", author: "", text: text });
          syncGreeting(); scrollToBottom();
        }
        // the cursor is by seq (polling advances it); the echo is deduped by seen[id], no need to touch the cursor
      } else {
        renderNote("⚠️ Couldn't send the message. Please try again.");
      }
    });
  }

  function autoGrow() {
    els.input.style.height = "auto";
    els.input.style.height = Math.min(els.input.scrollHeight, 120) + "px";
  }

  // ── DOM ──────────────────────────────────────────────────────────────────────
  // (UI copy below is the widget's default English locale; a product can restyle via CSS tokens.)
  var CHAT_ICON = '<svg viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor"'
    + ' stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2'
    + ' 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>';

  function build() {
    var root = document.createElement("div");
    root.id = "supportal-root";
    root.innerHTML =
      '<button id="supportal-fab" type="button" aria-label="Support" aria-expanded="false">'
      + CHAT_ICON + '<span class="supportal-fab-badge" style="display:none"></span></button>'
      + '<section class="supportal-drawer" role="dialog" aria-label="Support chat">'
      + '  <header class="supportal-drawer-head"><span class="supportal-drawer-title">Support</span>'
      + '    <button class="supportal-drawer-close" type="button" aria-label="Close">&times;</button></header>'
      + '  <div class="supportal-msg-list"><div class="supportal-greeting">Describe your question — a support operator will reply.'
      + ' The reply will arrive here; you don\'t have to wait on the page.</div></div>'
      + '  <form class="supportal-composer"><textarea rows="1" placeholder="Message…" class="supportal-input"></textarea>'
      + '    <button type="submit" class="supportal-send" aria-label="Send">'
      + '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2"'
      + ' stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/>'
      + '<polygon points="22 2 15 22 11 13 2 9 22 2"/></svg></button></form>'
      + '</section>';
    document.body.appendChild(root);

    els.root = root;
    els.fab = root.querySelector("#supportal-fab");
    els.badge = root.querySelector(".supportal-fab-badge");
    els.list = root.querySelector(".supportal-msg-list");
    els.greeting = root.querySelector(".supportal-greeting");
    els.input = root.querySelector(".supportal-input");
    els.form = root.querySelector(".supportal-composer");

    els.fab.addEventListener("click", function () { setOpen(!isOpen); });
    root.querySelector(".supportal-drawer-close").addEventListener("click", function () { setOpen(false); });
    els.form.addEventListener("submit", function (e) { e.preventDefault(); submit(); });
    els.input.addEventListener("input", autoGrow);
    els.input.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); }
    });
  }

  // ── start: fetch config (with token), then bring up the UI and polling ───────
  function start() {
    fetchConfig().then(function (j) {
      if (!j) { setTimeout(start, 30000); return; }  // not logged in / disabled → quietly retry later
      cfg = j;
      build();
      pollLoop();
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
