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

  // UI copy — English defaults. The product may localize by returning a `strings` object in
  // /support/config (it knows the user's language); provided keys override these (i18n).
  // Applied via textContent/setAttribute (never innerHTML) so product-supplied copy can't inject HTML.
  var STRINGS = {
    title: "Support",
    support: "Support",                     // launcher (fab) aria-label
    close: "Close",
    greeting: "Describe your question — a support operator will reply. The reply will arrive here;"
      + " you don't have to wait on the page.",
    placeholder: "Message…",
    send: "Send",
    attach: "Attach a file",
    sendFail: "⚠️ Couldn't send the message. Please try again.",
    uploadFail: "⚠️ Couldn't upload the file.",
    uploading: "📤 Uploading {name}…"
  };

  var cfg = null;          // {api_base, token, ttl, strings?}
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

  // Visitor context for the operator (feature SUPPORT_VISITOR_CONTEXT; the product reports it via cfg.context).
  // The host may add custom fields by setting window.supportalContext = { plan: "Pro", account: "…" }.
  function collectContext() {
    var c = { url: location.href, title: document.title };
    if (document.referrer) c.referrer = document.referrer;
    var custom = window.supportalContext;
    if (custom && typeof custom === "object") {
      for (var k in custom) { if (typeof custom[k] === "string") c[k] = custom[k]; }
    }
    return c;
  }

  function send(text, retried) {
    var body = { text: text };
    if (cfg && cfg.context) body.context = collectContext();
    return fetch(apiUrl("/v1/widget/send"), {
      method: "POST", headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(body)
    }).then(function (r) {
      if (r.status === 401 && !retried) return refreshToken().then(function (ok) { return ok ? send(text, true) : null; });
      return r.ok ? r.json() : null;
    }).catch(function () { return null; });
  }

  // Raw-body upload (no multipart): Content-Type → mime, ?name= → filename. Feature SUPPORT_ATTACHMENTS.
  function uploadFile(file, retried) {
    return fetch(apiUrl("/v1/widget/upload?name=" + encodeURIComponent(file.name || "file")), {
      method: "POST", headers: authHeaders({ "Content-Type": file.type || "application/octet-stream" }),
      body: file
    }).then(function (r) {
      if (r.status === 401 && !retried) return refreshToken().then(function (ok) { return ok ? uploadFile(file, true) : null; });
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
    if (!m.text && !m.attachment_id) bub.style.display = "none";  // empty bubble (attachment-only handled below)
    row.appendChild(bub);
    els.list.appendChild(row);
    bubbles[m.id] = bub;                     // for later edit/del by id
    if (m.attachment_id) loadAttachment(bub, m);
  }

  // Fetch attachment bytes with the Bearer token (an <img src> can't send headers), then render inline
  // for images or as a download link otherwise. The bytes stay on the relay; we only proxy them here.
  function loadAttachment(bub, m) {
    bub.style.display = "";
    fetch(apiUrl("/v1/widget/attachment/" + m.attachment_id), { headers: authHeaders() })
      .then(function (r) { return r.ok ? r.blob() : null; })
      .then(function (blob) {
        if (!blob) return;
        var url = URL.createObjectURL(blob);
        var mime = m.attachment_mime || blob.type || "";
        if (mime.indexOf("image/") === 0) {
          var img = document.createElement("img");
          img.className = "supportal-media";
          img.alt = m.attachment_name || "image";
          img.src = url;
          img.addEventListener("click", function () { window.open(url, "_blank"); });
          bub.appendChild(img);
        } else {
          var a = document.createElement("a");
          a.className = "supportal-file";
          a.href = url; a.download = m.attachment_name || "file";
          a.textContent = "📎 " + (m.attachment_name || "file");
          bub.appendChild(a);
        }
        if (isOpen) scrollToBottom();
      }).catch(function () { /* ignore — a transient fetch failure */ });
  }

  function onFilePicked(e) {
    var file = e.target.files && e.target.files[0];
    e.target.value = "";                      // allow re-picking the same file
    if (!file) return;
    renderNote(STRINGS.uploading.replace("{name}", file.name));
    uploadFile(file).then(function (res) {
      if (res && res.message_id) pollOnce();  // pull the new attachment message in promptly
      else renderNote(STRINGS.uploadFail);
    });
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
        renderNote(STRINGS.sendFail);
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
  var CLIP_ICON = '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor"'
    + ' stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0'
    + ' 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>';

  function build() {
    var root = document.createElement("div");
    root.id = "supportal-root";
    root.innerHTML =
      '<button id="supportal-fab" type="button" aria-label="Support" aria-expanded="false">'
      + CHAT_ICON + '<span class="supportal-fab-badge" style="display:none"></span></button>'
      + '<section class="supportal-drawer" role="dialog" aria-label="Support chat">'
      + '  <header class="supportal-drawer-head"><span class="supportal-drawer-title"></span>'
      + '    <button class="supportal-drawer-close" type="button">&times;</button></header>'
      + '  <div class="supportal-msg-list"><div class="supportal-greeting"></div></div>'
      + '  <form class="supportal-composer">'
      + '    <button type="button" class="supportal-attach">' + CLIP_ICON + '</button>'
      + '    <input type="file" class="supportal-file-input" hidden>'
      + '    <textarea rows="1" class="supportal-input"></textarea>'
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
    els.attach = root.querySelector(".supportal-attach");
    els.fileInput = root.querySelector(".supportal-file-input");
    els.title = root.querySelector(".supportal-drawer-title");

    // Localized copy — set as text/attributes (never innerHTML), so product-supplied strings are inert.
    els.title.textContent = STRINGS.title;
    els.greeting.textContent = STRINGS.greeting;
    els.input.setAttribute("placeholder", STRINGS.placeholder);
    els.fab.setAttribute("aria-label", STRINGS.support);
    els.attach.setAttribute("aria-label", STRINGS.attach);
    root.querySelector(".supportal-send").setAttribute("aria-label", STRINGS.send);
    root.querySelector(".supportal-drawer-close").setAttribute("aria-label", STRINGS.close);

    // Attachments are a feature flag: the product's /support/config reports whether the relay has it on.
    if (!(cfg && cfg.attachments)) { els.attach.style.display = "none"; }
    els.attach.addEventListener("click", function () { els.fileInput.click(); });
    els.fileInput.addEventListener("change", onFilePicked);

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
      if (cfg.strings && typeof cfg.strings === "object") {   // product-supplied localization (i18n)
        for (var k in STRINGS) { if (typeof cfg.strings[k] === "string") STRINGS[k] = cfg.strings[k]; }
      }
      build();
      pollLoop();
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
