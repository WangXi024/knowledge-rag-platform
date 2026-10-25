/**
 * 局域网上传回归：编译真实浏览器请求工具，在隔离环境模拟 HTTP/HTTPS 的随机数能力。
 * 请求只进入内存中的假 Gateway，验证文件、身份和幂等键，不连接网络或写入业务数据。
 */
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import { runInNewContext } from 'node:vm';
import ts from 'typescript';

const source = await readFile(new URL('../lib/gateway-api.ts', import.meta.url), 'utf8');
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
});
const session = { role: 'TENANT_ADMIN', tenantId: 'lan-test-tenant', userId: 'lan-test-user' };

/** 注入当前场景的随机数能力与假请求函数；返回真实模块，不修改全局浏览器对象。 */
function clientWith(crypto, fetch) {
  const exports = {};
  runInNewContext(outputText, { exports, crypto, fetch, Headers, FormData });
  return exports;
}

// HTTP 页面没有 randomUUID，但有 getRandomValues；两个文件必须各自发出合法且不同的键。
test('uploads FormData over LAN HTTP with independent UUID v4 keys', async () => {
  const requests = [];
  let sequence = 0;
  const { gatewayRequest } = clientWith({
    getRandomValues: (bytes) => bytes.fill(sequence++ === 0 ? 0 : 255),
  }, async (url, options) => {
    requests.push({ url, ...options });
    return Response.json({ accepted: true });
  });

  for (const filename of ['first.txt', 'second.txt']) {
    const body = new FormData();
    body.set('file', new Blob(['synthetic LAN upload fixture']), filename);
    const result = await gatewayRequest(session, '/v1/management/knowledge-bases/test/files', {
      method: 'POST', idempotent: true, body,
    });
    assert.deepEqual(result, { accepted: true });
    const request = requests.at(-1);
    assert.equal(request.url, '/api/gateway/v1/management/knowledge-bases/test/files');
    assert.equal(request.method, 'POST');
    assert.equal(request.body, body);
    assert.equal(request.body.get('file').name, filename);
    assert.equal(await request.body.get('file').text(), 'synthetic LAN upload fixture');
    assert.equal(request.headers.get('X-Tenant-ID'), session.tenantId);
    assert.equal(request.headers.get('X-User-ID'), session.userId);
    // 不手填 Content-Type，确保浏览器为每次 multipart 上传生成正确的 boundary。
    assert.equal(request.headers.has('content-type'), false);
  }
  assert.deepEqual(requests.map(({ headers }) => headers.get('Idempotency-Key')), [
    '00000000-0000-4000-8000-000000000000',
    'ffffffff-ffff-4fff-bfff-ffffffffffff',
  ]);
});

// HTTPS/localhost 仍优先使用原生 UUID；JSON 创建请求的序列化方式保持不变。
test('preserves native randomUUID and JSON requests in secure contexts', async () => {
  const key = '12345678-1234-4123-8123-123456789abc';
  const { gatewayRequest } = clientWith({
    randomUUID: () => key,
    getRandomValues: () => assert.fail('secure contexts should use native randomUUID'),
  }, async (url, options) => {
    assert.equal(url, '/api/gateway/v1/management/knowledge-bases');
    assert.equal(options.headers.get('Idempotency-Key'), key);
    assert.equal(options.headers.get('content-type'), 'application/json');
    assert.equal(options.body, JSON.stringify({ name: 'LAN fixture' }));
    return Response.json({ created: true });
  });
  assert.deepEqual(await gatewayRequest(session, 'v1/management/knowledge-bases', {
    method: 'POST', idempotent: true, body: { name: 'LAN fixture' },
  }), { created: true });
});

// 普通查询不需要随机数，也不能意外带上写操作的幂等键。
test('does not generate idempotency keys for ordinary reads', async () => {
  const { gatewayRequest } = clientWith({}, async (_url, options) => {
    assert.equal(options.method, 'GET');
    assert.equal(options.headers.has('Idempotency-Key'), false);
    return Response.json([]);
  });
  assert.deepEqual(await gatewayRequest(session, 'v1/management/knowledge-bases'), []);
});

// 随机数源故障时明确失败，不退化为 Math.random 或发送没有合格幂等键的上传。
test('does not send a write if secure randomness fails', async () => {
  const failure = new Error('randomness unavailable');
  const { gatewayRequest } = clientWith({
    getRandomValues: () => { throw failure; },
  }, () => assert.fail('failed key generation must prevent the request'));
  await assert.rejects(gatewayRequest(session, 'v1/management/knowledge-bases', {
    method: 'POST', idempotent: true,
  }), (error) => error === failure);
});

// HTTP 兼容分支不能吞掉 Gateway 的业务错误；页面仍能显示原有错误说明。
test('preserves upstream error messages after generating an HTTP-compatible key', async () => {
  const { gatewayRequest, GatewayError } = clientWith({
    getRandomValues: (bytes) => bytes.fill(42),
  }, async () => Response.json({ error: { message: '测试文件被拒绝' } }, { status: 400 }));
  await assert.rejects(gatewayRequest(session, 'v1/management/knowledge-bases/test/files', {
    method: 'POST', idempotent: true, body: new FormData(),
  }), (error) => error instanceof GatewayError && error.status === 400 && error.message === '测试文件被拒绝');
});

// 局域网监听必须显式选择，并且仅额外放行 wx.local；原来的 dev 命令仍只用于本机。
test('keeps LAN access opt-in with an explicit hostname allowlist', async () => {
  const { scripts } = JSON.parse(await readFile(new URL('../package.json', import.meta.url), 'utf8'));
  assert.doesNotMatch(scripts.dev, /0\.0\.0\.0|--hostname|--host/);
  assert.match(scripts['dev:lan'] ?? '', /^__VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS=wx\.local npm run dev -- --hostname 0\.0\.0\.0 --port 3002$/);
});
