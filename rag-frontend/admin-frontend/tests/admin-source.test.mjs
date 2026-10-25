// 管理端源码回归测试：锁定角色范围、管理能力、多文件上传和开发期安全警告。

import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

/** 以测试文件为基准读取源码，避免测试依赖启动中的浏览器或真实业务数据。 */
async function source(path) {
  return readFile(new URL(path, import.meta.url), 'utf8');
}

// 场景：进入系统前选择管理员类型；预期：两种角色存在且页面明确声明它不是正式鉴权。
test('offers separate tenant and system administrator entry modes', async () => {
  const [page, contracts] = await Promise.all([
    source('../app/page.tsx'),
    source('../lib/contracts.ts'),
  ]);
  assert.match(contracts, /'TENANT_ADMIN' \| 'SYSTEM_ADMIN'/);
  assert.match(page, /租户管理员/);
  assert.match(page, /系统管理员/);
  assert.match(page, /platform-management/);
  assert.match(page, /开发环境身份模拟/);
  assert.match(page, /未接入 JWT 与管理员鉴权/);
});

// 场景：任一管理员加载或创建知识库；预期：Scope 由角色计算，表单不能手动改成另一类。
test('fixes the manageable scope from the selected role', async () => {
  const workspace = await source('../components/admin-workspace.tsx');
  assert.match(workspace, /session\.role === 'TENANT_ADMIN' \? 'TENANT' : 'SYSTEM'/);
  assert.match(workspace, /scope_type: scope/);
  assert.match(workspace, /scope_type=\$\{scope\}/);
  assert.doesNotMatch(workspace, /name="scopeType"/);
});

// 场景：管理员维护资源；预期：源码包含知识库 CRUD、文档重试和重解析管理链路。
test('supports knowledge base CRUD and common document operations', async () => {
  const workspace = await source('../components/admin-workspace.tsx');
  assert.match(workspace, /v1\/management\/knowledge-bases/);
  assert.match(workspace, /method: 'PATCH'/);
  assert.match(workspace, /method: 'DELETE'/);
  assert.match(workspace, /\/retry/);
  assert.match(workspace, /\/reparse/);
  assert.match(workspace, /重新解析会替换现有文本块/);
});

// 场景：一次选择不同类型文件；预期：自动模式仅覆盖 PDF MinerU，其余路由仍由后端决定。
test('uploads multiple files with independent keys and automatic PDF MinerU routing', async () => {
  const [workspace, api] = await Promise.all([
    source('../components/admin-workspace.tsx'),
    source('../lib/gateway-api.ts'),
  ]);
  assert.match(workspace, /type="file" multiple/);
  assert.match(workspace, /for \(const file of files\)/);
  assert.match(workspace, /uploadMode === 'MANUAL'/);
  assert.match(workspace, /body\.set\('parser_profile'/);
  assert.match(workspace, /body\.set\('chunk_profile'/);
  assert.doesNotMatch(workspace, /parserProfileForFile|chunkProfileForFile/);
  // 幂等键统一经过 HTTP/HTTPS 兼容入口；独立键与实际请求由 lan-upload 测试验证。
  assert.match(api, /headers\.set\('Idempotency-Key', createIdempotencyKey\(\)\)/);
  assert.doesNotMatch(api, /parserProfileForFile|chunkProfileForFile/);
});

// 场景：管理端上传一个接近后端上限的文件；预期：浏览器限制为 100 MiB，
// vinext 为 multipart 封装额外预留空间，不再用默认 1 MiB 提前返回 413。
test('allows a 100 MiB file through the multipart proxy', async () => {
  const [workspace, config] = await Promise.all([
    source('../components/admin-workspace.tsx'),
    source('../next.config.ts'),
  ]);
  assert.match(workspace, /MAX_UPLOAD_FILE_BYTES = 100 \* 1024 \* 1024/);
  assert.match(workspace, /Math\.min\(capabilities\.max_file_bytes \?\? MAX_UPLOAD_FILE_BYTES, MAX_UPLOAD_FILE_BYTES\)/);
  assert.match(workspace, /单文件上限 100 MiB/);
  assert.match(config, /bodySizeLimit: '101mb'/);
});

// 场景：浏览器通过同源代理访问 Gateway；预期：路径/Header 白名单不含 Authorization。
test('keeps the same-origin proxy path and header allowlists narrow', async () => {
  const proxy = await source('../app/api/gateway/[...path]/route.ts');
  assert.match(proxy, /joinedPath\.startsWith\('v1\/'\)/);
  assert.match(proxy, /'x-tenant-id'/);
  assert.match(proxy, /'x-user-id'/);
  assert.match(proxy, /'idempotency-key'/);
  assert.doesNotMatch(proxy, /authorization/i);
});
