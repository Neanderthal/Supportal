// dali-support — встраиваемый виджет поддержки (ванильный JS, без зависимостей).
//
// Самодостаточный: рисует плавающую кнопку «💬» (снизу слева) + выдвижную панель справа, ведёт свою
// ленту, общается НАПРЯМУЮ с relay по токену. В продукт встраивается одним тегом <script> (+ widget.css);
// вся логика — здесь. Источник правды переписки — relay (БД), виджет лишь опрашивает.
//
// Контракт с хостом (продуктом):
//   GET  {CONFIG_URL}            → {api_base, token, ttl}  (продукт знает залогиненного, чеканит токен)
//                                   нет токена / не ок → виджет молча не поднимается (self-gating)
//   POST {api_base}/v1/widget/send      Bearer token  {text}          → {ok, message_id}
//   GET  {api_base}/v1/widget/messages?after=<id>  Bearer token       → {messages:[{id,direction,author,text,ts}], last_id}
//
// Доставка ответов оператора — polling (видимая вкладка ~4с, скрытая реже). `direction`: in = пользователь,
// out = оператор/система. Текст оператора приходит из Telegram → вставляем ТОЛЬКО как textContent (без innerHTML).
(function () {
  "use strict";
  if (window.__daliSupportInit) return;      // один инстанс на страницу
  window.__daliSupportInit = true;

  var CONFIG_URL = "/support/config";          // same-origin эндпоинт продукта (отдаёт токен)
  var POLL_VISIBLE = 4000, POLL_HIDDEN = 20000;

  var cfg = null;          // {api_base, token, ttl}
  var lastId = 0;          // курсор polling (последний виденный id)
  var unread = 0;          // непрочитанные ответы оператора при закрытой панели
  var firstSync = true;    // первый опрос = восстановление (не бейджим историю)
  var isOpen = false;
  var seen = {};           // id → true (дедуп между оптимистичным эхо и polling)
  var els = {};            // ссылки на DOM

  // ── сеть ───────────────────────────────────────────────────────────────────
  function apiUrl(path) { return ((cfg && cfg.api_base) || "") + path; }
  function authHeaders(extra) {
    var h = extra || {};
    h["Authorization"] = "Bearer " + ((cfg && cfg.token) || "");
    return h;
  }
  function fetchConfig() {
    return fetch(CONFIG_URL, { credentials: "same-origin", cache: "no-store" })
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
    return fetch(apiUrl("/v1/widget/messages?after=" + lastId), { headers: authHeaders() })
      .then(function (r) {
        if (r.status === 401 && !retried) return refreshToken().then(function (ok) { return ok ? pollOnce(true) : null; });
        return r.ok ? r.json() : null;
      })
      .then(function (j) { if (j && j.messages) ingest(j.messages); })
      .catch(function () { /* сеть мигнула — следующий тик повторит */ });
  }

  function pollLoop() {
    pollOnce().then(function () {
      var delay = (typeof document.hidden === "boolean" && document.hidden) ? POLL_HIDDEN : POLL_VISIBLE;
      setTimeout(pollLoop, delay);
    });
  }

  // ── лента ────────────────────────────────────────────────────────────────────
  function ingest(messages) {
    var appended = false;
    for (var i = 0; i < messages.length; i++) {
      var m = messages[i];
      if (seen[m.id]) continue;
      seen[m.id] = true;
      if (m.id > lastId) lastId = m.id;
      renderBubble(m);
      appended = true;
      if (m.direction === "out" && !firstSync && !isOpen) unread++;
    }
    firstSync = false;
    if (appended) { updateBadge(); hideGreetingIfAny(); if (isOpen) scrollToBottom(); }
  }

  function renderBubble(m) {
    var row = document.createElement("div");
    row.className = "dali-msg " + (m.direction === "in" ? "dali-in" : "dali-out");
    if (m.direction === "out" && m.author) {
      var who = document.createElement("div");
      who.className = "dali-msg-author";
      who.textContent = m.author;            // имя оператора — textContent (без HTML)
      row.appendChild(who);
    }
    var bub = document.createElement("div");
    bub.className = "dali-bubble";
    bub.textContent = m.text || "";          // ВАЖНО: текст из Telegram → только textContent
    row.appendChild(bub);
    els.list.appendChild(row);
  }

  function renderNote(text) {
    var n = document.createElement("div");
    n.className = "dali-note";
    n.textContent = text;
    els.list.appendChild(n);
    if (isOpen) scrollToBottom();
  }

  function hideGreetingIfAny() { if (els.greeting) els.greeting.style.display = "none"; }
  function scrollToBottom() { els.list.scrollTop = els.list.scrollHeight; }
  function updateBadge() {
    if (unread > 0) { els.badge.textContent = unread > 99 ? "99+" : String(unread); els.badge.style.display = ""; }
    else els.badge.style.display = "none";
  }

  // ── открытие/закрытие ──────────────────────────────────────────────────────
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
        if (!seen[res.message_id]) {   // мог уже прийти polling-ом — тогда не дублируем
          seen[res.message_id] = true;
          renderBubble({ id: res.message_id, direction: "in", author: "", text: text });
          hideGreetingIfAny(); scrollToBottom();
        }
        if (res.message_id > lastId) lastId = res.message_id;
      } else {
        renderNote("⚠️ Не удалось отправить сообщение. Попробуйте ещё раз.");
      }
    });
  }

  function autoGrow() {
    els.input.style.height = "auto";
    els.input.style.height = Math.min(els.input.scrollHeight, 120) + "px";
  }

  // ── DOM ──────────────────────────────────────────────────────────────────────
  var CHAT_ICON = '<svg viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor"'
    + ' stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2'
    + ' 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>';

  function build() {
    var root = document.createElement("div");
    root.id = "dali-support-root";
    root.innerHTML =
      '<button id="dali-support-fab" type="button" aria-label="Поддержка" aria-expanded="false">'
      + CHAT_ICON + '<span class="dali-fab-badge" style="display:none"></span></button>'
      + '<section class="dali-support-drawer" role="dialog" aria-label="Чат поддержки">'
      + '  <header class="dali-drawer-head"><span class="dali-drawer-title">Поддержка</span>'
      + '    <button class="dali-drawer-close" type="button" aria-label="Закрыть">&times;</button></header>'
      + '  <div class="dali-msg-list"><div class="dali-greeting">Опишите вопрос — ответит оператор поддержки.'
      + ' Ответ придёт сюда, можно не ждать на странице.</div></div>'
      + '  <form class="dali-composer"><textarea rows="1" placeholder="Сообщение…" class="dali-input"></textarea>'
      + '    <button type="submit" class="dali-send" aria-label="Отправить">'
      + '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2"'
      + ' stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/>'
      + '<polygon points="22 2 15 22 11 13 2 9 22 2"/></svg></button></form>'
      + '</section>';
    document.body.appendChild(root);

    els.root = root;
    els.fab = root.querySelector("#dali-support-fab");
    els.badge = root.querySelector(".dali-fab-badge");
    els.list = root.querySelector(".dali-msg-list");
    els.greeting = root.querySelector(".dali-greeting");
    els.input = root.querySelector(".dali-input");
    els.form = root.querySelector(".dali-composer");

    els.fab.addEventListener("click", function () { setOpen(!isOpen); });
    root.querySelector(".dali-drawer-close").addEventListener("click", function () { setOpen(false); });
    els.form.addEventListener("submit", function (e) { e.preventDefault(); submit(); });
    els.input.addEventListener("input", autoGrow);
    els.input.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); }
    });
  }

  // ── запуск: добываем конфиг (с токеном), затем поднимаем UI и polling ────────
  function start() {
    fetchConfig().then(function (j) {
      if (!j) { setTimeout(start, 30000); return; }  // не залогинен/выключено → тихо повторим позже
      cfg = j;
      build();
      pollLoop();
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
