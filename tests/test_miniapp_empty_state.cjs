// No browser/dependencies: execute the real search/queue handlers with a small
// DOM and HTTP double. This guards the explicit-click boundary, not SQL filters.
// Run: node --test tests/test_miniapp_empty_state.cjs
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = readFileSync(path.join(__dirname, '../static/miniapp.js'), 'utf8');
function section(from, until) {
  const start = source.indexOf(from);
  const end = source.indexOf(until, start + from.length);
  assert.ok(start >= 0 && end > start, 'real handler boundaries must exist');
  return source.slice(start, end);
}
const handlers = [
  section('  function archiveSearchLimited(', '  function appendStorageNotice('),
  section('  function searchCoverageMessage(', '  function localDate('),
  section('  function empty(', '  function restrictAccess('),
  section('  async function runSearch(', '  function hotelPhoto('),
  section('  function compositionPayload(', '  function hotelCard('),
].join('\n');

class Element {
  constructor(tag, className = '', text = '') {
    Object.assign(this, {tag, className, textContent: text, children: [], dataset: {}, hidden: false});
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  setAttribute() {}
  removeAttribute() {}
  focus() {}
  findAll(predicate) {
    return this.children.flatMap(child => [ ...(predicate(child) ? [child] : []), ...child.findAll(predicate)]);
  }
  querySelector(selector) {
    return this.findAll(child => selector.startsWith('.')
      ? child.className.split(' ').includes(selector.slice(1)) : child.tag === selector)[0] || null;
  }
}

function harness({known = true, demo = false, user = {id: 1}, partial = false, results = [], storage = {}, coverage, filterOverrides = {}} = {}) {
  const elements = new Map();
  const get = id => {
    if (!elements.has(id)) elements.set(id, new Element('div'));
    return elements.get(id);
  };
  get('search-submit').append(new Element('span'));
  const calls = [];
  let post = async () => ({queued: true, spec: '1'});
  const filters = {date_from: '2026-10-10', date_till: '2026-11-23', adults: 1,
    nights_min: 1, nights_max: 30, board_categories: 'BB,HB,FB,AI,UAI', stars_min: 4, ...filterOverrides};
  const context = vm.createContext({
    AbortController, URLSearchParams, demoMode: demo,
    state: {user, searchSerial: 0, queuedPax: new Set(), pendingPax: new Set(), paxErrors: new Map()},
    $: get, form: {elements: {date_from: new Element('input')}},
    window: {matchMedia: () => ({matches: false})},
    document: {querySelectorAll: () => get('search-results').findAll(child => Boolean(child.dataset.paxRequest))},
    el: (tag, className, text) => new Element(tag, className, text),
    icon: () => new Element('svg'),
    button: (text, className, action) => {
      const button = new Element('button', className);
      button.append(new Element('span', '', text)); button.click = action;
      return button;
    },
    validateFilters: () => ({...filters}),
    partyLabel: () => '1 взрослый',
    dateLabel: value => value,
    loading: target => target.replaceChildren(),
    collapseFilters: () => {}, haptic: () => {}, renderAccess: () => {},
    hotelCard: () => new Element('article', 'hotel-card'),
    api: async (url, options = {}) => {
      calls.push({url, method: options.method || 'GET', body: options.body && JSON.parse(JSON.stringify(options.body))});
      if (url === '/api/pax-requests') return post();
      assert.ok(url.startsWith('/api/search?'), 'unexpected API call');
      return {results, available_compositions: known ? [{pax_adl: 1, pax_chd: 0, children_ages: '', offers: 22238}] : [],
        storage: {live: 'empty', archive: 'ok', partial, ...storage}, coverage};
    },
  });
  vm.runInContext(handlers, context);
  return {context, calls, filters, get,
    setPost: callback => { post = callback; },
    panels: () => get('search-results').findAll(child => Boolean(child.dataset.paxRequest))};
}

test('historical single-adult coverage cannot hide refresh; only the click queues exact party', async () => {
  const h = harness();
  await h.context.runSearch();
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0].method, 'GET');
  const query = new URLSearchParams(h.calls[0].url.split('?')[1]);
  assert.equal(query.get('adults'), '1');
  assert.equal(query.get('stars_min'), '4');
  assert.equal(query.get('board_categories'), 'BB,HB,FB,AI,UAI');
  assert.match(h.get('search-results').querySelector('p').textContent, /В собранных данных сейчас нет совпадений/);
  const panel = h.panels()[0];
  assert.ok(panel, 'even 22,238 historic offers must leave a refresh action');
  assert.equal(panel.querySelector('span').textContent, 'Запросить свежие предложения');
  await panel.querySelector('button').click();
  assert.deepEqual(h.calls[1], {url: '/api/pax-requests', method: 'POST', body: {adults: 1, children_ages: []}});
  assert.equal(panel.querySelector('span').textContent, 'Состав добавлен в сбор');
  assert.equal(panel.querySelector('button').disabled, true);
  assert.match(panel.querySelector('p').textContent, /Срок пока неизвестен/);
  // A later empty search reuses confirmed queue state and never writes again.
  await h.context.runSearch();
  await h.panels()[0].querySelector('button').click();
  assert.equal(h.calls.filter(call => call.method === 'POST').length, 1);
  assert.equal(h.panels()[0].querySelector('button').disabled, true);
});

test('unknown composition and partial empty responses both retain explicit request', async () => {
  for (const options of [{known: false}, {partial: true}]) {
    const h = harness(options);
    await h.context.runSearch();
    assert.equal(h.panels().length, 1);
    assert.equal(h.calls.length, 1);
    assert.equal(h.calls[0].method, 'GET');
    if (!options.known && !options.partial) {
      assert.equal(h.panels()[0].querySelector('span').textContent, 'Собрать предложения для этого состава');
    }
  }
});

test('candidate cap explains an incomplete archive check without claiming a source outage', async () => {
  const h = harness({partial: true, storage: {partial_reasons: ['archive_candidate_limit']}});
  await h.context.runSearch();
  assert.match(h.get('search-storage').textContent, /Проверена ограниченная часть архива/);
  assert.doesNotMatch(h.get('search-storage').textContent, /недоступ|Показана часть совпадений/);
  assert.match(h.get('search-results').querySelector('p').textContent, /отсутствие совпадений пока не подтверждено/);
  assert.equal(h.panels().length, 1);
  assert.equal(h.calls.length, 1);
});

test('actual live/archive failure takes priority over a simultaneous candidate cap', async () => {
  for (const failed of ['live', 'archive']) {
    const h = harness({partial: true, storage: {[failed]: 'unavailable', partial_reasons: ['archive_candidate_limit']}});
    await h.context.runSearch();
    assert.match(h.get('search-storage').textContent, /недоступ/);
    assert.doesNotMatch(h.get('search-storage').textContent, /Проверена ограниченная часть архива/);
    assert.match(h.get('search-results').querySelector('p').textContent, /Не все данные удалось проверить/);
    assert.equal(h.panels().length, 1);
  }
});

test('old known party without fresh complete coverage means unverified parameters, not no supplier tours', async () => {
  for (const state of ['uncollected', 'stale', 'partial']) {
    const h = harness({coverage: {state, complete: false, reasons: ['no_completed_run'], queue: {requested: false}}});
    await h.context.runSearch();
    const description = h.get('search-results').querySelector('p').textContent;
    assert.match(description, /пока не подтверждён свежий полный сбор/);
    assert.match(description, /не означает, что у продавцов нет подходящих туров/);
    assert.equal(h.panels().length, 1);
    assert.equal(h.calls.length, 1);
  }
});

test('running, requested and unavailable coverage are described without an ETA or auto queue', async () => {
  for (const [coverage, message] of [
    [{state: 'running', complete: false}, /Последний сбор ещё не отмечен завершённым/],
    [{state: 'queued', complete: false, queue: {requested: true}}, /Заявка на состав активна; срок завершения неизвестен/],
    [{state: 'unavailable', complete: false}, /Не удалось проверить состояние сбора/],
  ]) {
    const h = harness({coverage}); await h.context.runSearch();
    assert.match(h.get('search-results').querySelector('p').textContent, message);
    assert.equal(h.calls.length, 1);
    assert.equal(h.calls[0].method, 'GET');
  }
});

test('entirely unsupported dates retain the requested filters and cannot claim a party request will collect them', async () => {
  const h = harness({coverage: {state: 'unsupported', complete: false, reasons: ['dates_outside_horizon'],
    horizon: {date_from: '2026-10-10', date_till: '2026-11-23'}},
    filterOverrides: {date_from: '2027-04-07', date_till: '2027-04-17'}});
  h.context.state.queuedPax.add('1'); // old successful queue acknowledgement is not date coverage
  await h.context.runSearch();
  const description = h.get('search-results').querySelector('p').textContent;
  assert.match(description, /Выбранные даты сейчас не собираются/);
  assert.match(description, /2026-10-10 — 2026-11-23/);
  assert.match(description, /Заявка на состав не расширяет диапазон дат/);
  assert.equal(h.get('search-results').querySelector('h3').textContent, 'За пределами текущего сбора');
  assert.equal(h.panels().length, 0);
  const query = new URLSearchParams(h.calls[0].url.split('?')[1]);
  assert.equal(query.get('date_from'), '2027-04-07');
  assert.equal(query.get('date_till'), '2027-04-17');
  assert.equal(query.get('stars_min'), '4');
  assert.equal(query.get('board_categories'), 'BB,HB,FB,AI,UAI');
  assert.equal(h.calls.length, 1);
});

test('partly unsupported dates allow an explicit party refresh with a limitation before and after the click', async () => {
  const h = harness({coverage: {state: 'partial', complete: false, reasons: ['dates_outside_horizon'],
    horizon: {date_from: '2026-10-10', date_till: '2026-11-23'}}});
  await h.context.runSearch();
  assert.match(h.get('search-results').querySelector('p').textContent, /Часть выбранных дат сейчас не собирается/);
  const panel = h.panels()[0]; assert.ok(panel);
  assert.match(panel.querySelector('p').textContent, /не расширяет диапазон дат и длительности/);
  await panel.querySelector('button').click();
  assert.match(panel.querySelector('p').textContent, /Запрос на состав сохранён/);
  assert.match(panel.querySelector('p').textContent, /не расширяет диапазон дат и длительности/);
  assert.equal(h.calls.filter(call => call.method === 'POST').length, 1);
});

test('source night coverage names only that source and appears alongside nonempty results', async () => {
  const coverage = {state: 'partial', complete: false, reasons: ['nights_outside_source_range'],
    sources: [{source: 'waavo', supported_nights: {min: 2, max: 21}, reasons: ['nights_outside_source_range']},
      {source: 'joinup', supported_nights: null, reasons: ['stays_not_recorded']}]};
  const h = harness({coverage, results: [{offer_id: 1}]});
  await h.context.runSearch();
  assert.match(h.get('search-storage').textContent, /Waavo: диапазон сбора по числу ночей — 2–21/);
  assert.doesNotMatch(h.get('search-storage').textContent, /Join Up:|у продавцов нет/);
  assert.equal(h.get('search-results').querySelector('.hotel-card').tag, 'article');
  assert.equal(h.panels().length, 0);
});

test('fresh or unknown additive coverage keeps ordinary empty-state behavior', async () => {
  for (const coverage of [{state: 'fresh', complete: true, reasons: []}, {state: 'future-version'}]) {
    const h = harness({coverage}); await h.context.runSearch();
    assert.match(h.get('search-results').querySelector('p').textContent, /В собранных данных сейчас нет совпадений/);
    assert.equal(h.panels().length, 1);
  }
});

test('pending request is deduplicated; a failed request stays retryable with the refresh label', async () => {
  const h = harness();
  await h.context.runSearch();
  let reject;
  h.setPost(() => new Promise((resolve, rejectPromise) => { reject = rejectPromise; }));
  const first = h.panels()[0].querySelector('button').click();
  await h.context.requestComposition(h.filters);
  assert.equal(h.calls.filter(call => call.method === 'POST').length, 1);
  assert.equal(h.panels()[0].querySelector('button').disabled, true);
  reject(Object.assign(new Error('fixture failure'), {status: 503}));
  await first;
  assert.equal(h.panels()[0].querySelector('button').disabled, false);
  assert.equal(h.panels()[0].querySelector('span').textContent, 'Запросить свежие предложения');
  assert.match(h.panels()[0].querySelector('p').textContent, /Не удалось подтвердить запрос/);
  h.setPost(async () => ({queued: true, spec: '1'}));
  await h.panels()[0].querySelector('button').click();
  assert.equal(h.calls.filter(call => call.method === 'POST').length, 2);
  assert.equal(h.panels()[0].querySelector('button').disabled, true);
});

test('demo, anonymous and nonempty results do not expose a collection action', async () => {
  for (const options of [{demo: true, user: null}, {user: null}, {results: [{offer_id: 1}]}]) {
    const h = harness(options);
    await h.context.runSearch();
    assert.equal(h.panels().length, 0);
    if (!options.results) await h.context.requestComposition(h.filters);
    assert.equal(h.calls.filter(call => call.method === 'POST').length, 0);
  }
});
