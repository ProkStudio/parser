"use strict";

const $ = (id) => document.getElementById(id);
const views = new Set(["overview", "messages", "jobs", "connection", "guide"]);
const labels = { queued: "В очереди", running: "Собираем", waiting: "Ждём Telegram", paused: "На паузе", cancelled: "Отменено", failed: "Ошибка", completed: "Завершено" };
const mediaLabels = { text: "Текст", photo: "Фото", video: "Видео", audio: "Аудио", document: "Документ", other: "Медиа" };
let token = "", currentView = "overview", status = null, page = 1, jobsOffset = 0;
let stopped = false, pollingTimer;
let authOverride = null, refreshing = false, messageRequest = 0, toastTimer, searchTimer;
const busyForms = new Set();
const number = (value) => new Intl.NumberFormat("ru-RU").format(value || 0);
const date = (value) => new Intl.DateTimeFormat("ru-RU", { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit", timeZone: "UTC" }).format(new Date(value));

function node(tag, className = "", text = "") {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== "") element.textContent = String(text);
  return element;
}

function showError(error) {
  $("alertText").textContent = error.message || "Не удалось выполнить действие.";
  $("alert").hidden = false;
}

function notify(message) {
  clearTimeout(toastTimer);
  $("toast").textContent = message;
  $("toast").hidden = false;
  toastTimer = setTimeout(() => { $("toast").hidden = true; }, 5000);
}

async function api(path, options = {}) {
  const { body, ...rest } = options;
  let response;
  try {
    response = await fetch(path, { ...rest, cache: "no-store", credentials: "omit", headers: { Authorization: "Bearer " + token, ...(body ? { "Content-Type": "application/json" } : {}) }, ...(body ? { body: JSON.stringify(body) } : {}) });
  } catch {
    throw new Error("Нет связи с локальным приложением. Убедитесь, что Parser запущен, и обновите страницу.");
  }
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    const error = new Error(payload.error || "Не удалось выполнить запрос.");
    error.code = payload.code;
    throw error;
  }
  return options.raw ? response : response.json();
}

function empty(container, title, description, symbol = "⌁") {
  const state = node("div", "empty-state");
  const icon = node("span", "empty-symbol", symbol);
  icon.setAttribute("aria-hidden", "true");
  state.append(icon, node("h3", "", title), node("p", "", description));
  container.replaceChildren(state);
}

function navigate(view) {
  if (!views.has(view)) return;
  currentView = view;
  document.querySelectorAll(".view").forEach((section) => { section.hidden = section.id !== "view-" + view; });
  document.querySelectorAll(".nav-button").forEach((button) => {
    const active = button.dataset.view === view;
    button.classList.toggle("selected", active);
    if (active) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current");
  });
  if (view === "messages") loadMessages().catch(showError);
  if (view === "jobs") loadJobs().catch(showError);
  $("main").focus({ preventScroll: true });
  window.scrollTo({ top: 0, behavior: "instant" });
}

document.addEventListener("click", (event) => {
  const target = event.target.closest("button[data-view]");
  if (target) navigate(target.dataset.view);
});

function renderAuth(auth) {
  const step = auth.authorized ? "ready" : (authOverride || auth.step);
  document.querySelectorAll("[data-auth-step]").forEach((element) => { element.hidden = element.dataset.authStep !== step; });
  $("authTitle").textContent = auth.authorized ? "Аккаунт подключён" : "Подключите Telegram";
  $("authSubtitle").textContent = auth.authorized ? "Сессия сохранена на этом устройстве" : "Параметры приложения → код → готово";
  $("accountName").textContent = auth.account || "Telegram подключён";
  $("connectionActions").hidden = !auth.configured || auth.authorized;
  $("authError").hidden = !auth.error;
  $("authError").textContent = auth.error || "";
}

function renderStatus(next) {
  const oldCount = status?.stats.messages;
  status = next;
  const auth = next.telegram;
  $("connectionLabel").textContent = auth.authorized ? "Telegram подключён" : "Не подключён";
  $("connectionChip").classList.toggle("connected", auth.authorized);
  $("version").textContent = "Parser / " + next.version;
  $("onboarding").hidden = auth.authorized;
  $("createJob").disabled = !auth.authorized || busyForms.has("jobForm");
  $("jobHint").textContent = auth.authorized ? "Можно закрыть вкладку: пока Parser запущен, сбор продолжится" : "Сначала подключите аккаунт";
  $("dataDirectory").textContent = next.data_dir;
  $("cooldownNotice").hidden = next.cooldown_until * 1000 <= Date.now();
  $("cooldownNotice").textContent = "Ограничение Telegram: новые запросы сбора возобновятся после " + date(new Date(next.cooldown_until * 1000)) + " UTC. Ожидание сохраняется при перезапуске.";
  renderAuth(auth);
  if (oldCount !== undefined && oldCount !== next.stats.messages && currentView === "messages") loadMessages().catch(showError);
}

function renderSources(sources) {
  const select = $("sourceFilter"), selected = select.value;
  const first = node("option", "", "Все источники");
  first.value = "";
  const options = sources.map((source) => {
    const option = node("option", "", source.title + " · " + number(source.message_count));
    option.value = String(source.id);
    return option;
  });
  select.replaceChildren(first, ...options);
  if (sources.some((source) => String(source.id) === selected)) select.value = selected;
}

function renderJobs(container, jobs, compact = false) {
  if (container.contains(document.activeElement)) return;
  const opened = new Set([...container.querySelectorAll("details[open]")].map((item) => item.dataset.job));
  if (!jobs.length) {
    empty(container, compact ? "Пока здесь тихо" : "Задач пока нет", compact ? "Добавьте источники и начните сбор. Здесь появится его прогресс." : "Создайте сбор во вкладке «Обзор». Прогресс и ошибки останутся в истории.", "⇅");
    return;
  }
  container.replaceChildren(...jobs.map((job) => {
    const card = node("article", "job-card"), top = node("div", "job-top");
    top.append(node("strong", "job-name", job.source_key), node("span", "badge " + job.state, labels[job.state] || job.state));
    const meta = node("p", "job-meta", "Просмотрено " + number(job.scanned) + " из лимита " + number(job.options.limit) + " · Совпадений " + number(job.matched));
    const progress = node("progress");
    progress.max = job.options.limit;
    progress.value = job.scanned;
    progress.setAttribute("aria-label", "Просмотрено сообщений в пределах лимита");
    card.append(top, meta, progress);
    if (job.state === "waiting" && job.wait_until) card.append(node("p", "job-error", "Telegram попросил подождать до " + date(job.wait_until) + " UTC. Продолжим автоматически."));
    if (job.error) card.append(node("p", "job-error", job.error));
    if (job.completion === "limit") card.append(node("p", "help", "Лимит достигнут. Создайте новый сбор с сохранённой позиции, чтобы получить следующую часть истории."));
    if (job.stop_request) card.append(node("p", "help", "Сохраняем позицию и останавливаем запрос…"));
    const actions = node("div", "job-actions");
    const choices = [];
    if (["queued", "running", "waiting"].includes(job.state)) choices.push(["pause", "Пауза"]);
    if (["paused", "failed"].includes(job.state)) choices.push(["resume", job.state === "failed" ? "Повторить" : "Продолжить"]);
    if (["queued", "running", "waiting", "paused", "failed"].includes(job.state)) choices.push(["cancel", "Отменить"]);
    for (const [action, label] of choices) {
      const button = node("button", "text-button" + (action === "cancel" ? " danger-text" : ""), label);
      button.disabled = Boolean(job.stop_request);
      button.addEventListener("click", async () => {
        if (action === "cancel" && !await confirmAction("Отменить задачу?", "Уже сохранённые сообщения останутся в базе. Для нового сбора создайте отдельную задачу.", "Отменить задачу")) return;
        await withBusy(button, async () => {
          await api("/api/jobs/" + job.id + "/" + action, { method: "POST", body: {} });
          button.blur();
          await refresh();
          notify(action === "resume" ? "Задача возвращена в очередь" : "Команда принята. Прогресс будет сохранён.");
        });
      });
      actions.append(button);
    }
    card.append(actions);
    if (!compact) {
      const details = node("details", "job-details");
      details.dataset.job = job.id;
      details.open = opened.has(job.id);
      details.append(node("summary", "", "Параметры · " + date(job.created_at) + " UTC"), node("p", "", "Период UTC: " + (job.options.date_from || "с начала") + " — " + (job.options.date_to || "без ограничения") + ". Содержит: " + (job.options.include.join(", ") || "любые фразы") + ". Исключает: " + (job.options.exclude.join(", ") || "ничего") + ". Тип: " + (mediaLabels[job.options.media] || "Все") + ". " + (job.options.incremental ? "С сохранённой позиции." : "Повтор истории.")));
      card.append(details);
    }
    return card;
  }));
}

async function loadJobs() {
  const data = await api("/api/jobs?offset=" + jobsOffset);
  renderJobs($("allJobs"), data.items);
  $("prevJobs").disabled = jobsOffset === 0;
  $("nextJobs").disabled = data.items.length < 50;
  $("jobsPageLabel").textContent = "Страница " + (jobsOffset / 50 + 1);
}

async function refresh() {
  if (refreshing || !token || stopped) return;
  refreshing = true;
  try {
    const [next, sources, jobs] = await Promise.all([api("/api/status"), api("/api/sources"), api("/api/jobs")]);
    renderStatus(next);
    renderSources(sources.items);
    renderJobs($("recentJobs"), jobs.items.slice(0, 2), true);
    if (currentView === "jobs") await loadJobs();
  } catch (error) {
    showError(error);
  } finally { refreshing = false; }
}

function messageQuery() {
  return new URLSearchParams({ q: $("search").value.trim(), source: $("sourceFilter").value });
}

async function loadMessages() {
  const request = ++messageRequest;
  const query = messageQuery();
  query.set("page", String(page));
  const data = await api("/api/messages?" + query);
  if (request !== messageRequest) return;
  $("resultCount").textContent = "Найдено: " + number(data.total);
  const pages = Math.max(1, Math.ceil(data.total / data.page_size));
  $("pageLabel").textContent = page + " / " + pages;
  $("prevPage").disabled = page <= 1;
  $("nextPage").disabled = page >= pages;
  $("exportButton").disabled = data.total === 0;
  const list = $("messageList");
  if (!data.items.length) {
    empty(list, "Сообщений не найдено", $("search").value || $("sourceFilter").value ? "Измените запрос или выберите другой источник." : "Запустите сбор: сохранённые сообщения появятся здесь.", "≡");
    return;
  }
  list.replaceChildren(...data.items.map((message) => {
    const card = node("article", "message-card"), meta = node("div", "message-meta");
    const timestamp = node("time", "", date(message.sent_at) + " UTC");
    timestamp.dateTime = message.sent_at;
    meta.append(node("strong", "", message.source_title), timestamp, node("span", "badge", mediaLabels[message.media] || message.media));
    const characters = Array.from(message.text);
    card.append(meta, node("p", "message-text", message.text ? characters.slice(0, 420).join("") + (characters.length > 420 ? "…" : "") : "Медиасообщение без подписи"));
    if (characters.length > 420) {
      const details = node("details", "message-details");
      details.append(node("summary", "", "Полный текст"), node("p", "", message.text));
      card.append(details);
    }
    const bottom = node("div", "message-bottom");
    bottom.append(node("span", "", "ID " + message.message_id + " · Просмотров " + number(message.views)));
    try {
      const url = new URL(message.link);
      if (url.protocol === "https:" && url.host === "t.me" && !url.username && !url.password) {
        const link = node("a", "", "В Telegram ↗");
        link.href = url.href;
        link.target = "_blank";
        link.rel = "noreferrer";
        bottom.append(link);
      }
    } catch { /* A basic group may have no permalink. */ }
    card.append(bottom);
    return card;
  }));
}

async function withBusy(button, action) {
  if (button.disabled) return;
  const text = button.textContent;
  button.disabled = true;
  button.textContent = "Подождите…";
  try { await action(); } catch (error) { showError(error); }
  finally { button.disabled = false; button.textContent = text; }
}

function confirmAction(title, description, label) {
  const dialog = $("confirmDialog");
  $("confirmTitle").textContent = title;
  $("confirmDescription").textContent = description;
  $("confirmOk").textContent = label;
  dialog.showModal();
  $("confirmCancel").focus();
  return new Promise((resolve) => {
    const finish = (value) => { dialog.close(); resolve(value); };
    $("confirmCancel").onclick = () => finish(false);
    $("confirmOk").onclick = () => finish(true);
    dialog.oncancel = (event) => { event.preventDefault(); finish(false); };
  });
}

$("jobForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("createJob");
  busyForms.add("jobForm");
  await withBusy(button, async () => {
    const body = { sources: $("sources").value.split(/[\n,]/).map((item) => item.trim()).filter(Boolean), include: $("include").value, exclude: $("exclude").value, date_from: $("dateFrom").value, date_to: $("dateTo").value, media: $("media").value, limit: Number($("limit").value), incremental: $("incremental").checked };
    if (body.date_from && body.date_to && body.date_from > body.date_to) throw new Error("Начало периода не может быть позже окончания.");
    const result = await api("/api/jobs", { method: "POST", body });
    notify("Добавлено задач: " + result.ids.length + ". Следите за прогрессом в очереди.");
    await refresh();
  });
  busyForms.delete("jobForm");
  button.disabled = !status?.telegram.authorized;
});

for (const [form, action, fields] of [
  ["configForm", "configure", { api_id: "apiId", api_hash: "apiHash" }],
  ["phoneForm", "send_code", { phone: "phone" }],
  ["codeForm", "verify_code", { code: "code" }],
  ["passwordForm", "verify_password", { password: "password" }],
]) {
  $(form).addEventListener("submit", async (event) => {
    event.preventDefault();
    await withBusy($(form).querySelector('[type="submit"]'), async () => {
      const body = Object.fromEntries(Object.entries(fields).map(([key, id]) => [key, $(id).value]));
      let next;
      try { next = await api("/api/auth/" + action, { method: "POST", body }); }
      finally { if (fields.password) $(fields.password).value = ""; if (fields.code) $(fields.code).value = ""; }
      if (fields.api_hash) $(fields.api_hash).value = "";
      authOverride = null;
      renderAuth(next);
      $("alert").hidden = true;
      await refresh();
      const first = document.querySelector('[data-auth-step="' + next.step + '"] input');
      if (first) first.focus();
      if (next.authorized) { $("phone").value = ""; notify("Telegram подключён. Можно начинать сбор."); }
    });
  });
}

$("editConfig").onclick = () => { authOverride = "settings"; renderAuth(status.telegram); $("apiId").focus(); };
$("backToPhone").onclick = () => { authOverride = "phone"; renderAuth(status.telegram); $("phone").focus(); };
$("refreshAuth").onclick = () => withBusy($("refreshAuth"), async () => { await api("/api/auth/refresh", { method: "POST", body: {} }); authOverride = null; await refresh(); });
$("logoutButton").onclick = async () => {
  if (!await confirmAction("Отключить Telegram?", "Сессия этого приложения будет отозвана. Собранные сообщения и задачи на паузе останутся. Активные задачи сначала нужно остановить.", "Отключить аккаунт")) return;
  await withBusy($("logoutButton"), async () => { await api("/api/auth/logout", { method: "POST", body: {} }); await refresh(); notify("Аккаунт отключён"); });
};
$("clearHistory").onclick = async () => {
  if (!await confirmAction("Удалить историю сборов?", "Все сохранённые сообщения, источники и задачи будут удалены с этого устройства без возможности отмены. Сначала сделайте экспорт. Сессия Telegram останется подключённой.", "Удалить историю")) return;
  await withBusy($("clearHistory"), async () => { await api("/api/history", { method: "DELETE", body: { confirm: "delete-local-history" } }); page = 1; jobsOffset = 0; await refresh(); notify("Локальная история удалена"); });
};
$("search").addEventListener("input", () => { clearTimeout(searchTimer); page = 1; searchTimer = setTimeout(() => loadMessages().catch(showError), 300); });
$("sourceFilter").onchange = () => { page = 1; loadMessages().catch(showError); };
$("refreshMessages").onclick = () => withBusy($("refreshMessages"), loadMessages);
$("prevPage").onclick = () => { if (page > 1) page--; loadMessages().catch(showError); };
$("nextPage").onclick = () => { page++; loadMessages().catch(showError); };
$("prevJobs").onclick = () => { jobsOffset = Math.max(0, jobsOffset - 50); loadJobs().catch(showError); };
$("nextJobs").onclick = () => { jobsOffset += 50; loadJobs().catch(showError); };
$("dismissAlert").onclick = () => { $("alert").hidden = true; };
$("exportButton").onclick = () => withBusy($("exportButton"), async () => {
  const fmt = $("exportFormat").value, query = messageQuery();
  query.set("format", fmt);
  let handle;
  if (window.showSaveFilePicker) {
    try { handle = await window.showSaveFilePicker({ suggestedName: "parser-messages." + fmt }); }
    catch (error) { if (error.name === "AbortError") return; throw error; }
  }
  const response = await api("/api/export?" + query, { raw: true });
  if (handle && response.body) await response.body.pipeTo(await handle.createWritable());
  else {
    const url = URL.createObjectURL(await response.blob());
    const link = node("a");
    link.href = url; link.download = "parser-messages." + fmt;
    document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  }
  notify("Выборка экспортирована. CSV защищён от формул в тексте сообщений.");
});

try {
  const key = new URLSearchParams(location.hash.slice(1)).get("key");
  if (key) {
    history.replaceState(null, "", location.pathname + location.search);
    sessionStorage.setItem("parser-key", key);
  }
  token = sessionStorage.getItem("parser-key") || "";
} catch { showError(new Error("Разрешите хранилище вкладки для локального приложения и откройте ссылку запуска снова.")); }
if (!token) {
  showError(new Error("Откройте личную ссылку, которую выдал Parser при запуске. Ключ доступа нужен только этой вкладке."));
  $("connectionLabel").textContent = "Нужна ссылка запуска";
  empty($("recentJobs"), "Откройте приложение", "Запустите Parser и используйте его личную ссылку.");
} else {
  refresh();
  pollingTimer = setInterval(() => { if (!document.hidden) refresh(); }, 2500);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
}

// Built-in illustrated guide. Only bundled same-origin screenshot paths are used.
document.querySelectorAll('.guide-zoom').forEach(button => {
  button.addEventListener('click', () => {
    const image = button.querySelector('img');
    if (!image.naturalWidth) return;
    $('guideImageFull').src = button.dataset.image;
    $('guideImageFull').alt = image.alt;
    $('imageDialog').showModal();
    $('closeImage').focus();
  });
  const image = button.querySelector('img');
  image.addEventListener('error', () => {
    if (button.querySelector('.guide-missing')) return;
    const note = node('span', 'guide-missing', 'Скриншот не найден в этой сборке. В установщике иллюстрации включены; для сборки из исходников выполните команду генерации из README.');
    button.append(note); image.hidden = true;
  });
});
$('closeImage').onclick = () => $('imageDialog').close();
$('quitButton').onclick = async () => {
  if (!await confirmAction('Завершить Parser?', 'Выполняющаяся задача сохранит позицию и останется на паузе. Чтобы продолжить работу позже, откройте Parser из меню Пуск.', 'Завершить приложение')) return;
  await withBusy($('quitButton'), async () => {
    await api('/api/shutdown', {method:'POST', body:{confirm:'quit'}});
    stopped = true; clearInterval(pollingTimer);
    $('connectionLabel').textContent = 'Приложение закрыто';
    $('createJob').disabled = true;
    $('alert').hidden = true;
    notify('Parser закрыт. Теперь можно закрыть эту вкладку.');
  });
};
