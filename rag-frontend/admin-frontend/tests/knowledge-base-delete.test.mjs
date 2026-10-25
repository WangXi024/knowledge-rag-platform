/**
 * 知识库删除回归：在内存中执行真实工作区事件，验证 Job 404 后的目标复查和页面提示。
 * 假请求只返回合成数据，不连接 Gateway，也不执行真实删除。
 */
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import test from 'node:test';
import { runInNewContext } from 'node:vm';
import * as React from 'react';
import ts from 'typescript';

const require = createRequire(import.meta.url);
const source = await readFile(new URL('../components/admin-workspace.tsx', import.meta.url), 'utf8');
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX },
});
const base = { id: 'kb-to-delete', name: '待删除知识库', document_count: 0, scope_type: 'TENANT', status: 'ACTIVE' };

/** 模拟真实 Gateway 错误码，使测试覆盖只处理指定 Job 404 的分支。 */
class GatewayError extends Error {
  constructor(message, status, payload) {
    super(message);
    this.status = status;
    this.payload = payload;
  }
}

/** 按页面状态槽模拟 React，每次重新渲染时检查知识库行和用户可见提示。 */
function workspace(request) {
  const states = [[base], [], { parser_profiles: [], chunk_profiles: [], retrieval_profiles: [], file_extensions: [], features: {} }, base.id];
  let cursor = 0;
  const exports = {};
  runInNewContext(outputText, {
    exports,
    window: { confirm: () => true },
    require: (name) => {
      if (name === 'react') return {
        ...React,
        useState: (initial) => {
          const index = cursor++;
          if (!(index in states)) states[index] = typeof initial === 'function' ? initial() : initial;
          return [states[index], (next) => { states[index] = typeof next === 'function' ? next(states[index]) : next; }];
        },
        useMemo: (calculate) => calculate(),
        useCallback: (callback) => callback,
        useEffect: () => {},
      };
      if (name === '@/lib/gateway-api') return { gatewayRequest: request, GatewayError, errorMessage: (error) => error.message };
      if (name === '@/components/document-chunks') return { DocumentChunks: () => null };
      return require(name);
    },
  });
  const render = () => {
    cursor = 0;
    return exports.AdminWorkspace({
      session: { role: 'TENANT_ADMIN', tenantId: 'test-tenant', userId: 'test-user' },
      section: 'knowledge', onSectionChange: () => {},
    });
  };
  const descendants = (value) => {
    if (Array.isArray(value)) return value.flatMap(descendants);
    if (!React.isValidElement(value)) return [];
    return [value, ...Object.values(value.props).flatMap(descendants)];
  };
  return {
    // 界面回调刻意不等待异步删除；下一轮事件循环后检查其最终可见状态。
    delete: async () => {
      descendants(render()).find((node) => typeof node.props.onDelete === 'function').props.onDelete(base);
      await new Promise(setImmediate);
    },
    notice: () => descendants(render()).find((node) => node.props.role === 'status'),
    knowledgeBases: () => states[0],
  };
}

/** 合成删除入口成功、任务读取失联；后续由各测试注入列表响应。 */
function requestWithList(listRequest, code = 'RAG-JOB-001', targetExists = false, targetError = null) {
  const calls = [];
  return {
    calls,
    request: async (_session, path, options) => {
      calls.push(path);
      if (options?.method === 'DELETE') return { data: { job_id: 'delete-job' } };
      if (path.endsWith('/jobs/delete-job')) throw new GatewayError('Job 不存在', 404, { error: { code } });
      if (path.endsWith(`/knowledge-bases/${base.id}`)) {
        if (targetError) throw targetError;
        if (targetExists) return { data: base };
        throw new GatewayError('知识库不存在', 404, { error: { code: 'RAG-KB-001' } });
      }
      return listRequest(path);
    },
  };
}

// Job 已无法读取且完整列表中已无目标：页面立即移除知识库并报告删除完成。
test('confirms deletion after the missing Job error and refreshes visible knowledge bases', async () => {
  const api = requestWithList(async () => ({ data: [], meta: { page: 1, page_size: 100, total: 0 } }));
  const view = workspace(api.request);
  await view.delete();
  assert.deepEqual(view.knowledgeBases(), []);
  assert.match(view.notice().props.className, /success/);
  assert.match(view.notice().props.children[1].props.children, /知识库已删除/);
  assert.ok(api.calls.some((path) => path.endsWith(`/knowledge-bases/${base.id}`)));
  assert.ok(api.calls.some((path) => path.includes('scope_type=TENANT')));
});

// 目标仍存在、目标查询失败或列表刷新失败时，不能把任务 404 伪报成删除成功。
test('preserves the Job error when the target exists or recheck fails', async () => {
  for (const [listRequest, targetExists, targetError] of [
    [async () => assert.fail('existing target must not refresh the list'), true, null],
    [async () => assert.fail('failed target lookup must not refresh the list'), false, new Error('lookup unavailable')],
    [async () => { throw new Error('list unavailable'); }, false, null],
  ]) {
    const api = requestWithList(listRequest, 'RAG-JOB-001', targetExists, targetError);
    const view = workspace(api.request);
    await view.delete();
    assert.equal(view.knowledgeBases()[0].id, base.id);
    assert.match(view.notice().props.className, /error/);
    assert.equal(view.notice().props.children[1].props.children, 'Job 不存在');
  }
});

// 其他 Job 错误不触发兜底查询，避免掩盖真实失败或跨资源状态。
test('does not recheck knowledge bases for unrelated Job errors', async () => {
  const api = requestWithList(async () => assert.fail('unrelated errors must not recheck'), 'OTHER-JOB-404');
  const view = workspace(api.request);
  await view.delete();
  assert.equal(api.calls.length, 2);
  assert.match(view.notice().props.className, /error/);
});
