const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function setup(initial) {
  const elements = new Map();
  const make = () => ({value: '', disabled: false, hidden: false, children: [], events: {},
    classList: {toggle() {}}, setAttribute() {}, removeAttribute() {},
    addEventListener(name, fn) { this.events[name] = fn; },
    replaceChildren() { this.children = []; }, append(...items) { this.children.push(...items); }});
  const get = id => { if (!elements.has(id)) elements.set(id, make()); return elements.get(id); };
  let state = structuredClone(initial);
  const calls = [];
  const context = {document: {getElementById: get, createElement: make}, window: {},
    confirm: () => true, setTimeout, clearTimeout,
    fetch: async (url, options) => {
      calls.push(url);
      if (url.endsWith('/brief')) {
        const body = JSON.parse(options.body);
        state.brief = body.brief; state.previewModel = body.previewModel; state.revision++;
      }
      if (url.endsWith('/generate')) return {ok: true, json: async () => ({jobId: 'mock-job'})};
      if (url.includes('/api/jobs/')) return {ok: true, json: async () => ({status: 'completed'})};
      return {ok: true, json: async () => structuredClone(state)};
    }};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../web/design-studio.js'), 'utf8'), context);
  return {get, calls, load: context.window.loadDesignStudio, setState: value => { state = value; }};
}

const initial = {brief: {product: '商城'}, previewModel: 'gpt-image-2.5', revision: 1,
  references: [], versions: [{id: 'v1', number: 1, spec: {}, summary: '规范', previewPrompt: 'board',
    inputRevision: 1, previewId: 'image1', previewUrl: '/image1'}], current: 'v1', adopted: null};

test('question edits survive settings reload and disable stale preview/adoption', async () => {
  const ui = setup(initial);
  await ui.load();
  assert.equal(ui.get('studioAdopt').disabled, false);
  ui.get('studioProduct').value = '编辑中的产品';
  ui.get('studioProduct').events.input();
  await ui.load();
  assert.equal(ui.get('studioProduct').value, '编辑中的产品');
  assert.equal(ui.get('studioAdopt').disabled, true);
  assert.equal(ui.get('studioPreview').disabled, true);
});

test('saved reference changes block old preview and adoption', async () => {
  const ui = setup({...initial, revision: 2});
  await ui.load();
  assert.equal(ui.get('studioAdopt').disabled, true);
  assert.equal(ui.get('studioPreview').disabled, true);
});

test('generation saves question changes first and polls without regenerating', async () => {
  const ui = setup(initial);
  await ui.load();
  ui.get('studioProduct').value = '新产品';
  ui.get('studioProduct').events.input();
  await ui.get('studioGenerate').onclick();
  assert.ok(ui.calls.indexOf('/api/design-studio/brief') < ui.calls.indexOf('/api/design-studio/generate'));
  assert.equal(ui.calls.filter(url => url.endsWith('/generate')).length, 1);
  assert.ok(ui.calls.includes('/api/jobs/mock-job'));
  assert.equal(ui.get('studioInputs').disabled, false);
});

test('failed task remains visible after reopening settings', async () => {
  const ui = setup({...initial, task: {status: 'failed', message: '上游请求失败'}});
  await ui.load();
  assert.equal(ui.get('studioStatus').textContent, '上游请求失败');
});
