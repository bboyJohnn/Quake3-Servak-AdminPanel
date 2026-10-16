'use strict';
/* Веб-панель сервера Quake III. Одностраничник без сборки и внешних библиотек.
   Всё, что приходит от сервера (имена игроков, строки логов), считается чужим
   текстом и выводится только через esc() или textContent: имя игрока задаёт
   кто угодно из интернета. Встроенные onclick запрещены политикой страницы,
   поэтому кнопки работают через data-act и общий обработчик. */

// ============================================================ языки
/* Исходный язык панели — русский: каждая надпись в коде обёрнута в L('…') и сама служит
   ключом перевода. Словарь выбранного языка (static/i18n/<код>.js) подключает boot.js ещё до
   этого файла. {0}, {1}… в ключе — места для чисел и имён: L('Игроков: {0}', n).
   Нет перевода — остаётся русский. */

const I18N = window.PANEL_I18N || null;
const LANG = document.documentElement.lang || 'ru';
const LANGS = window.PANEL_LANGS || [['ru', 'Русский', 'ru-RU']];
const LOCALE = LANGS.reduce((a, x) => (x[0] === LANG ? x[2] : a), 'ru-RU');

function L(key, ...args) {
  let text = I18N && I18N[key];
  if (!text) text = key;
  else if (key[0] === ' ' || key[key.length - 1] === ' ') {
    // пробелы по краям — часть склейки в коде, в переводе их держать не нужно
    text = key.match(/^ */)[0] + text.trim() + key.match(/ *$/)[0];
  }
  return args.length ? text.replace(/\{(\d+)\}/g, (m, n) => (args[n] == null ? '' : String(args[n]))) : text;
}

/* Одно русское слово с разным смыслом: Lx('one', 'раз') ищет «one|раз», нет — обычный перевод. */
function Lx(ctx, key, ...args) { return I18N && I18N[ctx + '|' + key] ? L(ctx + '|' + key, ...args) : L(key, ...args); }

/* Текст, который прислал сервер (ошибки, подсказки, записи журнала), переводится тем же
   словарём: сначала строка целиком, потом по образцам с {0} — «нет в pk3-архивах: {0}»
   узнаёт и «нет в pk3-архивах: q3dm99». Ответы самого игрового сервера остаются как есть. */
let srvPatterns = null;
function Lsrv(text) {
  if (!I18N || typeof text !== 'string' || !text) return text;
  if (I18N[text]) return I18N[text];
  if (!srvPatterns) {
    const re = x => x.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    srvPatterns = Object.keys(I18N).filter(k => /\{\d+\}/.test(k)).map(k => {
      const order = [];
      const src = re(k).replace(/\\\{(\d+)\\\}/g, (m, n) => { order.push(+n); return '([\\s\\S]+?)'; });
      return [new RegExp('^' + src + '$'), k, order, k.replace(/\{\d+\}/g, '').length];
    });
    srvPatterns.sort((a, b) => b[3] - a[3]);   // сначала образцы, где больше постоянного текста
  }
  return text.split('\n').map(line => {
    if (!line.trim() || !/[А-Яа-яЁё]/.test(line)) return line;
    if (I18N[line]) return I18N[line];
    for (const [rx, k, order] of srvPatterns) {
      const m = line.match(rx);
      if (!m) continue;
      const args = [];
      order.forEach((n, i) => { args[n] = Lsrv(m[i + 1]); });
      return L(k, ...args);
    }
    return line;
  }).join('\n');
}

/* Склонение по числу. Русские формы — ключи: «заход», «захода», «заходов». Категорию
   выбирает Intl.PluralRules языка: в английском one/other, в польском one/few/many,
   в японском и китайском только other. Если одна русская форма служит двум категориям
   («1 раз», «5 раз»), английский словарь различает их через «one|раз». */
const PLURAL = (() => { try { return new Intl.PluralRules(LOCALE); } catch (e) { return null; } })();
function plural(n, one, few, many) {
  let cat;
  if (LANG === 'ru' || !PLURAL) {
    const a = n % 10, b = n % 100;
    cat = a === 1 && b !== 11 ? 'one' : a >= 2 && a <= 4 && (b < 12 || b > 14) ? 'few' : 'many';
  } else cat = PLURAL.select(n);
  if (cat === 'one') return Lx('one', one);
  if (cat === 'few') return Lx('few', few);
  return Lx('many', many);
}
const nPlural = (n, one, few, many) => num(n) + ' ' + plural(n, one, few, many);

// ============================================================ основа

const S = { csrf: '', page: 'overview', timers: [], statsTab: 'humans', rotEdit: {}, site: {} };
const $ = (sel, root = document) => root.querySelector(sel);
const esc = v => String(v ?? '').replace(/[&<>"']/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

async function api(path, body) {
  const nav = S.nav;
  const opt = { credentials: 'same-origin' };
  if (body !== undefined) {
    opt.method = 'POST';
    opt.headers = { 'Content-Type': 'application/json', 'X-CSRF': S.csrf };
    opt.body = JSON.stringify(body);
  }
  const r = await fetch(path, opt);
  let j;
  try { j = await r.json(); } catch (e) { throw new Error(L('Панель ответила не JSON ({0})', r.status)); }
  if (r.status === 401 && S.authed) { showLogin(); throw new Error(L('Нужно войти')); }
  // пока ждали ответ, ушли на другую страницу — показывать его уже некуда
  if (body === undefined && nav !== S.nav) return new Promise(() => {});
  if (!j.ok) throw new Error(Lsrv(j.error) || L('Ошибка {0}', r.status));
  return j.data;
}

function toast(msg, bad) {
  const t = document.createElement('div');
  t.className = 'toast' + (bad ? ' bad' : '');
  t.textContent = msg;
  $('#toasts').appendChild(t);
  if (bad) sfx.play('menu4');
  setTimeout(() => t.remove(), bad ? 9000 : 4500);
}

async function act(fn, okMsg) {
  try {
    const d = await fn();
    if (okMsg) toast(typeof okMsg === 'function' ? okMsg(d) : okMsg);
    return d;
  } catch (e) { toast(e.message, true); return null; }
}

// ============================================================ форматирование

function fmtBytes(n) {
  n = Number(n) || 0;
  const u = [L('Б'), L('КБ'), L('МБ'), L('ГБ'), L('ТБ')]; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i ? n.toLocaleString(LOCALE, { maximumFractionDigits: 1 }) : n) + ' ' + u[i];
}
function fmtDur(sec) {
  sec = Math.max(0, Math.round(Number(sec) || 0));
  const d = Math.floor(sec / 86400), h = Math.floor(sec % 86400 / 3600), m = Math.floor(sec % 3600 / 60);
  if (d) return L('{0} д {1} ч', d, h);
  if (h) return L('{0} ч {1} мин', h, m);
  if (m) return L('{0} мин {1} с', m, sec % 60);
  return L('{0} с', sec);
}
const fmtTime = ts => !ts ? '—' : new Date(ts * 1000).toLocaleString(LOCALE, { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
const fmtDateTime = ts => new Date(ts * 1000).toLocaleString(LOCALE, { day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit' });
const fmtDate = ts => new Date(ts * 1000).toLocaleDateString(LOCALE, { day: '2-digit', month: '2-digit', year: 'numeric' });
const num = n => (Number(n) || 0).toLocaleString(LOCALE);
const dec = (x, digits) => (Number(x) || 0).toLocaleString(LOCALE, digits == null ? {} : { minimumFractionDigits: digits, maximumFractionDigits: digits });
const pill = (text, kind) => '<span class="pill ' + (kind || '') + '">' + esc(text) + '</span>';
const pad2 = n => String(n).padStart(2, '0');
const dayKey = d => d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate());
const sessEnd = s => s.online ? Math.floor(Date.now() / 1000) : (s.left_at || s.last_seen);
const cap1 = s => s.charAt(0).toLocaleUpperCase(LOCALE) + s.slice(1);
// дни недели с понедельника и месяцы — на языке панели
const WEEKDAYS = Array.from({ length: 7 }, (_, i) => cap1(new Date(2024, 0, 1 + i).toLocaleDateString(LOCALE, { weekday: 'short' })));
const monthTitle = (y, m) => cap1(new Date(y, m, 1).toLocaleDateString(LOCALE, { month: 'long', year: 'numeric' }));

// ============================================================ названия

const MODE_TITLES = { f: () => L('Все против всех'), tdm: () => L('Командный бой'), tour: () => L('Дуэли'), c: () => L('Захват флага') };
const GAMETYPE_TITLES = { 0: () => L('Все против всех'), 1: () => L('Дуэль'), 2: () => L('Одиночная'), 3: () => L('Командный бой'), 4: () => L('Захват флага') };
const modeTitle = k => (MODE_TITLES[k] ? MODE_TITLES[k]() : k);
const gtTitle = g => (GAMETYPE_TITLES[g] ? GAMETYPE_TITLES[g]() : String(g));
const EXIT_TITLES = { 'Timelimit hit.': () => L('время вышло'), 'Fraglimit hit.': () => L('лимит фрагов'), 'Capturelimit hit.': () => L('лимит флагов') };
const exitTitle = r => (EXIT_TITLES[r] ? EXIT_TITLES[r]() : r || '—');
const countryName = c => !c ? '' : c.code === 'LAN' ? L('локальная сеть') : c.code === '?' ? L('не определить')
  : (c.names && (c.names[LANG] || (LANG !== 'ru' && c.names.en))) || c.name;
const times = n => nPlural(n, 'раз', 'раза', 'раз');

// ============================================================ настройки браузера

const pref = {
  get(k, d) { try { const v = localStorage.getItem('q3panel-' + k); return v == null ? d : v; } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem('q3panel-' + k, v); } catch (e) { /* приватный режим */ } },
  del(k) { try { localStorage.removeItem('q3panel-' + k); } catch (e) { /* приватный режим */ } },
};
const BGS = [['gothic', () => L('Готика')], ['tech', () => L('Техно')], ['space', () => L('Космос')], ['lava', () => L('Лава')]];
const styleNow = () => (document.documentElement.getAttribute('data-style') === 'plain' ? 'plain' : 'q3');
const LEGAL = () => L('Quake III Arena® — товарный знак id Software. Фоны, звуки и музыка берутся из файлов игры (pk3) на вашем сервере; в панели их нет. Панель никак не связана с id Software.');

/* Звуки меню из pk3 игры: переход в раздел, окно, ошибка; диктор — при входе. */
const sfx = {
  play(name) {
    if (!(S.site.sounds || []).includes(name)) return;
    const voice = ['fight', 'prepare', 'excellent', 'impressive'].includes(name);
    if (pref.get(voice ? 'voice' : 'sounds', 'on') !== 'on') return;
    try { const a = new Audio('/static/q3/snd-' + name + '.wav'); a.volume = voice ? 0.6 : 0.4; a.play().catch(() => {}); } catch (e) { /* без звука */ }
  },
};

/* Музыка из pk3: Front Line Assembly и Sonic Mayhem — та же, что в игре. Играет по кругу:
   первой — первая тема, дальше вперемешку. Браузер не даёт включить звук, пока человек
   ничего не нажал, поэтому музыка начинается с первого клика (если её не выключили). */
const MUSIC_TITLES = {
  fla1: 'Front Line Assembly — 1', fla2: 'Front Line Assembly — 2', fla3: 'Front Line Assembly — 3',
  fla4: 'Front Line Assembly — 4', fla5: 'Front Line Assembly — 5', fla6: 'Front Line Assembly — 6',
  sonic1: 'Sonic Mayhem — 1', sonic2: 'Sonic Mayhem — 2', sonic3: 'Sonic Mayhem — 3',
  sonic4: 'Sonic Mayhem — 4', sonic5: 'Sonic Mayhem — 5', sonic6: 'Sonic Mayhem — 6',
};
const playerOn = () => (S.site.music || []).length > 0 && pref.get('player', 'on') === 'on';
const music = {
  audio: null, order: [], pos: 0, part: 0, started: false,
  tracks() { return S.site.music || []; },
  wanted() { return playerOn() && pref.get('music', 'on') === 'on'; },
  volume() { const v = Number(pref.get('volume', '0.35')); return isFinite(v) ? Math.max(0, Math.min(1, v)) : 0.35; },
  init() {
    if (this.audio) return;
    const a = new Audio();
    a.preload = 'none';
    a.volume = this.volume();
    a.muted = pref.get('muted', 'off') === 'on';
    a.addEventListener('ended', () => this.advance());
    a.addEventListener('play', musicUI);
    a.addEventListener('pause', musicUI);
    this.audio = a;
    const t = this.tracks().map((x, i) => i);
    const first = t.shift();
    for (let i = t.length - 1; i > 0; i--) { const j = Math.floor(Math.random() * (i + 1)); [t[i], t[j]] = [t[j], t[i]]; }
    this.order = first == null ? [] : [first, ...t];
  },
  track() { const t = this.tracks()[this.order[this.pos]]; return t ? [t.key, MUSIC_TITLES[t.key] || t.key] : ['', '']; },
  load() {
    const t = this.tracks()[this.order[this.pos]];
    if (!t) return;
    this.audio.src = '/q3/music/' + encodeURIComponent(t.files[Math.min(this.part, t.files.length - 1)]);
  },
  start() {
    this.init();
    if (!this.order.length) return;
    this.started = true;
    if (!this.audio.src) { this.part = 0; this.load(); }
    this.audio.play().catch(() => {});
    pref.set('music', 'on');
  },
  pause() { if (this.audio) this.audio.pause(); pref.set('music', 'off'); musicUI(); },
  playing() { return !!this.audio && !this.audio.paused; },
  toggle() { if (this.playing()) this.pause(); else this.start(); },
  advance() {
    const t = this.tracks()[this.order[this.pos]];
    if (t && this.part < t.files.length - 1) this.part++;     // после вступления — основная часть
    else { this.pos = (this.pos + 1) % this.order.length; this.part = 0; }
    this.load();
    this.audio.play().catch(() => {});
    musicUI();
  },
  next() { this.init(); this.pos = (this.pos + 1) % Math.max(1, this.order.length); this.part = 0; this.load(); this.start(); musicUI(); },
  pick(n) {
    this.init();
    const at = this.order.indexOf(n);
    if (at >= 0) this.pos = at;
    this.part = 0; this.load(); this.start(); musicUI();
  },
  toggleMute() { this.init(); this.audio.muted = !this.audio.muted; pref.set('muted', this.audio.muted ? 'on' : 'off'); musicUI(); },
  setVolume(v) { this.init(); this.audio.volume = v; pref.set('volume', String(v)); },
};

function musicUI() {
  const box = $('#mus');
  if (box) {
    box.classList.toggle('hidden', !playerOn());
    const [, title] = music.track();
    const muted = music.audio && music.audio.muted;
    $('#mus-t').textContent = music.playing() ? (muted ? L('{0} — звук выключен', title) : title)
      : music.wanted() ? L('Музыка начнётся с первого клика') : L('Музыка на паузе');
    $('#mus-t').title = $('#mus-t').textContent;
    $('#mus-play').textContent = music.playing() ? '❚❚' : '▶';
    $('#mus-mute').textContent = muted ? '🔇' : '🔊';
  }
  if (S.page === 'prefs' && $('#prefs-tracks')) renderTracks();
}

function applyLook() {
  const d = document.documentElement;
  d.setAttribute('data-style', pref.get('style', 'q3') === 'plain' ? 'plain' : 'q3');
  const bg = pref.get('bg', 'gothic');
  const have = S.site.backgrounds || [];
  d.setAttribute('data-bg', have.includes(bg) ? bg : have[0] || 'none');
  const t = pref.get('theme');
  if (t === 'light' || t === 'dark') d.setAttribute('data-theme', t); else d.removeAttribute('data-theme');
}

// ============================================================ полосы и картинки карт

function bars(container, items, label, value, opts = {}) {
  container.innerHTML = '';
  if (!items.length) { container.innerHTML = '<div class="empty">' + esc(L('Пока нет данных')) + '</div>'; return; }
  const max = Math.max(...items.map(i => i[value])) || 1;
  for (const it of items) {
    const row = document.createElement('div'); row.className = 'bar-row';
    const l = document.createElement('div'); l.textContent = it[label];
    if (opts.mapPreview) { l.className = 'mapname'; l.dataset.mapprev = it[label]; }
    if (opts.flags) l.innerHTML = flagHtml(it) + ' ' + esc(it[label]);
    const track = document.createElement('div'); track.className = 'bar-track';
    const fill = document.createElement('div');
    fill.className = 'bar-fill' + (opts.muted && opts.muted(it) ? ' muted' : '');
    fill.style.width = (100 * it[value] / max).toFixed(1) + '%';
    track.appendChild(fill);
    const v = document.createElement('div'); v.className = 'num'; v.textContent = opts.fmt ? opts.fmt(it) : num(it[value]);
    if (opts.tip) row.title = opts.tip(it);
    row.append(l, track, v); container.appendChild(row);
  }
}

/* static/maps/<карта>.jpg — те же картинки, что в меню игры. Достаёт их сама панель при запуске. */
const previewSrc = m => '/static/maps/' + encodeURIComponent(m) + '.jpg';
const mapLink = m => '<span class="mapname" data-mapprev="' + esc(m) + '">' + esc(m) + '</span>';
const hasShot = m => (S.maps && S.maps.previews || []).includes(m);
const shotImg = (m, cls) => hasShot(m)
  ? '<img class="' + cls + '" src="' + previewSrc(m) + '" alt="" loading="lazy" data-mapprev="' + esc(m) + '">'
  : '<span class="' + cls + ' none">' + esc(L('нет картинки')) + '</span>';

/* Картинка карты при наведении на её название. */
function initMapTip() {
  const tip = document.createElement('div');
  tip.className = 'map-tip hidden';
  tip.innerHTML = '<img alt=""><div class="cap"></div>';
  document.body.appendChild(tip);
  const img = tip.querySelector('img'), cap = tip.querySelector('.cap');
  let cur = null;
  img.addEventListener('error', () => { img.classList.add('hidden'); cap.textContent = L('{0} — картинки нет', cur); });
  const place = (x, y) => {
    const w = tip.offsetWidth, h = tip.offsetHeight, gap = 16;
    let left = x + gap, top = y + gap;
    if (left + w > innerWidth - 8) left = Math.max(8, x - w - gap);
    if (top + h > innerHeight - 8) top = Math.max(8, y - h - gap);
    tip.style.left = left + 'px';
    tip.style.top = top + 'px';
  };
  const show = (el, x, y) => {
    const m = el.dataset.mapprev;
    if (m !== cur) { cur = m; cap.textContent = m; img.classList.remove('hidden'); img.src = previewSrc(m); }
    tip.classList.remove('hidden');
    place(x, y);
  };
  const hide = () => tip.classList.add('hidden');
  const target = e => e.target.closest && e.target.closest('[data-mapprev]');
  document.addEventListener('mouseover', e => { const el = target(e); if (el) show(el, e.clientX, e.clientY); });
  document.addEventListener('mousemove', e => {
    if (tip.classList.contains('hidden')) return;
    if (target(e)) place(e.clientX, e.clientY); else hide();
  });
  document.addEventListener('mouseout', e => { const el = target(e); if (el && !el.contains(e.relatedTarget)) hide(); });
  document.addEventListener('touchstart', e => {
    const el = target(e);
    if (el) show(el, e.touches[0].clientX, e.touches[0].clientY); else hide();
  }, { passive: true });
  document.addEventListener('click', e => { if (!target(e)) hide(); });
  window.addEventListener('scroll', hide, true);
  S.hideTip = hide;
}

// ============================================================ окно поверх страницы

function openModal(html, cls) {
  closeModal(true);
  if (S.hideTip) S.hideTip();
  const back = document.createElement('div');
  back.className = 'modal-back';
  back.innerHTML = '<div class="modal ' + (cls || '') + '" role="dialog" aria-modal="true">'
    + '<button class="modal-x" data-act="modal-close" title="' + esc(L('Закрыть')) + '">×</button>' + html + '</div>';
  back.addEventListener('click', e => { if (e.target === back) closeModal(); });
  document.body.appendChild(back);
  S.modal = back;
  sfx.play('menu1');
  return back.querySelector('.modal');
}
function closeModal(quiet) {
  if (!S.modal) return;
  S.modal.remove();
  S.modal = null;
  if (!quiet) sfx.play('menu3');
}

// ============================================================ живые игроки: страна, ссылка на карточку

/* Флаги — static/flags/<код>.svg (набор flag-icons, MIT — см. static/flags/LICENSE). */
const flagHtml = c => !c || !c.code ? '' : /^[A-Z]{2}$/.test(c.code)
  ? '<img class="flag" src="/static/flags/' + c.code.toLowerCase() + '.svg" alt="' + esc(c.code) + '" title="' + esc(countryName(c)) + '">'
  : '<span class="cc" title="' + esc(countryName(c)) + '">' + esc(c.code) + '</span>';
const countryTag = c => c ? flagHtml(c) + ' ' + esc(countryName(c)) : '<span class="muted">—</span>';
/* Ключ игрока — IP-адрес, а у заходов без адреса — «~ник». */
const playerBtn = (key, name) => key
  ? '<button class="plink" data-act="player" data-key="' + esc(key) + '">' + esc(name) + '</button>' : esc(name);
const approxMark = s => s.approx ? '<span class="muted" title="' + esc(L('время восстановлено по длительностям матчей — примерно')) + '">≈</span> ' : '';
const noAddr = () => '<span class="muted">' + esc(L('без адреса')) + '</span>';

// ============================================================ вход и каркас

const siteTitle = live => S.site.title || live || L('Сервер Quake III');

function showLogin() {
  S.authed = false;
  stopTimers();
  closeModal(true);
  $('#root').innerHTML = `<div class="login">
    <form id="login-form" class="login-card">
      ${S.site.emblem ? '<div class="login-emblem"></div>' : ''}
      <div class="brand"><span class="brand-mark">Q3</span> <span>${esc(siteTitle())}</span></div>
      ${S.site.address ? '<div class="muted mono center">' + esc(S.site.address) + '</div>' : ''}
      <input id="login-password" type="password" autocomplete="current-password" placeholder="${esc(L('Пароль'))}" required>
      <button type="submit" class="btn primary">${esc(L('Войти'))}</button>
      <div id="login-error" class="error"></div>
      <p class="muted fs12 center">${esc(L('Пароль задаётся на сервере: venv/bin/python app.py --password'))}</p>
      <button type="button" class="btn ghost small" data-act="prefs-modal">⚙ ${esc(L('Язык и оформление'))}</button>
    </form></div>`;
  document.title = siteTitle();
  $('#login-form').addEventListener('submit', async e => {
    e.preventDefault();
    $('#login-error').textContent = '';
    try {
      const d = await api('/api/login', { password: $('#login-password').value });
      S.csrf = d.csrf;
      sfx.play('fight');
      enterApp();
    } catch (err) { $('#login-error').textContent = err.message; sfx.play('menu4'); }
  });
  setTimeout(() => { const p = $('#login-password'); if (p) p.focus(); }, 50);
}

const NAV = [
  ['overview', () => L('Обзор')], ['players', () => L('Игроки')], ['maps', () => L('Карты и ротации')],
  ['settings', () => L('Настройки сервера')], ['stats', () => L('Статистика')], ['logs', () => L('Логи')],
  ['console', () => L('Консоль')], ['audit', () => L('Журнал действий')], ['prefs', () => L('Настройки панели')]];

function enterApp() {
  S.authed = true;
  $('#root').innerHTML = `<div class="app">
    <aside class="side">
      <div class="brand"><span class="brand-mark">Q3</span> <span id="side-host">${esc(siteTitle())}</span></div>
      ${S.site.address ? '<div class="side-addr mono">' + esc(S.site.address) + '</div>' : ''}
      <nav id="nav">${NAV.map(([k, t]) => `<a data-page="${k}">${esc(t())}</a>`).join('')}</nav>
      <div class="side-foot">
        <div id="side-status" class="pill">…</div>
        <div class="mus hidden" id="mus">
          <div class="mus-t" id="mus-t"></div>
          <div class="row"><button class="btn small" data-act="music-toggle" id="mus-play" title="${esc(L('Играть / пауза'))}">▶</button>
            <button class="btn small" data-act="music-next" title="${esc(L('Следующий трек'))}">⏭</button>
            <button class="btn small" data-act="music-mute" id="mus-mute" title="${esc(L('Звук'))}">🔊</button></div>
        </div>
        <button id="logout" class="btn ghost small">${esc(L('Выйти'))}</button>
      </div>
    </aside>
    <main id="main"></main>
  </div>`;
  $('#logout').addEventListener('click', async () => { await act(() => api('/api/logout', {})); showLogin(); });
  $('#nav').addEventListener('click', e => { const a = e.target.closest('a[data-page]'); if (a) { sfx.play('menu1'); go(a.dataset.page); } });
  musicUI();
  go(location.hash.slice(1) || 'overview', true);
  sideStatus();
  if (S.sideTimer) clearInterval(S.sideTimer);
  S.sideTimer = setInterval(sideStatus, 15000);
}

async function sideStatus() {
  if (!S.authed) return;
  try {
    const d = await api('/api/overview');
    const s = d.server;
    if (!S.site.title && s) { $('#side-host').textContent = s.hostname; document.title = s.hostname; }
    $('#side-status').className = 'pill ' + (s ? 'ok' : 'bad');
    $('#side-status').textContent = s ? L('работает · {0}', s.map) : L('не отвечает');
  } catch (e) { /* тихо: основное сообщение покажет страница */ }
}

async function start() {
  document.addEventListener('click', onAction);
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });
  // флага нет в наборе — показываем код страны буквами
  document.addEventListener('error', e => {
    const t = e.target;
    if (t.classList && t.classList.contains('flag')) t.replaceWith(document.createTextNode(t.alt));
  }, true);
  // музыка — с первого нажатия: раньше браузер звук не даст
  const first = () => { if (music.wanted() && !music.started) music.start(); };
  document.addEventListener('pointerdown', first, { once: true });
  document.addEventListener('keydown', first, { once: true });
  initMapTip();
  window.addEventListener('hashchange', () => { if (S.authed) go(location.hash.slice(1) || 'overview', true); });

  let me = null;
  try { me = await api('/api/me'); } catch (e) { /* сервер панели недоступен — покажем вход */ }
  S.site = (me && me.site) || {};
  applyLook();
  if (me && me.auth) { S.csrf = me.csrf; enterApp(); } else showLogin();
}

// ============================================================ навигация и кнопки

const PAGES = {};
function stopTimers() { S.timers.forEach(clearInterval); S.timers = []; }
function every(ms, fn) { S.timers.push(setInterval(fn, ms)); }

function go(page, fromHash) {
  if (!PAGES[page]) page = 'overview';
  S.page = page;
  S.nav = (S.nav || 0) + 1;
  if (!fromHash) location.hash = page;
  document.querySelectorAll('#nav a').forEach(a => a.classList.toggle('active', a.dataset.page === page));
  stopTimers();
  closeModal(true);
  if (S.hideTip) S.hideTip();
  const main = $('#main');
  main.innerHTML = '<div class="empty">' + esc(L('Загрузка…')) + '</div>';
  PAGES[page](main);
}

/* Все кнопки с data-act попадают сюда. */
const ACTIONS = {};
ACTIONS['modal-close'] = () => closeModal();
async function onAction(e) {
  const b = e.target.closest('[data-act]');
  if (!b || b.disabled) return;
  const fn = ACTIONS[b.dataset.act];
  if (!fn) return;
  e.preventDefault();
  b.disabled = true;
  try { await fn(b.dataset, b); } finally { b.disabled = false; }
}

// ============================================================ Обзор

PAGES.overview = async main => {
  main.innerHTML = `
    <h1>${esc(L('Обзор'))}</h1>
    <div class="grid" id="ov-cards"></div>
    <div class="grid two">
      <div class="card"><h2>${esc(L('На арене'))} <span id="ov-count" class="muted"></span></h2><div id="ov-players"></div></div>
      <div class="card">
        <h2>${esc(L('Быстрые действия'))}</h2>
        <div class="row">
          <button class="btn" data-act="map-next">${esc(L('Следующая карта'))}</button>
          <button class="btn" data-act="map-restart">${esc(L('Перезапустить карту'))}</button>
        </div>
        <p class="muted">${esc(L('Сменить карту или режим — в разделе «Карты и ротации», выкинуть или забанить игрока — в «Игроках», любая команда сервера — в «Консоли».'))}</p>
      </div>
    </div>`;
  const load = async () => {
    let d;
    try { d = await api('/api/overview'); } catch (e) { $('#ov-cards').innerHTML = '<div class="card error">' + esc(e.message) + '</div>'; return; }
    renderOverview(d);
  };
  await load();
  every(5000, load);
};

function renderOverview(d) {
  const s = d.server, h = d.host || {}, st = d.stats || {};
  const cards = [];
  if (s) {
    cards.push(`<div class="card"><h2>${esc(L('Сервер'))} ${pill(L('работает'), 'ok')}</h2>
      <div class="big mono">${esc(s.map)}</div>
      <dl class="kv"><dt>${esc(L('Режим'))}</dt><dd>${esc(gtTitle(s.gametype))}</dd>
      <dt>${esc(L('Игроки'))}</dt><dd>${esc(L('{0} из {1} · людей {2}, ботов {3}', s.players.length, s.maxclients, s.humans, s.bots))}</dd>
      <dt>${esc(L('Лимиты'))}</dt><dd>${esc(L('{0} мин / {1} фрагов', s.timelimit, s.fraglimit))}</dd>
      <dt>${esc(L('Адрес'))}</dt><dd class="mono">${esc(s.address)}</dd></dl></div>`);
  } else {
    cards.push(`<div class="card"><h2>${esc(L('Сервер'))} ${pill(L('не отвечает'), 'bad')}</h2>
      <p>${esc(L('Сервер не ответил на запрос статуса (UDP, порт из Q3_PORT). Проверьте, запущен ли он, и что Q3_HOST и Q3_PORT в .env панели совпадают с его адресом.'))}</p></div>`);
  }
  const memUsed = (h.mem_total || 0) - (h.mem_available || 0);
  cards.push(`<div class="card"><h2>${esc(L('Машина'))}</h2>
    <dl class="kv"><dt>${esc(L('Нагрузка'))}</dt><dd>${esc((h.load || []).join(' · '))} (${esc(nPlural(h.cpus || 0, 'ядро', 'ядра', 'ядер'))})</dd>
    <dt>${esc(L('Память'))}</dt><dd>${esc(L('{0} из {1}', fmtBytes(memUsed), fmtBytes(h.mem_total)))}</dd>
    <dt>${esc(L('Своп'))}</dt><dd>${esc(L('{0} из {1}', fmtBytes((h.swap_total || 0) - (h.swap_free || 0)), fmtBytes(h.swap_total)))}</dd>
    <dt>${esc(L('Диск'))}</dt><dd>${esc(L('свободно {0}', fmtBytes(h.disk_free)))}${h.disk_path ? ' <span class="muted mono">(' + esc(h.disk_path) + ')</span>' : ''}</dd>
    <dt>${esc(L('Аптайм'))}</dt><dd>${esc(fmtDur(h.uptime))}</dd></dl></div>`);
  cards.push(`<div class="card"><h2>${esc(L('История'))}</h2>
    <div class="big">${num(st.matches)} <span class="muted fs15">${esc(plural(st.matches || 0, 'матч', 'матча', 'матчей'))}</span></div>
    <dl class="kv"><dt>${esc(L('Убийств'))}</dt><dd>${num(st.kills)}</dd><dt>${esc(L('Сыграно'))}</dt><dd>${esc(fmtDur(st.seconds))}</dd>
    <dt>${esc(L('Живых игроков'))}</dt><dd>${esc(L('{0} (в {1} матчах)', num(st.humans), num(st.matches_with_humans)))}</dd></dl></div>`);
  $('#ov-cards').innerHTML = (d.setup && d.setup.length ? '<div class="note span-all"><b>' + esc(L('Проверьте настройку панели')) + '</b><ul>'
    + d.setup.map(x => '<li>' + esc(Lsrv(x)) + '</li>').join('') + '</ul></div>' : '') + cards.join('');

  const list = s ? s.players : [];
  $('#ov-count').textContent = s ? '(' + list.length + ')' : '';
  $('#ov-players').innerHTML = list.length ? `<div class="tbl-wrap"><table><thead><tr><th>${esc(L('Игрок'))}</th><th class="num">${esc(L('Счёт'))}</th><th class="num">${esc(L('Пинг'))}</th></tr></thead><tbody>${
    list.map(p => `<tr><td>${p.ping ? '🧑 ' : '<span class="tag-bot">🤖</span> '}${esc(p.name)}</td><td class="num">${p.score}</td><td class="num">${p.ping ? esc(L('{0} мс', p.ping)) : '—'}</td></tr>`).join('')
  }</tbody></table></div>` : '<div class="empty">' + esc(L('Никого')) + '</div>';
}

// ============================================================ Игроки

PAGES.players = async main => {
  main.innerHTML = `
    <h1>${esc(L('Игроки'))}</h1>
    <div class="card"><h2>${esc(L('На сервере'))} <button class="btn small" data-act="players-reload">${esc(L('Обновить'))}</button></h2><div id="pl-table"></div></div>
    <div class="grid two mt16">
      <div class="card"><h2>${esc(L('Сообщение всем'))}</h2>
        <div class="row"><input id="say-text" class="wide" maxlength="150" placeholder="${esc(L('Текст в игровой чат'))}">
        <button class="btn primary" data-act="say">${esc(L('Отправить'))}</button></div></div>
      <div class="card"><h2>${esc(L('Боты'))}</h2>
        <div class="stack">
          <div class="row"><span class="muted">${esc(L('Держать участников:'))}</span><input id="bots-count" class="narrow" type="number" min="0" max="16">
            <button class="btn" data-act="bots">${esc(L('Применить'))}</button></div>
          <div class="row"><input id="addbot-name" placeholder="Sarge" class="narrow w140">
            <select id="addbot-skill"><option>1</option><option>2</option><option selected>3</option><option>4</option><option>5</option></select>
            <button class="btn" data-act="addbot">${esc(L('Добавить бота'))}</button>
            <button class="btn danger" data-act="kickbots">${esc(L('Убрать всех ботов'))}</button></div>
          <div class="muted">${esc(L('Число участников действует до перезапуска. Насовсем — в «Настройках сервера», bot_minplayers.'))}</div>
        </div></div>
    </div>
    <div class="card mt16"><h2>${esc(L('Баны'))}</h2>
      <div class="row mb10"><input id="ban-addr" placeholder="${esc(L('1.2.3.4 или 1.2.3.0/24'))}">
        <button class="btn danger" data-act="ban-address">${esc(L('Забанить адрес'))}</button></div>
      <div id="pl-bans"></div></div>`;
  await loadPlayers();
  every(15000, loadPlayers);
};

async function loadPlayers() {
  let d;
  try { d = await api('/api/players'); } catch (e) { $('#pl-table').innerHTML = '<div class="error">' + esc(e.message) + '</div>'; return; }
  const rows = d.players.sort((a, b) => a.is_bot - b.is_bot || b.score - a.score);
  $('#pl-table').innerHTML = rows.length ? `<div class="tbl-wrap"><table><thead><tr>
    <th class="num">#</th><th>${esc(L('Игрок'))}</th><th class="num">${esc(L('Счёт'))}</th><th class="num">${esc(L('Пинг'))}</th><th>${esc(L('Адрес'))}</th><th></th></tr></thead><tbody>${
    rows.map(p => `<tr><td class="num">${p.num}</td><td>${p.is_bot ? '<span class="tag-bot">🤖 ' + esc(L('бот')) + '</span> ' : '🧑 '}${esc(p.name)}</td>
      <td class="num">${p.score}</td><td class="num">${p.is_bot ? '—' : p.ping == null
        ? '<span class="muted">' + esc(p.state === 'leaving' ? L('выходит') : L('подключается')) + '</span>' : esc(L('{0} мс', p.ping))}</td>
      <td class="mono">${p.is_bot ? '—' : esc(p.address)}</td>
      <td><div class="row">${p.is_bot ? '' : `<button class="btn small" data-act="tell" data-num="${p.num}" data-name="${esc(p.name)}">${esc(L('Написать'))}</button>`}
        <button class="btn small" data-act="kick" data-num="${p.num}" data-name="${esc(p.name)}">${esc(L('Выкинуть'))}</button>
        ${p.is_bot ? '' : `<button class="btn small danger" data-act="ban" data-num="${p.num}" data-name="${esc(p.name)}">${esc(L('Бан'))}</button>`}</div></td></tr>`).join('')
  }</tbody></table></div>` : '<div class="empty">' + esc(L('На сервере никого')) + '</div>';
  $('#pl-bans').innerHTML = d.bans.length ? `<table><thead><tr><th>#</th><th>${esc(L('Тип'))}</th><th>${esc(L('Адрес'))}</th><th></th></tr></thead><tbody>${
    d.bans.map(b => `<tr><td>${b.num}</td><td>${esc(b.kind === 'ban' ? L('бан') : L('исключение'))}</td><td class="mono">${esc(b.target)}</td>
      <td><button class="btn small" data-act="unban" data-num="${b.num}" data-kind="${b.kind}">${esc(L('Снять'))}</button></td></tr>`).join('')
  }</tbody></table>` : '<div class="empty">' + esc(L('Банов нет')) + '</div>';
}

const player = body => api('/api/players', body);
ACTIONS['players-reload'] = () => loadPlayers();
ACTIONS.say = async () => {
  const text = $('#say-text').value.trim(); if (!text) return;
  if (await act(() => player({ action: 'say', text }), L('Отправлено'))) $('#say-text').value = '';
};
ACTIONS.tell = async d => {
  const text = prompt(L('Личное сообщение для {0}:', d.name)); if (!text) return;
  await act(() => player({ action: 'tell', num: +d.num, text }), L('Отправлено'));
};
ACTIONS.kick = async d => {
  if (!confirm(L('Выкинуть {0}?', d.name))) return;
  await act(() => player({ action: 'kick', num: +d.num, name: d.name }), L('Выкинут'));
  loadPlayers();
};
ACTIONS.ban = async d => {
  if (!confirm(L('Забанить адрес игрока {0} и выкинуть его?', d.name))) return;
  await act(() => player({ action: 'ban', num: +d.num, name: d.name }), L('Забанен'));
  loadPlayers();
};
ACTIONS['ban-address'] = async () => {
  const address = $('#ban-addr').value.trim(); if (!address) return;
  if (!confirm(L('Забанить {0}?', address))) return;
  if (await act(() => player({ action: 'ban_address', address }), L('Адрес забанен'))) { $('#ban-addr').value = ''; loadPlayers(); }
};
ACTIONS.unban = async d => { await act(() => player({ action: 'unban', num: +d.num, kind: d.kind }), L('Снято')); loadPlayers(); };
ACTIONS.bots = async () => {
  const count = $('#bots-count').value; if (count === '') return;
  await act(() => player({ action: 'bots', count: +count }), L('Готово. Боты подстроятся за 10–20 секунд'));
};
ACTIONS.addbot = async () => {
  const name = $('#addbot-name').value.trim() || 'Sarge';
  await act(() => player({ action: 'addbot', name, skill: +$('#addbot-skill').value }), L('Бот добавлен'));
  setTimeout(loadPlayers, 1500);
};
ACTIONS.kickbots = async () => {
  if (!confirm(L('Убрать всех ботов? Если bot_minplayers больше нуля, они вернутся.'))) return;
  await act(() => player({ action: 'kickbots' }), L('Боты убраны')); loadPlayers();
};

// ============================================================ Карты

PAGES.maps = async main => {
  let d;
  try { d = await api('/api/maps'); } catch (e) { main.innerHTML = '<div class="error">' + esc(e.message) + '</div>'; return; }
  S.maps = d;
  S.rotEdit = {};
  S.rotAll = {};      // «все карты» в списке «Добавить» — по ротациям
  for (const [k, r] of Object.entries(d.rotations)) S.rotEdit[k] = r.maps.slice();
  resetModeEdit();
  const mode = d.mode;
  S.hasQueue = !!(d.modes.enabled.length && d.default);   // очередь режимов в server.cfg
  const where = !mode || !mode.key ? '' : mode.position && mode.on_track
    ? L('карта {0} из {1}', mode.position, mode.total)
    : L('карта загружена вручную');
  S.pick = null;
  // дальше по ротации или — после карты, загруженной вручную, — очередь с начала
  const nextBlock = !d.nextmap ? '' : `<div class="muted fs12 mt12">${esc(d.next_from_start
      ? L('Дальше — очередь режимов с начала: {0}', modeTitle(d.next_from_start)) : L('Дальше'))}</div>
    <div class="row">${hasShot(d.nextmap) ? shotImg(d.nextmap, 'thumb-s') : ''}<span class="mono">${mapLink(d.nextmap)}</span></div>`;
  const nowTitle = !mode ? '—' : mode.key ? modeTitle(mode.key) : gtTitle(d.gametype);
  main.innerHTML = `
    <h1>${esc(L('Карты и ротации'))}</h1>
    <div class="maps-top">
      <div class="maps-left">
        <div class="card"><h2>${esc(L('Сейчас играет'))}</h2>
          <div class="now-q3">
            ${d.current ? shotImg(d.current, 'now-shot') : '<span class="now-shot none">—</span>'}
            <div>
              <div class="big">${esc(nowTitle)}</div>
              <div class="mono">${d.current ? mapLink(d.current) : '—'}${where ? ' <span class="muted">· ' + esc(where) + '</span>' : ''}</div>
              ${nextBlock}
            </div>
          </div>
          <div class="row mt12"><button class="btn" data-act="map-next">${esc(L('Следующая'))}</button>
            <button class="btn" data-act="map-restart">${esc(L('Перезапустить'))}</button></div></div>
        <div class="card mode-card"><h2>${esc(L('Очередь режимов'))}</h2>
          ${Object.keys(d.rotations).length ? '' : '<div class="note">' + esc(L('В server.cfg нет ротаций в формате панели (set f1 "…", set start_ffa "vstr f1" и т. д.), поэтому очереди режимов и редактора ротаций нет. Как их завести — README, раздел «Rotations». Загрузка карты в любом режиме работает и без них.')) + '</div>'}
          <ol class="mode-list" id="mode-list"></ol>
          <div class="row mt12"><button class="btn small ghost" data-act="mode-reset">${esc(L('Отменить'))}</button>
            <span id="mode-dirty" class="dirty hidden">${esc(L('Есть несохранённые изменения'))}</span>
            <span class="grow"></span><button id="mode-save" class="btn small" data-act="mode-save">${esc(L('Сохранить очередь'))}</button></div>
          <p class="muted">${esc(L('«В ротации» — режимы идут сверху вниз и по кругу: сервер доиграл все карты одного и переходит к следующему. С первого он начинает после перезапуска. «Выключен» — сервер сам на него никогда не переключится; сыграть его можно только кнопкой «Включить сейчас». Изменения вступают в силу после «Сохранить очередь».'))}</p></div>
      </div>
      <div class="card pick-card"><h2>${esc(L('Загрузить карту'))} <span class="muted fs12">${esc(nPlural(d.available.length, 'карта', 'карты', 'карт'))}</span></h2>
        <p class="muted fs12 mb8">${esc(L('Нажмите на карту — панель спросит режим. Текущий матч прервётся.'))} ${esc(S.hasQueue
          ? L('После этой карты сервер пойдёт по очереди режимов с самого начала.') : L('После этой карты сервер пойдёт дальше так же, как шёл до неё.'))}</p>
        <div class="pick" id="pick"></div></div>
    </div>
    <div class="grid two mt16" id="rot-cards"></div>`;
  renderPick();
  renderModes();
  renderRotations();
};

/* Выбор карты картинкой: нажать — откроется окно с выбором режима. */
function renderPick() {
  const d = S.maps;
  $('#pick').innerHTML = d.available.map(m => `<button class="pick-m${m === d.current ? ' cur' : ''}"
      data-act="pick" data-map="${esc(m)}" title="${esc(arenaName(m) ? m + ' — ' + arenaName(m) : m)}">${hasShot(m)
      ? `<img src="${previewSrc(m)}" alt="" loading="lazy">` : '<span class="noimg">' + esc(L('нет картинки')) + '</span>'}
      <span class="pm">${esc(m)}</span></button>`).join('');
}

/* Описание карты из её .arena: длинное имя и режимы, на которые она рассчитана. */
const arenaName = m => ((S.maps.arenas || {})[m] || {}).name || '';
function arenaModes(m) {
  const a = (S.maps.arenas || {})[m];
  return a && a.modes.length ? a.modes : null;     // null — неизвестно (нет описания)
}

/* Режимы для выбора: сначала в порядке очереди, потом остальные из четырёх. */
function modeKeys() {
  const d = S.maps, keys = d.modes.order.filter(k => d.rotations[k]);
  for (const x of d.mode_list) if (!keys.includes(x.key)) keys.push(x.key);
  return keys;
}

/* Какой режим предложить для карты: где она есть в ротации (лучше — текущий режим),
   иначе текущий, если карта на него рассчитана, иначе первый подходящий. */
function suggestMode(m) {
  const d = S.maps, order = modeKeys();
  const gt = d.mode ? d.mode_list.find(x => String(x.gametype) === String(d.gametype)) : null;
  const cur = d.mode && d.mode.key ? d.mode.key : gt ? gt.key : null;
  const inRot = k => !!d.rotations[k] && d.rotations[k].maps.includes(m);
  const fits = k => { const a = arenaModes(m); return !a || a.includes(k) || inRot(k); };
  if (cur && inRot(cur)) return cur;
  return order.find(inRot) || (cur && fits(cur) ? cur : null) || order.find(fits) || cur || order[0];
}

ACTIONS.pick = d => {
  S.pick = d.map;
  S.pickMode = suggestMode(d.map);
  const name = arenaName(d.map), a = arenaModes(d.map);
  openModal(`<h2>${esc(L('Загрузить карту'))}</h2>
    <div class="load-head">
      ${shotImg(d.map, 'load-shot')}
      <div>
        <div class="big mono">${esc(d.map)}</div>
        ${name ? '<div class="muted">' + esc(name) + '</div>' : ''}
        <div class="muted fs12 mt8">${esc(a ? L('Авторы карты рассчитывали её на: {0}', a.map(modeTitle).join(', ')) : L('Описания режимов у этой карты нет.'))}</div>
      </div>
    </div>
    <h3 class="mt16">${esc(L('В каком режиме загрузить?'))}</h3>
    <div class="mode-pick" id="lm-modes"></div>
    <div class="note mt12" id="lm-note"></div>
    <div class="row"><span class="grow"></span>
      <button class="btn ghost" data-act="modal-close">${esc(L('Отмена'))}</button>
      <button class="btn primary" data-act="map-load" id="lm-go"></button></div>`);
  renderLoadChoice();
};
ACTIONS['lm-mode'] = d => { S.pickMode = d.k; renderLoadChoice(); };

function renderLoadChoice() {
  const d = S.maps, m = S.pick, k = S.pickMode, a = arenaModes(m);
  const gt = d.mode_list.find(x => String(x.gametype) === String(d.gametype));
  const cur = d.mode && d.mode.key ? d.mode.key : gt ? gt.key : null;
  $('#lm-modes').innerHTML = modeKeys().map(x => {
    const r = d.rotations[x], hints = [];
    if (x === cur) hints.push('<span class="h">' + esc(L('сейчас идёт')) + '</span>');
    if (r && r.maps.includes(m)) hints.push('<span class="h ok">' + esc(L('есть в его ротации')) + '</span>');
    else if (a && !a.includes(x)) hints.push('<span class="h warn">' + esc(x === 'c' ? L('на карте нет флагов') : L('карта не для этого режима')) + '</span>');
    else if (a) hints.push('<span class="h">' + esc(L('карта подходит')) + '</span>');
    return `<button class="mode-opt${x === k ? ' sel' : ''}" data-act="lm-mode" data-k="${x}">
      <span class="t">${esc(modeTitle(x))}</span>${hints.join('')}</button>`;
  }).join('');
  const first = d.modes.enabled[0], fr = d.rotations[first];
  const bad = a && !a.includes(k) && !(d.rotations[k] && d.rotations[k].maps.includes(m));
  $('#lm-note').innerHTML = esc(L('Будет: {0} в режиме «{1}». Текущий матч прервётся.', m, modeTitle(k))) + ' '
    + esc(S.hasQueue && fr ? L('После этой карты сервер вернётся к обычной очереди режимов с самого начала — «{0}», карта {1}.', modeTitle(first), fr.maps[0])
      : L('После этой карты сервер пойдёт дальше так же, как шёл до неё (по своему nextmap).'))
    + (bad ? '<br><b>' + esc(L('Внимание:')) + '</b> ' + esc(k === 'c'
      ? L('на этой карте нет флагов — захватывать будет нечего, получится обычный командный бой.')
      : L('авторы не рассчитывали карту на этот режим — играть можно, но точки появления могут быть неудачными.')) : '');
  $('#lm-go').textContent = L('Загрузить в режиме «{0}»', modeTitle(k));
}

/* Есть ли в очереди режимов несохранённые изменения. */
function modesDirty() {
  const m = S.maps.modes, e = S.modeEdit;
  return m.order.join() !== e.order.join() || [...m.enabled].sort().join() !== [...e.enabled].sort().join();
}

function resetModeEdit() {
  const m = S.maps.modes;
  S.modeEdit = { order: m.order.slice(), enabled: new Set(m.enabled) };
}

function renderModes() {
  const d = S.maps, e = S.modeEdit, cur = d.mode ? d.mode.key : null;
  const chain = e.order.filter(k => e.enabled.has(k));
  $('#mode-list').innerHTML = e.order.map((k, i) => {
    const r = d.rotations[k], on = e.enabled.has(k), pos = chain.indexOf(k);
    const tags = [];
    if (k === cur) tags.push(pill(L('играет сейчас'), 'ok'));
    if (pos === 0) tags.push(pill(L('при запуске'), ''));
    if (on && chain.length > 1) tags.push('<span class="muted">→ ' + esc(modeTitle(chain[(pos + 1) % chain.length])) + '</span>');
    return `<li class="${k === cur ? 'current' : ''} ${on ? '' : 'off'}">
      <span class="n">${on ? pos + 1 : '—'}</span>
      <button class="toggle ${on ? 'on' : ''}" data-act="mode-toggle" data-k="${k}"
        title="${esc(on ? L('Нажмите, чтобы убрать режим из ротации') : L('Нажмите, чтобы вернуть режим в ротацию'))}">${esc(on ? L('В ротации') : L('Выключен'))}</button>
      <span class="grow mode-name">${esc(modeTitle(k))} <span class="muted">· ${esc(nPlural(r.maps.length, 'карта', 'карты', 'карт'))}</span> ${tags.join(' ')}</span>
      <button class="btn small ghost" data-act="mode-move" data-k="${k}" data-d="-1" ${i ? '' : 'disabled'}>↑</button>
      <button class="btn small ghost" data-act="mode-move" data-k="${k}" data-d="1" ${i < e.order.length - 1 ? '' : 'disabled'}>↓</button>
      <button class="btn small" data-act="mode" data-rot="${k}">${esc(L('Включить сейчас'))}</button></li>`;
  }).join('');
  const dirty = modesDirty();
  $('#mode-dirty').classList.toggle('hidden', !dirty);
  $('#mode-save').classList.toggle('primary', dirty);
}

ACTIONS['mode-toggle'] = d => {
  const en = S.modeEdit.enabled;
  if (en.has(d.k)) {
    if (en.size === 1) { toast(L('Хотя бы один режим должен остаться в ротации'), true); return; }
    en.delete(d.k);
  } else en.add(d.k);
  renderModes();
};

ACTIONS['mode-move'] = d => {
  const o = S.modeEdit.order, i = o.indexOf(d.k), j = i + +d.d;
  [o[i], o[j]] = [o[j], o[i]]; renderModes();
};
ACTIONS['mode-reset'] = () => { resetModeEdit(); renderModes(); };
ACTIONS['mode-save'] = async () => {
  const e = S.modeEdit;
  if (!e.enabled.size) { toast(L('Включите хотя бы один режим'), true); return; }
  const r = await act(() => api('/api/modes', { order: e.order, enabled: [...e.enabled] }),
    x => L('Очередь сохранена, копия {0}.', x.backup) + ' ' + (x.applied_live ? L('Сервер уже знает новую очередь — карта не менялась.') : L('Применится после перезапуска сервера.')));
  if (r) go('maps', true);
};

function renderRotations() {
  const d = S.maps;
  $('#rot-cards').innerHTML = Object.entries(d.rotations).map(([k, r]) => {
    const maps = S.rotEdit[k];
    // подсвечиваем карту только там, где она реально играет: в текущем режиме и на
    // её месте в ротации, а не везде, где встречается то же имя
    const playing = d.mode && d.mode.on_track && d.mode.key === k ? d.mode.position - 1 : -1;
    const off = !d.modes.enabled.includes(k);
    return `<div class="card${off ? ' rot-off' : ''}"><h2>${esc(modeTitle(k))} ${off ? pill(L('выключен — не участвует в ротации'), 'warn') : ''}
        <span class="muted mono fs12">${esc(r.head)}</span></h2>
      <ol class="rot-list">${maps.map((m, i) => `<li class="${i === playing ? 'current' : ''}"><span class="n">${i + 1}</span>${shotImg(m, 'rot-thumb')}<span class="m">${mapLink(m)}</span>${i === playing ? ' ' + pill(L('сейчас'), 'ok') : ''}
        <button class="btn small ghost" data-act="rot-move" data-rot="${k}" data-i="${i}" data-d="-1" ${i ? '' : 'disabled'}>↑</button>
        <button class="btn small ghost" data-act="rot-move" data-rot="${k}" data-i="${i}" data-d="1" ${i < maps.length - 1 ? '' : 'disabled'}>↓</button>
        <button class="btn small ghost" data-act="rot-del" data-rot="${k}" data-i="${i}" ${maps.length > 1 ? '' : 'disabled'}>✕</button></li>`).join('')}</ol>
      <div class="row"><select id="rot-add-${k}">${d.available.filter(m => S.rotAll[k] || mapFits(k, m)).map(m =>
          `<option value="${esc(m)}">${esc(m)}${arenaName(m) ? ' — ' + esc(arenaName(m)) : ''}</option>`).join('')}</select>
        <button class="btn small" data-act="rot-add" data-rot="${k}">${esc(L('Добавить'))}</button>
        <label class="row muted" title="${esc(L('Показать в списке и карты, которые авторы не рассчитывали на этот режим'))}">
          <input type="checkbox" data-act="rot-all" data-rot="${k}"${S.rotAll[k] ? ' checked' : ''}> ${esc(L('все карты'))}</label>
        <span class="grow"></span>
        <button class="btn small ghost" data-act="rot-reset" data-rot="${k}">${esc(L('Отменить'))}</button>
        <button class="btn small primary" data-act="rot-save" data-rot="${k}">${esc(L('Сохранить'))}</button></div></div>`;
  }).join('');
}

ACTIONS['map-next'] = async () => { await act(() => api('/api/maps', { action: 'next' }), L('Переключаю на следующую карту')); };
ACTIONS['map-restart'] = async () => { await act(() => api('/api/maps', { action: 'restart' }), L('Карта перезапущена')); };
ACTIONS['map-load'] = async () => {
  const map = S.pick, rotation = S.pickMode;
  if (!map || !rotation) return;
  $('#lm-go').disabled = true;
  if (await act(() => api('/api/maps', { action: 'map', map, rotation }), L('Загружаю {0} — {1}', map, modeTitle(rotation)))) {
    closeModal(true);
    setTimeout(() => go('maps', true), 2500);
  } else $('#lm-go').disabled = false;
};
ACTIONS.mode = async d => {
  if (!confirm(L('Переключить режим? Текущий матч прервётся.'))) return;
  await act(() => api('/api/maps', { action: 'mode', rotation: d.rot }), L('Режим переключён'));
  setTimeout(() => go('maps', true), 2500);
};
ACTIONS['rot-move'] = d => {
  const a = S.rotEdit[d.rot], i = +d.i, j = i + +d.d;
  [a[i], a[j]] = [a[j], a[i]]; renderRotations();
};
ACTIONS['rot-del'] = d => { S.rotEdit[d.rot].splice(+d.i, 1); renderRotations(); };
/* Подходит ли карта режиму — по её описанию в игре (scripts/*.arena), как в меню
   игры. Без описания (test_bigbox) — в любой режим, кроме захвата флага. */
function mapFits(k, m) {
  const a = arenaModes(m);
  return a ? a.includes(k) : k !== 'c';
}
ACTIONS['rot-all'] = d => { S.rotAll[d.rot] = !S.rotAll[d.rot]; renderRotations(); };
ACTIONS['rot-add'] = d => { S.rotEdit[d.rot].push($('#rot-add-' + d.rot).value); renderRotations(); };
ACTIONS['rot-reset'] = d => { S.rotEdit[d.rot] = S.maps.rotations[d.rot].maps.slice(); renderRotations(); };
ACTIONS['rot-save'] = async d => {
  const r = await act(() => api('/api/rotation', { rotation: d.rot, maps: S.rotEdit[d.rot] }),
    x => L('Сохранено, копия {0}.', x.backup) + ' ' + (x.applied_live ? L('Сервер уже знает новую ротацию.') : L('На сервере применится после перезапуска.')));
  if (r) go('maps', true);
};

// ============================================================ Настройки сервера (server.cfg)

PAGES.settings = async main => {
  let d;
  try { d = await api('/api/settings'); } catch (e) { main.innerHTML = '<div class="error">' + esc(e.message) + '</div>'; return; }
  const groups = {};
  for (const s of d.settings) (groups[s.section || L('Прочее')] ||= []).push(s);
  main.innerHTML = `<h1>${esc(L('Настройки сервера'))}</h1>
    <div class="note">${esc(L('Это server.cfg. Перед каждой записью панель сохраняет копию файла. Большинство настроек применяется сразу; отмеченные — после перезапуска или смены карты.'))}</div>
    ${Object.entries(groups).map(([g, items]) => `<div class="card"><h2>${esc(g)}</h2><div class="tbl-wrap"><table>
      <thead><tr><th>${esc(L('Настройка'))}</th><th>${esc(L('В файле'))}</th><th>${esc(L('На сервере'))}</th><th></th></tr></thead><tbody>${
      items.map(s => `<tr><td><span class="mono">${esc(s.name)}</span>${s.comment ? '<div class="muted">' + esc(s.comment) + '</div>' : ''}
        ${s.restart_note ? '<div>' + pill(Lsrv(s.restart_note), 'warn') + '</div>' : ''}</td>
        <td><input data-setting="${esc(s.name)}" ${s.secret ? 'type="password" placeholder="' + esc(L('скрыто — введите новое')) + '"' : ''} value="${esc(s.value)}"></td>
        <td class="mono">${s.secret ? '•••' : esc(s.live ?? '—')}</td>
        <td><button class="btn small" data-act="setting-save" data-name="${esc(s.name)}">${esc(L('Сохранить'))}</button></td></tr>`).join('')
    }</tbody></table></div></div>`).join('')}
    <div class="card"><h2>${esc(L('Резервные копии'))}</h2>${d.backups.length
      ? '<div class="mono">' + d.backups.map(esc).join('<br>') + '</div><p class="muted">' + esc(L('Лежат в data/cfg-backups рядом с панелью.')) + '</p>'
      : '<div class="empty">' + esc(L('Пока ни одной правки')) + '</div>'}</div>`;
};

ACTIONS['setting-save'] = async d => {
  const input = document.querySelector('input[data-setting="' + CSS.escape(d.name) + '"]');
  const value = input.value;
  if (input.type === 'password' && !value) { toast(L('Введите новое значение'), true); return; }
  const r = await act(() => api('/api/settings', { name: d.name, value }),
    x => L('{0} сохранена (копия {1})', d.name, x.backup) + (x.applied_live ? L(', применена на сервере') : x.note ? '. ' + Lsrv(x.note) : ''));
  if (r) go('settings', true);
};

// ============================================================ Статистика

PAGES.stats = async main => {
  let d, p;
  try {
    // часовой пояс браузера — чтобы часы и дни были по вашим часам, а не по серверным
    [d, p] = await Promise.all([api('/api/stats?tz=' + new Date().getTimezoneOffset()), api('/api/sessions')]);
  } catch (e) { main.innerHTML = '<div class="error">' + esc(e.message) + '</div>'; return; }
  S.stats = d;
  S.sessions = p;
  if (!S.calMonth) { const n = new Date(); S.calMonth = { y: n.getFullYear(), m: n.getMonth() }; }
  const t = d.totals;
  const tabs = [['humans', L('Живые игроки')], ['summary', L('Сводка по живым')], ['calendar', L('Календарь')],
    ['bots', L('Боты')], ['maps', L('Карты')], ['activity', L('Активность')]];
  main.innerHTML = `<h1>${esc(L('Статистика'))}</h1>
    <div class="grid">
      <div class="card"><div class="muted">${esc(L('Матчей'))}</div><div class="big">${num(t.matches)}</div></div>
      <div class="card"><div class="muted">${esc(L('Убийств'))}</div><div class="big">${num(t.kills)}</div></div>
      <div class="card"><div class="muted">${esc(L('Сыграно'))}</div><div class="big">${esc(fmtDur(t.seconds))}</div></div>
      <div class="card"><div class="muted">${esc(L('Живых игроков'))}</div><div class="big">${num(t.humans)}</div><div class="muted">${esc(L('были в {0} матчах', num(t.matches_with_humans)))}</div></div>
    </div>
    <div class="tabs" id="st-tabs">${tabs.map(([k, v]) => `<button data-act="stats-tab" data-tab="${k}">${esc(v)}</button>`).join('')}</div>
    <div id="st-body"></div>
    <p class="muted">${esc(L('Лог разобран {0} · обновляется раз в минуту', fmtTime(d.state.parsed_at)))}
      <button class="btn small ghost" data-act="stats-refresh">${esc(L('обновить сейчас'))}</button></p>`;
  renderStatsTab();
};

ACTIONS['stats-tab'] = d => { S.statsTab = d.tab; renderStatsTab(); };
ACTIONS['stats-refresh'] = async () => {
  if (await act(() => api('/api/stats/refresh', {}), x => L('Добавлено матчей: {0}', x.added))) go('stats', true);
};

function playerTable(rows) {
  if (!rows.length) return '<div class="empty">' + esc(L('Пока никого')) + '</div>';
  return `<div class="tbl-wrap"><table><thead><tr><th>${esc(L('Игрок'))}</th><th class="num">${esc(L('Матчей'))}</th><th class="num">${esc(L('Убийств'))}</th>
    <th class="num">${esc(L('Смертей'))}</th><th class="num">${esc(L('У/С'))}</th><th class="num">${esc(L('Самоубийств'))}</th><th class="num">${esc(L('В игре'))}</th></tr></thead><tbody>${
    rows.map(p => `<tr><td>${esc(p.name)}</td><td class="num">${num(p.matches)}</td><td class="num">${num(p.kills)}</td>
      <td class="num">${num(p.deaths)}</td><td class="num">${dec(p.kd)}</td><td class="num">${num(p.suicides)}</td>
      <td class="num">${esc(fmtDur(p.playtime))}</td></tr>`).join('')
  }</tbody></table></div>`;
}

const hourLabels = () => Array.from({ length: 24 }, (_, i) => '<span>' + i + '</span>').join('');

function renderStatsTab() {
  const d = S.stats, tab = S.statsTab, body = $('#st-body');
  document.querySelectorAll('#st-tabs button').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
  if (tab === 'humans') {
    const P = S.sessions;
    body.innerHTML = `<div class="grid split">
        <div class="card"><h2>${esc(L('Заходы живых игроков'))} <span class="muted fs12">${esc(L('точное время'))}</span></h2>${sessionsTable(P.sessions.filter(s => !s.approx), 200)}</div>
        <div class="card"><h2>${esc(L('Итоги по живым игрокам'))}</h2>${humansTable(P.players)}
          <p class="muted">${esc(L('Игрок — это IP-адрес: ники бывают одинаковые (например, «UnnamedPlayer» у всех, кто не сменил имя). Нажмите на ник — откроется его статистика.'))} ${esc(ipNote())}</p></div>
      </div>`;
  } else if (tab === 'summary') {
    renderSummary(body);
  } else if (tab === 'calendar') {
    renderCalendar(body);
  } else if (tab === 'bots') {
    body.innerHTML = `<div class="grid two">
      <div class="card"><h2>${esc(L('Боты'))}</h2>${playerTable(d.bots)}</div>
      <div class="card"><h2>${esc(L('Оружие ботов'))}</h2><div class="bars" id="w-b"></div>
        <p class="muted">${esc(L('Серым — смерти не от оружия: ловушки, лава, падения.'))}</p></div></div>`;
    bars($('#w-b'), d.weapons_bots.map(w => ({ ...w, title: Lsrv(w.title) })), 'title', 'kills', { muted: w => w.environment });
  } else if (tab === 'maps') {
    body.innerHTML = `<div class="grid two"><div class="card"><h2>${esc(L('Сколько раз играли'))}</h2><div class="bars" id="m-b"></div></div>
      <div class="card"><h2>${esc(L('Подробно'))}</h2><div class="tbl-wrap"><table><thead><tr><th>${esc(L('Карта'))}</th><th class="num">${esc(L('Матчей'))}</th>
        <th class="num">${esc(L('С людьми'))}</th><th class="num">${esc(L('Убийств'))}</th><th class="num">${esc(L('Средний матч'))}</th></tr></thead><tbody>${
        d.maps.map(m => `<tr><td class="mono">${mapLink(m.map)}</td><td class="num">${num(m.matches)}</td><td class="num">${num(m.with_humans)}</td>
          <td class="num">${num(m.kills)}</td><td class="num">${esc(fmtDur(m.avg_duration))}</td></tr>`).join('')
      }</tbody></table></div></div></div>
      <div class="card mt16"><h2>${esc(L('Последние матчи'))}</h2>${d.recent.length ? `<div class="tbl-wrap"><table class="compact"><thead><tr>
        <th>${esc(L('Начало'))}</th><th>${esc(L('Карта'))}</th><th>${esc(L('Режим'))}</th><th class="num">${esc(L('Длился'))}</th><th class="num">${esc(L('Людей'))}</th><th class="num">${esc(L('Ботов'))}</th>
        <th class="num">${esc(L('Убийств'))}</th><th>${esc(L('Чем кончился'))}</th></tr></thead><tbody>${
        d.recent.map(m => `<tr><td class="nowrap">${m.started_at ? approxMark({ approx: m.started_src === 'fill' }) + esc(fmtDateTime(m.started_at)) : '—'}</td><td class="mono">${mapLink(m.map)}</td>
          <td>${esc(gtTitle(m.gametype))}</td><td class="num nowrap">${esc(fmtDur(m.duration))}</td>
          <td class="num">${m.humans}</td><td class="num">${m.bots}</td><td class="num">${num(m.kills)}</td>
          <td class="muted">${esc(exitTitle(m.exit_reason))}</td></tr>`).join('')
      }</tbody></table></div>` : '<div class="empty">' + esc(L('Пока ни одного законченного матча')) + '</div>'}
      <p class="muted">${esc(L('Время начала — по логу консоли сервера (время запуска) или по тому, что панель видела сама; «≈» — достроено между точными по длительностям матчей. Часы — по часам этого компьютера. Наведите на карту — покажется её картинка.'))}</p></div>`;
    bars($('#m-b'), d.maps, 'map', 'matches', { mapPreview: true });
  } else {
    const a = d.activity;
    body.innerHTML = `<div class="note">${esc(L('В игровом логе нет календарных дат — только время от начала карты. Время матчей восстановлено по логу консоли (к моменту запуска сервера прибавлены длительности матчей) и по началам матчей, которые панель видела сама; «≈» — достроено между ними.'))}
      ${esc(L('С датой: {0} из {1} матчей. Часы — по часам этого компьютера.', num(a.stamped), num(d.totals.matches)))}</div>
      <div class="card"><h2>${esc(L('Матчи по часам суток'))}</h2><div class="hours" id="hrs"></div>
        <div class="hours-labels">${hourLabels()}</div></div>
      <div class="grid two mt16">
        <div class="card"><h2>${esc(L('Когда заходят живые игроки'))}</h2><div class="hours" id="hrs-h"></div>
          <div class="hours-labels">${hourLabels()}</div></div>
        <div class="card"><h2>${esc(L('По дням'))}</h2><div class="bars" id="days"></div></div></div>`;
    hoursChart($('#hrs'), a.hours);
    hoursChart($('#hrs-h'), a.human_hours);
    bars($('#days'), a.days.map(x => ({ ...x, day: /^\d{4}-\d\d-\d\d$/.test(x.day) ? new Date(x.day + 'T12:00:00').toLocaleDateString(LOCALE) : x.day })), 'day', 'matches');
  }
}

/* Сводка по живым игрокам: оружие, страны, рекорды и ниже — карты, режимы,
   сколько длятся заходы и какой у игроков пинг. */
function renderSummary(body) {
  const d = S.stats, P = S.sessions;
  // заходы, где игрок действительно играл: закончились и не «не вошёл в игру»
  const played = P.sessions.filter(s => !s.online && !(s.best_ping >= 999));
  const notIn = P.sessions.filter(s => s.best_ping >= 999).length;
  const bucket = (rows, edges, labels, val) => labels.map((title, i) => ({
    title, n: rows.filter(r => { const v = val(r); return v >= edges[i] && v < edges[i + 1]; }).length }));
  const lengths = bucket(played, [0, 60, 300, 900, 1800, 3600, Infinity],
    [L('до 1 мин'), L('1–5 мин'), L('5–15 мин'), L('15–30 мин'), L('30–60 мин'), L('больше часа')], s => sessEnd(s) - s.joined_at);
  const pinged = P.sessions.filter(s => s.best_ping > 0 && s.best_ping < 999);
  const pings = bucket(pinged, [0, 50, 100, 150, 250, Infinity],
    [L('до 50 мс'), L('50–100 мс'), L('100–150 мс'), L('150–250 мс'), L('больше 250 мс')], s => s.best_ping);
  const visits = n => nPlural(n, 'заход', 'захода', 'заходов');
  const share = (n, all) => all ? ' · ' + Math.round(100 * n / all) + '%' : '';
  const matchesKills = x => nPlural(x.matches, 'матч', 'матча', 'матчей') + ' · ' + nPlural(x.kills, 'убийство', 'убийства', 'убийств');
  body.innerHTML = `<div class="grid three">
      <div class="card"><h2>${esc(L('Оружие живых игроков'))}</h2><div class="bars" id="w-h"></div></div>
      <div class="card"><h2>${esc(L('Откуда игроки'))}</h2><div class="bars" id="c-h"></div>
        <p class="muted">${esc(L('Страна — по IP-адресу, из локальной базы на сервере (адреса никуда не отправляются).'))}</p></div>
      <div class="card"><h2>${esc(L('Рекорды'))}</h2>${recordsList(P.records)}</div>
    </div>
    <div class="grid two">
      <div class="card"><h2>${esc(L('Карты живых игроков'))}</h2><div class="bars wide-val" id="s-maps"></div>
        <p class="muted">${esc(L('Сколько времени живые игроки провели на каждой карте — по игровому логу, за всё время. Наведите на карту — покажется картинка.'))}</p></div>
      <div class="card"><h2>${esc(L('Режимы живых игроков'))}</h2><div class="bars wide-val" id="s-modes"></div>
        <p class="muted">${esc(L('Время живых игроков по режимам, за всё время. Наведите на строку — матчи и убийства.'))}</p></div>
    </div>
    <div class="grid two">
      <div class="card"><h2>${esc(L('Сколько длится заход'))}</h2><div class="bars wide-val" id="s-len"></div>
        <p class="muted">${esc(L('Все законченные заходы живых игроков.'))}${notIn ? ' ' + esc(L('Ещё {0} — «не вошёл в игру», они не считаются.', visits(notIn))) : ''}
          ${esc(L('Много коротких — люди заходят и сразу уходят.'))}</p></div>
      <div class="card"><h2>${esc(L('Пинг живых игроков'))}</h2><div class="bars wide-val" id="s-ping"></div>
        <p class="muted">${esc(L('Лучший пинг за заход — по опросу сервера, с первого запуска панели. Больше 150 мс играть уже тяжело.'))}</p></div>
    </div>`;
  bars($('#w-h'), d.weapons_humans.map(w => ({ ...w, title: Lsrv(w.title) })), 'title', 'kills', { muted: w => w.environment });
  bars($('#c-h'), P.countries.map(c => ({ ...c, title: countryName(c) || c.title })), 'title', 'players', { flags: true });
  bars($('#s-maps'), d.human_maps, 'map', 'playtime', { mapPreview: true, fmt: m => fmtDur(m.playtime), tip: matchesKills });
  bars($('#s-modes'), d.human_modes.map(g => ({ ...g, title: gtTitle(g.gametype) })),
    'title', 'playtime', { fmt: g => fmtDur(g.playtime), tip: matchesKills });
  bars($('#s-len'), lengths, 'title', 'n', { fmt: b => visits(b.n) + share(b.n, played.length) });
  bars($('#s-ping'), pings, 'title', 'n', { fmt: b => visits(b.n) + share(b.n, pinged.length) });
}

// ============================================================ заходы, календарь

function trackingNote() {
  const t = S.sessions.tracking_since;
  return t ? L('Точное время заходов панель отмечает с {0}. Более ранние заходы восстановлены по игровому логу и помечены «≈».', fmtDateTime(t)) : '';
}

/* Все заходы для календаря: сервер уже сложил точные (панель видела сама) и
   восстановленные по игровому логу — только те, что были до начала точного учёта. */
function allVisits() { return S.sessions.sessions; }

function ipNote() {
  const t = S.sessions.ip_since;
  return (t ? L('IP-адреса панель записывает с {0}.', fmtDateTime(t)) : L('IP-адреса панель записывает с первого живого игрока (пока ни одного).'))
    + ' ' + L('У более ранних заходов адреса нет — они сгруппированы по нику и помечены «без адреса».');
}

function sessionsTable(rows, limit) {
  if (!rows.length) return '<div class="empty">' + esc(L('Пока никто из живых игроков не заходил.')) + ' ' + esc(trackingNote()) + '</div>';
  return `<div class="tbl-wrap"><table><thead><tr><th>${esc(L('Зашёл'))}</th><th class="num">${esc(L('Пробыл'))}</th><th>${esc(L('Игрок'))}</th>
    <th>${esc(L('IP · страна'))}</th><th>${esc(L('Карты'))}</th><th class="num">${esc(L('У / С'))}</th></tr></thead><tbody>${
    rows.slice(0, limit).map(s => `<tr><td class="nowrap">${approxMark(s)}${esc(fmtDateTime(s.joined_at))}</td>
      <td class="num nowrap">${s.online ? pill(L('на сервере'), 'ok') : esc(fmtDur(sessEnd(s) - s.joined_at))}${
        s.best_ping >= 999 ? '<div class="muted fs12" title="' + esc(L('Всё время подключался (пинг 999) и в игру так и не вошёл')) + '">' + esc(L('не вошёл в игру')) + '</div>' : ''}</td>
      <td>${playerBtn(s.key, s.name)}</td>
      <td class="nowrap">${s.ip ? flagHtml(s.country) + ' <span class="mono">' + esc(s.ip) + '</span>' : noAddr()}</td>
      <td class="mono">${s.maps.map(mapLink).join(', ')}</td><td class="num">${s.kills} / ${s.deaths}</td></tr>`).join('')
  }</tbody></table></div><p class="muted">${esc(trackingNote())}</p>`;
}

function humansTable(rows) {
  if (!rows || !rows.length) return '<div class="empty">' + esc(L('Пока никого')) + '</div>';
  return `<div class="tbl-wrap"><table><thead><tr><th>${esc(L('Игрок'))}</th><th>IP</th><th>${esc(L('Страна'))}</th>
    <th class="num">${esc(L('Заходов'))}</th><th class="num">${esc(L('В игре'))}</th><th class="num">${esc(L('У/С'))}</th></tr></thead><tbody>${
    rows.map(p => `<tr><td>${playerBtn(p.key, p.name)}${p.online ? ' ' + pill(L('сейчас'), 'ok') : ''}
        ${p.names.length > 1 ? '<div class="muted fs12">' + esc(L('ещё: {0}', p.names.slice(1).join(', '))) + '</div>' : ''}</td>
      <td class="mono">${p.ip ? esc(p.ip) : noAddr()}</td><td class="nowrap">${countryTag(p.country)}</td>
      <td class="num">${num(p.visits)}</td><td class="num nowrap">${esc(fmtDur(p.seconds))}</td>
      <td class="num" title="${esc(L('{0} убийств / {1} смертей', p.kills, p.deaths))}">${dec(p.kd)}</td></tr>`).join('')
  }</tbody></table></div>`;
}

function recordsList(rows) {
  if (!rows || !rows.length) return '<div class="empty">' + esc(L('Пока нет данных')) + '</div>';
  const val = v => typeof v === 'object' ? esc(v.times != null ? times(v.times) : fmtDur(v.duration))
    : esc(v !== '' && isFinite(Number(v)) ? dec(Number(v)) : v);
  return '<ul class="recs">' + rows.map(r => `<li><div class="muted fs12">${esc(Lsrv(r.title))}</div>
    <div><b>${val(r.value)}</b>${r.name ? ' — ' + playerBtn(r.key, r.name) : ''}${r.at
      ? ' <span class="muted">· ' + approxMark(r) + esc(fmtDateTime(r.at)) + '</span>' : ''}</div></li>`).join('') + '</ul>';
}

/* Карточка игрока — окно поверх страницы.
   Плиток 10 — ровно два ряда по 5 (на телефоне по 2). Дальше: оружие | карты,
   режимы | где играет лучше | дни недели, когда заходит, последние заходы. */
ACTIONS.player = async d => {
  let c;
  try { c = await api('/api/player?key=' + encodeURIComponent(d.key)); } catch (e) { toast(e.message, true); return; }
  const hours = Array(24).fill(0), week = Array(7).fill(0), days = new Set();
  for (const [t, dur] of c.spans) {
    const dt = new Date(t * 1000);
    hours[dt.getHours()]++;
    week[(dt.getDay() + 6) % 7] += dur;          // понедельник — первый
    days.add(dayKey(dt));
  }
  const kpi = (label, value, tip, sub) => '<div class="kpi"' + (tip ? ' title="' + esc(tip) + '"' : '') + '><div class="muted">'
    + esc(label) + '</div><div class="kv-big">' + esc(value) + '</div>' + (sub ? '<div class="muted fs12">' + esc(sub) + '</div>' : '') + '</div>';
  const byNick = '<div class="muted fs12 mt8">' + esc(L('по нику — статистика игрового лога ведётся по нику')) + '</div>';
  const m = openModal(`
    <h2>${esc(c.name)} ${c.online ? pill(L('на сервере'), 'ok') : ''}</h2>
    ${c.names.length > 1 ? '<div class="muted">' + esc(c.ip ? L('Ники с этого адреса: {0}', c.names.join(', ')) : L('Ники этого игрока: {0}', c.names.join(', '))) + '</div>' : ''}
    <dl class="kv mt12"><dt>IP</dt><dd class="mono">${c.ip ? esc(c.ip) : '<span class="muted">' + esc(L('без адреса — заходы до запуска панели, сгруппированы по нику')) + '</span>'}</dd>
      <dt>${esc(L('Страна'))}</dt><dd>${countryTag(c.country)}</dd>
      <dt>${esc(L('Первый заход'))}</dt><dd>${esc(fmtDateTime(c.first))}</dd>
      <dt>${esc(L('Последний'))}</dt><dd>${esc(c.online ? L('сейчас на сервере') : fmtDateTime(c.last))}</dd></dl>
    <div class="kpis five mt12">${kpi(L('Заходов'), num(c.visits))}${kpi(L('Всего в игре'), fmtDur(c.seconds))}${kpi(L('Средний заход'), fmtDur(c.avg))}
      ${kpi(L('Убийств / смертей'), num(c.kills) + ' / ' + num(c.deaths))}${kpi(L('У/С'), dec(c.kd))}
      ${kpi(L('Лучший заход'), L('{0} уб.', c.best.kills), '', fmtDate(c.best.at))}
      ${kpi(L('Самый долгий заход'), fmtDur(c.longest.duration), '', fmtDate(c.longest.at))}
      ${kpi(L('Убийств в час'), c.kph == null ? '—' : dec(c.kph), L('по заходам, где известны убийства и смерти'))}
      ${kpi(L('Дней на сервере'), num(days.size))}
      ${kpi(L('Место по времени'), c.rank ? L('{0} из {1}', c.rank.pos, c.rank.of) : '—', L('среди всех живых игроков — по времени в игре'))}</div>
    <div class="duo mt16">
      <div><h3>${esc(L('Любимое оружие'))}</h3><div class="bars" id="pc-w"></div>${c.weapons.length ? byNick : ''}</div>
      <div><h3>${esc(L('Любимые карты'))}</h3><div class="bars" id="pc-m"></div></div>
    </div>
    <div class="trio mt16">
      <div><h3>${esc(L('Режимы'))}</h3><div class="bars lbl-m" id="pc-g"></div></div>
      <div><h3>${esc(L('Где играет лучше'))}</h3><div class="bars lbl-m" id="pc-k"></div></div>
      <div><h3>${esc(L('Дни недели'))}</h3><div class="bars lbl-s" id="pc-d"></div></div>
    </div>
    <div class="muted fs12 mt8">${esc(L('Режимы и «где играет лучше» (У/С на картах, где набралось от 10 убийств и смертей) — по нику, из игрового лога. Дни недели — время в игре по дню захода, по часам вашего компьютера.'))}</div>
    <h3 class="mt16">${esc(L('Когда заходит'))}</h3><div class="hours small" id="pc-h"></div>
    <div class="hours-labels">${hourLabels()}</div>
    <h3 class="mt16">${esc(L('Последние заходы'))}</h3><div class="tbl-wrap"><table><thead><tr><th>${esc(L('Зашёл'))}</th><th class="num">${esc(L('Пробыл'))}</th>
      <th>${esc(L('Ник'))}</th><th>${esc(L('Карты'))}</th><th class="num">${esc(L('У / С'))}</th></tr></thead><tbody>${c.recent.map(r => `<tr><td class="nowrap">${approxMark(r)}${esc(fmtDateTime(r.joined_at))}</td>
      <td class="num">${r.online ? pill(L('сейчас'), 'ok') : esc(fmtDur(r.duration))}</td><td>${esc(r.name)}</td>
      <td class="mono">${r.maps.map(mapLink).join(', ')}</td><td class="num">${r.kills} / ${r.deaths}</td></tr>`).join('')}</tbody></table></div>
    ${c.same_names_from.length ? '<div class="note mt12">' + esc(L('Этим же ником заходили и под другим ключом: {0} — возможно, это другой человек.',
      c.same_names_from.map(k => (k.startsWith('~') ? L('без адреса') : k)).join(', '))) + '</div>' : ''}`, 'wide');
  hoursChart(m.querySelector('#pc-h'), hours);
  bars(m.querySelector('#pc-w'), c.weapons.map(w => ({ ...w, title: Lsrv(w.title) })), 'title', 'kills', { fmt: w => L('{0} уб.', num(w.kills)) });
  bars(m.querySelector('#pc-m'), c.maps, 'map', 'visits', { mapPreview: true, fmt: x => times(x.visits) });
  bars(m.querySelector('#pc-g'), c.modes.map(g => ({ ...g, title: gtTitle(g.gametype) })),
    'title', 'playtime', { fmt: g => fmtDur(g.playtime), tip: g => nPlural(g.matches, 'матч', 'матча', 'матчей') + ' · ' + L('{0} уб. / {1} смертей', num(g.kills), num(g.deaths)) });
  bars(m.querySelector('#pc-k'), c.map_kd, 'map', 'kd', { mapPreview: true, fmt: x => dec(x.kd, 2),
    tip: x => L('У/С {0}: {1} уб. / {2} смертей за {3}', dec(x.kd, 2), num(x.kills), num(x.deaths), nPlural(x.matches, 'матч', 'матча', 'матчей')) });
  bars(m.querySelector('#pc-d'), WEEKDAYS.map((w, i) => ({ title: w, seconds: week[i] })), 'title', 'seconds',
    { fmt: x => x.seconds ? fmtDur(x.seconds) : '—' });
};

function renderCalendar(body) {
  const rows = allVisits();
  const byDay = {};
  for (const s of rows) (byDay[dayKey(new Date(s.joined_at * 1000))] ||= []).push(s);

  const { y, m } = S.calMonth;
  const first = new Date(y, m, 1);
  const lead = (first.getDay() + 6) % 7;             // понедельник — первый день недели
  const days = new Date(y, m + 1, 0).getDate();
  const counts = Object.entries(byDay).filter(([k]) => k.startsWith(y + '-' + pad2(m + 1))).map(([, v]) => v.length);
  const max = Math.max(1, ...counts);
  const today = dayKey(new Date());

  let cells = WEEKDAYS.map(w => `<div class="cal-h">${esc(w)}</div>`).join('');
  for (let i = 0; i < lead; i++) cells += '<div class="cal-d empty-cell"></div>';
  for (let d = 1; d <= days; d++) {
    const key = y + '-' + pad2(m + 1) + '-' + pad2(d);
    const list = byDay[key] || [];
    const people = new Set(list.map(s => s.name)).size;
    const lvl = list.length ? 1 + Math.min(3, Math.floor(3 * list.length / max)) : 0;
    cells += `<button class="cal-d l${lvl}${key === today ? ' today' : ''}${key === S.calDay ? ' sel' : ''}" data-act="cal-day" data-day="${key}" ${list.length ? '' : 'disabled'}>
      <span class="cal-n">${d}</span>${list.length ? `<span class="cal-b">${list.length}</span><span class="cal-c">${esc(nPlural(list.length, 'заход', 'захода', 'заходов'))}</span><span class="cal-p">${esc(nPlural(people, 'игрок', 'игрока', 'игроков'))}</span>` : ''}</button>`;
  }

  const sel = S.calDay && byDay[S.calDay] ? byDay[S.calDay].slice().sort((a, b) => a.joined_at - b.joined_at) : null;
  body.innerHTML = `<div class="note">${esc(trackingNote())}</div>
    <div class="grid two">
      <div class="card"><h2><button class="btn small ghost" data-act="cal-prev">←</button>
        ${esc(monthTitle(y, m))}<button class="btn small ghost" data-act="cal-next">→</button></h2>
        <div class="cal">${cells}</div>
        <p class="muted">${esc(L('Чем темнее день, тем больше заходов. Нажмите на день — внизу появится список.'))}</p></div>
      <div class="card"><h2>${esc(L('Когда заходят: дни недели × часы'))}</h2><div class="heat" id="heat"></div>
        <p class="muted">${esc(L('Сколько минут живые игроки провели на сервере в каждый час — за всё время, включая восстановленные заходы. Время — по часам этого компьютера.'))}</p></div>
    </div>
    ${sel ? `<div class="card mt16"><h2>${esc(new Date(S.calDay + 'T12:00:00').toLocaleDateString(LOCALE, { day: 'numeric', month: 'long', year: 'numeric' }))}</h2>${sessionsTable(sel.reverse(), 500)}</div>` : ''}`;
  renderHeat($('#heat'), rows);
}

function renderHeat(el, rows) {
  // минуты присутствия по (день недели, час) — интервалы режутся по границам часов
  const grid = Array.from({ length: 7 }, () => new Array(24).fill(0));
  for (const s of rows) {
    let t = s.joined_at * 1000;
    const end = sessEnd(s) * 1000;
    while (t < end) {
      const d = new Date(t);
      const next = new Date(d.getFullYear(), d.getMonth(), d.getDate(), d.getHours() + 1).getTime();
      grid[(d.getDay() + 6) % 7][d.getHours()] += (Math.min(next, end) - t) / 60000;
      t = next;
    }
  }
  const max = Math.max(1, ...grid.flat());
  let html = '<div class="heat-h"></div>' + Array.from({ length: 24 }, (_, h) => `<div class="heat-h">${h % 3 ? '' : h}</div>`).join('');
  grid.forEach((row, w) => {
    html += `<div class="heat-w">${esc(WEEKDAYS[w])}</div>`;
    row.forEach((v, h) => {
      const lvl = v ? 1 + Math.min(3, Math.floor(3 * v / max)) : 0;
      html += `<div class="heat-c l${lvl}" title="${esc(WEEKDAYS[w])} ${pad2(h)}:00–${pad2(h + 1)}:00 — ${esc(L('{0} мин', Math.round(v)))}"></div>`;
    });
  });
  el.innerHTML = html;
}

ACTIONS['cal-prev'] = () => { const c = S.calMonth; S.calMonth = c.m ? { y: c.y, m: c.m - 1 } : { y: c.y - 1, m: 11 }; renderStatsTab(); };
ACTIONS['cal-next'] = () => { const c = S.calMonth; S.calMonth = c.m < 11 ? { y: c.y, m: c.m + 1 } : { y: c.y + 1, m: 0 }; renderStatsTab(); };
ACTIONS['cal-day'] = d => { S.calDay = S.calDay === d.day ? null : d.day; renderStatsTab(); };

function hoursChart(el, values) {
  const max = Math.max(...values, 1);
  el.innerHTML = '';
  values.forEach((v, i) => {
    const c = document.createElement('div'); c.className = 'col';
    c.style.height = (100 * v / max).toFixed(1) + '%';
    c.title = i + ':00 — ' + v;
    el.appendChild(c);
  });
}

// ============================================================ Логи

PAGES.logs = async main => {
  main.innerHTML = `<h1>${esc(L('Логи'))}</h1>
    <div class="card"><div class="row mb12">
      <select id="lg-key"><option value="console">${esc(L('Консоль сервера (qconsole.log)'))}</option><option value="games">${esc(L('Игра (games.log)'))}</option></select>
      <select id="lg-lines"><option>100</option><option selected>300</option><option>1000</option><option>3000</option></select>
      <input id="lg-grep" class="wide" placeholder="${esc(L('Фильтр, например Kill или InitGame'))}">
      <button class="btn primary" data-act="logs-load">${esc(L('Показать'))}</button>
      <label class="row"><input type="checkbox" id="lg-follow"> ${esc(L('обновлять'))}</label></div>
      <div id="lg-files" class="muted mb8"></div>
      <pre class="log" id="lg-out"></pre></div>`;
  $('#lg-grep').addEventListener('keydown', e => { if (e.key === 'Enter') loadLogs(); });
  $('#lg-key').addEventListener('change', loadLogs);
  await loadLogs();
  every(5000, () => { if ($('#lg-follow') && $('#lg-follow').checked) loadLogs(); });
};

async function loadLogs() {
  const key = $('#lg-key').value;
  const q = new URLSearchParams({ log: key, lines: $('#lg-lines').value, grep: $('#lg-grep').value });
  let d;
  try { d = await api('/api/logs?' + q); } catch (e) { $('#lg-out').textContent = e.message; return; }
  const f = d.files[key];
  $('#lg-files').textContent = f ? f.path + ' · ' + fmtBytes(f.size) + ' · ' + L('изменён {0}', fmtTime(f.mtime)) : '';
  const out = $('#lg-out');
  out.textContent = d.lines.length ? d.lines.join('\n') : (f && f.missing
    ? (key === 'console' ? L('(файла нет — включите в server.cfg «seta logfile 1» или укажите путь в .env)')
      : L('(файла нет — включите в server.cfg «seta g_log games.log» или укажите путь в .env)'))
    : L('(пусто)'));
  out.scrollTop = out.scrollHeight;
}
ACTIONS['logs-load'] = () => loadLogs();

// ============================================================ Консоль

PAGES.console = async main => {
  main.innerHTML = `<h1>${esc(L('Консоль rcon'))}</h1>
    <div class="card"><div class="row"><input id="cn-cmd" class="wide mono" placeholder="status" autocomplete="off">
      <button class="btn primary" data-act="console-run">${esc(L('Выполнить'))}</button></div>
      <pre class="out" id="cn-out">${esc(L('Ответ сервера появится здесь. Каждая команда пишется в журнал действий.'))}</pre></div>
    <div class="card"><h2>${esc(L('Справочник команд'))}</h2><p class="muted">${esc(L('По исходникам ioquake3 и официальному гайду. Нажмите на команду, чтобы подставить её.'))}</p>
      <div class="tbl-wrap"><table class="cmd-ref"><tbody id="cn-ref"></tbody></table></div></div>`;
  $('#cn-cmd').addEventListener('keydown', e => { if (e.key === 'Enter') ACTIONS['console-run'](); });
  $('#cn-ref').addEventListener('click', e => {
    const td = e.target.closest('td[data-cmd]'); if (!td) return;
    $('#cn-cmd').value = td.dataset.cmd.split(' ')[0] + ' '; $('#cn-cmd').focus();
  });
  const ref = await act(() => api('/api/commands'));
  if (ref) $('#cn-ref').innerHTML = ref.map(c => `<tr><td data-cmd="${esc(c.cmd)}">${esc(Lsrv(c.cmd))}</td><td>${esc(Lsrv(c.help))}</td></tr>`).join('');
};

ACTIONS['console-run'] = async () => {
  const command = $('#cn-cmd').value.trim(); if (!command) return;
  $('#cn-out').textContent = '…';
  try {
    const d = await api('/api/console', { command });
    $('#cn-out').textContent = d.output || L('(сервер ничего не ответил — для многих команд это нормально)');
  } catch (e) { $('#cn-out').textContent = L('Ошибка: {0}', e.message); }
};

// ============================================================ Журнал

PAGES.audit = async main => {
  let rows;
  try { rows = await api('/api/audit'); } catch (e) { main.innerHTML = '<div class="error">' + esc(e.message) + '</div>'; return; }
  main.innerHTML = `<h1>${esc(L('Журнал действий'))}</h1><div class="card">${rows.length ? `<div class="tbl-wrap"><table>
    <thead><tr><th>${esc(L('Когда'))}</th><th>${esc(L('Действие'))}</th><th>${esc(L('Подробности'))}</th><th>${esc(L('Итог'))}</th></tr></thead><tbody>${
    rows.map(r => `<tr><td>${esc(fmtTime(r.at))}</td><td>${esc(Lsrv(r.action))}</td><td class="mono">${esc(Lsrv(r.detail))}</td>
      <td>${r.ok ? pill(L('ок'), 'ok') : pill(L('ошибка'), 'bad')}${r.output ? '<div class="muted mono">' + esc(Lsrv(r.output.slice(0, 200))) + '</div>' : ''}</td></tr>`).join('')
  }</tbody></table></div>` : '<div class="empty">' + esc(L('Пока пусто')) + '</div>'}</div>`;
};

// ============================================================ Настройки панели: язык, оформление, звук
/* Всё хранится в этом браузере (localStorage). Та же страница открывается окном и с экрана
   входа. Язык меняется перезагрузкой: словарь подключает boot.js. */

const seg = (act, items, cur) => '<div class="seg">' + items.map(([k, t]) =>
  `<button type="button" class="${k === cur ? 'active' : ''}" data-act="${act}" data-v="${k}">${esc(t)}</button>`).join('') + '</div>';

function prefsHtml() {
  const style = styleNow();
  const chosen = pref.get('lang', '');
  const langName = (LANGS.find(([c]) => c === LANG) || [LANG, LANG])[1];
  const theme = document.documentElement.getAttribute('data-theme') || 'auto';
  const bg = document.documentElement.getAttribute('data-bg');
  const have = S.site.backgrounds || [];
  const snd = S.site.sounds || [];
  return `<div class="grid two prefs">
    <div class="card"><h2>${esc(L('Язык'))} ${LANG === 'en' ? '' : '<span class="muted fs12">Language</span>'}</h2>
      <div class="lang-pick">
        <button type="button" class="chip${chosen ? '' : ' on'}" data-act="pref-lang" data-v="">🌐 ${esc(L('Как в браузере'))}</button>
        ${LANGS.map(([code, name]) => `<button type="button" lang="${code}" class="chip${chosen === code ? ' on' : ''}${LANG === code ? ' cur' : ''}" data-act="pref-lang" data-v="${code}">${esc(name)}</button>`).join('')}
      </div>
      <p class="muted fs12">${esc(L('Сейчас: {0}. Переводы — в файлах static/i18n/<код>.js, их можно поправить.', langName))}</p></div>
    <div class="card"><h2>${esc(L('Оформление'))}</h2>
      <div class="field"><span>${esc(L('Стиль'))}</span>${seg('pref-style', [['q3', L('Как в Quake III')], ['plain', L('Обычный')]], style)}</div>
      ${style === 'q3' ? (have.length ? `<div class="field"><span>${esc(L('Фон'))}</span><div class="bg-pick">${BGS.filter(([k]) => have.includes(k)).map(([k, t]) =>
        `<button type="button" class="bg-opt bg-${k}${bg === k ? ' on' : ''}" data-act="pref-bg" data-v="${k}"><span>${esc(t())}</span></button>`).join('')}</div></div>`
        : `<p class="muted fs12">${esc(L('Фонов нет: в pk3 сервера не нашлось текстур игры (нужен pak0.pk3).'))}</p>`)
        : `<div class="field"><span>${esc(L('Тема'))}</span>${seg('pref-theme', [['auto', L('Как в системе')], ['light', L('Светлая')], ['dark', L('Тёмная')]], theme)}</div>`}
    </div>
    <div class="card"><h2>${esc(L('Звук'))}</h2>
      ${snd.length ? `<div class="field"><span>${esc(L('Звуки меню'))}</span>${seg('pref-sounds', [['on', L('Включены')], ['off', L('Выключены')]], pref.get('sounds', 'on'))}</div>
        ${snd.includes('fight') ? `<div class="field"><span>${esc(L('Диктор при входе'))}</span>${seg('pref-voice', [['on', L('«Fight!»')], ['off', L('Выключен')]], pref.get('voice', 'on'))}</div>` : ''}`
        : `<p class="muted fs12">${esc(L('Звуков нет: в pk3 сервера не нашлось sound/misc/menu*.wav.'))}</p>`}
      ${(S.site.music || []).length ? `<div class="field"><span>${esc(L('Плеер в меню'))}</span>${seg('pref-player', [['on', L('Показывать')], ['off', L('Убрать, музыку не играть')]], playerOn() ? 'on' : 'off')}</div>
        ${playerOn() ? `<div class="row"><button type="button" class="btn${music.playing() ? '' : ' primary'}" data-act="music-toggle">${esc(music.playing() ? L('Пауза') : L('Играть'))}</button>
          <button type="button" class="btn" data-act="music-next">${esc(L('Следующий трек'))}</button>
          <label class="row muted">${esc(L('Громкость'))} <input type="range" id="mus-vol" min="0" max="1" step="0.05" value="${music.volume()}"></label></div>
          <div class="tracks" id="prefs-tracks"></div>` : ''}`
        : `<p class="muted fs12">${esc(L('Музыки нет: в pk3 сервера не нашлось music/*.wav.'))}</p>`}
    </div>
    <div class="card"><h2>${esc(L('О панели'))}</h2><p class="muted fs12">${esc(LEGAL())}</p></div>
  </div>`;
}

function renderTracks() {
  const box = $('#prefs-tracks');
  if (!box) return;
  const [cur] = music.track();
  box.innerHTML = music.tracks().map((t, n) => {
    const on = music.playing() && t.key === cur;
    return `<button type="button" class="chip${on ? ' on' : ''}" data-act="music-pick" data-n="${n}">${on ? '♪ ' : ''}${esc(MUSIC_TITLES[t.key] || t.key)}</button>`;
  }).join('');
}

function drawPrefs() {
  const box = S.page === 'prefs' && $('#main') && S.authed ? $('#prefs-box') : $('.modal #prefs-box');
  if (!box) return;
  box.innerHTML = prefsHtml();
  renderTracks();
  const vol = $('#mus-vol');
  if (vol) vol.addEventListener('input', () => music.setVolume(Number(vol.value)));
}

PAGES.prefs = async main => {
  main.innerHTML = `<h1>${esc(L('Настройки панели'))}</h1>
    <p class="muted">${esc(L('Язык, оформление и звук. Всё хранится в этом браузере.'))}</p><div id="prefs-box"></div>`;
  drawPrefs();
};
ACTIONS['prefs-modal'] = () => { openModal(`<h2>${esc(L('Язык и оформление'))}</h2><div id="prefs-box"></div>`, 'wide'); drawPrefs(); };
ACTIONS['pref-lang'] = d => { if (d.v) pref.set('lang', d.v); else pref.del('lang'); location.reload(); };
ACTIONS['pref-style'] = d => { pref.set('style', d.v); applyLook(); drawPrefs(); };
ACTIONS['pref-bg'] = d => { pref.set('bg', d.v); applyLook(); drawPrefs(); };
ACTIONS['pref-theme'] = d => { if (d.v === 'auto') pref.del('theme'); else pref.set('theme', d.v); applyLook(); drawPrefs(); };
ACTIONS['pref-sounds'] = d => { pref.set('sounds', d.v); if (d.v === 'on') sfx.play('menu1'); drawPrefs(); };
ACTIONS['pref-voice'] = d => { pref.set('voice', d.v); if (d.v === 'on') sfx.play('fight'); drawPrefs(); };
ACTIONS['pref-player'] = d => {
  pref.set('player', d.v);
  if (d.v === 'off') music.pause(); else pref.set('music', 'on');
  musicUI(); drawPrefs();
};
ACTIONS['music-toggle'] = () => { music.toggle(); musicUI(); drawPrefs(); };
ACTIONS['music-next'] = () => { music.next(); drawPrefs(); };
ACTIONS['music-mute'] = () => { music.toggleMute(); };
ACTIONS['music-pick'] = d => { music.pick(+d.n); drawPrefs(); };

document.addEventListener('DOMContentLoaded', start);
