// Execute the actual saved-filter/options handlers without packages or a browser.
// DOM doubles model only the controls touched here; no production API is called.
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = readFileSync(path.join(__dirname, '../static/miniapp.js'), 'utf8');
function section(from, until) {
  const start = source.indexOf(from), end = source.indexOf(until, start + from.length);
  assert.ok(start >= 0 && end > start, 'real handler boundaries must exist');
  return source.slice(start, end);
}
const handlers = [
  section('  const AIRPORTS =', '  const dialogOpeners ='),
  section('  function countLabel(', '  function dateLabel('),
  section('  function el(', '  function icon('),
  section('  function originCodes(', '  function partyLabel('),
  section('  function currentFilters(', '  function collapseFilters('),
  section('  function applyFilters(', '  function updateBackButton('),
  section('  function matchingFilterPayload(', '  function refreshCollectionRequests('),
  section('  async function toggleVariants(', '  function variantRow('),
  section('  async function runDeals(', '  async function showHistory('),
  section('  async function saveSubscription(', '  async function loadSaved('),
  section('  function openSavedSearch(', '  function subscriptionCard('),
].join('\n');

class Element {
  constructor(tag, className = '') {
    Object.assign(this, {tag, className, children: [], attributes: {}, dataset: {}, checked: false, value: '', listeners: {}});
  }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return (this.text || '') + this.children.map(child => child.textContent).join(''); }
  set innerHTML(value) { throw new Error('untrusted labels must use textContent, never innerHTML'); }
  get nextElementSibling() { return this.parent?.children[this.parent.children.indexOf(this) + 1] || null; }
  append(...children) { for (const child of children) { child.parent = this; this.children.push(child); } }
  replaceChildren(...children) { this.children = []; this.append(...children); }
  setAttribute(name, value) { this.attributes[name] = value; }
  removeAttribute(name) { delete this.attributes[name]; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  closest(selector) { return this.parent?.matches(selector) ? this.parent : this.parent?.closest(selector); }
  matches(selector) {
    const name = selector.match(/\[name=([^\]]+)\]/)?.[1];
    const tag = selector.match(/^[a-z]+/)?.[0];
    const className = selector.match(/^\.([\w-]+)/)?.[1];
    return (!name || this.name === name) && (!tag || this.tag === tag)
      && (!className || this.className.split(' ').includes(className))
      && (!selector.includes(':checked') || this.checked);
  }
  querySelectorAll(selector) {
    return this.children.flatMap(child => [...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector)]);
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}

const SAVED = {date_from: '2026-10-10', date_till: '2026-11-10', origins: 'RIX,TLL,VNO', adults: 1, children_ages: '8,6',
  nights_min: 5, nights_max: 10, countries: 'EG', boards: 'SOFTAI', board_categories: 'AI',
  budget_max: 1500, stars_min: 4, only_hot: true, sort: 'price_per_night'};
const LABELS = {RO: 'Без питания', BB: 'Завтраки', HB: 'Двухразовое', FB: 'Трёхразовое',
  AI: 'Всё включено', UAI: 'Ультра всё включено', OTHER: 'Другое'};
const OPTIONS = {countries: [{country_id: 'EG', country_name: 'Египет'}],
  boards: [{board_code: 'SOFTAI', board_name: 'Soft all inclusive'}], storage: {live: 'ok'}};

function harness() {
  const form = new Element('form'), elements = new Map();
  form.reportValidity = () => true;
  form.elements = Object.fromEntries(['date_from', 'date_till', 'adults', 'nights_min', 'nights_max',
    'budget_max', 'stars_min', 'sort', 'children', 'only_hot'].map(name => [name, new Element('input')]));
  function get(id) {
    if (!elements.has(id)) elements.set(id, new Element('div'));
    return elements.get(id);
  }
  const more = new Element('details', 'more-filters'); more.append(get('raw-meals'));
  get('raw-meals').append(get('board-options'));
  const meals = new Element('div', 'meal-chips');
  form.append(get('origin-options'), more, meals, get('country-options'), get('age-inputs'));
  for (const [value, name] of [['RIX', 'Рига'], ['VNO', 'Вильнюс'], ['TLL', 'Таллин']]) {
    const input = new Element('input'); Object.assign(input, {name: 'origin', value, checked: true});
    const item = new Element('label'), span = new Element('span'); span.textContent = name;
    item.append(input, span); get('origin-options').append(item);
  }
  for (const [value, label] of Object.entries(LABELS)) {
    const input = new Element('input'); Object.assign(input, {name: 'board_category', value});
    const item = new Element('label'), span = new Element('span'); span.textContent = label;
    item.append(input, span); meals.append(item);
  }
  const calls = [], searches = [], toasts = [], errors = [], navigation = [];
  let options = async () => OPTIONS;
  let subscriptionReply = {id: 1, collection_requested: true, collection_request: {id: 7, status: 'queued', queued: true}};
  const context = vm.createContext({
    AbortController, URLSearchParams,
    form, $: get, document: {createElement: tag => new Element(tag)},
    state: {user: {id: 1}, optionsLoaded: false, optionsLoading: false, dealsSerial: 0,
      queuedRequests: new Set(), pendingRequests: new Set(), requestErrors: new Map()},
    BOARD_LABELS: LABELS, INCLUDED_MEALS: ['BB', 'HB', 'FB', 'AI', 'UAI'],
    localDate: () => '2026-10-09', hasAccess: () => true, haptic: () => {}, collapseFilters: () => {},
    showError: (target, error) => errors.push(error), toast: text => toasts.push(text),
    navigate: view => navigation.push(view),
    partyLabel: () => '1 взрослый', loading: target => target.replaceChildren(),
    empty: () => {}, appendStorageNotice: () => {}, closeDialog: () => {}, refreshCollectionRequests: () => {},
    api: async (url, request = {}) => {
      if (url === '/api/options') { calls.push(url); return options(); }
      calls.push({url, request: JSON.parse(JSON.stringify(request))});
      if (url === '/api/subscriptions') return subscriptionReply;
      assert.ok(url.startsWith('/api/search?') || url.startsWith('/api/drops?'));
      return {results: [], storage: {}};
    },
    syncChildAges: ages => get('age-inputs').replaceChildren(...ages.map(age => {
      const input = new Element('input'); input.value = age; return input;
    })),
    runSearch: async () => {
      const filters = context.validateFilters();
      if (filters) searches.push(JSON.parse(JSON.stringify(filters)));
    },
  });
  vm.runInContext(handlers, context);
  return {context, get, form, calls, searches, toasts, errors, navigation,
    setOptions: callback => { options = callback; },
    setSubscriptionReply: reply => { subscriptionReply = reply; },
    filters: () => JSON.parse(JSON.stringify(context.currentFilters())),
    input: (name, value) => form.querySelectorAll('[name=' + name + ']').find(item => item.value === value),
    refresh: () => { context.state.optionsLoaded = false; return context.loadOptions(); }};
}

function assertSavedFilters(h) {
  assert.deepEqual(h.filters(), {...SAVED, children_ages: '6,8'});
}

test('saved constraints are visible and preserved before delayed dictionaries, then receive real labels', async () => {
  const h = harness();
  let resolve;
  h.setOptions(() => new Promise(done => { resolve = done; }));
  const loading = h.context.loadOptions();
  h.context.applyFilters(SAVED);
  assertSavedFilters(h);
  assert.match(h.input('country', 'EG').nextElementSibling.textContent, /Сохранённая страна · EG/);
  assert.match(h.input('board', 'SOFTAI').nextElementSibling.textContent, /Сохранённое питание · SOFTAI/);
  resolve(OPTIONS); await loading;
  assertSavedFilters(h);
  assert.equal(h.input('country', 'EG').nextElementSibling.textContent, 'Египет');
  assert.equal(h.input('board', 'SOFTAI').nextElementSibling.textContent, 'SOFTAI · Soft all inclusive');
  assert.deepEqual(h.calls, ['/api/options']);
});

test('missing values after options load, failure and retry never broaden a saved search', async () => {
  const h = harness();
  h.setOptions(async () => ({countries: [{country_id: 'TR', country_name: 'Турция'}], boards: [{board_code: 'AI'}]}));
  await h.context.loadOptions();
  h.context.applyFilters(SAVED); assertSavedFilters(h);
  h.setOptions(async () => { throw new Error('fixture options unavailable'); });
  await h.refresh(); assertSavedFilters(h);
  assert.match(h.get('options-note').textContent, /Не удалось обновить/);
  h.setOptions(async () => OPTIONS);
  await h.context.loadOptions(); assertSavedFilters(h);
  assert.equal(h.input('country', 'EG').nextElementSibling.textContent, 'Египет');
});

test('intentional edits during options loading win over old saved choices', async () => {
  const h = harness(); h.context.applyFilters(SAVED);
  let resolve;
  h.setOptions(() => new Promise(done => { resolve = done; }));
  const loading = h.context.loadOptions();
  h.input('country', 'EG').checked = false;
  h.context.selectMeals(['BB']); // user deliberately removes the raw SOFTAI constraint
  resolve(OPTIONS); await loading;
  const edited = h.filters();
  assert.equal(edited.countries, undefined);
  assert.equal(edited.boards, undefined);
  assert.equal(edited.board_categories, 'BB');
  h.context.applyFilters({...SAVED, countries: 'TR', boards: 'BB', board_categories: 'BB'});
  assert.equal(h.filters().countries, 'TR');
  assert.equal(h.filters().boards, 'BB');
  assert.equal(h.input('country', 'EG').checked, false);
  assert.equal(h.input('board', 'SOFTAI').checked, false);
});

test('unknown saved category and raw labels remain literal text, never markup', () => {
  const h = harness(); const raw = '<img src=x onerror=alert(1)>';
  h.context.applyFilters({...SAVED, countries: raw, boards: raw, board_categories: 'LEGACY'});
  assert.equal(h.filters().countries, raw);
  assert.equal(h.filters().boards, raw);
  assert.equal(h.filters().board_categories, 'LEGACY');
  assert.equal(h.input('country', raw).nextElementSibling.textContent, 'Сохранённая страна · ' + raw);
  assert.equal(h.form.querySelectorAll('img').length, 0);
});

test('opening an active saved period excludes past days visibly without mutating the subscription', async () => {
  const h = harness();
  const sub = {id: 7, filters: {...SAVED, date_from: '2026-10-08'}};
  const before = JSON.stringify(sub);
  await h.context.openSavedSearch(sub);
  assert.equal(JSON.stringify(sub), before);
  assert.deepEqual(h.searches, [{...SAVED, children_ages: '6,8', date_from: '2026-10-09'}]);
  assert.deepEqual(h.navigation, ['search']);
  assert.match(h.toasts[0], /прошедшие даты исключены/);
  assert.deepEqual(h.calls, []); // no PATCH/POST to the persisted saved search
});

test('expired saved period stays expired and does not search or invent future dates', async () => {
  const h = harness(); const sub = {filters: {...SAVED, date_from: '2026-10-01', date_till: '2026-10-08'}};
  const before = JSON.stringify(sub);
  await h.context.openSavedSearch(sub);
  assert.equal(JSON.stringify(sub), before);
  assert.deepEqual(h.searches, []); assert.deepEqual(h.navigation, []); assert.deepEqual(h.calls, []);
  assert.match(h.toasts[0], /Даты этого поиска уже прошли/);
});

test('future saved dates are unchanged and ordinary manual past-date validation remains strict', async () => {
  const h = harness(); await h.context.openSavedSearch({filters: SAVED});
  assert.deepEqual(h.searches, [{...SAVED, children_ages: '6,8'}]);
  assert.deepEqual(h.toasts, []);
  h.context.applyFilters({...SAVED, date_from: '2026-10-08'});
  assert.equal(h.context.validateFilters(), null);
  assert.match(h.errors.at(-1), /не раньше сегодняшней/);
});

test('new form defaults to all three, legacy saved search stays Riga, and explicit empty airports are invalid', () => {
  const h = harness();
  assert.equal(h.filters().origins, 'RIX,TLL,VNO');
  const legacy = {...SAVED}; delete legacy.origins;
  h.context.applyFilters(legacy);
  assert.equal(h.filters().origins, 'RIX');
  assert.equal(h.get('route-origin').textContent, 'Рига');
  h.context.selectOrigins([]);
  assert.equal(h.filters().origins, '');
  assert.equal(h.context.validateFilters(), null);
  assert.match(h.errors.at(-1), /Выберите хотя бы один аэропорт/);
  h.context.selectOrigins(['TLL', 'RIX', 'VNO']);
  assert.equal(h.filters().origins, 'RIX,TLL,VNO');
  assert.equal(h.get('origins-any').attributes['aria-pressed'], 'true');
  assert.equal(h.get('route-origin').textContent, 'Три аэропорта');
});

test('airport choices survive delayed dictionaries and user changes are retained exactly', async () => {
  const h = harness(); h.context.applyFilters({...SAVED, origins: 'VNO'});
  let resolve; h.setOptions(() => new Promise(done => { resolve = done; }));
  const loading = h.context.loadOptions();
  h.context.selectOrigins(['TLL']);
  resolve(OPTIONS); await loading;
  assert.equal(h.filters().origins, 'TLL');
  assert.equal(h.get('route-origin').textContent, 'Таллин');
  assert.equal(h.input('origin', 'VNO').checked, false);
  h.context.applyFilters({...SAVED, origins: 'VNO,XYZ'});
  assert.equal(h.filters().origins, 'VNO,XYZ');
  assert.match(h.input('origin', 'XYZ').nextElementSibling.textContent, /Неизвестный аэропорт · XYZ/);
  assert.equal(h.context.validateFilters(), null);
  h.context.selectOrigins(['VNO']);
  assert.equal(h.context.validateFilters().origins, 'VNO');
});

test('actual offer origin is never inferred from an Any search or hardcoded to Riga', () => {
  const h = harness();
  assert.equal(h.context.originSelectionLabel({origins: 'RIX,TLL,VNO'}), 'Рига, Вильнюс, Таллин');
  assert.equal(h.context.offerOriginLabel({origin_code: 'VNO', origin_name: 'Vilnius'}), 'Вильнюс (VNO)');
  assert.equal(h.context.offerOriginLabel({origin_code: 'TLL', origin_name: 'Tallinn'}), 'Таллин (TLL)');
  assert.equal(h.context.offerOriginLabel({origin_name: 'Unknown source airport'}), 'Unknown source airport');
  assert.equal(h.context.offerOriginLabel({}), 'Аэропорт не указан');
});

test('hotel card variant button uses Russian count forms including teen exceptions', () => {
  const h = harness();
  Object.assign(h.context, {OPERATORS: {}, isArchived: () => false,
    hotelPhoto: () => new Element('div'), boardLabel: () => 'Завтраки',
    dateLabel: value => value, money: () => '900 €',
    button: (text, className) => { const item = new Element('button', className); item.textContent = text; return item; },
  });
  h.context.document.createTextNode = text => { const node = new Element('text'); node.textContent = text; return node; };
  vm.runInContext(section('  function hotelCard(', '  async function toggleVariants('), h.context);
  for (const [count, label] of [[2, '2 варианта'], [3, '3 варианта'], [4, '4 варианта'], [5, '5 вариантов'],
    [11, '11 вариантов'], [14, '14 вариантов'], [21, '21 вариант'], [22, '22 варианта'], [25, '25 вариантов']]) {
    const card = h.context.hotelCard({variants: count, origin_code: 'VNO', date_start: '2026-10-16', nights: 7}, SAVED);
    assert.ok(card.querySelectorAll('button').some(item => item.textContent === label));
  }
});

test('variant and drops requests preserve exact airports, while drops deliberately exclude other search filters', async () => {
  const h = harness(); h.context.applyFilters({...SAVED, origins: 'VNO'});
  const box = new Element('div'); box.hidden = true;
  await h.context.toggleVariants(box, new Element('button'), {source: 'waavo', source_hotel_id: 'fixture'}, h.filters());
  const variants = new URLSearchParams(h.calls[0].url.split('?')[1]);
  assert.equal(variants.get('origins'), 'VNO');
  assert.equal(variants.get('source'), 'waavo');
  assert.equal(variants.get('stars_min'), '4');
  assert.equal(variants.get('board_categories'), 'AI');
  await h.context.runDeals();
  const drops = new URLSearchParams(h.calls[1].url.split('?')[1]);
  assert.equal(drops.get('origins'), 'VNO');
  assert.equal(drops.get('adults'), '1');
  assert.equal(drops.get('children_ages'), '6,8');
  assert.equal(drops.has('date_from'), false);
  assert.equal(drops.has('stars_min'), false);
  assert.match(h.get('deals-party').textContent, /Вильнюс/);
});

function prepareSave(h, filters) {
  h.context.state.saveFilters = filters;
  const form = h.get('save-form'); form.reportValidity = () => true;
  form.elements = Object.fromEntries(Object.entries({name: 'Мой поиск', notify_mode: 'deal',
    min_drop_pct: '10', min_saving_eur: '100', min_review_rating: '4', min_review_count: '20'})
    .map(([key, value]) => [key, {value}]));
}

test('saving uses exact origins and matching filters without changing strict criteria; only new request ack confirms the scope', async () => {
  const h = harness(); prepareSave(h, {...SAVED, origins: 'VNO'});
  await h.context.saveSubscription({preventDefault() {}});
  const payload = h.calls[0].request.body;
  const expected = {...SAVED, origins: 'VNO', children_ages: '6,8'}; delete expected.sort;
  assert.deepEqual(payload.filters, expected);
  assert.equal(payload.notify_mode, 'deal');
  assert.equal(payload.min_drop_pct, 10); assert.equal(payload.min_saving_eur, 100);
  assert.equal(payload.min_review_rating, 4); assert.equal(payload.min_review_count, 20);
  assert.ok(h.context.state.queuedRequests.has(h.context.collectionScopeKey({...SAVED, origins: 'VNO'})));
  assert.equal(h.context.state.queuedRequests.has(h.context.collectionScopeKey({...SAVED, origins: 'RIX'})), false);
  const legacy = harness(); prepareSave(legacy, SAVED);
  legacy.setSubscriptionReply({id: 1, collection_requested: true});
  await legacy.context.saveSubscription({preventDefault() {}});
  assert.equal(legacy.context.state.queuedRequests.size, 0);
  assert.doesNotMatch(legacy.toasts.at(-1), /заявка.*сохранены/);
});
