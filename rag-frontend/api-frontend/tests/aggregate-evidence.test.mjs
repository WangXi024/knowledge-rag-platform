/** 开发控制台聚合与代理回归：使用纯合成事件与假 fetch，不触发模型或业务写入。 */
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import test from 'node:test';
import { runInNewContext } from 'node:vm';
import ts from 'typescript';

const require = createRequire(import.meta.url);
async function module(path, dependencies = {}, globals = {}) {
  const exports = {};
  const source = await readFile(new URL(path, import.meta.url), 'utf8');
  runInNewContext(ts.transpileModule(source, { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
  } }).outputText, { exports, Request, Response, Headers, URL, ...globals,
    require: (name) => name in dependencies ? dependencies[name] : require(name) });
  return exports;
}

test('independent aggregate events deduplicate and reject ordinary citations or upstream reports', async () => {
  const { aggregateEvents } = await module('../components/console/aggregate-evidence.tsx', { '@/lib/client-api': {} });
  const item = { aggregate_id: 'aggregate-1', label: 'A1', kind: 'community_aggregate', derived: true,
    origin: 'gateway_generated', sources: [{ document_id: 'synthetic-1' }, { document_id: 'synthetic-2' }] };
  const first = aggregateEvents([], item);
  assert.equal(aggregateEvents(first, [item]).length, 1);
  assert.equal(aggregateEvents([], []).length, 0);
  assert.throws(() => aggregateEvents([], { ...item, label: 'C1' }), /来源无效/);
  assert.throws(() => aggregateEvents([], { ...item, origin: 'ragflow_native' }), /来源无效/);
  assert.throws(() => aggregateEvents([], { ...item, sources: [] }), /来源无效/);
});

test('PUT proxy permits only the exact graph configuration route and strips unrelated credentials', async () => {
  const calls = [];
  const route = await module('../app/api/gateway/[...path]/route.ts', {
    '@/lib/runtime-env': { getRuntimeEnvironment: () => ({ GATEWAY_BASE_URL: 'https://gateway.test' }) },
  }, { fetch: async (url, init) => { calls.push({ url, init }); return Response.json({ data: {} }); } });
  const id = '11111111-1111-4111-8111-111111111111';
  const invoke = (path) => route.PUT(new Request(`https://console.test/api/gateway/${path.join('/')}`, {
    method: 'PUT', headers: { 'X-Tenant-ID': 'synthetic-tenant', 'X-User-ID': 'synthetic-user', Authorization: 'untrusted-token' },
  }), { params: Promise.resolve({ path }) });
  assert.equal((await invoke(['v1', 'management', 'knowledge-bases', id, 'graph', 'configuration'])).status, 200);
  assert.equal((await invoke(['v1', 'management', 'knowledge-bases', id])).status, 403);
  assert.equal((await invoke(['v1', 'management', 'knowledge-bases', 'invalid', 'graph', 'configuration'])).status, 403);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].init.headers.get('X-Tenant-ID'), 'synthetic-tenant');
  assert.equal(calls[0].init.headers.has('Authorization'), false);
});

test('read-only graph requests preserve generation and encoded search without authorizing another host', async () => {
  const calls = [];
  const route = await module('../app/api/gateway/[...path]/route.ts', {
    '@/lib/runtime-env': { getRuntimeEnvironment: () => ({ GATEWAY_BASE_URL: 'https://gateway.test' }) },
  }, { fetch: async (url, init) => { calls.push({ url, init }); return Response.json({ data: {} }); } });
  const response = await route.GET(new Request('https://console.test/api/gateway/v1/management/knowledge-bases/id/graph/view?query=A%26B&generation=3', {
    headers: { 'X-Tenant-ID': 'synthetic-tenant', 'X-User-ID': 'synthetic-user' },
  }), { params: Promise.resolve({ path: ['v1', 'management', 'knowledge-bases', 'id', 'graph', 'view'] }) });
  assert.equal(response.status, 200);
  assert.equal(calls[0].url.origin, 'https://gateway.test');
  assert.equal(calls[0].url.searchParams.get('query'), 'A&B');
  assert.equal(calls[0].url.searchParams.get('generation'), '3');
});
