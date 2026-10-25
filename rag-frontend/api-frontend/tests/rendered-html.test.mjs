// 源码级页面测试：确保构建前仍存在 Direct User 隔离、产品导航和关键面板。
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

test("defines the direct tenant and user development surface", async () => {
  const [page, shell, layout] = await Promise.all([
    readFile(new URL("../app/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../components/console/console-app.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/layout.tsx", import.meta.url), "utf8"),
  ]);
  assert.match(page, /ConsoleApp/);
  assert.match(shell, /Direct Identity/);
  assert.match(shell, /X-Tenant-ID/);
  assert.match(shell, /X-User-ID/);
  assert.match(shell, /禁止生产使用/);
  assert.match(layout, /lang="zh-CN"/);
  assert.doesNotMatch(`${page}\n${shell}\n${layout}`, /Your site is taking shape|react-loading-skeleton/i);
});

test("declares product entry points for all 30 Gateway endpoints", async () => {
  const source = await readFile(
    new URL("../components/console/api-console-panel.tsx", import.meta.url),
    "utf8",
  );
  const declarations = source.match(/method: "(?:GET|POST|PATCH|DELETE)"/g) ?? [];
  assert.equal(declarations.length, 30);
  for (const requiredPath of [
    "health/live",
    "health/ready",
    "v1/knowledge-bases",
    "v1/documents/{document_id}/chunks",
    "v1/documents/{document_id}/versions",
    "v1/retrieval",
    "v1/answers/stream",
    "v1/requests/{request_id}/citations",
    "v1/feedback",
  ]) {
    assert.match(source, new RegExp(requiredPath.replace(/[{}]/g, "\\$&")));
  }
});

test("exposes safe document version upload, history and rollback controls", async () => {
  const [source, client] = await Promise.all([
    readFile(new URL("../components/console/documents-panel.tsx", import.meta.url), "utf8"),
    readFile(new URL("../lib/client-api.ts", import.meta.url), "utf8"),
  ]);
  assert.match(source, /title="安全更新文档"/);
  assert.match(source, /新版成功前旧版持续可检索/);
  assert.match(source, /SAFE_REPLACE/);
  assert.match(source, /name="versionMode"/);
  assert.match(source, /name="versionParserProfile"/);
  assert.match(source, /name="versionChunkProfile"/);
  assert.match(source, /Idempotency-Key/);
  assert.match(source, /setTimeout\(\(\) => void poll\(\), 2500\)/);
  assert.match(source, /version\.content_sha256/);
  assert.match(source, /version\.activated_at/);
  assert.match(source, /versions\/\$\{versionId\}\/rollback/);
  assert.match(client, /idempotencyKey\?: string/);
  assert.match(client, /options\.idempotencyKey\.trim\(\)/);
});

test("forwards explicit Direct Tenant/User IDs without creating a JWT", async () => {
  const [proxy, client] = await Promise.all([
    readFile(new URL("../app/api/gateway/[...path]/route.ts", import.meta.url), "utf8"),
    readFile(new URL("../lib/client-api.ts", import.meta.url), "utf8"),
  ]);
  assert.match(proxy, /X-Tenant-ID/);
  assert.match(proxy, /X-User-ID/);
  assert.match(client, /DIRECT_TENANT_STORAGE_KEY/);
  assert.match(client, /DIRECT_USER_STORAGE_KEY/);
  assert.match(client, /headers\.set\("X-Tenant-ID"/);
  assert.match(client, /headers\.set\("X-User-ID"/);
  assert.doesNotMatch(`${proxy}\n${client}`, /issueGatewayToken|TEST_JWT_PRIVATE_KEY|Authorization/);
});

test("keeps the knowledge form reference across the asynchronous create request", async () => {
  const source = await readFile(
    new URL("../components/console/knowledge-panel.tsx", import.meta.url),
    "utf8",
  );
  assert.match(source, /const formElement = event\.currentTarget;/);
  assert.match(source, /formElement\.reset\(\);/);
  assert.doesNotMatch(source, /event\.currentTarget\.reset\(\)/);
});

test("guides text fixture uploads to the compatible parser profile", async () => {
  const [documents, consoleSource, guide] = await Promise.all([
    readFile(new URL("../components/console/documents-panel.tsx", import.meta.url), "utf8"),
    readFile(new URL("../components/console/api-console-panel.tsx", import.meta.url), "utf8"),
    readFile(new URL("../MANUAL_ACCEPTANCE_TEST.md", import.meta.url), "utf8"),
  ]);
  assert.match(documents, /\.txt Fixture 必须使用 office-default@1/);
  assert.match(consoleSource, /parser_profile: "office-default@1"/);
  assert.match(guide, /RAG-KB-004/);
});

test("does not present an unavailable knowledge document count as zero", async () => {
  const source = await readFile(
    new URL("../components/console/knowledge-panel.tsx", import.meta.url),
    "utf8",
  );
  assert.match(source, /document_count == null \? "文档数未统计"/);
  assert.doesNotMatch(source, /document_count \?\? 0/);
});

test("keeps upload resource IDs and response visible while refreshing documents", async () => {
  const source = await readFile(
    new URL("../components/console/documents-panel.tsx", import.meta.url),
    "utf8",
  );
  assert.match(source, /setDocumentId\(patch\.documentId\)/);
  assert.match(source, /setJobId\(patch\.jobId\)/);
  assert.match(source, /await list\(false\)/);
});

test("allows supported multipart uploads through the local proxy", async () => {
  const [config, documents] = await Promise.all([
    readFile(new URL("../next.config.ts", import.meta.url), "utf8"),
    readFile(new URL("../components/console/documents-panel.tsx", import.meta.url), "utf8"),
  ]);
  assert.match(config, /bodySizeLimit: "101mb"/);
  assert.match(documents, /MAX_UPLOAD_FILE_BYTES = 100 \* 1024 \* 1024/);
  assert.match(documents, /LOCAL-UPLOAD-001/);
  assert.match(documents, /单文件上限 100 MiB/);
});

test("shows the original and rewritten query for REST and SSE answers", async () => {
  const source = await readFile(
    new URL("../components/console/answers-panel.tsx", import.meta.url),
    "utf8",
  );
  assert.match(source, /title="Query Rewrite"/);
  assert.match(source, /原始问题/);
  assert.match(source, /实际检索问题/);
  assert.match(source, /中文问题会转成英文检索问题/);
  assert.match(source, /response\.data\.effective_query/);
  assert.match(source, /eventName === "retrieval\.completed"/);
});

test("sends and explains the selected answer language", async () => {
  const source = await readFile(
    new URL("../components/console/answers-panel.tsx", import.meta.url),
    "utf8",
  );
  assert.match(source, /response_language: String\(form\.get\("responseLanguage"\)\)/);
  assert.match(source, /<option value="zh-CN">简体中文<\/option>/);
  assert.match(source, /<option value="en-US">English<\/option>/);
  assert.match(source, /仅控制最终答案和引用修复语言/);
});

test("shows every retrieval route that contributed to a candidate", async () => {
  // 开发控制台必须读取 Gateway 新增的完整路线数组，不能继续只依赖会被压缩成单值
  // 的 sidecar_route；这样人工验收时才能区分向量、BM25、摘要和 Parent 是否都生效。
  const source = await readFile(
    new URL("../components/console/retrieval-panel.tsx", import.meta.url),
    "utf8",
  );
  assert.match(source, /gateway_retrieval_routes\?: string\[\]/);
  assert.match(source, /路线：/);
  assert.match(source, /gateway_retrieval_routes\.join\(" · "\)/);
});
