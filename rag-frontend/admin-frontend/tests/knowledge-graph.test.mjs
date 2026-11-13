/** 图谱管理行为回归：合成状态与内存 Hook 验证异步任务、草稿保护和 BFF 写路径边界。 */
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import test from 'node:test';
import { runInNewContext } from 'node:vm';
import * as React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import ts from 'typescript';

const require = createRequire(import.meta.url);
const compile = (source) => ts.transpileModule(source, { compilerOptions: {
  module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
} }).outputText;
const component = compile(await readFile(new URL('../components/knowledge-graph-settings.tsx', import.meta.url), 'utf8'));
const proxy = compile(await readFile(new URL('../app/api/gateway/[...path]/route.ts', import.meta.url), 'utf8'));
const id = '11111111-1111-4111-8111-111111111111';
const session = { role: 'TENANT_ADMIN', tenantId: 'synthetic-tenant', userId: 'synthetic-user' };
const flush = () => new Promise((done) => setImmediate(done));

/** 状态来自模拟后端，不根据按钮行为伪造任务已完成。 */
function response(status = 'DISABLED', overrides = {}) {
  return { request_id: 'synthetic-request', data: {
    knowledge_base_id: id,
    configuration: { enabled: status !== 'DISABLED', profile_ref: 'native-graph-light@1', auto_build: true, retrieval_enabled: true },
    status, target_content_revision: 7, built_for_revision: null, graph_generation: 0,
    configuration_version: 1, job_id: null, last_error: null,
    queued_at: null, started_at: null, completed_at: null, updated_at: '2026-11-13T03:00:00Z', ...overrides,
  } };
}

/** 按 Hook 槽保存状态、依赖和清理函数；revision 可模拟尚未更新的父列表快照。 */
function mount(gatewayRequest, available = true, revision = 7) {
  const slots = [], effects = new Map(), timers = new Map();
  let cursor = 0, nextTimer = 0, updates = 0;
  const exports = {};
  const dependenciesChanged = (previous, next) => !previous || next.some((value, index) => !Object.is(value, previous[index]));
  runInNewContext(component, { exports, Date, Set,
    window: {
      setTimeout: (callback, delay) => { timers.set(++nextTimer, { callback, delay }); return nextTimer; },
      clearTimeout: (handle) => timers.delete(handle),
    },
    require: (name) => {
      if (name === 'react') return {
        useState(initial) {
          const index = cursor++;
          if (!(index in slots)) slots[index] = initial;
          return [slots[index], (next) => { slots[index] = typeof next === 'function' ? next(slots[index]) : next; updates++; }];
        },
        useRef(initial) { const index = cursor++; return slots[index] ??= { current: initial }; },
        useCallback(callback, dependencies) {
          const index = cursor++;
          if (dependenciesChanged(slots[index]?.dependencies, dependencies)) slots[index] = { callback, dependencies };
          return slots[index].callback;
        },
        useEffect(pending, dependencies) {
          const index = cursor++, previous = effects.get(index);
          if (dependenciesChanged(previous?.dependencies, dependencies)) effects.set(index, { pending, dependencies, cleanup: previous?.cleanup });
        },
      };
      if (name === '@/lib/gateway-api') return { gatewayRequest, errorMessage: (error) => error.message };
      return require(name);
    },
  });
  const elements = (value) => Array.isArray(value) ? value.flatMap(elements)
    : React.isValidElement(value) ? [value, ...elements(value.props.children)] : [];
  const text = (value) => Array.isArray(value) ? value.map(text).join('')
    : React.isValidElement(value) ? text(value.props.children) : typeof value === 'string' ? value : '';
  const render = () => {
    cursor = 0;
    const tree = exports.KnowledgeGraphSettings({ session, knowledgeBase: { id, name: '合成知识库', revision }, available });
    for (const effect of effects.values()) if (effect.pending) {
      effect.cleanup?.(); effect.cleanup = effect.pending(); effect.pending = undefined;
    }
    return tree;
  };
  return {
    html: () => renderToStaticMarkup(render()),
    button: (label) => elements(render()).find((node) => node.type === 'button' && text(node) === label),
    toggle: (label, checked) => {
      const target = elements(render()).find((node) => node.type === 'label' && text(node).startsWith(label));
      elements(target).find((node) => node.type === 'input').props.onChange({ target: { checked } });
    },
    save: () => elements(render()).find((node) => node.type === 'form').props.onSubmit({ preventDefault() {} }),
    tick: (delay) => {
      for (const [handle, timer] of [...timers]) if (timer.delay === delay) { timers.delete(handle); timer.callback(); }
    },
    timers,
    updates: () => updates,
    unmount: () => { for (const effect of effects.values()) effect.cleanup?.(); },
  };
}

test('saves explicit configuration with PUT and keeps an accepted build queued', async () => {
  const calls = [];
  const view = mount(async (identity, path, options) => {
    assert.equal(identity, session); calls.push({ path, options });
    return response(options ? 'QUEUED' : 'DISABLED', { job_id: options ? 'synthetic-job' : null });
  });
  assert.match(view.html(), /正在读取图谱状态/);
  view.tick(0); await flush();
  assert.match(view.html(), /未启用/);
  view.toggle('启用知识图谱', true);
  view.save(); await flush();
  const write = calls[1];
  assert.equal(write.path, `v1/management/knowledge-bases/${id}/graph/configuration`);
  assert.equal(write.options.method, 'PUT');
  assert.deepEqual(JSON.parse(JSON.stringify(write.options.body)), {
    enabled: true, profile_ref: 'native-graph-light@1', auto_build: true, retrieval_enabled: true,
  });
  const html = view.html();
  assert.match(html, /等待构建/); assert.doesNotMatch(html, />可用</);
  assert.match(html, /synthetic-job/);
  assert.equal(view.button('取消构建').props.disabled, false);
  assert.equal(view.button('构建图谱').props.disabled, true);
  view.unmount();
});

test('build and cancel use separate asynchronous actions and preserve pending cancellation', async () => {
  const paths = [];
  const view = mount(async (_identity, path) => {
    paths.push(path);
    return response(path.endsWith('/cancel') ? 'CANCELLING' : path.endsWith('/build') ? 'QUEUED' : 'NOT_BUILT');
  });
  view.html(); view.tick(0); await flush(); view.html();
  view.button('构建图谱').props.onClick(); await flush();
  assert.match(view.html(), /构建请求已提交/);
  view.button('取消构建').props.onClick(); await flush();
  assert.match(view.html(), /取消待确认/);
  assert.match(view.html(), /不代表模型调用已经停止/);
  assert.equal(view.button('取消构建').props.disabled, true);
  assert.deepEqual(paths, ['', '/build', '/cancel'].map((suffix) => `v1/management/knowledge-bases/${id}/graph${suffix}`));
  view.unmount();
});

test('polling preserves an unsaved draft and never presents an old revision as ready', async () => {
  const view = mount(async () => response('READY', { built_for_revision: 6, graph_generation: 3 }));
  view.html(); view.tick(0); await flush();
  assert.match(view.html(), /内容已变更，待重建/);
  assert.doesNotMatch(view.html(), />可用</);
  view.toggle('自动构建', false); view.html();
  view.tick(10000); await flush();
  assert.match(view.html(), /配置尚未保存/);
  assert.equal(view.button('保存图谱配置').props.disabled, false);
  assert.equal(view.button('重建图谱').props.disabled, true);
  view.unmount();
});

test('refresh trusts current backend graph revisions over an old parent list and preserves stale or queued states', async () => {
  // 上传被接收时父列表仍为 1；后台已完成内容 2 的自动构图，不能误导管理员再次付费重建。
  let status = 'READY', target = 2;
  let calls = 0;
  const view = mount(async () => {
    calls++;
    return response(status, { built_for_revision: 2, target_content_revision: target, graph_generation: 1 });
  }, true, 1);
  view.html(); view.tick(0); await flush();
  let html = view.html();
  assert.match(html, />可用</);
  assert.match(html, /当前内容版本 2/);
  assert.match(html, /构图目标版本 2/);
  assert.match(html, /已构建内容版本 2/);
  assert.doesNotMatch(html, /内容已变更，待重建|现有图谱与当前内容版本不一致/);

  view.button('刷新图谱状态').props.onClick(); await flush();
  html = view.html();
  assert.equal(calls, 2);
  assert.match(html, />可用</);
  assert.doesNotMatch(html, /内容已变更，待重建|现有图谱与当前内容版本不一致/);

  // 后台真正报告 STALE 时保留过期警告；随后排队也必须显示真实阶段，不能继续显示 READY。
  status = 'STALE'; target = 3;
  view.tick(10000); await flush();
  html = view.html();
  assert.match(html, /内容已变更，待重建/);
  assert.match(html, /现有图谱与当前内容版本不一致/);
  assert.match(html, /当前内容版本 3/);
  assert.doesNotMatch(html, />可用</);
  status = 'QUEUED';
  view.button('刷新图谱状态').props.onClick(); await flush();
  html = view.html();
  assert.match(html, /等待构建/);
  assert.doesNotMatch(html, /内容已变更，待重建|>可用</);
  assert.equal(view.button('重建图谱').props.disabled, true);
  view.unmount();

  // 反向交错也必须保守：父列表已知内容 3 时，迟到的 READY 2 不能显示当前可用。
  const newerParent = mount(async () => response('READY', {
    built_for_revision: 2, target_content_revision: 2, graph_generation: 1,
  }), true, 3);
  newerParent.html(); newerParent.tick(0); await flush();
  const outdatedGraph = newerParent.html();
  assert.match(outdatedGraph, /当前内容版本 3/);
  assert.match(outdatedGraph, /已构建内容版本 2/);
  assert.match(outdatedGraph, /内容已变更，待重建/);
  assert.doesNotMatch(outdatedGraph, />可用</);
  newerParent.unmount();
});

test('reports unavailable capability and read errors without allowing a build', async () => {
  const view = mount(async () => { throw new Error('合成错误：模型尚未登记'); }, false);
  view.html(); view.tick(0); await flush();
  const html = view.html();
  assert.match(html, /尚未启用原生图谱能力/);
  assert.match(html, /合成错误：模型尚未登记/);
  assert.match(html, /图谱状态暂不可用/);
  assert.equal(view.button('刷新图谱状态').props.disabled, false);
  view.unmount();
});

test('ignores a delayed status response after unmount and clears timers', async () => {
  let finish;
  const view = mount(() => new Promise((resolve) => { finish = resolve; }));
  view.html(); view.tick(0); view.unmount();
  const before = view.updates(); finish(response('READY')); await flush();
  assert.equal(view.updates(), before);
  assert.equal(view.timers.size, 0);
});

test('BFF only allows PUT for the exact graph configuration path and forwards identity headers', async () => {
  const calls = [], exports = {};
  runInNewContext(proxy, { exports, Request, Response, Headers, URL,
    require: () => ({ getGatewayBaseUrl: () => 'https://gateway.test' }),
    fetch: async (url, options) => { calls.push({ url, options }); return Response.json({ data: {} }); },
  });
  for (const path of [
    ['v1', 'management', 'knowledge-bases', id, 'graph', 'configuration'],
    ['v1', 'management', 'knowledge-bases', id],
    ['v1', 'management', 'knowledge-bases', 'not-a-uuid', 'graph', 'configuration'],
  ]) {
    const request = new Request('https://admin.test/api/gateway/' + path.join('/'), {
      method: 'PUT', headers: { 'X-Tenant-ID': session.tenantId, 'X-User-ID': session.userId, 'Authorization': 'synthetic-secret' },
    });
    const result = await exports.PUT(request, { params: Promise.resolve({ path }) });
    assert.equal(result.status, path.length === 6 && path[3] === id ? 200 : 403);
  }
  assert.equal(calls.length, 1);
  assert.equal(calls[0].options.method, 'PUT');
  assert.equal(calls[0].options.headers.get('x-tenant-id'), session.tenantId);
  assert.equal(calls[0].options.headers.has('authorization'), false);
});
