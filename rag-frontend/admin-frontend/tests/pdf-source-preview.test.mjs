/** PDF 阅读器回归：模拟只读文件响应与 PDF.js，验证身份、错误重试和卸载清理，不访问浏览器或真实文件。 */
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
const positions = {};
runInNewContext(compile(await readFile(new URL('../lib/pdf-positions.ts', import.meta.url), 'utf8')), { exports: positions });
const code = compile(await readFile(new URL('../components/pdf-source-preview.tsx', import.meta.url), 'utf8'));
const identity = { tenantId: 'synthetic-tenant', userId: 'synthetic-user' };
const flush = () => new Promise((done) => setImmediate(done));
const success = () => new Response('%PDF-1.7\nsynthetic', { headers: { 'content-type': 'application/pdf' } });

/** 执行组件的真实加载 Effect；画布子组件只作占位，视觉坐标由纯函数测试独立验证。 */
function mount(fetcher, promise = Promise.resolve({ numPages: 3 })) {
  const states = [], effects = new Map(), requests = [], options = [];
  let cursor = 0, updates = 0, destroyed = 0;
  const worker = {};
  const exports = {};
  runInNewContext(code, { exports, AbortController, Uint8Array, Error,
    fetch: (...args) => { requests.push(args); return fetcher(...args); },
    require: (name) => {
      if (name === 'react') return {
        useState: (initial) => {
          const index = cursor++;
          if (!(index in states)) states[index] = initial;
          return [states[index], (next) => { states[index] = next; updates++; }];
        },
        useEffect: (pending, dependencies) => {
          const index = cursor++, previous = effects.get(index);
          if (!previous || dependencies.some((value, n) => value !== previous.dependencies[n]))
            effects.set(index, { pending, dependencies, cleanup: previous?.cleanup });
        },
      };
      if (name === '@/lib/pdf-positions') return positions;
      if (name === 'pdfjs-dist') return { version: 'synthetic', GlobalWorkerOptions: worker,
        getDocument: (value) => { options.push(value); return { promise, destroy: async () => { destroyed++; } }; },
      };
      return require(name);
    },
  });
  const elements = (value) => Array.isArray(value) ? value.flatMap(elements)
    : React.isValidElement(value) ? [value, ...elements(value.props.children)] : [];
  const render = () => {
    cursor = 0;
    const tree = exports.PdfSourcePreview({ session: identity, documentId: 'synthetic-id', revision: 7,
      selectedChunk: { positions: [{ page: 1 }, { page: 3 }] }, page: 1, onPageChange() {},
    });
    for (const effect of effects.values()) if (effect.pending) {
      effect.cleanup?.(); effect.cleanup = effect.pending(); effect.pending = undefined;
    }
    return tree;
  };
  return { render, requests, options, worker,
    html: () => renderToStaticMarkup(render()),
    button: (label) => elements(render()).find((node) => node.type === 'button' && node.props.children === label),
    unmount: () => { for (const effect of effects.values()) effect.cleanup?.(); },
    updates: () => updates, destroyed: () => destroyed,
  };
}

test('loads fixed-revision PDF with scoped same-origin resources and destroys it on close', async () => {
  const view = mount(async () => success());
  assert.match(view.html(), /正在加载原文件/);
  await flush();
  const [url, init] = view.requests[0];
  assert.equal(url, '/api/gateway/v1/management/documents/synthetic-id/source?document_revision=7');
  assert.equal(init.headers['X-Tenant-ID'], identity.tenantId);
  assert.equal(init.headers['X-User-ID'], identity.userId);
  assert.equal(init.cache, 'no-store');
  assert.equal(view.worker.workerSrc, '/pdfjs/synthetic/pdf.worker.min.mjs');
  assert.equal(view.options[0].cMapUrl, '/pdfjs/synthetic/cmaps/');
  assert.equal(view.options[0].wasmUrl, '/pdfjs/synthetic/wasm/');
  assert.equal(view.options[0].standardFontDataUrl, '/pdfjs/synthetic/standard_fonts/');
  assert.equal(new TextDecoder().decode(view.options[0].data), '%PDF-1.7\nsynthetic');
  view.unmount();
  assert.equal(init.signal.aborted, true);
  assert.equal(view.destroyed(), 1);
});

test('displays backend errors and retries without changing identity or revision', async () => {
  let count = 0;
  const view = mount(async () => ++count === 1
    ? Response.json({ error: { message: '合成错误：请刷新文档列表' } }, { status: 409 }) : success());
  view.render(); await flush();
  assert.match(view.html(), /合成错误：请刷新文档列表/);
  assert.equal(view.options.length, 0);
  view.button('重新加载原文件').props.onClick();
  assert.match(view.html(), /正在加载原文件/);
  await flush();
  assert.equal(view.options.length, 1);
  assert.equal(view.requests[0][0], view.requests[1][0]);
  assert.equal(view.requests[0][1].signal.aborted, true);
  view.unmount();
});

test('rejects HTML and truncated responses before opening the PDF engine', async () => {
  for (const fetcher of [
    async () => new Response('<html>not a PDF</html>', { headers: { 'content-type': 'text/html' } }),
    async () => ({ ok: true, headers: new Headers({ 'content-type': 'application/pdf' }),
      arrayBuffer: async () => { throw new Error('合成错误：响应中断'); } }),
  ]) {
    const view = mount(fetcher); view.render(); await flush();
    assert.match(view.html(), /未返回 PDF|响应中断/);
    assert.equal(view.options.length, 0);
    view.unmount();
  }
});

test('ignores pending download or PDF loading completion after closing', async () => {
  let respond;
  const view = mount(() => new Promise((resolve) => { respond = resolve; }));
  view.render(); view.unmount(); respond(success()); await flush();
  assert.equal(view.updates(), 0);
  assert.equal(view.options.length, 0);
  let loaded;
  const pending = mount(async () => success(), new Promise((resolve) => { loaded = resolve; }));
  pending.render(); await flush(); pending.unmount(); loaded({ numPages: 3 }); await flush();
  assert.equal(pending.updates(), 0);
  assert.equal(pending.destroyed(), 1);
});
