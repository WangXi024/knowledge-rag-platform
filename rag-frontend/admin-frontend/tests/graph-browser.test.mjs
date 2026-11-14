/** 只读浏览行为回归：内存 Hook、假 Gateway 和合成实体，验证版本、来源及异步边界。 */
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import test from 'node:test';
import { runInNewContext } from 'node:vm';
import * as React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import ts from 'typescript';

const require = createRequire(import.meta.url);
const source = await readFile(new URL('../components/knowledge-graph-browser.tsx', import.meta.url), 'utf8');
const code = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX } }).outputText;
const id = '11111111-1111-4111-8111-111111111111';
const session = { role: 'TENANT_ADMIN', tenantId: 'synthetic-tenant', userId: 'synthetic-user' };
const state = (status = 'READY', generation = 3) => ({ knowledge_base_id: id, status, graph_generation: generation,
  built_for_revision: 6, configuration_version: 2, configuration: { profile_ref: 'native-graph-with-summaries@1' } });
const view = () => ({ knowledge_base_id: id, status: 'READY', graph_generation: 3, content_revision: 6,
  profile_ref: 'native-graph-with-summaries@1', truncated: false, warnings: [],
  nodes: [{ id: 'opaque-node', name: 'Aurora', entity_type: 'organization', description: '<script>generated</script>',
    generated: true, sources: [], source_status: 'unavailable' }], edges: [] });
const flush = () => new Promise((resolve) => setImmediate(resolve));

function mount(gatewayRequest, initial = state()) {
  const slots = [], effects = new Map(), timers = new Map(), exports = {};
  let cursor = 0, nextTimer = 0, currentState = initial;
  const elements = (value) => Array.isArray(value) ? value.flatMap(elements)
    : React.isValidElement(value) ? [value, ...elements(value.props.children)] : [];
  const text = (value) => Array.isArray(value) ? value.map(text).join('') : React.isValidElement(value)
    ? text(value.props.children) : typeof value === 'string' || typeof value === 'number' ? String(value) : '';
  runInNewContext(code, { exports, URLSearchParams, AbortController,
    window: { setTimeout: (callback) => { timers.set(++nextTimer, callback); return nextTimer; }, clearTimeout: (id) => timers.delete(id) },
    require: (name) => {
      if (name === 'react') return {
        useState(initial) { const index = cursor++; if (!(index in slots)) slots[index] = initial;
          return [slots[index], (value) => { slots[index] = typeof value === 'function' ? value(slots[index]) : value; }]; },
        useRef(initial) { const index = cursor++; return slots[index] ??= { current: initial }; },
        useEffect(callback, dependencies) { const index = cursor++, previous = effects.get(index);
          if (!previous || dependencies.some((item, index) => !Object.is(item, previous.dependencies[index]))) effects.set(index, { callback, dependencies, cleanup: previous?.cleanup }); },
      };
      if (name === '@/lib/gateway-api') return { gatewayRequest, errorMessage: (error) => error.message };
      if (name === '@/components/graph-source-reader') return { GraphSourceReader: () => null };
      if (name === '@/lib/pdf-positions') return { sourcePages: () => [] };
      return require(name);
    },
  });
  function render() {
    cursor = 0;
    const tree = exports.KnowledgeGraphBrowser({ session, knowledgeBase: { id, name: '合成知识库' }, state: currentState });
    for (const effect of effects.values()) if (effect.callback) { effect.cleanup?.(); effect.cleanup = effect.callback(); effect.callback = undefined; }
    return tree;
  }
  const tick = () => { for (const [id, callback] of [...timers]) { timers.delete(id); callback(); } };
  return {
    html: () => renderToStaticMarkup(render()), tick,
    button: (name) => elements(render()).find((node) => node.type === 'button' && text(node).startsWith(name)),
    search: () => elements(render()).find((node) => node.type === 'form').props.onSubmit({ preventDefault() {} }),
    setState: (next) => { currentState = next; render(); },
    setQuery: (value) => elements(render()).find((node) => node.type === 'input').props.onChange({ target: { value } }),
    unmount: () => { for (const effect of effects.values()) effect.cleanup?.(); },
  };
}

test('search and selected entity expansion are bounded read-only requests tied to one generation', async () => {
  const calls = [];
  const mounted = mount(async (identity, path, options) => { calls.push({ identity, path, options }); return { data: view() }; });
  mounted.html(); mounted.tick();
  assert.equal(calls.length, 0);
  mounted.setQuery('Aurora & <script>'); mounted.search(); await flush();
  const url = new URL(calls[0].path, 'https://gateway.test/');
  assert.equal(url.searchParams.get('query'), 'Aurora & <script>');
  assert.equal(url.searchParams.get('generation'), '3'); assert.equal(url.searchParams.get('limit'), '40');
  assert.equal(calls[0].identity, session); assert.equal(calls[0].options.method, undefined);
  assert.match(mounted.html(), /<svg/);
  mounted.button('Aurora').props.onClick();
  assert.match(mounted.html(), /&lt;script&gt;generated&lt;\/script&gt;/);
  assert.match(mounted.html(), /来源已失效或不可用/);
  mounted.button('展开此实体邻域').props.onClick(); await flush();
  assert.equal(new URL(calls[1].path, 'https://gateway.test').searchParams.get('node_id'), 'opaque-node');
  mounted.unmount();
});

test('nonready states cannot show old content or make graph requests', () => {
  const mounted = mount(async () => { throw new Error('unexpected network'); }, state('STALE'));
  assert.match(mounted.html(), /当前不展示旧图内容/);
  assert.doesNotMatch(mounted.html(), /<svg|搜索 \/ 刷新图谱/);
  mounted.unmount();
});

test('a late read and a generation change cannot restore an obsolete graph', async () => {
  let finish;
  const mounted = mount(() => new Promise((resolve) => { finish = resolve; }));
  mounted.html(); mounted.tick(); mounted.search();
  mounted.setState(state('STALE')); mounted.tick();
  finish({ data: view() }); await flush();
  assert.doesNotMatch(mounted.html(), /Aurora|<svg/);
  mounted.unmount();
});

test('read failures clear old data and a mismatched generation is not rendered', async () => {
  let first = true;
  const mounted = mount(async () => ({ data: first ? view() : { ...view(), graph_generation: 99 } }));
  mounted.html(); mounted.tick(); mounted.search(); await flush();
  assert.match(mounted.html(), /Aurora/);
  first = false; mounted.search(); await flush();
  assert.match(mounted.html(), /范围或版本不一致/);
  assert.doesNotMatch(mounted.html(), /<svg/);
  mounted.unmount();
});

test('community list and detail use the same generation and label the platform-generated origin', async () => {
  const requests = [];
  const item = { aggregate_id: 'aggregate-1', label: '', kind: 'community_aggregate', derived: true, origin: 'gateway_generated',
    knowledge_base_id: id, community_id: 'community/opaque', title: '合成社区摘要', summary: '只读派生内容', graph_generation: 3,
    content_revision: 6, configuration_version: 2, graph_profile_ref: 'native-graph-with-summaries@1', generator_model_ref: 'synthetic-model', strategy_ref: 'community@1', sources: [] };
  const mounted = mount(async (_identity, path) => { requests.push(path); return { data: path.includes('/communities/') ? item : {
    knowledge_base_id: id, status: 'READY', graph_generation: 3, content_revision: 6, communities: [item], warnings: [] } }; });
  mounted.html(); mounted.tick(); mounted.button('社区摘要').props.onClick();
  mounted.button('读取 / 刷新社区摘要').props.onClick(); await flush();
  mounted.button('合成社区摘要').props.onClick(); await flush();
  assert.match(requests[0], /communities\?generation=3$/);
  assert.match(requests[1], /communities\/community%2Fopaque\?generation=3$/);
  assert.match(mounted.html(), /本平台生成社区摘要/);
  assert.match(mounted.html(), /没有可用原文来源/);
  mounted.unmount();
});
