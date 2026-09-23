// Дашборд РАДАРа: список тендеров, оценка, черновик первого касания.
// Весь текст из данных вставляется через esc() — без HTML-инъекций.

const state = {
  status: null,
  items: [],          // [{tender, evaluation, has_draft}]
  drafts: {},         // tender_id -> Draft
  selected: null,
  filter: "all",
  sort: "score",
  busy: new Set(),    // tender_id, по которым идёт запрос
  errors: {},         // tender_id -> {evaluate?, draft?}
};

const LEVELS = {
  high: { label: "Стоит заняться", short: "высокая" },
  medium: { label: "Посмотреть внимательнее", short: "средняя" },
  low: { label: "Отсеяно", short: "низкая" },
};
const CHECKS = { okpd2: "Предмет и ОКПД2", brand: "Бренд", price: "Цена (НМЦК)", deadline: "Срок подачи" };
const CHECK_ICONS = { ok: "✓", warn: "!", bad: "✕" };
const SOURCES = { mock: "Тестовые данные", tenderplan: "Тендерплан" };

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

// --- форматирование ---

function money(v) {
  if (!v) return "—";
  if (v >= 1e9) return `${(v / 1e9).toLocaleString("ru-RU", { maximumFractionDigits: 1 })} млрд ₽`;
  if (v >= 1e6) return `${(v / 1e6).toLocaleString("ru-RU", { maximumFractionDigits: 1 })} млн ₽`;
  return `${Math.round(v).toLocaleString("ru-RU")} ₽`;
}
const date = (iso) => new Date(iso).toLocaleDateString("ru-RU", { day: "numeric", month: "long" });
const dateTime = (iso) => new Date(iso).toLocaleString("ru-RU", { day: "numeric", month: "long", hour: "2-digit", minute: "2-digit" });

function daysLeft(iso) {
  return Math.floor((new Date(iso) - new Date()) / 86400000);
}
function deadlineText(iso) {
  const d = daysLeft(iso);
  if (d < 0) return { text: "приём заявок закрыт", cls: "deadline-past" };
  if (d === 0) return { text: "последний день подачи", cls: "deadline-soon" };
  const word = plural(d, "день", "дня", "дней");
  return { text: `до ${date(iso)} · ${d} ${word}`, cls: d < 7 ? "deadline-soon" : "" };
}
function plural(n, one, few, many) {
  const m10 = n % 10, m100 = n % 100;
  if (m10 === 1 && m100 !== 11) return one;
  if (m10 >= 2 && m10 <= 4 && (m100 < 10 || m100 >= 20)) return few;
  return many;
}

// --- API ---

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  if (!res.ok) {
    let detail = `Ошибка ${res.status}`;
    try { detail = (await res.json()).detail || detail; } catch { /* не JSON */ }
    const err = new Error(detail);
    err.status = res.status;
    throw err;
  }
  return res.json();
}

async function load() {
  try {
    state.status = await api("/api/status");
    state.items = await api("/api/tenders");
  } catch (e) {
    showBanner(`Не удалось загрузить тендеры: ${e.message}`, true);
    state.items = [];
  }
  renderHeader();
  renderAll();
  const first = sortedFiltered()[0];
  if (first && !state.selected) select(first.tender.id);
}

function item(id) {
  return state.items.find((i) => i.tender.id === id);
}

async function evaluate(id, force = false) {
  state.busy.add(id);
  delete (state.errors[id] ||= {}).evaluate;
  renderAll();
  try {
    item(id).evaluation = await api(`/api/tenders/${encodeURIComponent(id)}/evaluate`, { method: "POST", body: { force } });
  } catch (e) {
    state.errors[id].evaluate = e.message;
  } finally {
    state.busy.delete(id);
    renderAll();
  }
}

async function makeDraft(id, force = false) {
  state.busy.add(id);
  delete (state.errors[id] ||= {}).draft;
  renderAll();
  try {
    state.drafts[id] = await api(`/api/tenders/${encodeURIComponent(id)}/draft`, { method: "POST", body: { force } });
    item(id).has_draft = true;
  } catch (e) {
    state.errors[id].draft = e.message;
  } finally {
    state.busy.delete(id);
    renderAll();
  }
  if (state.selected === id) $("#draft-body")?.scrollIntoView({ behavior: "smooth", block: "center" });
}

async function evaluateAll() {
  const btn = $("#evaluate-all");
  const todo = state.items.filter((i) => !i.evaluation).map((i) => i.tender.id);
  btn.disabled = true;
  for (let n = 0; n < todo.length; n++) {
    btn.innerHTML = `<span class="spinner"></span> Оцениваю ${n + 1} из ${todo.length}`;
    await evaluate(todo[n]);
    if (state.errors[todo[n]]?.evaluate && !state.status.anthropic_configured) break;
  }
  btn.disabled = false;
  btn.textContent = "Оценить все";
}

async function select(id, scroll = false) {
  state.selected = id;
  renderAll();
  const it = item(id);
  if (it?.has_draft && !state.drafts[id]) {
    try {
      state.drafts[id] = await api(`/api/tenders/${encodeURIComponent(id)}/draft`);
      renderDetail();
    } catch { /* черновика нет — не страшно */ }
  }
  if (scroll && window.matchMedia("(max-width: 900px)").matches) $("#detail").scrollIntoView({ behavior: "smooth" });
}

// --- отрисовка ---

function showBanner(html, isError = false) {
  const b = $("#banner");
  b.innerHTML = html;
  b.classList.toggle("error", isError);
  b.hidden = false;
}

function renderHeader() {
  const s = state.status;
  if (!s) return;
  $("#source-badge").textContent = `Источник: ${SOURCES[s.source] || s.source}`;
  if (!s.anthropic_configured) {
    showBanner("<strong>Ключ Anthropic не задан.</strong> Тендеры и отсев по предфильтру видны, " +
      "но оценка Claude и черновики станут доступны после того, как вы впишете " +
      "<code>ANTHROPIC_API_KEY</code> в файл <code>.env</code> и перезапустите сервер.");
  }
}

function sortedFiltered() {
  // Неоценённые — между «внимательнее» и «отсеяно»: их ещё предстоит разобрать.
  const rank = (i) => (i.evaluation ? i.evaluation.score : 39.5);
  let list = state.items.filter((i) => {
    if (state.filter === "all") return true;
    if (state.filter === "none") return !i.evaluation;
    return i.evaluation?.level === state.filter;
  });
  const by = {
    score: (a, b) => rank(b) - rank(a)
      || (daysLeft(a.tender.deadline) < 0) - (daysLeft(b.tender.deadline) < 0)
      || new Date(a.tender.deadline) - new Date(b.tender.deadline),
    deadline: (a, b) => new Date(a.tender.deadline) - new Date(b.tender.deadline),
    nmck: (a, b) => b.tender.nmck - a.tender.nmck,
  }[state.sort];
  return [...list].sort(by);
}

function renderKpis() {
  const lv = (l) => state.items.filter((i) => i.evaluation?.level === l);
  $("#kpi-total").textContent = state.items.length;
  $("#kpi-high").textContent = lv("high").length;
  $("#kpi-medium").textContent = lv("medium").length;
  $("#kpi-low").textContent = lv("low").length;
  const sum = [...lv("high"), ...lv("medium")].reduce((s, i) => s + i.tender.nmck, 0);
  $("#kpi-sum").textContent = sum ? money(sum) : "—";

  const counts = {
    all: state.items.length, high: lv("high").length, medium: lv("medium").length,
    low: lv("low").length, none: state.items.filter((i) => !i.evaluation).length,
  };
  document.querySelectorAll("#filters .tab").forEach((t) => {
    const f = t.dataset.filter;
    t.classList.toggle("active", f === state.filter);
    const base = t.textContent.replace(/\s*\d+$/, "");
    t.innerHTML = `${esc(base)}<span class="count">${counts[f]}</span>`;
  });
}

function scorePill(it) {
  const id = it.tender.id;
  if (state.busy.has(id) && !it.evaluation) return `<span class="score-pill pending"><span class="spinner"></span> оценка…</span>`;
  const e = it.evaluation;
  if (!e) return `<span class="score-pill pending">не оценён</span>`;
  return `<span class="score-pill lvl-${e.level}" title="${esc(LEVELS[e.level].label)}"><span class="dot dot-${e.level}"></span>${e.score} · ${LEVELS[e.level].short}</span>`;
}

function renderList() {
  const list = sortedFiltered();
  if (!list.length) {
    $("#list").innerHTML = `<div class="card" style="cursor:default"><span class="muted">Нет тендеров в этом разделе.</span></div>`;
    return;
  }
  $("#list").innerHTML = list.map((it) => {
    const t = it.tender;
    const dl = deadlineText(t.deadline);
    const lvl = it.evaluation ? `lvl-${it.evaluation.level}` : "";
    return `
      <button type="button" class="card ${lvl} ${t.id === state.selected ? "selected" : ""}" data-id="${esc(t.id)}">
        <div class="card-title">${esc(t.title)}</div>
        <div class="card-side">
          <span class="card-price">${money(t.nmck)}</span>
          ${scorePill(it)}
        </div>
        <div class="card-meta">
          <span>${esc(t.customer.name)}</span>
          <span>${esc(t.region)}</span>
          <span class="${dl.cls}">${esc(dl.text)}</span>
        </div>
      </button>`;
  }).join("");
}

function gauge(score, level) {
  const r = 30, c = 2 * Math.PI * r, color = { high: "var(--good)", medium: "var(--warn)", low: "var(--low)" }[level];
  return `
    <div class="gauge" role="img" aria-label="Балл ${score} из 100">
      <svg viewBox="0 0 72 72">
        <circle cx="36" cy="36" r="${r}" fill="none" stroke="var(--surface-2)" stroke-width="7"/>
        ${score > 0 ? `<circle cx="36" cy="36" r="${r}" fill="none" stroke="${color}" stroke-width="7" stroke-linecap="round"
          stroke-dasharray="${(c * score) / 100} ${c}"/>` : ""}
      </svg>
      <div class="gauge-value">${score}</div>
    </div>`;
}

function evaluationSection(it) {
  const id = it.tender.id, e = it.evaluation, busy = state.busy.has(id), err = state.errors[id]?.evaluate;
  if (!e) {
    return `
      <div class="section">
        <div class="section-title">Оценка релевантности</div>
        ${err ? `<div class="error-box">${esc(err)}</div>` : `<p class="muted" style="margin:0 0 12px">Лот прошёл предфильтр — его нужно оценить через Claude.</p>`}
        <div class="draft-actions">
          <button class="btn btn-primary" data-action="evaluate" ${busy ? "disabled" : ""}>
            ${busy ? `<span class="spinner"></span> Claude оценивает…` : "Оценить тендер"}
          </button>
        </div>
      </div>`;
  }
  const how = e.method === "prefilter" ? "Отсеян предфильтром по коду ОКПД2 и ключевым словам, без вызова Claude"
    : `Оценка Claude (${esc(e.model)}) · ${dateTime(e.evaluated_at)}`;
  return `
    <div class="section">
      <div class="section-title">Оценка релевантности
        ${e.method === "llm" ? `<button class="btn btn-ghost" data-action="reevaluate" ${busy ? "disabled" : ""}>${busy ? `<span class="spinner"></span>` : "Переоценить"}</button>` : ""}
      </div>
      <div class="verdict">
        ${gauge(e.score, e.level)}
        <div>
          <div class="verdict-level"><span class="dot dot-${e.level}"></span>${esc(LEVELS[e.level].label)}</div>
          <p class="verdict-summary">${esc(e.summary)}</p>
          <div class="verdict-meta">${how}</div>
        </div>
      </div>
      ${err ? `<div class="error-box" style="margin-bottom:10px">${esc(err)}</div>` : ""}
      <div class="checks">
        ${Object.entries(CHECKS).map(([k, name]) => {
          const c = e.checks[k];
          return `<div class="check">
            <span class="check-icon ${c.status}" aria-label="${c.status}">${CHECK_ICONS[c.status]}</span>
            <div><div class="check-name">${name}</div><div class="check-note">${esc(c.note)}</div></div>
          </div>`;
        }).join("")}
      </div>
    </div>`;
}

function draftSection(it) {
  const id = it.tender.id, d = state.drafts[id], busy = state.busy.has(id), err = state.errors[id]?.draft;
  const e = it.evaluation;
  const lowHint = e && e.level === "low"
    ? `<p class="muted" style="margin:0 0 12px">Лот отсеян — черновик обычно не нужен, но его можно сделать.</p>` : "";
  if (!d) {
    return `
      <div class="section">
        <div class="section-title">Черновик первого касания</div>
        ${lowHint}
        ${err ? `<div class="error-box" style="margin-bottom:10px">${esc(err)}</div>` : ""}
        <button class="btn btn-primary" data-action="draft" ${busy ? "disabled" : ""}>
          ${busy ? `<span class="spinner"></span> Claude пишет письмо…` : "Сгенерировать черновик касания"}
        </button>
        <div class="notice">✉ Письмо не отправляется автоматически — только текст для менеджера.</div>
      </div>`;
  }
  return `
    <div class="section">
      <div class="section-title">Черновик первого касания
        <span class="muted" style="text-transform:none;letter-spacing:0;font-weight:400">${dateTime(d.generated_at)}</span>
      </div>
      <dl class="draft-meta">
        <dt>Кому</dt><dd>${esc(d.recipient)}</dd>
        <dt>Тема</dt><dd>${esc(d.subject)}</dd>
      </dl>
      <textarea class="draft-body" id="draft-body" aria-label="Текст письма">${esc(d.body)}</textarea>
      ${err ? `<div class="error-box" style="margin-top:10px">${esc(err)}</div>` : ""}
      <div class="draft-actions">
        <button class="btn btn-primary" data-action="copy">Скопировать письмо</button>
        <button class="btn" data-action="redraft" ${busy ? "disabled" : ""}>${busy ? `<span class="spinner"></span> Пишу…` : "Другой вариант"}</button>
      </div>
      ${d.manager_notes.length ? `<ul class="notes">${d.manager_notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>` : ""}
      <div class="notice">✉ Письмо не отправляется автоматически. Проверьте, заполните [поля в скобках] и отправьте сами.</div>
    </div>`;
}

function renderDetail() {
  const it = item(state.selected);
  if (!it) return;
  const t = it.tender, e = it.evaluation, dl = deadlineText(t.deadline);
  const steps = [
    ["Найден", true],
    ["Оценён", !!e],
    ["Черновик", !!state.drafts[t.id] || it.has_draft],
  ];
  $("#detail").innerHTML = `
    <div class="steps">
      ${steps.map(([name, done], i) => `<div class="step ${done ? "done" : ""}"><span class="step-num">${done ? "✓" : i + 1}</span>${name}</div>`).join("")}
    </div>
    <div class="section">
      <h2 class="detail-title">${esc(t.title)}</h2>
      <div class="detail-customer">${esc(t.customer.name)}${t.customer.industry ? ` · ${esc(t.customer.industry)}` : ""}</div>
      <div class="facts">
        <div><div class="fact-label">НМЦК</div><div class="fact-value">${money(t.nmck)}</div></div>
        <div><div class="fact-label">Окончание подачи</div><div class="fact-value ${dl.cls}">${dateTime(t.deadline)}</div></div>
        <div><div class="fact-label">Регион</div><div class="fact-value">${esc(t.region)}</div></div>
        <div><div class="fact-label">ОКПД2</div><div class="fact-value">${esc(t.okpd2.code || "—")}</div></div>
        <div><div class="fact-label">Количество</div><div class="fact-value">${t.quantity ?? "—"}</div></div>
        <div><div class="fact-label">Контакт</div><div class="fact-value">${esc(t.customer.contact?.name || "—")}</div></div>
      </div>
      <p class="description">${esc(t.description)}</p>
      <div class="source-line">
        ${esc(t.platform)} · ${esc(t.law)} · № ${esc(t.number)} · опубликован ${date(t.published_at)}
        · источник: ${esc(SOURCES[t.source] || t.source)}
        ${t.url ? ` · <a href="${esc(t.url)}" target="_blank" rel="noopener">открыть карточку</a>` : ""}
      </div>
    </div>
    ${evaluationSection(it)}
    ${draftSection(it)}`;
}

function renderAll() {
  renderKpis();
  renderList();
  renderDetail();
}

// --- события ---

$("#list").addEventListener("click", (ev) => {
  const card = ev.target.closest(".card[data-id]");
  if (card) select(card.dataset.id, true);
});

$("#detail").addEventListener("click", async (ev) => {
  const btn = ev.target.closest("[data-action]");
  if (!btn || !state.selected) return;
  const id = state.selected;
  switch (btn.dataset.action) {
    case "evaluate": return evaluate(id);
    case "reevaluate": return evaluate(id, true);
    case "draft": return makeDraft(id);
    case "redraft": return makeDraft(id, true);
    case "copy": {
      const d = state.drafts[id];
      const text = `Тема: ${d.subject}\n\n${$("#draft-body").value}`;
      try {
        await navigator.clipboard.writeText(text);
        btn.textContent = "Скопировано ✓";
      } catch {
        $("#draft-body").select();
        btn.textContent = "Выделено — нажмите Ctrl+C";
      }
      setTimeout(() => (btn.textContent = "Скопировать письмо"), 2000);
    }
  }
});

// Правки менеджера в тексте письма сохраняются, пока открыта страница.
$("#detail").addEventListener("input", (ev) => {
  if (ev.target.id === "draft-body" && state.drafts[state.selected]) {
    state.drafts[state.selected].body = ev.target.value;
  }
});

$("#filters").addEventListener("click", (ev) => {
  const tab = ev.target.closest(".tab");
  if (!tab) return;
  state.filter = tab.dataset.filter;
  renderAll();
});

$("#sort").addEventListener("change", (ev) => {
  state.sort = ev.target.value;
  renderList();
});

$("#evaluate-all").addEventListener("click", evaluateAll);

load();
