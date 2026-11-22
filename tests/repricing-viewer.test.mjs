import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const staticFile = name => readFile(new URL(`../housesync/static/${name}`, import.meta.url), 'utf8');
const moduleURL = source => 'data:text/javascript;base64,' + Buffer.from(source).toString('base64');
const coreURL = moduleURL(await staticFile('plan-core.js'));
const html = await staticFile('index.html');
const source = (await staticFile('app.js'))
  .replace("import * as THREE from 'three';", 'const THREE = { Vector3: class {} };')
  .replace("import { OrbitControls } from '/vendor/OrbitControls.js';", '')
  .replace("import { PlanEditor } from './plan-editor.js';", '')
  .replace("from './plan-core.js'", `from '${coreURL}'`)
  .split("form.addEventListener('submit', buildHouse);")[0]
  + '\nexport { repriceSavedRevision, clearRepriceResult, saveSurcharge, loadOrderStatuses, syncOrderStatus, saveOrderStatus };';

class Element {
  constructor(value = '') { this.value = value; this.children = []; this.dataset = {}; }
  append(child) { this.children.push(child); }
  replaceChildren() { this.children = []; }
  removeAttribute(name) { delete this[name]; }
  reset() {}
}

const elements = new Map();
const get = id => {
  if (!elements.has(id)) elements.set(id, new Element());
  return elements.get(id);
};
const links = [...html.matchAll(/data-reprice-file="([^"]+)"/g)].map(([, key]) => {
  const link = new Element();
  link.dataset.repriceFile = key;
  return link;
});
globalThis.document = { querySelector: get, querySelectorAll: selector => selector === '[data-reprice-file]' ? links : [], createElement: () => new Element() };
globalThis.window = { location: { origin: 'http://127.0.0.1:8766' } };
const app = await import(moduleURL(source));

const revision = 'abcdef123456';
const baseInputs = { revision, as_at: '2026-12-01', before: '2026-11-30' };
const event = { preventDefault() {} };
const response = data => ({ ok: true, json: async () => data });

function chooseInputs() {
  get('#repricing-revision').value = revision;
  get('#repricing-date').value = baseInputs.as_at;
  get('#repricing-before').value = baseInputs.before;
}

const result = () => ({
  ...baseInputs, currency: 'AUD', totals: { old_amount: 100, new_amount: 105, delta: 5 },
  lines: [{ code: 'ROOF', description: 'Roof', quantity: 1, unit: 'm2', old_rate: 100, new_rate: 105, delta: 5, causes: ['Freight rate increase'], applied_surcharges: [{ name: 'Fuel levy', component: 'freight', unit_rate_change: 5 }] }],
  by_trade: [{ name: 'Roofing', old_amount: 100, new_amount: 105, delta: 5 }],
  by_supplier: [{ name: 'Supplier A', old_amount: 100, new_amount: 105, delta: 5 }],
  purchase_orders: { updated_count: 1, variation_count: 2 },
  files: { xlsx: '/repricing/report.xlsx', client_quote_pdf: 'https://example.com/other.pdf' },
});

test('dated preview submits both dates and shows a matching report with line causes and order changes', async () => {
  chooseInputs();
  globalThis.fetch = async (url, options) => {
    assert.equal(url, '/api/reprice');
    assert.equal(options.method, 'POST');
    assert.deepEqual(JSON.parse(options.body), baseInputs);
    return response(result());
  };
  await app.repriceSavedRevision(event);
  assert.equal(get('#repricing-result').hidden, false);
  assert.match(get('#repricing-status').textContent, /2026-12-01.*2026-11-30/);
  assert.match(get('#repricing-lines').children[0].children[5].textContent, /Freight rate increase\nApplied Fuel levy \(freight, AUD 5\.00\/m2\)/);
  assert.equal(get('#repricing-by-trade').children[0].children[0].textContent, 'Roofing');
  assert.match(get('#repricing-order-counts').textContent, /1 draft PO updated.*2 draft PO variations/);
  assert.equal(links.find(link => link.dataset.repriceFile === 'xlsx').href, 'http://127.0.0.1:8766/repricing/report.xlsx');
  assert.equal(links.find(link => link.dataset.repriceFile === 'client_quote_pdf').hidden, true);
});

test('changing pricing inputs hides downloads and ignores an older response', async () => {
  chooseInputs();
  let resolve;
  globalThis.fetch = () => new Promise(done => { resolve = done; });
  const pending = app.repriceSavedRevision(event);
  get('#repricing-date').value = '2026-12-02';
  app.clearRepriceResult();
  resolve(response(result()));
  await pending;
  assert.equal(get('#repricing-result').hidden, true);
  assert.equal(get('#repricing-submit').disabled, false);
});

test('surcharge form appends one scoped rule and invalidates the earlier report', async () => {
  const values = { name: ' Fuel levy ', from: '2026-12-01', end: '', type: 'percentage', amount: '5', component: 'freight', scope: 'rate_codes', values: 'ROOF, FLOOR' };
  for (const [key, value] of Object.entries(values)) get(`#surcharge-${key}`).value = value;
  globalThis.fetch = async (url, options) => {
    assert.equal(url, '/api/surcharges');
    const posted = JSON.parse(options.body);
    assert.deepEqual(posted, { name: 'Fuel levy', effective_from: '2026-12-01', type: 'percentage', amount: 5, component: 'freight', scope: { type: 'rate_codes', values: ['ROOF', 'FLOOR'] } });
    return response({ surcharges: [posted] });
  };
  await app.saveSurcharge(event);
  assert.match(get('#surcharge-list').children[0].textContent, /Fuel levy.*5% on freight.*ROOF.*FLOOR/);
  assert.equal(get('#repricing-result').hidden, true);
  assert.match(get('#repricing-status').textContent, /Surcharge added/);
});

test('recording acceptance updates the selected saved order and requires a fresh repricing run', async () => {
  chooseInputs();
  const orders = [{ code: 'ROOF', description: 'Roof' }];
  globalThis.fetch = async (url, options) => {
    if (!options.method) {
      assert.equal(url, `/api/purchase-order-statuses?revision=${revision}`);
      return response({ statuses: { ROOF: 'issued' }, orders });
    }
    assert.equal(url, '/api/purchase-order-statuses');
    assert.deepEqual(JSON.parse(options.body), { revision, code: 'ROOF', status: 'accepted' });
    return response({ statuses: { ROOF: 'accepted' }, orders });
  };
  await app.loadOrderStatuses();
  get('#repricing-order-code').value = 'ROOF';
  app.syncOrderStatus();
  assert.equal(get('#repricing-order-status').value, 'issued');
  get('#repricing-order-status').value = 'accepted';
  await app.saveOrderStatus(event);
  assert.match(get('#repricing-order-code').children[1].textContent, /accepted/);
  assert.equal(get('#repricing-order-submit').disabled, true);
  assert.equal(get('#repricing-result').hidden, true);
});
