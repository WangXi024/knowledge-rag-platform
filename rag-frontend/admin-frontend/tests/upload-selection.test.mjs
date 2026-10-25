/**
 * 已选文件展示回归：执行真实工作区的选择/关闭事件，检查生成的 React 元素和文本。
 * 用内存状态模拟已经加载的测试知识库，不启动浏览器、不读取文件内容或调用后端。
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
const source = await readFile(new URL('../components/admin-workspace.tsx', import.meta.url), 'utf8');
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX },
});

/** 遍历元素及其插槽（如标题操作区），不执行其他组件或触发它们的副作用。 */
function elements(value) {
  if (Array.isArray(value)) return value.flatMap(elements);
  if (!React.isValidElement(value)) return [];
  return [value, ...Object.values(value.props).flatMap(elements)];
}

/** 用已加载知识库填充前四个状态槽，其余状态按源码初始化；事件后显式重新渲染。 */
function workspace({ profiles = [], request } = {}) {
  const states = [
    [{ id: 'selection-test-kb', name: '测试知识库', scope_type: 'TENANT' }],
    [],
    { file_extensions: ['.txt', '.pdf'], parser_profiles: profiles, chunk_profiles: [], retrieval_profiles: [], features: {} },
    'selection-test-kb',
  ];
  let cursor = 0;
  const exports = {};
  runInNewContext(outputText, {
    exports,
    // 模拟表单读取和 multipart 字段容器；请求内容由真实上传处理函数构造。
    FormData: class extends Map {
      constructor(form) { super(form?.fields ?? []); }
    },
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
      if (name === '@/lib/gateway-api') return {
        gatewayRequest: request ?? (() => assert.fail('file selection must not contact the backend')),
        errorMessage: (error) => error.message,
      };
      // 这里验证抽屉入口与参数，切块组件的异步查询由独立回归测试负责。
      if (name === '@/components/document-chunks') return { DocumentChunks: () => null };
      return require(name);
    },
  });
  const find = (predicate) => {
    cursor = 0;
    const tree = exports.AdminWorkspace({
      session: { role: 'TENANT_ADMIN', tenantId: 'selection-test', userId: 'selection-test' },
      section: 'documents', onSectionChange: () => {},
    });
    return elements(tree).find(predicate);
  };
  return {
    find,
    open: () => find((node) => typeof node.props.onUpload === 'function').props.onUpload(),
    select: (files) => find((node) => node.type === 'input' && node.props.type === 'file')
      .props.onChange({ currentTarget: { files: files.map((file) => typeof file === 'string' ? { name: file, size: 0 } : file) } }),
    list: () => find((node) => node.type === 'ul' && node.props['aria-label'] === '已选择的文件'),
  };
}

// 选择一个文件后，名称必须成为常驻页面文本，而不是只有鼠标悬停提示。
test('shows the selected filename and file count without hover', () => {
  const view = workspace();
  view.open();
  assert.equal(view.list(), undefined);
  view.select(['测试资料.txt']);
  assert.match(renderToStaticMarkup(view.list()), /测试资料\.txt/);
  assert.ok(view.find((node) => node.type === 'strong' && React.Children.toArray(node.props.children).join('') === '已选择 1 个文件'));
});

// 只读取 File 的大小元数据，不读取正文；每个文件旁显示与文档表一致的二进制单位。
test('shows each selected file size in bytes, KiB or MiB', () => {
  const view = workspace();
  view.open();
  view.select([
    { name: '空文件.txt', size: 0 },
    { name: '小文件.txt', size: 500 },
    { name: '中等文件.txt', size: 2048 },
    { name: '大文件.pdf', size: 2.5 * 1024 * 1024 },
  ]);
  const rows = React.Children.toArray(view.list().props.children).map(renderToStaticMarkup);
  for (const [index, expected] of ['0 B', '500 B', '2 KiB', '2.50 MiB'].entries()) {
    assert.ok(rows[index].includes(expected));
  }
  view.select([{ name: '替换文件.txt', size: 1024 }]);
  assert.match(renderToStaticMarkup(view.list()), /1 KiB/);
  assert.doesNotMatch(renderToStaticMarkup(view.list()), /2\.50 MiB/);
});

// 多文件、同名文件和长名称都完整显示；再次选择替换列表，而非累加旧记录。
test('lists every selected file and replaces the list on reselection', () => {
  const view = workspace();
  view.open();
  const longName = `${'长文件名'.repeat(30)}.pdf`;
  view.select(['资料.txt', '资料.txt', longName]);
  const html = renderToStaticMarkup(view.list());
  assert.equal((html.match(/<li/g) ?? []).length, 3);
  assert.ok(html.includes(longName));
  view.select(['新文件.txt']);
  const updated = renderToStaticMarkup(view.list());
  assert.match(updated, /新文件\.txt/);
  assert.doesNotMatch(updated, /资料\.txt|长文件名/);
  view.select([]);
  assert.equal(view.list(), undefined);
});

// 关闭按钮/遮罩和取消按钮都应清空显示；重新打开不能出现已经失效的文件选择。
for (const closeVia of ['close', 'cancel']) {
  test(`clears selection after ${closeVia} and reopening the upload dialog`, () => {
    const view = workspace();
    view.open();
    view.select(['上次选择.txt']);
    view.find((item) => item.type === 'select' && item.props['aria-label'] === '解析策略')
      .props.onChange({ target: { value: 'MANUAL' } });
    const node = closeVia === 'close'
      ? view.find((item) => item.props.title === '上传知识文件')
      : view.find((item) => item.props.submitLabel === '开始上传');
    node.props[closeVia === 'close' ? 'onClose' : 'onCancel']();
    assert.equal(view.list(), undefined);
    view.open();
    assert.equal(view.list(), undefined);
    assert.equal(view.find((item) => item.type === 'select' && item.props['aria-label'] === '解析策略').props.value, 'AUTO');
  });
}

// 文件名按普通文本输出，长名称允许换行，不能把文件名当 HTML 或裁成省略号。
test('escapes filenames and enables wrapping for long names', async () => {
  const view = workspace();
  view.open();
  view.select(['<img src=x>.txt']);
  const html = renderToStaticMarkup(view.list());
  assert.match(html, /&lt;img src=x&gt;\.txt/);
  assert.doesNotMatch(html, /<img/);
  const css = await readFile(new URL('../app/globals.css', import.meta.url), 'utf8');
  assert.match(css, /\.selected-upload-files li\s*\{[^}]*overflow-wrap:\s*anywhere/);
});

// 只有已解析完成的文件可查看；直接打开独立抽屉，保留文档概要、身份隔离和关闭清理。
test('opens chunk details for completed documents with identity-scoped component keys', () => {
  const view = workspace();
  const table = view.find((node) => typeof node.props.onViewChunks === 'function');
  const documents = ['READY', 'INDEXED', 'PARSING', 'FAILED'].map((status, index) => ({
    id: `chunk-entry-test-${index}`, filename: `文档-${index}.txt`, status,
    progress: 1, file_size_bytes: 2048, chunk_count: 1, token_count: 10, document_revision: 1,
    updated_at: '2026-09-04T00:00:00Z',
  }));
  const buttons = elements(table.type({ ...table.props, items: documents }))
    .filter((node) => node.type === 'button' && node.props.className === 'view-chunks-button');
  assert.deepEqual(buttons.map((button) => button.props.disabled), [false, false, true, true]);
  buttons[0].props.onClick();
  assert.equal(view.find((node) => node.props.title === '文档切块'), undefined);
  const first = view.find((node) => node.props.documentId === documents[0].id);
  assert.equal(first.props.filename, documents[0].filename);
  assert.equal(first.props.chunkCount, documents[0].chunk_count);
  assert.deepEqual(JSON.parse(first.key), ['TENANT_ADMIN', 'selection-test', 'selection-test', documents[0].id, 1]);
  assert.equal(first.props.documentRevision, 1);
  assert.equal(first.props.session.tenantId, 'selection-test');
  first.props.onClose();
  assert.equal(view.find((node) => node.props.documentId === documents[0].id), undefined);
  buttons[1].props.onClick();
  assert.notEqual(view.find((node) => node.props.documentId === documents[1].id).key, first.key);
});

// 自动模式只为 PDF 指定 MinerU，同批其他文件仍保留后端路由和分块选择。
test('AUTO uses MinerU for PDF files in a mixed upload when available', async () => {
  const requests = [];
  const view = workspace({ profiles: ['mineru-cloud@1'], request: async (...args) => {
    requests.push(args);
    return { data: [] };
  } });
  view.open();
  const option = view.find((node) => node.type === 'option' && node.props.value === 'AUTO');
  assert.match(renderToStaticMarkup(option), /PDF 默认 MinerU/);
  await view.find((node) => node.type === 'form' && typeof node.props.onSubmit === 'function')
    .props.onSubmit({ preventDefault() {}, currentTarget: {
      fields: [], elements: { namedItem: () => ({ files: [
        { name: 'a.PDF', size: 10 }, { name: 'b.docx', size: 10 },
      ] }) },
    } });
  const uploads = requests.filter((args) => args[2]?.method === 'POST');
  assert.equal(uploads[0][2].body.get('parser_profile'), 'mineru-cloud@1');
  assert.equal(uploads[1][2].body.has('parser_profile'), false);
  assert.ok(uploads.every((args) => !args[2].body.has('chunk_profile')));
});

// 后端没有导入 MinerU 时，自动模式沿用原有规则，不提交不可用的解析策略。
test('AUTO leaves PDF routing to the backend when MinerU is unavailable', async () => {
  const requests = [];
  const view = workspace({ request: async (...args) => {
    requests.push(args);
    return { data: [] };
  } });
  view.open();
  await view.find((node) => node.type === 'form' && typeof node.props.onSubmit === 'function')
    .props.onSubmit({ preventDefault() {}, currentTarget: {
      fields: [], elements: { namedItem: () => ({ files: [{ name: 'a.pdf', size: 10 }] }) },
    } });
  const upload = requests.find((args) => args[2]?.method === 'POST');
  assert.equal(upload[2].body.has('parser_profile'), false);
});

// 默认值不能覆盖管理员为单次 PDF 上传手动选择的解析器和分块策略。
test('manual PDF selection overrides the MinerU default', async () => {
  const requests = [];
  const view = workspace({ profiles: ['mineru-cloud@1', 'deepdoc-default@1'], request: async (...args) => {
    requests.push(args);
    return { data: [] };
  } });
  view.open();
  view.find((node) => node.type === 'select' && node.props['aria-label'] === '解析策略')
    .props.onChange({ target: { value: 'MANUAL' } });
  await view.find((node) => node.type === 'form' && typeof node.props.onSubmit === 'function')
    .props.onSubmit({ preventDefault() {}, currentTarget: {
      fields: [['parserProfile', 'deepdoc-default@1'], ['chunkProfile', 'general-256@2']],
      elements: { namedItem: () => ({ files: [{ name: 'a.pdf', size: 10 }] }) },
    } });
  const upload = requests.find((args) => args[2]?.method === 'POST');
  assert.equal(upload[2].body.get('parser_profile'), 'deepdoc-default@1');
  assert.equal(upload[2].body.get('chunk_profile'), 'general-256@2');
});


// 两种 PDF 快捷策略均提交各自引用，且不覆盖后端分块选择。
for (const [modeValue, profile] of [['VISION', 'vision-pdf@1'], ['MINERU', 'mineru-cloud@1']]) {
test(`${modeValue} PDF selection sends the explicit parser profile`, async () => {
  const requests = [];
  const view = workspace({ profiles: [profile], request: async (...args) => {
    requests.push(args);
    return { data: [] };
  } });
  view.open();
  const mode = () => view.find((node) => node.type === 'select' && node.props['aria-label'] === '解析策略');
  assert.equal(mode().props.value, 'AUTO');
  mode().props.onChange({ target: { value: modeValue } });
  const form = view.find((node) => node.type === 'form' && typeof node.props.onSubmit === 'function');
  await form.props.onSubmit({ preventDefault() {}, currentTarget: {
    fields: [], elements: { namedItem: () => ({ files: [{ name: 'example.PDF', size: 10 }] }) },
  } });
  const upload = requests.find((args) => args[2]?.method === 'POST');
  assert.equal(upload[2].body.get('parser_profile'), profile);
  assert.equal(upload[2].body.has('chunk_profile'), false);
});

// 混合文件必须在任何请求发出前拦截，避免只上传一部分。
test(`${modeValue} mode rejects non-PDF files before uploading`, async () => {
  const view = workspace({ profiles: [profile] });
  view.open();
  view.find((node) => node.type === 'select' && node.props['aria-label'] === '解析策略')
    .props.onChange({ target: { value: modeValue } });
  await view.find((node) => node.type === 'form' && typeof node.props.onSubmit === 'function')
    .props.onSubmit({ preventDefault() {}, currentTarget: {
      fields: [], elements: { namedItem: () => ({ files: [{ name: 'a.pdf', size: 1 }, { name: 'b.docx', size: 1 }] }) },
    } });
  const notice = view.find((node) => node.props.className === 'notice error');
  assert.match(renderToStaticMarkup(notice), /支持 PDF/);
});

}

// 后端未导入时禁用云端选项，避免用户把配置未完成误认为可解析。
test('MinerU option is disabled before profile import', () => {
  const view = workspace({ profiles: [] });
  view.open();
  const option = view.find((node) => node.type === 'option' && node.props.value === 'MINERU');
  assert.equal(option.props.disabled, true);
});


// 混选 PDF/图片时分别传递策略，原始图片由后端转换，前端不改文件字节。
test('MinerU mixed upload chooses a profile for each file', async () => {
  const requests = [];
  const view = workspace({ profiles: ['mineru-cloud@1', 'mineru-image@1'], request: async (...args) => {
    requests.push(args);
    return { data: [] };
  } });
  view.open();
  view.find((node) => node.type === 'select' && node.props['aria-label'] === '解析策略')
    .props.onChange({ target: { value: 'MINERU' } });
  await view.find((node) => node.type === 'form' && typeof node.props.onSubmit === 'function')
    .props.onSubmit({ preventDefault() {}, currentTarget: {
      fields: [], elements: { namedItem: () => ({ files: [
        { name: 'a.pdf', size: 10 }, { name: 'b.PNG', size: 10 }, { name: 'c.jpeg', size: 10 },
      ] }) },
    } });
  assert.deepEqual(requests.filter((args) => args[2]?.method === 'POST')
    .map((args) => args[2].body.get('parser_profile')),
    ['mineru-cloud@1', 'mineru-image@1', 'mineru-image@1']);
});
