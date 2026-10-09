(() => {
  'use strict';

  const $ = id => document.getElementById(id);
  const form = $('search-form');
  const tg = window.Telegram?.WebApp;
  const initData = typeof tg?.initData === 'string' ? tg.initData : '';
  const configured = document.body.dataset.telegramEnabled === 'true';
  const demoMode = document.body.dataset.demoMode === 'true';
  const state = {
    view: 'search', user: null, isOwner: false, canNotify: false, session: demoMode ? 'demo' : configured ? 'preview' : 'unconfigured', accessState: 'not_requested', accessVersion: 0, sessionSerial: 0,
    botUsername: document.body.dataset.botUsername || '', lastFilters: null, saveFilters: null,
    searchRequest: null, searchSerial: 0, dealsRequest: null, dealsSerial: 0,
    savedSerial: 0, historySerial: 0, detailSerial: 0, detailRequest: null, toastTimer: null, subscriptions: [], optionsLoaded: false, optionsLoading: false,
    queuedPax: new Set(), pendingPax: new Set(), paxErrors: new Map(),
  };
  const OPERATORS = {joinup: 'Join Up', teztour: 'Tez Tour', novaturas: 'Novatours', coral: 'Coral', anextour: 'Anex', itaka: 'Itaka'};
  const BOARD_LABELS = {RO: 'Без питания', BB: 'Завтраки', HB: 'Двухразовое питание', FB: 'Трёхразовое питание', AI: 'Всё включено', UAI: 'Ультра всё включено', OTHER: 'Другое / не указано'};
  const INCLUDED_MEALS = ['BB', 'HB', 'FB', 'AI', 'UAI'];
  const dialogOpeners = new WeakMap();
  const activeRequests = new Set();
  const SVG_NS = 'http://www.w3.org/2000/svg';

  function el(tag, className, text) {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (text !== undefined && text !== null) item.textContent = String(text);
    return item;
  }
  function icon(name) {
    const svg = document.createElementNS(SVG_NS, 'svg');
    svg.classList.add('icon'); svg.setAttribute('aria-hidden', 'true');
    const use = document.createElementNS(SVG_NS, 'use');
    use.setAttribute('href', '#i-' + name); svg.append(use);
    return svg;
  }
  function button(text, className, action, iconName) {
    const result = el('button', className);
    result.type = 'button';
    if (iconName) result.append(icon(iconName));
    result.append(el('span', '', text));
    if (action) result.addEventListener('click', action);
    return result;
  }
  function validUrl(value) {
    if (!value || typeof value !== 'string') return null;
    try {
      const url = new URL(value);
      return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password ? url.href : null;
    } catch { return null; }
  }
  function externalLink(value, text, className = 'button button-primary', isTelegram = false) {
    const url = validUrl(value);
    if (!url) return el('span', 'field-hint', 'Ссылка продавца недоступна');
    const a = el('a', className, text);
    a.href = url; a.target = '_blank'; a.rel = 'noopener noreferrer';
    a.addEventListener('click', event => {
      if (!initData || !tg) return;
      const method = isTelegram ? tg.openTelegramLink : tg.openLink;
      if (typeof method === 'function') {
        event.preventDefault();
        try { method.call(tg, url); } catch { window.open(url, '_blank', 'noopener,noreferrer'); }
      }
    });
    return a;
  }
  function botLink(text = 'Открыть бота', className = 'button button-primary') {
    const username = state.botUsername.replace(/^@/, '');
    if (!/^[A-Za-z0-9_]{5,32}$/.test(username)) return null;
    return externalLink('https://t.me/' + username + '?start=tourfinder', text, className, true);
  }
  function money(cents, currency = 'EUR') {
    const value = Number(cents);
    if (!Number.isFinite(value)) return 'Цена не указана';
    try { return new Intl.NumberFormat('ru-RU', {style: 'currency', currency, maximumFractionDigits: 0}).format(value / 100); }
    catch { return new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 0}).format(value / 100) + ' ' + String(currency); }
  }
  function dateLabel(value, withTime = false) {
    if (!value) return 'Дата неизвестна';
    const date = new Date(String(value).length === 10 ? value + 'T12:00:00' : value);
    if (!Number.isFinite(date.getTime())) return 'Дата неизвестна';
    return new Intl.DateTimeFormat('ru-RU', withTime
      ? {day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit'}
      : {day: 'numeric', month: 'short'}).format(date);
  }
  function isArchived(row) { return row.archived === true || row.data_source === 'archive'; }
  function observationLabel(row) {
    return (isArchived(row) ? 'Архивная цена, наблюдали ' : row.stale ? 'Последнее наблюдение ' : 'Проверено ') + dateLabel(row.fetched_at || row.last_seen_at, true);
  }
  function archiveSearchLimited(storage) {
    return Boolean(storage?.partial && storage.live !== 'unavailable' && storage.archive !== 'unavailable'
      && Array.isArray(storage.partial_reasons) && storage.partial_reasons.includes('archive_candidate_limit'));
  }
  function storageMessage(storage, context = 'search') {
    if (!storage) return '';
    const messages = [];
    if (storage.live === 'unavailable') messages.push('Свежие данные сейчас недоступны. Показаны сохранённые наблюдения; актуальную цену и наличие уточняйте у продавца.');
    else if (storage.archive === 'unavailable') messages.push(context === 'history' ? 'Архив сейчас недоступен. История показана не полностью.' : 'Архив сейчас недоступен. Показаны только доступные предложения из текущего сбора.');
    else if (context !== 'history' && storage.mode === 'archive') messages.push('Это архивные предложения. Цены исторические и могут уже не действовать.');
    else if (context !== 'history' && storage.mode === 'mixed') messages.push('Архивные цены отмечены отдельно. Их актуальность нужно проверить у продавца.');
    if (storage.partial && storage.live !== 'unavailable' && storage.archive !== 'unavailable') messages.push(context === 'history' ? 'Часть истории недоступна.' : archiveSearchLimited(storage)
      ? 'Проверена ограниченная часть архива. Сузьте даты или другие параметры и повторите поиск.'
      : 'Не все данные удалось проверить. Уточните параметры или повторите поиск позже.');
    if (storage.archive_as_of && storage.archive === 'ok') messages.push((context === 'history' ? 'История дополнена архивом от ' : 'Архив обновлён ') + dateLabel(storage.archive_as_of, true) + '.');
    return messages.join(' ');
  }
  function appendStorageNotice(target, storage, context = 'search') {
    const message = storageMessage(storage, context);
    if (message) { const note = el('p', 'storage-notice', message); note.setAttribute('role', 'status'); target.prepend(note); }
  }
  function localDate(offset = 0) {
    const date = new Date(); date.setDate(date.getDate() + offset);
    return [date.getFullYear(), String(date.getMonth() + 1).padStart(2, '0'), String(date.getDate()).padStart(2, '0')].join('-');
  }
  function partyLabel(filters) {
    const adults = Number(filters.adults) || 2;
    const ages = String(filters.children_ages || '').split(',').filter(Boolean);
    return adults + (adults === 1 ? ' взрослый' : ' взрослых') + (ages.length ? ' + дети ' + ages.join(', ') + ' лет' : '');
  }
  function boardLabel(offer) {
    const original = offer.board_name || offer.board_code;
    if (offer.board_category === 'OTHER') return original ? 'Другое: ' + original : 'Питание не указано';
    return offer.board_label || BOARD_LABELS[offer.board_category] || original || 'Питание не указано';
  }
  function filterDescription(filters) {
    const parts = [dateLabel(filters.date_from) + ' — ' + dateLabel(filters.date_till), partyLabel(filters), filters.nights_min + '–' + filters.nights_max + ' ночей'];
    if (filters.budget_max) parts.push('до ' + money(Number(filters.budget_max) * 100));
    if (filters.stars_min) parts.push('от ' + filters.stars_min + ' ★');
    if (filters.board_categories) parts.push(String(filters.board_categories).split(',').map(code => BOARD_LABELS[code] || code).join(' / '));
    if (filters.boards) parts.push('у источника: ' + String(filters.boards).split(',').join(' / '));
    if (filters.countries) {
      const names = String(filters.countries).split(',').map(id => {
        const input = Array.from(form.querySelectorAll('[name=country]')).find(item => item.value === id);
        return input?.nextElementSibling?.textContent || id;
      });
      parts.push(names.join(', '));
    }
    if (filters.only_hot) parts.push('горящие');
    return parts.join(' · ');
  }
  function toast(message) {
    clearTimeout(state.toastTimer);
    $('toast').textContent = message; $('toast').hidden = false;
    state.toastTimer = setTimeout(() => { $('toast').hidden = true; }, 5000);
  }
  function haptic(type = 'light') {
    try { if (initData) tg?.HapticFeedback?.impactOccurred(type); } catch { /* Old Telegram client. */ }
  }
  function showError(target, message) { target.textContent = message; target.hidden = !message; }
  function loading(target, message) {
    const box = el('div', 'loading-state');
    box.append(el('span', 'spinner'), el('p', '', message));
    target.replaceChildren(box); target.setAttribute('aria-busy', 'true');
  }
  function empty(target, title, description, action, actionText = 'Повторить', iconName = 'search') {
    target.removeAttribute('aria-busy');
    const box = el('div', 'empty-state');
    const mark = el('div', 'empty-icon'); mark.append(icon(iconName));
    box.append(mark, el('h3', '', title), el('p', '', description));
    if (action) box.append(button(actionText, 'button', action));
    target.replaceChildren(box);
  }

  function hasAccess() { return demoMode || Boolean(state.user); }
  function restrictAccess(error) {
    const wasOpen = hasAccess();
    state.user = null; state.isOwner = false; state.canNotify = false;
    state.session = error.status === 401 ? 'expired' : error.status === 403 ? 'forbidden' : error.status === 503 && !configured ? 'unconfigured' : 'unavailable';
    state.accessState = ['pending', 'denied', 'not_requested'].includes(error.accessState) ? error.accessState : 'not_requested';
    state.accessVersion++; state.sessionSerial++; state.searchSerial++; state.dealsSerial++; state.savedSerial++; state.historySerial++; state.detailSerial++;
    for (const controller of activeRequests) controller.abort();
    state.lastFilters = null; state.saveFilters = null; state.subscriptions = [];
    state.optionsLoaded = false; state.optionsLoading = false;
    state.queuedPax.clear(); state.pendingPax.clear(); state.paxErrors.clear();
    $('search-storage').hidden = true; $('search-storage').textContent = ''; $('options-note').hidden = true;
    clearTimeout(state.toastTimer); $('toast').hidden = true; $('toast').textContent = '';
    for (const dialog of document.querySelectorAll('dialog[open]')) closeDialog(dialog);
    for (const id of ['deals-results', 'subscriptions-list', 'alerts-list', 'history-content', 'offer-content', 'offer-booking']) $(id).replaceChildren();
    $('save-form').reset(); $('save-filters-summary').textContent = ''; $('saved-caption').textContent = ''; $('deals-caption').textContent = ''; $('saved-nav-count').hidden = true; $('save-search').hidden = true;
    $('search-results-title').textContent = 'Выберите свой следующий отпуск'; $('search-results-caption').textContent = 'Предложения появятся после поиска';
    $('search-submit').disabled = false; $('search-submit').querySelector('span').textContent = 'Найти мой отпуск'; $('refresh-deals').disabled = false;
    empty($('search-results'), 'Начнём с ваших параметров', 'Выберите даты, состав туристов и питание, затем запустите поиск.');
    renderAccess();
    if (wasOpen && !demoMode) $('gate-title').focus({preventScroll: true});
  }

  async function api(path, {method = 'GET', body, signal, timeout = 45000} = {}) {
    // initData is sent only to this app's API. Never add it to source/image links.
    if (!path.startsWith('/api/')) throw new Error('Недопустимый адрес запроса');
    const isSession = path.split('?')[0] === '/api/telegram/session';
    if (!isSession && !hasAccess()) throw new Error('Дождитесь одобрения доступа владельцем.');
    const accessVersion = state.accessVersion;
    const controller = new AbortController();
    activeRequests.add(controller);
    const cancel = () => controller.abort();
    if (signal?.aborted) controller.abort();
    else signal?.addEventListener('abort', cancel, {once: true});
    let timedOut = false;
    const timer = setTimeout(() => { timedOut = true; controller.abort(); }, timeout);
    const headers = {'Accept': 'application/json'};
    if (initData) headers['X-Telegram-Init-Data'] = initData;
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    try {
      const response = await fetch(path, {method, headers, body: body === undefined ? undefined : JSON.stringify(body), signal: controller.signal, credentials: 'same-origin', redirect: 'error'});
      let data;
      try { data = await response.json(); } catch { data = {}; }
      if (!response.ok) {
        const descriptions = {401: 'Сессия истекла. Закройте приложение и откройте его снова через бота.', 403: 'Этот личный бот пока недоступен вашему Telegram-аккаунту.', 429: 'Слишком много запросов. Попробуйте через минуту.', 503: 'Сервис ещё не настроен или временно недоступен.'};
        const error = new Error(descriptions[response.status] || (response.status >= 500 ? 'Сервис не смог обработать запрос. Попробуйте чуть позже.' : 'Не удалось сохранить изменения. Проверьте параметры.'));
        error.status = response.status;
        error.code = typeof data?.detail?.code === 'string' ? data.detail.code : null;
        if (data?.detail?.code === 'access_required') error.accessState = data.detail.access_state;
        if (!isSession && !demoMode && [401, 403].includes(response.status)) restrictAccess(error);
        throw error;
      }
      if (!isSession && accessVersion !== state.accessVersion) throw new DOMException('Доступ изменился', 'AbortError');
      return data;
    } catch (error) {
      if (timedOut) throw new Error('Ответ занимает слишком много времени. Попробуйте позже или сузьте даты поиска.');
      if (error.name === 'AbortError') throw error;
      if (error instanceof TypeError) throw new Error('Нет соединения с сервисом. Проверьте интернет и повторите.');
      throw error;
    } finally {
      clearTimeout(timer); signal?.removeEventListener('abort', cancel); activeRequests.delete(controller);
    }
  }

  function syncChildAges(values) {
    const count = Math.max(0, Math.min(4, Number(form.elements.children.value) || 0));
    const previous = values || Array.from($('age-inputs').querySelectorAll('input')).map(input => input.value);
    if (!values && $('age-inputs').children.length === count) return;
    $('children-ages').hidden = !count;
    const fields = [];
    for (let index = 0; index < count; index++) {
      const label = el('label', 'field', 'Ребёнок ' + (index + 1));
      const input = el('input'); input.type = 'number'; input.min = '0'; input.max = '17'; input.step = '1'; input.required = true; input.inputMode = 'numeric'; input.value = previous[index] ?? '7';
      input.setAttribute('aria-label', 'Возраст ребёнка ' + (index + 1) + ' на момент поездки');
      label.append(input); fields.push(label);
    }
    $('age-inputs').replaceChildren(...fields);
  }
  function currentFilters() {
    const fields = form.elements;
    const filters = {date_from: fields.date_from.value, date_till: fields.date_till.value, adults: Number(fields.adults.value), nights_min: Number(fields.nights_min.value), nights_max: Number(fields.nights_max.value)};
    const ages = Array.from($('age-inputs').querySelectorAll('input')).map(input => Number(input.value)).sort((a, b) => a - b);
    if (ages.length) filters.children_ages = ages.join(',');
    if (fields.budget_max.value) filters.budget_max = Number(fields.budget_max.value);
    for (const [name, key] of [['board', 'boards'], ['board_category', 'board_categories'], ['country', 'countries']]) {
      const selected = Array.from(form.querySelectorAll('input[name=' + name + ']:checked')).map(input => input.value);
      if (selected.length) filters[key] = selected.join(',');
    }
    if (fields.stars_min.value) filters.stars_min = Number(fields.stars_min.value);
    if (fields.only_hot.checked) filters.only_hot = true;
    if (fields.sort.value !== 'price') filters.sort = fields.sort.value;
    return filters;
  }
  function validateFilters() {
    collapseFilters(false);
    if (!form.reportValidity()) return null;
    const filters = currentFilters();
    let error = '';
    if (filters.date_from < localDate()) error = 'Выберите дату вылета не раньше сегодняшней.';
    else if (filters.date_till < filters.date_from) error = 'Конец периода должен быть не раньше его начала.';
    else if ((Date.parse(filters.date_till) - Date.parse(filters.date_from)) / 86400000 > 90) error = 'Выберите диапазон вылета не длиннее 90 дней.';
    else if (filters.nights_max < filters.nights_min) error = 'Максимальное число ночей должно быть не меньше минимального.';
    showError($('search-validation'), error);
    return error ? null : filters;
  }
  function collapseFilters(collapsed) {
    $('filter-fields').hidden = collapsed;
    $('filter-summary').hidden = !collapsed;
    $('filter-summary').textContent = filterDescription(currentFilters());
    $('filters-toggle').setAttribute('aria-expanded', String(!collapsed));
    $('filters-toggle').setAttribute('aria-label', collapsed ? 'Изменить параметры поиска' : 'Свернуть параметры поиска');
  }
  function applyFilters(filters) {
    for (const name of ['date_from', 'date_till', 'adults', 'nights_min', 'nights_max']) if (filters[name] !== undefined) form.elements[name].value = filters[name];
    form.elements.budget_max.value = filters.budget_max || '';
    form.elements.stars_min.value = filters.stars_min || '';
    form.elements.sort.value = filters.sort || 'price';
    form.elements.only_hot.checked = Boolean(filters.only_hot);
    const ages = String(filters.children_ages || '').split(',').filter(Boolean);
    form.elements.children.value = ages.length; syncChildAges(ages);
    for (const [name, key] of [['board', 'boards'], ['board_category', 'board_categories'], ['country', 'countries']]) {
      const selected = new Set(String(filters[key] || '').split(','));
      for (const input of form.querySelectorAll('[name=' + name + ']')) input.checked = selected.has(input.value);
    }
    if (filters.boards) { $('raw-meals').open = true; $('raw-meals').closest('.more-filters').open = true; }
    syncMealPresets(); collapseFilters(false);
  }

  function syncMealPresets() {
    const selected = Array.from(form.querySelectorAll('[name=board_category]:checked')).map(input => input.value);
    const hasRaw = Boolean(form.querySelector('[name=board]:checked'));
    $('meals-any').setAttribute('aria-pressed', String(!selected.length && !hasRaw));
    $('meals-included').setAttribute('aria-pressed', String(!hasRaw && selected.length === INCLUDED_MEALS.length && INCLUDED_MEALS.every(code => selected.includes(code))));
  }
  function selectMeals(categories) {
    for (const input of form.querySelectorAll('[name=board_category]')) input.checked = categories.includes(input.value);
    for (const input of form.querySelectorAll('[name=board]')) input.checked = false;
    syncMealPresets(); haptic();
  }

  function mergeOptionChips(container, name, values, valueKey, label) {
    const checked = new Set(Array.from(container.querySelectorAll('input:checked')).map(input => input.value));
    const entries = new Map(Array.from(container.querySelectorAll('input')).map(input => [input.value, input.nextElementSibling?.textContent || input.value]));
    for (const row of values) if (row && row[valueKey] !== null && row[valueKey] !== undefined && String(row[valueKey])) entries.set(String(row[valueKey]), label(row));
    container.replaceChildren(...Array.from(entries).sort((a, b) => a[1].localeCompare(b[1], 'ru')).map(([value, text]) => {
      const item = el('label', 'filter-chip'); const input = el('input');
      input.type = 'checkbox'; input.name = name; input.value = value; input.checked = checked.has(value);
      if (name === 'board') input.addEventListener('change', syncMealPresets);
      item.append(input, el('span', '', text)); return item;
    }));
  }
  async function loadOptions() {
    if (!hasAccess() || state.optionsLoaded || state.optionsLoading) return;
    state.optionsLoading = true;
    try {
      const data = await api('/api/options');
      if (!Array.isArray(data.countries) || !Array.isArray(data.boards)) throw new Error('Не удалось прочитать справочники.');
      mergeOptionChips($('country-options'), 'country', data.countries, 'country_id', row => row.country_name || String(row.country_id));
      mergeOptionChips($('board-options'), 'board', data.boards, 'board_code', row => String(row.board_code) + (row.board_name && row.board_name !== row.board_code ? ' · ' + row.board_name : ''));
      $('countries-empty').hidden = Boolean($('country-options').children.length);
      $('boards-empty').hidden = Boolean($('board-options').children.length);
      $('options-note').textContent = data.storage?.live === 'unavailable' ? 'Страны и питание загружены из архива. Свежие данные временно недоступны.' : '';
      $('options-note').hidden = !$('options-note').textContent;
      state.optionsLoaded = true; syncMealPresets();
    } catch (error) {
      if (hasAccess() && error.name !== 'AbortError') {
        $('options-note').textContent = 'Не удалось обновить список стран и питания. Можно искать без этих уточнений и повторить загрузку при следующем открытии.';
        $('options-note').hidden = false;
      }
    } finally { state.optionsLoading = false; }
  }

  function updateBackButton() {
    try {
      if (!initData || !tg?.BackButton) return;
      if (!hasAccess()) { tg.BackButton.hide(); return; }
      if (state.view !== 'search' || document.querySelector('dialog[open]')) tg.BackButton.show();
      else tg.BackButton.hide();
    } catch { /* Compatibility with older clients. */ }
  }
  function navigate(view) {
    if (!hasAccess()) { renderAccess(); return; }
    if (!['search', 'deals', 'saved'].includes(view)) return;
    state.view = view;
    for (const section of document.querySelectorAll('.view')) section.hidden = section.id !== 'view-' + view;
    for (const nav of document.querySelectorAll('[data-view]')) {
      const active = nav.dataset.view === view;
      nav.classList.toggle('active', active);
      if (active) nav.setAttribute('aria-current', 'page'); else nav.removeAttribute('aria-current');
    }
    window.scrollTo({top: 0, behavior: 'instant'}); updateBackButton();
    if (view === 'deals') runDeals();
    if (view === 'saved') { renderAccess(); if (state.user) loadSaved(); }
  }
  function openDialog(dialog) { if (!dialog.open) { dialogOpeners.set(dialog, document.activeElement); dialog.showModal(); } updateBackButton(); }
  function closeDialog(dialog) { dialog.close(); updateBackButton(); }

  async function verifySession() {
    if (demoMode) { renderAccess(); loadOptions(); return; }
    if (!configured || !initData || state.session === 'checking') { renderAccess(); return; }
    const serial = ++state.sessionSerial;
    state.session = 'checking'; renderAccess();
    try {
      const data = await api('/api/telegram/session', {timeout: 15000});
      if (serial !== state.sessionSerial) return;
      if (!data.user || !Number.isSafeInteger(Number(data.user.id))) throw new Error('Telegram-сессия не подтверждена. Откройте приложение заново через бота.');
      state.user = data.user; state.isOwner = data.is_owner === true; state.canNotify = data.can_notify === true; state.session = 'authorized'; state.accessState = 'approved';
      if (typeof data.bot_username === 'string') state.botUsername = data.bot_username;
    } catch (error) {
      if (serial !== state.sessionSerial) return;
      restrictAccess(error); return;
    }
    renderAccess();
    loadOptions();
    if (state.view === 'saved' && state.user) loadSaved();
  }
  function renderGate() {
    let title = ['Вход', 'через Telegram.'];
    let description = 'Поиск туров доступен только после одобрения владельцем. Откройте бота и нажмите «Старт», чтобы отправить заявку. Затем запустите приложение из чата с ботом.';
    let footnote = 'До одобрения предложения и личные поиски закрыты.';
    if (state.session === 'checking') {
      title = ['Проверяем', 'ваш доступ.']; description = 'Подтверждаем Telegram-сессию и разрешение владельца.'; footnote = 'Это займёт несколько секунд.';
    } else if (state.session === 'unconfigured') {
      title = ['Сервис', 'готовится к запуску.']; description = 'Владелец ещё не завершил подключение Telegram. Доступ к поиску пока закрыт.';
    } else if (state.session === 'expired') {
      title = ['Откройте', 'приложение заново.']; description = 'Telegram-сессия истекла или не подтвердилась. Закройте это окно и снова запустите приложение кнопкой в чате с ботом.';
    } else if (state.session === 'unavailable') {
      title = ['Не удалось', 'проверить доступ.']; description = 'Проверьте соединение и повторите попытку. Пока сессия не подтверждена, поиск остаётся закрыт.';
    } else if (state.session === 'forbidden') {
      if (state.accessState === 'pending') {
        title = ['Заявка', 'на рассмотрении.']; description = 'Ваша заявка передана владельцу. После одобрения нажмите «Проверить решение», чтобы открыть поиск туров и сохранённые поиски.'; footnote = 'Повторно отправлять заявку не нужно.';
      } else if (state.accessState === 'denied') {
        title = ['Доступ', 'пока не одобрен.']; description = 'Владелец не предоставил доступ этому Telegram-аккаунту. Статус заявки можно посмотреть в чате с ботом.';
      } else {
        title = ['Запросите', 'доступ к поиску.']; description = 'Откройте бота и нажмите «Старт». Он передаст вашу заявку владельцу; после одобрения здесь появится поиск туров.';
      }
    }
    $('gate-title').replaceChildren(document.createTextNode(title[0]), el('br'), el('span', '', title[1]));
    $('gate-description').textContent = description; $('gate-footnote').textContent = footnote;
    $('gate-mark').replaceChildren(state.session === 'checking' ? el('span', 'spinner') : icon(state.accessState === 'pending' ? 'clock' : 'lock'));
    const actions = $('gate-actions'); actions.replaceChildren();
    if (!['checking', 'unconfigured'].includes(state.session)) {
      const link = botLink(state.session === 'forbidden' && state.accessState === 'not_requested' ? 'Открыть бота и подать заявку' : 'Открыть бота');
      if (link) actions.append(link);
      else actions.append(el('p', 'field-hint', 'Ссылку на бота можно получить у владельца Tour Finder.'));
    }
    if (initData && ['forbidden', 'unavailable'].includes(state.session)) actions.append(button(state.accessState === 'pending' ? 'Проверить решение' : 'Проверить доступ', 'button', verifySession));
    $('access-gate').setAttribute('aria-busy', String(state.session === 'checking'));
  }
  function renderAccess() {
    const allowed = hasAccess();
    $('access-gate').hidden = allowed; $('main-content').hidden = !allowed; $('app-nav').hidden = !allowed; $('app-footer').hidden = !allowed;
    document.body.classList.toggle('access-restricted', !allowed);
    const access = $('saved-access'); const note = $('connection-note'); const ownerNote = $('owner-note');
    access.replaceChildren(); note.replaceChildren(); ownerNote.replaceChildren();
    note.hidden = true; access.hidden = true; ownerNote.hidden = true;
    $('watchlist-content').hidden = !state.user;
    $('session-badge').classList.toggle('connected', Boolean(state.user));
    $('session-badge').textContent = state.user ? (state.isOwner ? 'Владелец · ' : '') + (state.user.first_name || 'В Telegram') : demoMode ? 'Локальное демо' : state.session === 'checking' ? 'Проверяем доступ…' : 'Закрытый доступ';
    if (!allowed) { renderGate(); updateBackButton(); return; }
    if (demoMode) {
      access.hidden = false; access.append(el('h3', '', 'Локальный демонстрационный режим'), el('p', '', 'Здесь можно проверить поиск, карточки и историю на тестовых данных. Личные поиски и сообщения требуют одобренной Telegram-сессии.'));
      return;
    }
    if (state.isOwner) {
      ownerNote.hidden = false; ownerNote.append(icon('lock'), el('p', '', 'Вы управляете доступом. Заявки — /requests, одобренные пользователи — /approved в чате с ботом.'));
      const link = botLink('В бот', 'text-button'); if (link) ownerNote.append(link);
    }
    if (state.canNotify) return;
    access.hidden = false; access.append(el('h3', '', 'Подключите сообщения от бота'), el('p', '', 'Доступ к поиску одобрен. Откройте бота и нажмите «Старт», чтобы получать уведомления; сохранение поисков уже доступно.'));
    const link = botLink(); if (link) access.append(link);
    access.append(button('Проверить подключение', 'text-button', verifySession));
    note.hidden = false; note.append(icon('bell'), el('p', '', 'Для уведомлений откройте бота и нажмите «Старт».'));
    const shortLink = botLink('В Telegram', 'text-button'); if (shortLink) note.append(shortLink);
  }

  async function runSearch() {
    if (!hasAccess()) { renderAccess(); return; }
    const filters = validateFilters();
    if (!filters) return;
    state.searchRequest?.abort();
    const controller = new AbortController(); state.searchRequest = controller;
    const serial = ++state.searchSerial;
    state.lastFilters = {...filters};
    $('save-search').hidden = false;
    $('search-submit').disabled = true; $('search-submit').querySelector('span').textContent = 'Ищем подходящие туры…';
    $('search-results-title').textContent = 'Сверяем ваши параметры';
    $('search-results-caption').textContent = partyLabel(filters) + ' · цена за всех';
    $('search-storage').hidden = true; $('search-storage').textContent = '';
    loading($('search-results'), 'Ищем среди собранных предложений…');
    try {
      const data = await api('/api/search?' + new URLSearchParams(filters), {signal: controller.signal});
      if (serial !== state.searchSerial) return;
      if (!Array.isArray(data.results)) throw new Error('Не удалось прочитать предложения. Попробуйте позже.');
      $('search-results').removeAttribute('aria-busy');
      $('search-results-title').textContent = data.results.length ? 'Варианты для вашего отпуска' : 'Пока без совпадений';
      $('search-results-caption').textContent = (data.results.length ? data.results.length + ' отелей · ' : '') + partyLabel(filters) + ' · цена за всех';
      $('search-storage').textContent = storageMessage(data.storage);
      $('search-storage').hidden = !$('search-storage').textContent;
      if (data.results.length) {
        $('search-results').replaceChildren(...data.results.map(row => hotelCard(row, filters)));
        if (data.results.length >= 100) $('search-results-caption').textContent += ' · первые 100';
      } else {
        const ages = filters.children_ages || '';
        const known = Array.isArray(data.available_compositions) && data.available_compositions.some(item => Number(item.pax_adl) === filters.adults && String(item.children_ages || '') === ages);
        if (data.queued_spec) { const spec = compositionSpec(filters); state.queuedPax.add(spec); state.paxErrors.delete(spec); }
        const queued = Boolean(data.queued_spec) || state.queuedPax.has(compositionSpec(filters));
        const message = queued
          ? 'Запрос на этот состав сохранён. Предложения появятся, если следующий успешный сбор найдёт подходящие туры; точное время неизвестно.'
          : data.storage?.partial ? (archiveSearchLimited(data.storage)
            ? 'Проверена ограниченная часть архива, поэтому отсутствие совпадений пока не подтверждено. Уточните параметры или запросите свежий сбор для этого состава.'
            : 'Не все данные удалось проверить. Повторите поиск позже или уточните параметры.')
          : known ? 'В собранных данных сейчас нет совпадений для этих дат и фильтров. Можно изменить параметры или запросить свежий сбор для этого состава.'
          : 'Для этого точного состава пока нет собранных предложений. Цены другого состава могут отличаться — мы не будем их подменять.';
        empty($('search-results'), queued ? 'Состав добавлен в сбор' : 'Попробуем другие параметры?', message, () => { collapseFilters(false); form.elements.date_from.focus(); }, 'Изменить поиск');
        if (!demoMode && state.user) appendCompositionRequest($('search-results').querySelector('.empty-state'), filters, known);
      }
      if (window.matchMedia('(max-width: 699px)').matches) collapseFilters(true);
    } catch (error) {
      if (serial !== state.searchSerial || error.name === 'AbortError') return;
      $('search-results-title').textContent = 'Не получилось загрузить туры';
      empty($('search-results'), 'Поиск временно недоступен', error.message, runSearch);
    } finally {
      if (serial === state.searchSerial) { $('search-submit').disabled = false; $('search-submit').querySelector('span').textContent = 'Найти мой отпуск'; }
    }
  }

  function hotelPhoto(row, eager = false) {
    const photo = el('div', 'hotel-photo');
    const placeholder = el('div', 'photo-placeholder'); placeholder.append(icon('sun'), el('span', '', 'Здесь начинается отпуск')); photo.append(placeholder);
    const url = validUrl(row.photo_url);
    if (url) {
      const img = el('img'); img.alt = row.hotel_name ? 'Фото отеля ' + row.hotel_name : 'Фото отеля'; img.loading = eager ? 'eager' : 'lazy'; img.referrerPolicy = 'no-referrer';
      img.addEventListener('load', () => { placeholder.hidden = true; }, {once: true});
      img.addEventListener('error', () => img.remove(), {once: true}); img.src = url; photo.append(img);
    }
    return photo;
  }

  function compositionPayload(filters) {
    return {adults: Number(filters.adults), children_ages: String(filters.children_ages || '').split(',').filter(value => value.trim()).map(Number).sort((a, b) => a - b)};
  }
  function compositionSpec(filters) {
    const payload = compositionPayload(filters);
    return String(payload.adults) + (payload.children_ages.length ? '+' + payload.children_ages.length + ':' + payload.children_ages.join(',') : '');
  }
  function refreshCompositionRequests(spec) {
    for (const panel of document.querySelectorAll('[data-pax-request]')) {
      if (panel.dataset.paxRequest !== spec) continue;
      const trigger = panel.querySelector('button'); const note = panel.querySelector('p');
      const queued = state.queuedPax.has(spec); const pending = state.pendingPax.has(spec); const error = state.paxErrors.get(spec);
      trigger.disabled = queued || pending;
      trigger.querySelector('span').textContent = queued ? 'Состав добавлен в сбор' : pending ? 'Сохраняем запрос…' : panel.dataset.paxRequestLabel;
      note.className = error ? 'form-error' : 'field-hint';
      note.setAttribute('role', error ? 'alert' : 'status');
      note.textContent = error || (queued ? 'Запрос сохранён. Предложения появятся, если следующий успешный сбор найдёт подходящие туры. Срок пока неизвестен.' : 'После отправки состав попадёт в очередной сбор источников.');
    }
  }
  function appendCompositionRequest(target, filters, known = false) {
    const spec = compositionSpec(filters); const panel = el('div'); panel.dataset.paxRequest = spec;
    panel.dataset.paxRequestLabel = known ? 'Запросить свежие предложения' : 'Собрать предложения для этого состава';
    const request = button(panel.dataset.paxRequestLabel, 'button button-primary', () => requestComposition({...filters}));
    panel.append(request, el('p', 'field-hint')); target.append(panel); refreshCompositionRequests(spec);
  }
  async function requestComposition(filters) {
    if (demoMode || !state.user) { renderAccess(); return; }
    const spec = compositionSpec(filters);
    if (state.pendingPax.has(spec) || state.queuedPax.has(spec)) return;
    const payload = compositionPayload(filters);
    if (!Number.isInteger(payload.adults) || payload.adults < 1 || payload.adults > 6 || payload.children_ages.length > 4 || payload.children_ages.some(age => !Number.isInteger(age) || age < 0 || age > 17)) {
      state.paxErrors.set(spec, 'Проверьте состав: 1–6 взрослых и не больше 4 детей с возрастом от 0 до 17 лет.');
      refreshCompositionRequests(spec); return;
    }
    state.pendingPax.add(spec); state.paxErrors.delete(spec); refreshCompositionRequests(spec);
    try {
      const data = await api('/api/pax-requests', {method: 'POST', body: payload});
      if (data.queued !== true) throw new Error('Сервис не подтвердил сохранение запроса. Повторите позже.');
      state.queuedPax.add(spec); haptic();
    } catch (error) {
      if (!state.user || state.queuedPax.has(spec) || error.name === 'AbortError') return;
      const message = [400, 422].includes(error.status) ? 'Проверьте состав: 1–6 взрослых и не больше 4 детей с возрастом от 0 до 17 лет.'
        : error.status === 503 ? 'Не удалось подтвердить запрос на сбор: сервис временно недоступен. Повторите позже.' : error.message;
      state.paxErrors.set(spec, message);
    } finally { state.pendingPax.delete(spec); refreshCompositionRequests(spec); }
  }
  function hotelCard(row, filters, isDrop = false) {
    const card = el('article', 'hotel-card');
    const photo = hotelPhoto(row);
    const photoOpen = button('', 'photo-open', () => showOffer(row, filters));
    photoOpen.setAttribute('aria-label', 'Подробнее: ' + (row.hotel_name || 'предложение'));
    photo.append(photoOpen);
    const badges = el('div', 'photo-badges');
    const operator = OPERATORS[row.operator] || row.operator;
    if (operator) badges.append(el('span', 'badge badge-photo', operator));
    if (isArchived(row)) badges.append(el('span', 'badge badge-archive', 'Архивная цена'));
    else if (row.stale) badges.append(el('span', 'badge badge-warm', 'Цена не обновлялась'));
    else if (isDrop && Number(row.prev_price_cents) > Number(row.price_cents)) {
      const percent = (Number(row.prev_price_cents) - Number(row.price_cents)) / Number(row.prev_price_cents) * 100;
      badges.append(el('span', 'badge badge-deal', '↓ ' + percent.toFixed(1).replace('.', ',') + '% по истории'));
    } else if (row.is_hot) badges.append(el('span', 'badge badge-warm', 'Горящий у источника'));
    photo.append(badges); card.append(photo);
    const body = el('div', 'hotel-body');
    body.append(el('p', 'hotel-location', [row.country_name, row.city_name].filter(Boolean).join(' · ') || 'Курорт не указан'));
    const title = el('h3', 'hotel-title');
    title.append(button(row.hotel_name || 'Отель без названия', 'hotel-title-button', () => showOffer(row, filters)));
    const stars = String(row.category || '').match(/^([1-5])(\+)?/);
    if (stars) title.append(el('span', 'hotel-stars', stars[1] + ' ★' + (stars[2] || '')));
    body.append(title);
    if (row.review_rating !== null && row.review_rating !== undefined && Number.isFinite(Number(row.review_rating))) {
      const rating = el('div', 'hotel-rating');
      rating.append(el('span', 'rating-score', Number(row.review_rating).toFixed(1).replace('.', ',') + ' / ' + (row.review_scale || 5)));
      const platform = {google: 'Google', tripadvisor: 'TripAdvisor'}[row.review_platform] || row.review_platform || 'Гости';
      rating.append(el('span', '', platform + (row.review_count ? ' · ' + Number(row.review_count).toLocaleString('ru-RU') + ' отзывов' : '')));
      if (row.star_gap !== null && row.star_gap !== undefined && Number(row.star_gap) <= -0.75) rating.append(el('span', 'badge badge-warm', 'Гости оценивают ниже звёзд'));
      body.append(rating);
    }
    const facts = el('div', 'hotel-facts');
    [dateLabel(row.date_start), row.nights + ' ночей', boardLabel(row)].forEach(text => facts.append(el('span', '', text)));
    body.append(facts);
    if (row.room_name) body.append(el('p', 'room-name', row.room_name));
    const prices = el('div', 'price-block'); const priceMain = el('div');
    if (isDrop && Number(row.prev_price_cents) > Number(row.price_cents)) {
      const was = el('div', 'price-was'); was.append(el('s', '', money(row.prev_price_cents, row.currency)), el('span', '', 'наблюдали ' + dateLabel(row.prev_fetched_at))); priceMain.append(was);
    }
    const amount = el('div', 'price-total');
    if (!isDrop && Number(row.variants) > 1) amount.append(el('span', 'price-prefix', 'от'));
    amount.append(document.createTextNode(money(row.price_cents, row.currency)));
    priceMain.append(amount, el('p', 'price-caption', (isArchived(row) ? 'историческая цена · ' : '') + 'за всех · ' + partyLabel(filters)));
    prices.append(priceMain);
    if (Number(row.nights) > 0) prices.append(el('p', 'price-night', money(Number(row.price_cents) / Number(row.nights), row.currency) + ' / ночь'));
    body.append(prices);
    if (row.fetched_at) { const observed = el('p', 'observation-note'); observed.append(icon('clock'), document.createTextNode(observationLabel(row))); body.append(observed); }
    if (Number(row.archived_variants) > 0) body.append(el('p', 'field-hint', 'Среди вариантов есть архивные цены: ' + row.archived_variants + '.'));
    if (isDrop) body.append(el('p', 'field-hint', 'Снижение к предыдущему уровню цены в нашей истории, не прогноз.'));
    const actions = el('div', 'card-actions');
    if (isDrop || Number(row.variants) <= 1) actions.append(button('История цены', 'button', () => showHistory(row), 'trend'));
    else {
      const variants = el('div', 'variants-area'); variants.hidden = true;
      const trigger = button(row.variants + ' вариантов', 'button', () => toggleVariants(variants, trigger, row, filters), 'down');
      trigger.setAttribute('aria-expanded', 'false'); actions.append(trigger);
      body.append(actions); body.append(variants);
    }
    actions.append(button('Подробнее', 'button button-primary', () => showOffer(row, filters), 'arrow'));
    if (!actions.parentElement) body.append(actions);
    card.append(body); return card;
  }
  async function toggleVariants(box, trigger, hotel, filters) {
    if (!hasAccess()) { renderAccess(); return; }
    if (!box.hidden) { box.hidden = true; trigger.setAttribute('aria-expanded', 'false'); return; }
    box.hidden = false; trigger.setAttribute('aria-expanded', 'true');
    if (box.dataset.loaded === 'true') return;
    loading(box, 'Загружаем варианты…');
    trigger.disabled = true;
    try {
      const params = {...filters, hotel_id: hotel.source_hotel_id, group: 'false', limit: '50'};
      if (hotel.source) params.source = hotel.source;
      const data = await api('/api/search?' + new URLSearchParams(params));
      if (!Array.isArray(data.results)) throw new Error('Не удалось загрузить варианты.');
      box.removeAttribute('aria-busy'); box.replaceChildren(...data.results.map(offer => variantRow(offer, filters)));
      appendStorageNotice(box, data.storage);
      box.dataset.loaded = 'true';
      if (!data.results.length) box.append(el('p', 'field-hint', 'Варианты больше не доступны по этим фильтрам.'));
      if (data.results.length >= 50) box.append(el('p', 'variant-count-note', 'Показаны первые 50 вариантов. Сузьте даты или число ночей.'));
    } catch (error) {
      empty(box, 'Варианты не загрузились', error.message, () => { box.hidden = true; toggleVariants(box, trigger, hotel, filters); });
    } finally { trigger.disabled = false; }
  }
  function variantRow(offer, filters) {
    const row = el('div', 'variant-row'); const head = el('div', 'variant-head');
    head.append(el('span', 'variant-title', dateLabel(offer.date_start) + ' · ' + offer.nights + ' н. · ' + boardLabel(offer)), el('strong', 'variant-price', money(offer.price_cents, offer.currency)));
    row.append(head, el('p', 'room-name', offer.room_name || offer.board_name || ''));
    if (isArchived(offer) || offer.stale) row.append(el('p', 'variant-archive-note', observationLabel(offer)));
    const actions = el('div', 'variant-actions');
    actions.append(button('Подробнее о варианте', 'text-button', () => showOffer(offer, filters)), externalLink(offer.link, 'У продавца ↗', 'text-link'));
    row.append(actions); return row;
  }

  async function showOffer(seed, filters = state.lastFilters || currentFilters()) {
    if (!hasAccess()) { renderAccess(); return; }
    if (!seed.offer_id) { toast('У этого предложения нет идентификатора для подробного просмотра.'); return; }
    state.detailRequest?.abort();
    const controller = new AbortController(); state.detailRequest = controller;
    const serial = ++state.detailSerial;
    const dialog = $('offer-dialog'); const target = $('offer-content');
    const replacingFocusedContent = dialog.open && target.contains(document.activeElement);
    const selectedFilters = {...filters};
    $('offer-booking').hidden = true;
    loading(target, 'Открываем детали предложения…');
    openDialog(dialog); target.scrollTop = 0;
    if (replacingFocusedContent) dialog.querySelector('[data-close-dialog]').focus({preventScroll: true});
    try {
      const data = await api('/api/offers/' + encodeURIComponent(seed.offer_id), {signal: controller.signal});
      if (serial !== state.detailSerial || !dialog.open) return;
      if (!data.offer || !data.offer.offer_id) throw new Error('Не удалось прочитать детали предложения.');
      renderOffer(data.offer, selectedFilters, serial, data.storage);
    } catch (error) {
      if (serial !== state.detailSerial || !dialog.open || error.name === 'AbortError') return;
      empty(target, error.status === 404 ? 'Предложение не найдено' : 'Детали пока недоступны', error.status === 404 ? 'Возможно, оно уже удалено из базы. Обновите поиск или выберите другой вариант.' : error.message, () => showOffer(seed, selectedFilters));
    }
  }
  function renderOffer(offer, filters, serial, storage) {
    const target = $('offer-content'); target.removeAttribute('aria-busy');
    const photo = hotelPhoto(offer, true); photo.classList.add('offer-photo');
    const body = el('div', 'offer-body');
    const location = [offer.country_name, offer.city_name].filter(Boolean).join(' · ');
    body.append(el('p', 'hotel-location', location || 'Курорт не указан'));
    const title = el('h3', 'offer-hotel-title', offer.hotel_name || 'Отель без названия');
    const stars = String(offer.category || '').match(/^([1-5])(\+)?/);
    if (stars) title.append(el('span', 'hotel-stars', stars[1] + ' ★' + (stars[2] || '')));
    body.append(title);
    if (offer.review_rating !== null && offer.review_rating !== undefined && Number.isFinite(Number(offer.review_rating))) {
      const rating = el('div', 'hotel-rating');
      const platform = {google: 'Google', tripadvisor: 'TripAdvisor'}[offer.review_platform] || offer.review_platform || 'Гости';
      rating.append(el('span', 'rating-score', Number(offer.review_rating).toFixed(1).replace('.', ',') + ' / ' + (offer.review_scale || 5)), el('span', '', platform + (offer.review_count ? ' · ' + Number(offer.review_count).toLocaleString('ru-RU') + ' отзывов' : '')));
      body.append(rating);
    }
    const party = {adults: offer.pax_adl ?? filters.adults, children_ages: offer.children_ages ?? filters.children_ages};
    const partyText = Number(offer.pax_chd) > 0 && !party.children_ages
      ? String(party.adults) + ' взрослых · ' + offer.pax_chd + ' детей (возраст не указан)'
      : partyLabel(party);
    const price = el('div', 'offer-price');
    price.append(el('strong', '', money(offer.price_cents, offer.currency)), el('span', '', (isArchived(offer) ? 'историческая цена · ' : '') + 'за весь тур · ' + partyText));
    body.append(price);
    const stopped = !['', '0', 'false', 'no', 'n'].includes(String(offer.stop_sale || '').trim().toLowerCase());
    if (stopped) body.append(el('p', 'history-warning', 'По последней проверке источник отметил остановку продаж этого варианта. Уточните у продавца, доступно ли предложение сейчас.'));
    if (isArchived(offer)) body.append(el('p', 'storage-notice', 'Архивное предложение. Эту цену наблюдали ' + dateLabel(offer.fetched_at || offer.last_seen_at, true) + '. Она может уже не действовать; подтвердите цену и наличие у продавца.' + (offer.archive_as_of ? ' Архив обновлён ' + dateLabel(offer.archive_as_of, true) + '.' : '')));
    else if (offer.stale) body.append(el('p', 'history-warning', 'Предложение давно не встречалось в сборе. Последний раз: ' + dateLabel(offer.last_seen_at, true) + '. Наличие и цену нужно подтвердить у продавца.'));
    if (storage?.partial) appendStorageNotice(body, storage);
    const specs = el('dl', 'offer-specs');
    const facts = [
      ['Даты тура', dateLabel(offer.date_start) + (offer.date_end ? ' — ' + dateLabel(offer.date_end) : '')],
      ['Продолжительность', Number(offer.nights) > 0 ? offer.nights + ' ночей' : 'Не указана'],
      ['Питание', boardLabel(offer)],
      ['Номер', offer.room_name || 'Не указан'],
      ['Туристы', partyText],
      ['Оператор', OPERATORS[offer.operator] || offer.operator || 'Не указан'],
    ];
    if (offer.room_placement) facts.push(['Размещение', offer.room_placement]);
    if (Number(offer.nights) > 0) facts.push(['Цена за ночь', money(offer.price_per_night_cents ?? Number(offer.price_cents) / Number(offer.nights), offer.currency) + ' за всех']);
    for (const [label, value] of facts) { const item = el('div'); item.append(el('dt', '', label), el('dd', '', value)); specs.append(item); }
    body.append(specs);
    const originalMeal = offer.board_name || offer.board_code;
    if (originalMeal && originalMeal !== boardLabel(offer)) body.append(el('p', 'field-hint meal-original', 'Питание у источника: ' + originalMeal));
    const observed = offer.fetched_at || offer.last_seen_at;
    if (observed) { const note = el('p', 'observation-note'); note.append(icon('clock'), document.createTextNode(observationLabel(offer))); body.append(note); }
    body.append(el('p', 'offer-note', 'Это собранные данные предложения. Состав услуг, правила питания, наличие и окончательную стоимость подтвердит продавец.'));

    const history = el('details', 'detail-section'); const historySummary = el('summary');
    historySummary.append(icon('trend'), el('span', '', 'История цены'), icon('down')); history.append(historySummary);
    const historyContent = el('div', 'detail-section-content'); history.append(historyContent);
    let historyStarted = false;
    history.addEventListener('toggle', () => {
      if (!history.open || historyStarted) return; historyStarted = true;
      showHistory(offer, historyContent, () => serial === state.detailSerial && $('offer-dialog').open);
    });
    body.append(history);

    if (offer.source && offer.source_hotel_id) {
      const variants = el('details', 'detail-section'); const summary = el('summary');
      summary.append(icon('tune'), el('span', '', 'Другие варианты этого отеля'), icon('down')); variants.append(summary);
      const caption = el('p', 'field-hint', 'По параметрам вашего поиска: ' + filterDescription(filters));
      const list = el('div', 'detail-variants'); const content = el('div', 'detail-section-content'); content.append(caption, list); variants.append(content);
      let variantsStarted = false;
      const load = async () => {
        loading(list, 'Ищем другие даты, номера и питание…');
        try {
          const params = {...filters, hotel_id: offer.source_hotel_id, source: offer.source, group: 'false', limit: '50'};
          const data = await api('/api/search?' + new URLSearchParams(params), {signal: state.detailRequest.signal});
          if (serial !== state.detailSerial || !variants.isConnected) return;
          if (!Array.isArray(data.results)) throw new Error('Не удалось прочитать варианты.');
          const alternatives = data.results.filter(row => String(row.offer_id) !== String(offer.offer_id));
          list.removeAttribute('aria-busy'); list.replaceChildren(...alternatives.map(row => variantRow(row, filters)));
          appendStorageNotice(list, data.storage);
          if (!alternatives.length) list.append(el('p', 'field-hint', 'Других вариантов по этим параметрам пока нет. Можно расширить даты или снять часть фильтров в поиске.'));
          if (data.results.length >= 50) list.append(el('p', 'variant-count-note', 'Показаны первые 50 совпадений.'));
        } catch (error) { if (serial === state.detailSerial && error.name !== 'AbortError' && variants.isConnected) empty(list, 'Варианты не загрузились', error.message, load); }
      };
      variants.addEventListener('toggle', () => { if (variants.open && !variantsStarted) { variantsStarted = true; load(); } });
      body.append(variants);
    }
    target.replaceChildren(photo, body); target.scrollTop = 0;
    const booking = $('offer-booking'); const summary = el('div', 'offer-booking-price');
    summary.append(el('strong', '', money(offer.price_cents, offer.currency)), el('span', '', isArchived(offer) ? 'архивная цена за всех' : 'за всех'));
    booking.replaceChildren(summary, externalLink(offer.link, stopped || offer.stale || isArchived(offer) ? 'Проверить у продавца ↗' : 'Бронировать у продавца ↗'));
    booking.hidden = false;
  }

  async function runDeals() {
    if (!hasAccess()) { renderAccess(); return; }
    // /api/drops currently supports party composition, not the other search filters.
    const filters = currentFilters();
    const ages = String(filters.children_ages || '').split(',').filter(Boolean).map(Number);
    if (!Number.isInteger(filters.adults) || filters.adults < 1 || filters.adults > 6 || ages.length > 4 || ages.some(age => !Number.isInteger(age) || age < 0 || age > 17)) {
      empty($('deals-results'), 'Уточните состав', 'В поиске выберите от 1 до 6 взрослых и возраст каждого ребёнка.', () => navigate('search'), 'Изменить состав'); return;
    }
    $('deals-party').textContent = partyLabel(filters);
    state.dealsRequest?.abort(); const controller = new AbortController(); state.dealsRequest = controller;
    const serial = ++state.dealsSerial;
    const params = {adults: filters.adults, hours: 72, limit: 100}; if (filters.children_ages) params.children_ages = filters.children_ages;
    $('refresh-deals').disabled = true; loading($('deals-results'), 'Сравниваем собранные цены…');
    try {
      const data = await api('/api/drops?' + new URLSearchParams(params), {signal: controller.signal});
      if (serial !== state.dealsSerial) return;
      if (!Array.isArray(data.results)) throw new Error('Не удалось прочитать историю снижений.');
      $('deals-results').removeAttribute('aria-busy');
      $('deals-caption').textContent = data.results.length ? data.results.length + ' предложений · снижение по нашей истории' : 'Для вашего состава пока нет наблюдаемых снижений';
      if (data.results.length) $('deals-results').replaceChildren(...data.results.map(row => hotelCard(row, filters, true)));
      else empty($('deals-results'), 'Ждём движения цены', 'Для вашего состава сейчас нет подтверждённых снижений к предыдущему уровню цены. Подходящие туры могут быть в обычном поиске.', () => navigate('search'), 'Перейти к поиску', 'trend');
    } catch (error) { if (serial === state.dealsSerial && error.name !== 'AbortError') empty($('deals-results'), 'История пока недоступна', error.message, runDeals, 'Повторить', 'trend'); }
    finally { if (serial === state.dealsSerial) $('refresh-deals').disabled = false; }
  }

  async function showHistory(offer, embeddedTarget = null, isCurrent = () => true) {
    if (!hasAccess()) { renderAccess(); return; }
    const serial = embeddedTarget ? null : ++state.historySerial;
    const target = embeddedTarget || $('history-content');
    const current = () => embeddedTarget ? isCurrent() && target.isConnected : serial === state.historySerial && $('history-dialog').open;
    if (!embeddedTarget) { openDialog($('history-dialog')); $('history-title').textContent = 'История цены'; }
    loading(target, 'Загружаем наши наблюдения…');
    try {
      const data = await api('/api/offers/' + encodeURIComponent(offer.offer_id) + '/history');
      if (!current()) return;
      const rows = (Array.isArray(data.history) ? data.history : []).filter(row => Number.isFinite(Number(row.price_cents)) && Number.isFinite(Date.parse(row.fetched_at))).sort((a, b) => Date.parse(a.fetched_at) - Date.parse(b.fetched_at));
      const currency = data.statistics?.currency || offer.currency || rows.at(-1)?.currency || 'EUR';
      const history = rows.filter(row => (row.currency || currency) === currency);
      target.removeAttribute('aria-busy'); target.replaceChildren(el('h3', 'history-headline', offer.hotel_name || 'Выбранный вариант'), el('p', 'field-hint', dateLabel(offer.date_start) + ' · ' + offer.nights + ' ночей · ' + boardLabel(offer)));
      appendStorageNotice(target, data.storage, 'history');
      if (data.history_truncated) target.append(el('p', 'history-warning', 'На графике последние ' + rows.length + ' из ' + (data.history_total || rows.length) + ' наблюдений. Более ранние цены учтены в статистике, когда история доступна полностью.'));
      if (data.gone) target.append(el('p', 'history-warning', isArchived(offer) ? 'Это история архивного предложения. Наличие и текущая цена не подтверждены.' : 'Предложение давно не встречалось в сборе. Последний раз: ' + dateLabel(data.last_seen_at, true) + '. Это не подтверждение, что оно распродано.'));
      if (!history.length) { target.append(el('p', 'history-warning', 'Для этого предложения пока нет доступных наблюдений.')); return; }
      const first = history[0]; const last = history.at(-1);
      if (history.length >= 2) target.append(priceChart(history));
      else target.append(el('p', 'history-warning', 'Пока только одно наблюдение. Для сравнения нужно дождаться следующего сбора.'));
      const labels = el('div', 'chart-labels'); labels.append(el('span', '', dateLabel(first.fetched_at, true)), el('span', '', history.length > 1 ? dateLabel(last.fetched_at, true) : '')); target.append(labels);
      const stats = el('div', 'history-stat-grid');
      let minimum = Infinity; for (const row of history) minimum = Math.min(minimum, Number(row.price_cents));
      const fullMinimum = data.statistics?.currency === currency && data.statistics.min_seen_cents !== null && Number.isFinite(Number(data.statistics.min_seen_cents));
      if (fullMinimum) minimum = Number(data.statistics.min_seen_cents);
      const minimumLabel = data.statistics?.complete === false ? 'Минимум в доступных наблюдениях' : data.history_truncated && !fullMinimum ? 'Минимум на показанном отрезке' : 'Минимум в нашей истории';
      for (const [label, value] of [['Последнее наблюдение', last.price_cents], [minimumLabel, minimum]]) { const stat = el('div', 'history-stat'); stat.append(el('strong', '', money(value, currency)), el('span', '', label)); stats.append(stat); }
      target.append(stats);
      if (history.length > 1) {
        const difference = Number(last.price_cents) - Number(first.price_cents);
        target.append(el('p', 'field-hint', (data.history_truncated ? 'От первого показанного наблюдения: ' : 'От первого наблюдения: ') + (difference < 0 ? 'дешевле на ' : difference > 0 ? 'дороже на ' : 'цена не изменилась') + (difference ? money(Math.abs(difference), currency) : '') + '. Это история одного варианта, не прогноз.'));
      }
      target.append(el('p', 'field-hint', 'Точки — фактические наблюдения. Между ними цена могла меняться. Рекламная «старая цена» источника здесь не используется.'));
      const detail = el('details', 'history-observations'); detail.append(el('summary', '', 'Посмотреть наблюдения (' + history.length + ')'));
      const table = el('table', 'history-table'); const head = el('thead'); const header = el('tr'); header.append(el('th', '', 'Когда проверили'), el('th', '', 'Цена')); head.append(header); table.append(head);
      const tbody = el('tbody');
      for (const row of history.slice(-50).reverse()) { const tr = el('tr'); tr.append(el('td', '', dateLabel(row.fetched_at, true)), el('td', '', money(row.price_cents, currency))); tbody.append(tr); }
      table.append(tbody); detail.append(table);
      if (history.length > 50) detail.append(el('p', 'field-hint', data.history_truncated ? 'В таблице последние 50 наблюдений; график показывает последние 2000.' : 'В таблице последние 50 наблюдений; график учитывает всю доступную историю.'));
      target.append(detail);
    } catch (error) { if (current()) empty(target, 'История не загрузилась', error.message, () => showHistory(offer, embeddedTarget, isCurrent), 'Повторить', 'trend'); }
  }
  function priceChart(history) {
    const svg = document.createElementNS(SVG_NS, 'svg'); svg.classList.add('history-chart'); svg.setAttribute('viewBox', '0 0 400 160'); svg.setAttribute('role', 'img'); svg.setAttribute('aria-label', 'График фактических наблюдений цены');
    const values = history.map(row => Number(row.price_cents)); const times = history.map(row => Date.parse(row.fetched_at));
    const min = values.reduce((a, b) => Math.min(a, b), Infinity); const max = values.reduce((a, b) => Math.max(a, b), -Infinity);
    const x = value => times.at(-1) === times[0] ? 200 : 10 + (value - times[0]) / (times.at(-1) - times[0]) * 380;
    const y = value => max === min ? 80 : 145 - (value - min) / (max - min) * 125;
    for (const height of [20, 80, 145]) { const line = document.createElementNS(SVG_NS, 'line'); for (const [key, value] of Object.entries({x1: 10, y1: height, x2: 390, y2: height, stroke: 'var(--line)', 'stroke-dasharray': '4 5'})) line.setAttribute(key, value); svg.append(line); }
    const polyline = document.createElementNS(SVG_NS, 'polyline'); polyline.setAttribute('points', history.map((row, index) => x(times[index]).toFixed(2) + ',' + y(values[index]).toFixed(2)).join(' ')); polyline.setAttribute('fill', 'none'); polyline.setAttribute('stroke', 'currentColor'); polyline.setAttribute('stroke-width', '2.5'); polyline.setAttribute('stroke-linejoin', 'round'); svg.append(polyline);
    for (const index of [0, history.length - 1]) { const dot = document.createElementNS(SVG_NS, 'circle'); dot.setAttribute('cx', x(times[index])); dot.setAttribute('cy', y(values[index])); dot.setAttribute('r', '4'); dot.setAttribute('fill', 'currentColor'); svg.append(dot); }
    return svg;
  }

  function openSave(filters) {
    if (!state.user) { navigate('saved'); haptic(); return; }
    const selected = filters || validateFilters(); if (!selected) { navigate('search'); return; }
    state.saveFilters = {...selected};
    const saveForm = $('save-form'); saveForm.reset();
    saveForm.elements.name.value = 'Отпуск ' + dateLabel(selected.date_from);
    $('save-filters-summary').textContent = filterDescription(selected);
    $('save-delivery-note').textContent = state.canNotify ? 'Сообщения придут в ваш чат с ботом.' : 'Поиск сохранится. Для сообщений откройте бота и нажмите «Старт».';
    showError($('save-validation'), ''); syncCriteriaMode(); openDialog($('save-dialog'));
  }
  function syncCriteriaMode() {
    const budgetOnly = $('save-form').elements.notify_mode.value === 'budget';
    $('deal-criteria').hidden = budgetOnly;
    for (const input of $('deal-criteria').querySelectorAll('input')) input.disabled = budgetOnly;
  }
  async function saveSubscription(event) {
    event.preventDefault();
    if (!state.user || !state.saveFilters) { closeDialog($('save-dialog')); navigate('saved'); return; }
    const fields = $('save-form').elements; const mode = fields.notify_mode.value;
    if (mode !== 'deal' && !state.saveFilters.budget_max) { showError($('save-validation'), 'Для сигналов по бюджету сначала задайте бюджет в поиске. Или выберите «О заметной выгоде».'); return; }
    if (!$('save-form').reportValidity()) return;
    if (!fields.name.value.trim()) { showError($('save-validation'), 'Дайте поиску короткое название.'); return; }
    const allowedFilters = ['date_from', 'date_till', 'adults', 'children_ages', 'nights_min', 'nights_max', 'budget_max', 'boards', 'board_categories', 'countries', 'only_hot', 'stars_min'];
    const filters = Object.fromEntries(allowedFilters.filter(key => state.saveFilters[key] !== undefined).map(key => [key, state.saveFilters[key]]));
    const payload = {name: fields.name.value.trim(), filters, notify_mode: mode, min_drop_pct: mode === 'budget' ? 10 : Number(fields.min_drop_pct.value), min_saving_eur: mode === 'budget' ? 100 : Number(fields.min_saving_eur.value), min_review_rating: mode === 'budget' ? 4 : Number(fields.min_review_rating.value), min_review_count: mode === 'budget' ? 20 : Number(fields.min_review_count.value)};
    $('confirm-save').disabled = true; showError($('save-validation'), '');
    try {
      const result = await api('/api/subscriptions', {method: 'POST', body: payload});
      if (result.collection_requested === true) {
        const spec = compositionSpec(filters); state.queuedPax.add(spec); state.paxErrors.delete(spec); refreshCompositionRequests(spec);
      }
      closeDialog($('save-dialog')); haptic('medium'); navigate('saved');
      toast(result.collection_requested === true ? 'Поиск сохранён, состав добавлен в сбор. Предложения зависят от следующего успешного обновления источников.' : state.canNotify ? 'Поиск сохранён. Сообщим о подходящей цене.' : 'Поиск сохранён. Нажмите «Старт» в боте для сообщений.');
    } catch (error) { showError($('save-validation'), error.message); }
    finally { $('confirm-save').disabled = false; }
  }
  async function loadSaved() {
    if (!state.user) return;
    const serial = ++state.savedSerial;
    loading($('subscriptions-list'), 'Загружаем ваши поиски…');
    loading($('alerts-list'), 'Проверяем непрочитанные сигналы…');
    const outcomes = await Promise.allSettled([api('/api/subscriptions'), api('/api/alerts')]);
    if (serial !== state.savedSerial || !state.user) return;
    const [subscriptions, alerts] = outcomes;
    if (subscriptions.status === 'fulfilled' && Array.isArray(subscriptions.value.subscriptions)) {
      state.subscriptions = subscriptions.value.subscriptions;
      $('saved-caption').textContent = state.subscriptions.filter(sub => sub.enabled).length + ' активных';
      $('subscriptions-list').removeAttribute('aria-busy');
      if (state.subscriptions.length) $('subscriptions-list').replaceChildren(...state.subscriptions.map(subscriptionCard));
      else empty($('subscriptions-list'), 'Первый поиск — с вас', 'Задайте даты, состав и бюджет. Сохраните параметры, чтобы не проверять цены вручную.', () => navigate('search'), 'Подобрать тур', 'bookmark');
    } else empty($('subscriptions-list'), 'Поиски не загрузились', subscriptions.reason?.message || 'Попробуйте обновить страницу.', loadSaved);
    if (alerts.status === 'fulfilled' && Array.isArray(alerts.value.alerts)) {
      const rows = alerts.value.alerts;
      $('alerts-list').removeAttribute('aria-busy');
      $('saved-nav-count').hidden = !rows.length; $('saved-nav-count').textContent = rows.length > 99 ? '99+' : rows.length;
      if (rows.length) $('alerts-list').replaceChildren(...rows.map(alertCard));
      else empty($('alerts-list'), 'Пока тихо', 'Здесь появятся непрочитанные совпадения ваших поисков. Их наличие зависит от свежего сбора предложений.', null, '', 'bell');
    } else empty($('alerts-list'), 'Сигналы не загрузились', alerts.reason?.message || 'Попробуйте позже.', loadSaved, 'Повторить', 'bell');
  }
  function subscriptionCard(sub) {
    const card = el('article', 'subscription-card' + (sub.enabled ? '' : ' paused'));
    const top = el('div', 'subscription-top'); top.append(el('h3', '', sub.name || 'Мой поиск'), el('span', 'badge ' + (sub.enabled ? 'badge-deal' : 'badge-warm'), sub.enabled ? 'Следим' : 'Пауза')); card.append(top);
    card.append(el('p', '', filterDescription(sub.filters || {})));
    const mode = sub.notify_mode || 'deal'; const criteria = [];
    if (mode !== 'budget') criteria.push('Снижение от ' + (sub.min_drop_pct ?? 10) + '% и ' + money(Number(sub.min_saving_eur ?? 100) * 100) + '; оценка от ' + (sub.min_review_rating ?? 4) + '/5, от ' + (sub.min_review_count ?? 20) + ' отзывов');
    if (mode !== 'deal') criteria.push('Новые совпадения в бюджете');
    card.append(el('div', 'subscription-criteria', criteria.join(' · ')));
    const actions = el('div', 'subscription-actions');
    actions.append(button('Посмотреть', 'text-button', () => { applyFilters(sub.filters || {}); navigate('search'); runSearch(); }));
    const toggle = button(sub.enabled ? 'Пауза' : 'Включить', 'text-button', async () => {
      toggle.disabled = true;
      try { await api('/api/subscriptions/' + encodeURIComponent(sub.id), {method: 'PATCH', body: {enabled: !sub.enabled}}); await loadSaved(); }
      catch (error) { toast(error.message); toggle.disabled = false; }
    });
    const remove = button('Удалить', 'text-button danger-button', async () => {
      if (!await confirmDelete(sub.name || 'Мой поиск')) return;
      remove.disabled = true;
      try { await api('/api/subscriptions/' + encodeURIComponent(sub.id), {method: 'DELETE'}); await loadSaved(); toast('Поиск удалён'); }
      catch (error) { toast(error.message); remove.disabled = false; }
    });
    actions.append(toggle, remove); card.append(actions); return card;
  }
  function confirmDelete(name) {
    const message = 'Удалить поиск «' + name + '» и его уведомления?';
    return new Promise(resolve => {
      if (initData && typeof tg?.showConfirm === 'function') { try { tg.showConfirm(message, resolve); return; } catch { /* Browser confirmation below. */ } }
      resolve(window.confirm(message));
    });
  }
  function alertCard(alert) {
    const card = el('article', 'alert-card');
    const description = alert.reason === 'price_drop' ? 'Цена снизилась' : alert.reason === 'deal' ? 'Есть заметная выгода' : 'Подходит вашему поиску';
    card.append(el('span', 'badge badge-deal', description), el('h3', '', alert.hotel_name || 'Предложение'), el('p', '', alert.sub_name || 'Сохранённый поиск'), el('p', '', [alert.country_name, dateLabel(alert.date_start), alert.nights + ' ночей', alert.board_code].filter(Boolean).join(' · ')), el('p', 'alert-price', money(alert.price_cents, alert.currency || 'EUR')));
    const actions = el('div', 'alert-actions');
    actions.append(alert.offer_id ? button('Подробнее', 'button button-primary', () => showOffer(alert)) : externalLink(alert.link, 'Посмотреть ↗'));
    const dismiss = button('Прочитано', 'text-button', async () => {
      dismiss.disabled = true;
      try { await api('/api/alerts/seen', {method: 'POST', body: {ids: [alert.id]}}); card.remove(); if (!$('alerts-list').children.length) loadSaved(); else { const count = $('alerts-list').children.length; $('saved-nav-count').textContent = count > 99 ? '99+' : count; } }
      catch (error) { toast(error.message); dismiss.disabled = false; }
    }); actions.append(dismiss); card.append(actions); return card;
  }

  function syncTelegramTheme() {
    const dark = initData ? tg?.colorScheme === 'dark' : window.matchMedia('(prefers-color-scheme: dark)').matches;
    document.body.dataset.theme = dark ? 'dark' : 'light';
    if (initData && tg?.themeParams) {
      const mapping = {bg_color: '--surface', secondary_bg_color: '--bg', text_color: '--text', hint_color: '--muted', button_color: '--accent', button_text_color: '--accent-text'};
      for (const [key, variable] of Object.entries(mapping)) {
        const value = tg.themeParams[key];
        if (typeof value === 'string' && /^#[0-9a-f]{6}$/i.test(value)) document.documentElement.style.setProperty(variable, value);
        else document.documentElement.style.removeProperty(variable);
      }
      try { tg.setHeaderColor('secondary_bg_color'); tg.setBackgroundColor('secondary_bg_color'); } catch { /* Supported on modern Telegram. */ }
    }
    document.querySelector('meta[name=theme-color]').content = getComputedStyle(document.body).backgroundColor;
  }
  function syncSafeArea() {
    const safe = tg?.safeAreaInset || {}; const content = tg?.contentSafeAreaInset || {};
    for (const direction of ['top', 'bottom', 'left', 'right']) {
      const physical = Math.max(0, Number(safe[direction]) || 0); const telegram = Math.max(0, Number(content[direction]) || 0);
      document.documentElement.style.setProperty('--safe-' + direction, (physical + telegram) + 'px');
    }
  }

  form.elements.date_from.value = localDate(1); form.elements.date_till.value = localDate(21);
  form.elements.date_from.min = localDate(); form.elements.date_till.min = localDate();
  form.addEventListener('submit', event => { event.preventDefault(); runSearch(); });
  form.elements.children.addEventListener('input', () => syncChildAges());
  $('meals-any').addEventListener('click', () => selectMeals([]));
  $('meals-included').addEventListener('click', () => selectMeals(INCLUDED_MEALS));
  for (const input of form.querySelectorAll('[name=board_category], [name=board]')) input.addEventListener('change', syncMealPresets);
  for (const step of document.querySelectorAll('[data-step]')) step.addEventListener('click', () => {
    const [name, amount] = step.dataset.step.split(':'); const input = form.elements[name];
    input.value = Math.max(Number(input.min), Math.min(Number(input.max), (Number(input.value) || 0) + Number(amount)));
    input.dispatchEvent(new Event('input', {bubbles: true})); haptic();
  });
  $('filters-toggle').addEventListener('click', () => collapseFilters(!$('filter-fields').hidden));
  $('save-search').addEventListener('click', () => openSave(state.lastFilters));
  $('new-watchlist').addEventListener('click', () => openSave());
  $('save-form').addEventListener('submit', saveSubscription);
  $('save-form').addEventListener('change', () => { syncCriteriaMode(); showError($('save-validation'), ''); });
  $('refresh-deals').addEventListener('click', runDeals);
  $('refresh-saved').addEventListener('click', loadSaved);
  $('change-party').addEventListener('click', () => { navigate('search'); collapseFilters(false); form.elements.adults.focus(); });
  for (const nav of document.querySelectorAll('[data-view]')) nav.addEventListener('click', () => { haptic(); navigate(nav.dataset.view); });
  for (const closer of document.querySelectorAll('[data-close-dialog]')) closer.addEventListener('click', () => closeDialog(closer.closest('dialog')));
  for (const dialog of document.querySelectorAll('dialog')) {
    dialog.addEventListener('close', () => {
      if (dialog.id === 'offer-dialog') { state.detailRequest?.abort(); state.detailRequest = null; state.detailSerial++; }
      updateBackButton();
      const opener = dialogOpeners.get(dialog); if (opener?.isConnected) opener.focus({preventScroll: true});
    });
    dialog.addEventListener('click', event => { if (event.target === dialog) { const rect = dialog.getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) closeDialog(dialog); } });
  }
  if (initData && tg) {
    try {
      tg.ready(); tg.expand();
      tg.onEvent('themeChanged', syncTelegramTheme); tg.onEvent('safeAreaChanged', syncSafeArea); tg.onEvent('contentSafeAreaChanged', syncSafeArea);
      tg.BackButton?.onClick(() => { const dialog = Array.from(document.querySelectorAll('dialog[open]')).at(-1); if (dialog) closeDialog(dialog); else navigate('search'); });
      tg.MainButton?.hide();
    } catch { /* Search still works in older Telegram clients. */ }
  }
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener?.('change', syncTelegramTheme);
  document.addEventListener('visibilitychange', () => { if (!document.hidden && initData && !demoMode && state.session !== 'expired') verifySession(); });
  syncTelegramTheme(); syncSafeArea(); syncMealPresets(); updateBackButton(); renderAccess(); verifySession();
})();
