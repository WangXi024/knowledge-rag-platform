/** 聚合前端协议回归：纯合成响应、假 Gateway 与分块 SSE，不访问真实模型或业务资源。 */
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import { runInNewContext } from 'node:vm';
import ts from 'typescript';

async function load(path, dependencies = {}) {
  const exports = {};
  const source = await readFile(new URL(path, import.meta.url), 'utf8');
  runInNewContext(ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText,
    { exports, Request, Response, Headers, URL, URLSearchParams, ReadableStream, TextDecoder, TextEncoder,
      require: (name) => { if (name in dependencies) return dependencies[name]; throw new Error(`Unexpected import ${name}`); } });
  return exports;
}
const helpers = await load('../lib/aggregate-evidence.ts');
const user = { tenantId: 'synthetic-tenant', userId: 'synthetic-user' };
const evidence = () => ({ aggregate_id: 'aggregate-1', label: 'A1', kind: 'community_aggregate', derived: true,
  origin: 'gateway_generated', knowledge_base_id: 'kb-1', community_id: 'community-1', title: '合成社区', summary: '本平台合成摘要',
  graph_generation: 3, content_revision: 6, configuration_version: 2,
  graph_profile_ref: 'native-graph-with-summaries@1', generator_model_ref: 'synthetic-chat', strategy_ref: 'community@1',
  sources: [1, 2].map((id) => ({ knowledge_base_id: 'kb-1', document_id: `document-${id}`, document_name: `synthetic-${id}.txt`,
    document_revision: 4, active_version_id: `version-${id}`, chunk_id: `chunk-${id}`, quote: '原文逐字摘录', positions: [] })) });

test('original is the default and arbitrary modes cannot be forwarded', () => {
  assert.equal(helpers.requestedEvidenceMode(undefined), 'original');
  assert.equal(helpers.requestedEvidenceMode('community'), 'community');
  for (const value of [null, 'native', 'COMMUNITY', {}, ['community']]) assert.equal(helpers.requestedEvidenceMode(value), null);
});

test('aggregate events and final payload deduplicate without becoming ordinary citations', () => {
  const item = evidence();
  const events = helpers.mergeAggregateEvidence([], item);
  const completed = helpers.mergeAggregateEvidence(events, [item]);
  assert.equal(completed.length, 1);
  assert.equal(completed[0].summary, item.summary);
  assert.equal(completed[0].sources.length, 2);
  assert.equal('quote' in completed[0], false);
  assert.equal(helpers.mergeAggregateEvidence([], []).length, 0);
  assert.equal(helpers.historicalAggregateEvidence(undefined).length, 0);
  const other = { ...item, aggregate_id: 'aggregate-2' };
  assert.throws(() => helpers.mergeAggregateEvidence(events, other), /标签冲突/);
});

test('invalid, cross-knowledge-base or single-source summaries cannot become evidence', () => {
  for (const invalid of [
    { ...evidence(), origin: 'ragflow_native' }, { ...evidence(), label: 'C1' },
    { ...evidence(), derived: false }, { ...evidence(), sources: evidence().sources.slice(0, 1) },
    { ...evidence(), sources: evidence().sources.map((source) => ({ ...source, knowledge_base_id: 'outside-kb' })) },
  ]) assert.throws(() => helpers.mergeAggregateEvidence([], invalid), /来源无法核验/);
  const legacySource = evidence(); legacySource.sources[0].active_version_id = null;
  legacySource.sources[0].document_revision = 0;
  assert.equal(helpers.validAggregateEvidence(legacySource), true);
});

test('user stream BFF forwards only the selected mode under its cookie identity', async () => {
  const calls = [];
  const route = await load('../app/api/chat/stream/route.ts', {
    '@/lib/auth': { getAuthenticatedUser: async () => user },
    '@/lib/gateway': { friendlyError: () => ({ error: 'synthetic failure' }), gatewayFetch: async (identity, path, init) => {
      calls.push({ identity, path, init }); return new Response('event: completed\ndata: {}\n\n', { headers: { 'Content-Type': 'text/event-stream' } });
    } },
    '@/lib/sse': { sanitizedSseBody: (body) => body }, '@/lib/aggregate-evidence': helpers,
  });
  const post = (body) => route.POST(new Request('https://user.test/api/chat/stream', { method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Tenant-ID': 'untrusted-other-tenant' }, body: JSON.stringify({ query: '合成问题', ...body }) }));
  assert.equal((await post({ evidenceMode: 'community', tenantId: 'outside', model: 'unapproved' })).status, 200);
  assert.equal(calls[0].identity, user);
  assert.equal(calls[0].path, '/v1/answers/stream');
  const forwarded = JSON.parse(calls[0].init.body);
  assert.equal(forwarded.evidence_mode, 'community');
  assert.equal('tenantId' in forwarded || 'model' in forwarded, false);
  assert.equal((await post({ evidenceMode: 'unsupported' })).status, 400);
  assert.equal(calls.length, 1);
  assert.equal((await post({})).status, 200);
  assert.equal(JSON.parse(calls[1].init.body).evidence_mode, 'original');
});

test('public source BFF rejects an old revision before reading chunks and binds valid reads', async () => {
  const calls = [];
  class GatewayError extends Error {}
  const route = await load('../app/api/documents/[documentId]/chunks/route.ts', {
    '@/lib/auth': { getAuthenticatedUser: async () => user },
    '@/lib/gateway': { GatewayError, gatewayJson: async (identity, path) => {
      calls.push({ identity, path });
      return path.includes('/chunks?') ? { data: [{ id: 'chunk-1', content: '原文', positions: [] }], meta: { page: 1, page_size: 20, total: 1 } }
        : { data: { id: 'document-1', knowledge_base_id: 'kb-1', document_revision: 4, active_version_id: 'version-1' } };
    } },
  });
  const get = (query) => route.GET(new Request(`https://user.test/api/documents/document-1/chunks?${query}`), { params: Promise.resolve({ documentId: 'document-1' }) });
  assert.equal((await get('document_revision=3')).status, 409);
  assert.equal(calls.some((call) => call.path.includes('/chunks?')), false);
  assert.equal((await get('document_revision=4&active_version_id=old-version')).status, 409);
  assert.equal((await get('document_revision=4&active_version_id=version-1')).status, 200);
  assert.equal(calls.at(-1).path, '/v1/documents/document-1/chunks?page=1&page_size=20&document_revision=4');
  assert.equal(calls.every((call) => call.identity === user && !call.path.includes('/management/')), true);
});

test('split SSE preserves independent aggregate and original quote payloads', { timeout: 5000 }, async () => {
  const { sanitizedSseBody } = await load('../lib/sse.ts');
  const frames = `event: aggregate.evidence\ndata: ${JSON.stringify({ evidence: evidence() })}\n\nevent: citation\ndata: {"citation":{"label":"C1","quote":"真实原文"}}\n\nevent: completed\ndata: ${JSON.stringify({ aggregate_evidence: [evidence()], warnings: ['rerank_unavailable', 'community_source_verified'] })}\n\n`;
  const bytes = new TextEncoder().encode(frames);
  let offset = 0;
  const stream = new ReadableStream({ pull(controller) {
    if (offset === bytes.length) return controller.close();
    controller.enqueue(bytes.slice(offset, offset += Math.min(7, bytes.length - offset)));
  } });
  const text = await new Response(sanitizedSseBody(stream)).text();
  assert.match(text, /event: aggregate\.evidence/);
  assert.match(text, /本平台合成摘要/);
  assert.match(text, /真实原文/);
  assert.match(text, /community_source_verified/);
  assert.doesNotMatch(text, /rerank_unavailable/);
});
