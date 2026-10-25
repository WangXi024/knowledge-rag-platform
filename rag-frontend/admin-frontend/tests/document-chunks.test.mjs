/**
 * 切块只读视图回归：用内存 Hook 状态和假 Gateway 执行真实组件，验证分页与错误状态。
 * 不启动浏览器，不访问真实文件/账号；卸载测试确保迟到响应不会更新已关闭的视图。
 */
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import test from 'node:test';
import { runInNewContext } from 'node:vm';
import * as React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import ts from 'typescript';

const require = createRequire(import.meta.url);
const positionExports = {};
runInNewContext(ts.transpileModule(await readFile(new URL('../lib/pdf-positions.ts', import.meta.url), 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText, { exports: positionExports });
const PdfSourcePreview = () => null;
const source = await readFile(new URL('../components/document-chunks.tsx', import.meta.url), 'utf8');
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX },
});
const documentId = '11111111-1111-4111-8111-111111111111';
const session = { role: 'TENANT_ADMIN', tenantId: 'chunk-test-tenant', userId: 'chunk-test-user' };

// 回归上一版套用普通弹窗的问题：抽屉必须独立存在，页脚不能放进正文滚动区。
test('uses a document drawer with separate header, scrollable list and footer', async () => {
  const view = mountChunks(async () => response());
  view.html();
  await Promise.resolve();
  const html = view.html();
  assert.match(html, /<dialog[^>]*class="chunk-drawer"/);
  assert.match(html, /<header[\s>]/);
  assert.match(html, /class="chunk-list"/);
  assert.match(html, /<\/article><\/div><footer class="chunk-pagination"/);
  assert.match(html, /合成测试文档\.pdf/);
  assert.match(html, /21 个内容片段/);
  view.unmount();
});

/** 生成无业务内容的分页响应，页码和总数可用于验证首尾页边界。 */
function response(page = 1, total = 21, content = '合成切块正文') {
  return {
    data: total ? [{ id: `chunk-${page}`, document_id: documentId, content, positions: [{ page: 3 }, { page: 1 }, { page: 3 }, { page: null }] }] : [],
    meta: { page, page_size: 20, total },
  };
}

/** 用内存节点模拟原生 dialog 生命周期，不启动浏览器；多个 Effect 按依赖独立清理。 */
function mountChunks(gatewayRequest, id = documentId, revision) {
  const states = [];
  const effects = new Map();
  let cursor = 0;
  let updates = 0;
  let closes = 0;
  class HTMLElement {
    isConnected = true;
    focused = false;
    focus() { this.focused = true; }
  }
  const trigger = new HTMLElement();
  const document = { activeElement: trigger, body: { style: { overflow: 'auto' } } };
  const drawer = { open: false, showModal() { this.open = true; }, close() { this.open = false; } };
  const list = { scrollTop: 0 };
  const exports = {};
  runInNewContext(outputText, {
    exports, document, HTMLElement,
    require: (name) => {
      if (name === 'react') return {
        useState: (initial) => {
          const index = cursor++;
          if (!(index in states)) states[index] = initial;
          return [states[index], (next) => {
            states[index] = typeof next === 'function' ? next(states[index]) : next;
            updates += 1;
          }];
        },
        useRef: (initial) => {
          const index = cursor++;
          if (!(index in states)) states[index] = { current: initial };
          return states[index];
        },
        useEffect: (effect, nextDependencies) => {
          const index = cursor++;
          const previous = effects.get(index);
          if (!previous || nextDependencies.some((value, index) => !Object.is(value, previous.dependencies[index]))) {
            effects.set(index, { dependencies: nextDependencies, pending: effect, cleanup: previous?.cleanup });
          }
        },
      };
      if (name === '@/lib/gateway-api') return { gatewayRequest, errorMessage: (error) => error.message };
      if (name === '@/lib/pdf-positions') return positionExports;
      if (name === '@/components/pdf-source-preview') return { PdfSourcePreview };
      return require(name);
    },
  });
  const render = () => {
    cursor = 0;
    const tree = exports.DocumentChunks({ session, documentId: id, filename: '合成测试文档.pdf', documentRevision: revision, chunkCount: 21, onClose: () => { closes += 1; } });
    for (const node of elements(tree)) {
      if (node.props.ref) node.props.ref.current = node.type === 'dialog' ? drawer : list;
    }
    for (const effect of effects.values()) {
      if (!effect.pending) continue;
      effect.cleanup?.();
      effect.cleanup = effect.pending();
      effect.pending = undefined;
    }
    return tree;
  };
  const elements = (value) => {
    if (Array.isArray(value)) return value.flatMap(elements);
    return React.isValidElement(value) ? [value, ...elements(value.props.children)] : [];
  };
  return {
    html: () => renderToStaticMarkup(render()),
    button: (label) => elements(render()).find((node) => node.type === 'button' && (node.props['aria-label'] === label || React.Children.toArray(node.props.children).filter((child) => typeof child === 'string').join('').trim() === label)),
    dialog: () => render(),
    preview: () => elements(render()).find((node) => node.type === PdfSourcePreview),
    drawer, list, document, trigger,
    closes: () => closes,
    unmount: () => { for (const effect of effects.values()) effect.cleanup?.(); },
    updates: () => updates,
  };
}

// 请求必须保留当前身份、管理端路径和 20 条分页；显示正文、总数、去重页码及首尾页按钮。
test('loads scoped chunks and paginates with correct content numbering', async () => {
  const requests = [];
  const view = mountChunks(async (identity, path, options) => {
    assert.equal(identity, session);
    assert.equal(options, undefined); // gatewayRequest 默认 GET，不传写操作或幂等配置。
    requests.push(path);
    return response(path.includes('page=2&') ? 2 : 1);
  });
  assert.match(view.html(), /正在读取内容/);
  await Promise.resolve();
  assert.match(view.html(), /21 个内容片段/);
  assert.match(view.html(), /合成切块正文/);
  assert.match(view.html(), /第 1、3 页/);
  assert.equal(view.button('上一页').props.disabled, true);
  assert.equal(view.button('下一页').props.disabled, false);
  view.list.scrollTop = 600;
  view.button('下一页').props.onClick();
  assert.match(view.html(), /正在读取内容/);
  assert.equal(view.list.scrollTop, 0);
  assert.equal(view.button('上一页').props.disabled, true);
  assert.equal(view.button('下一页').props.disabled, true);
  assert.match(view.html(), /21 个内容片段/);
  await Promise.resolve();
  assert.match(view.html(), /片段 21/);
  assert.equal(view.button('下一页').props.disabled, true);
  view.button('上一页').props.onClick();
  view.html();
  await Promise.resolve();
  assert.deepEqual(requests, [1, 2, 1].map((page) => `v1/management/documents/${documentId}/chunks?page=${page}&page_size=20`));
  view.unmount();
});

// 用户端只在多页时显示页脚；空响应必须覆盖列表中的旧总数，不伪造正文或页码。
test('shows an empty state without unnecessary pagination', async () => {
  const view = mountChunks(async () => response(1, 0));
  view.html();
  await Promise.resolve();
  assert.match(view.html(), /没有可显示的内容片段/);
  assert.match(view.html(), /0 个内容片段/);
  assert.equal(view.button('上一页'), undefined);
  assert.equal(view.button('下一页'), undefined);
  view.unmount();
});

// 第二页失败后重试同一页，不能回到第一页或继续显示旧页数据。
test('shows upstream errors and retries the failed page', async () => {
  let calls = 0;
  const view = mountChunks(async (_identity, path) => {
    calls += 1;
    if (calls === 2) throw new Error('测试：无权访问或服务暂不可用');
    return response(path.includes('page=2&') ? 2 : 1);
  });
  view.html();
  await Promise.resolve();
  view.button('下一页').props.onClick();
  view.html();
  await Promise.resolve();
  assert.match(view.html(), /无法加载切块：测试：无权访问或服务暂不可用/);
  assert.doesNotMatch(view.html(), /合成切块正文/);
  assert.equal(view.button('上一页').props.disabled, true);
  assert.equal(view.button('下一页').props.disabled, true);
  view.button('重试').props.onClick();
  assert.match(view.html(), /正在读取内容/);
  await Promise.resolve();
  assert.match(view.html(), /片段 21/);
  assert.equal(calls, 3);
  view.unmount();
});

// 关闭后才返回的旧请求不能更新状态；另一个文档重新挂载必须从第一页请求。
test('ignores late responses after closing and starts new documents at page one', async () => {
  let resolve;
  const pending = new Promise((done) => { resolve = done; });
  const oldView = mountChunks(() => pending);
  oldView.html();
  oldView.unmount();
  resolve(response());
  await Promise.resolve();
  assert.equal(oldView.updates(), 0);
  const newId = '22222222-2222-4222-8222-222222222222';
  const newView = mountChunks(async (_identity, path) => {
    assert.equal(path, `v1/management/documents/${newId}/chunks?page=1&page_size=20`);
    return response(1, 0);
  }, newId);
  assert.match(newView.html(), /正在读取内容/);
  await Promise.resolve();
  assert.match(newView.html(), /没有可显示的内容片段/);
  newView.unmount();
});

// 文档可能包含 HTML/脚本片段，必须原样显示为文本；没有来源页码时不编造页码。
test('escapes chunk markup and preserves text wrapping without invented source pages', async () => {
  const view = mountChunks(async () => {
    const result = response(1, 1, '<script>alert(1)</script>\n合成第二行');
    result.data[0].positions = [];
    return result;
  });
  view.html();
  await Promise.resolve();
  const html = view.html();
  assert.match(html, /&lt;script&gt;/);
  assert.doesNotMatch(html, /<script>|第 \d+ 页/);
  assert.match(html, /无页码信息/);
  assert.match(html, /\n合成第二行/);
  const css = await readFile(new URL('../app/globals.css', import.meta.url), 'utf8');
  assert.match(css, /\.chunk-content\s*\{[^}]*white-space:\s*pre-wrap/);
  assert.match(css, /\.chunk-list\s*\{[^}]*min-height:\s*0[^}]*overflow-y:\s*auto/);
  assert.match(css, /\.chunk-column\s*\{[^}]*grid-template-rows:\s*minmax\(0, 1fr\) auto/);
  assert.match(css, /\.chunk-drawer\s*\{[^}]*height:\s*100dvh/);
  assert.match(css, /@media \(max-width: 760px\)[\s\S]*\.chunk-drawer\s*\{\s*width:\s*100vw/);
  assert.doesNotMatch(html, /class="chunk-pagination"/);
  view.unmount();
});

// 预览和切块固定同一文档修订；点击切块跳首个来源页，翻页取消旧高亮。
test('links a PDF chunk to the same revision and clears selection on chunk pagination', async () => {
  const view = mountChunks(async (_identity, path) => {
    assert.match(path, /&document_revision=7$/);
    return response();
  }, documentId, 7);
  view.html();
  await Promise.resolve();
  assert.match(view.html(), /class="chunk-drawer with-source"/);
  assert.equal(view.preview().props.revision, 7);
  assert.equal(view.preview().props.documentId, documentId);
  view.button('查看原文').props.onClick();
  assert.equal(view.preview().props.page, 1);
  assert.equal(view.preview().props.selectedChunk.id, 'chunk-1');
  assert.match(view.html(), /class="chunk-card selected"/);
  view.preview().props.onPageChange(3);
  assert.equal(view.preview().props.page, 3);
  view.button('下一页').props.onClick();
  assert.equal(view.preview().props.selectedChunk, null);
  view.unmount();
});

// 模态使用原生焦点隔离；三种关闭方式通知父级，清理时恢复原滚动设置与触发按钮焦点。
test('opens modally and handles close button, Escape and backdrop without closing on content clicks', async () => {
  const view = mountChunks(async () => response());
  view.html();
  await Promise.resolve();
  assert.equal(view.drawer.open, true);
  assert.equal(view.document.body.style.overflow, 'hidden');
  view.button('关闭内容片段').props.onClick();
  let prevented = false;
  view.dialog().props.onCancel({ preventDefault() { prevented = true; } });
  assert.equal(prevented, true);
  view.dialog().props.onClick({ target: {}, currentTarget: view.drawer });
  assert.equal(view.closes(), 2);
  view.dialog().props.onClick({ target: view.drawer, currentTarget: view.drawer });
  assert.equal(view.closes(), 3);
  view.unmount();
  assert.equal(view.drawer.open, false);
  assert.equal(view.document.body.style.overflow, 'auto');
  assert.equal(view.trigger.focused, true);
});
