/** 坐标映射回归：合成页面验证 RAGFlow 坐标顺序、跨页与异常数据，不访问业务文档。 */
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import { runInNewContext } from 'node:vm';
import ts from 'typescript';

const exports = {};
const source = await readFile(new URL('../lib/pdf-positions.ts', import.meta.url), 'utf8');
runInNewContext(ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText, { exports });
const { sourcePages, sourceRects } = exports;

// 600×800 页面中的左右上下坐标，必须得到正确百分比，而非错误地交换 y1/x2。
test('maps left/right/top/bottom positions and preserves multiple rectangles on one page', () => {
  const positions = [{ page: 2, bbox: [60, 300, 160, 240] }, { page: 2, bbox: [0, 60, 0, 80] }, { page: 3, bbox: [0, 60, 0, 80] }];
  assert.deepEqual(JSON.parse(JSON.stringify(sourceRects(positions, 2, 600, 800))), [
    { left: 10, top: 20, width: 40, height: 10 }, { left: 0, top: 0, width: 10, height: 10 },
  ]);
});

// 不裁剪越界位置、不交换逆序坐标；没有有效坐标时仅允许页码定位。
test('rejects missing, inverted, nonfinite and out-of-page rectangles', () => {
  const invalid = [null, [3, 2, 0, 1], [0, 1, 5, 4], [-1, 1, 0, 1], [0, 601, 0, 1], [0, 1, 0, 801], [0, Infinity, 0, 1], [0, NaN, 0, 1]];
  assert.equal(sourceRects(invalid.map((bbox) => ({ page: 1, bbox })), 1, 600, 800).length, 0);
  assert.equal(sourceRects([{ page: 1 }], 1, 600, 800).length, 0);
  assert.equal(sourceRects([{ page: 1, bbox: [0, 1, 0, 1] }], 1, 0, 800).length, 0);
  assert.equal(sourceRects([{ page: 1, bbox: [0, 1, 0, 1] }], 1, Infinity, 800).length, 0);
  assert.deepEqual(Array.from(sourcePages([3, 1, 3, null, 0, -1, 1.5].map((page) => ({ page })))), [1, 3]);
});
