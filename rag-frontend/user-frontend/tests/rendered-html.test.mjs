// 源码级用户面回归测试：锁定 Direct Tenant/User、共享知识标签、只读边界与问答能力。

import assert from "node:assert/strict";
import { access, readFile } from "node:fs/promises";
import test from "node:test";

async function source(path) {
  return readFile(new URL(path, import.meta.url), "utf8");
}

async function isMissing(path) {
  try {
    await access(new URL(path, import.meta.url));
    return false;
  } catch {
    return true;
  }
}

test("shows only shared knowledge browsing and chat in the end-user shell", async () => {
  const app = await source("../components/user-app.tsx");
  assert.match(app, /共享知识库/);
  assert.match(app, /KnowledgeView/);
  assert.match(app, /智能问答/);
  assert.match(app, /Tenant: \{user\?\.tenantId\}/);
  assert.doesNotMatch(app, /我的文件|FilesView|知识库管理|Parser Profile|Job ID/);
});

test("uses Tenant ID and User ID as an explicitly unverified development identity", async () => {
  const [form, login, auth, me] = await Promise.all([
    source("../components/auth-form.tsx"),
    source("../app/api/auth/login/route.ts"),
    source("../lib/auth.ts"),
    source("../app/api/auth/me/route.ts"),
  ]);
  assert.match(form, /name="tenantId"/);
  assert.match(form, /name="userId"/);
  assert.match(form, /开发联调身份/);
  assert.match(form, /不会验证你是否拥有所填身份/);
  assert.doesNotMatch(form, /name="email"|name="password"|\/register/);
  assert.match(login, /createDirectIdentity\(body\.tenantId, body\.userId\)/);
  assert.match(auth, /lazytt_direct_identity/);
  assert.match(auth, /HttpOnly; SameSite=Lax/);
  assert.doesNotMatch(auth, /PBKDF2|CREATE TABLE IF NOT EXISTS users|password_hash/);
  assert.match(me, /tenantId: user\.tenantId/);
  assert.match(me, /userId: user\.userId/);
  assert.equal(await isMissing("../app/register/page.tsx"), true);
  assert.equal(await isMissing("../app/api/auth/register/route.ts"), true);
});

test("forwards the selected Tenant/User identity only from the server cookie", async () => {
  const [gateway, runtime, exampleEnvironment] = await Promise.all([
    source("../lib/gateway.ts"),
    source("../lib/runtime-env.ts"),
    source("../.env.example"),
  ]);
  assert.match(gateway, /headers\.set\("X-Tenant-ID", user\.tenantId\)/);
  assert.match(gateway, /headers\.set\("X-User-ID", user\.userId\)/);
  assert.match(gateway, /headers\.delete\("Authorization"\)/);
  assert.doesNotMatch(`${gateway}\n${runtime}\n${exampleEnvironment}`, /TEST_JWT_PRIVATE_KEY|AUTH_AUDIENCE/);
});

test("lists many shared knowledge bases with accurate scope filters and counts", async () => {
  const [view, route, types, css] = await Promise.all([
    source("../components/knowledge-view.tsx"),
    source("../app/api/knowledge-bases/route.ts"),
    source("../lib/ui-types.ts"),
    source("../app/globals.css"),
  ]);
  assert.match(types, /scopeType: "TENANT" \| "SYSTEM"/);
  assert.match(route, /scope_type/);
  assert.match(route, /tenantCount\.meta\.total \+ systemCount\.meta\.total/);
  assert.match(route, /page_size/);
  assert.doesNotMatch(route, /\/v1\/management|ensureUserWorkspace|workspaceKnowledgeBases/);
  assert.match(view, /全部/);
  assert.match(view, /租户共享/);
  assert.match(view, /系统共享/);
  assert.match(view, /租户共享 · \$\{tenantId\}/);
  assert.match(view, /knowledgePages/);
  assert.match(css, /\.scope-badge\.scope-tenant/);
  assert.match(css, /\.scope-badge\.scope-system/);
});

test("browses documents and sanitized chunks through read-only public RAG routes", async () => {
  const [documents, chunks] = await Promise.all([
    source("../app/api/knowledge-bases/[knowledgeBaseId]/documents/route.ts"),
    source("../app/api/documents/[documentId]/chunks/route.ts"),
  ]);
  assert.match(documents, /`\/v1\/knowledge-bases\/\$\{encodeURIComponent\(knowledgeBaseId\)\}\/documents/);
  assert.match(chunks, /`\/v1\/documents\/\$\{encodeURIComponent\(documentId\)\}`/);
  assert.match(chunks, /content: item\.content/);
  assert.match(chunks, /pages: Array\.from/);
  assert.doesNotMatch(`${documents}\n${chunks}`, /\/v1\/management|ensureUserWorkspace|knowledgeBaseIds/);
});

test("removes all user-side upload, retry, delete, and personal workspace endpoints", async () => {
  const removed = await Promise.all([
    isMissing("../components/files-view.tsx"),
    isMissing("../lib/workspace.ts"),
    isMissing("../app/api/files/route.ts"),
    isMissing("../app/api/files/[documentId]/route.ts"),
    isMissing("../app/api/files/[documentId]/retry/route.ts"),
    isMissing("../app/api/workspace/route.ts"),
  ]);
  assert.deepEqual(removed, [true, true, true, true, true, true]);
});

test("stores history in new Tenant/User principal tables without mutating old account tables", async () => {
  const history = await source("../lib/history.ts");
  assert.match(history, /CREATE TABLE IF NOT EXISTS direct_conversations/);
  assert.match(history, /principal_id TEXT NOT NULL/);
  assert.match(history, /tenant_id TEXT NOT NULL/);
  assert.match(history, /user_id TEXT NOT NULL/);
  assert.match(history, /direct_conversation_messages/);
  assert.match(history, /user\.tenantId, user\.userId/);
  assert.doesNotMatch(history, /REFERENCES users/);
  assert.doesNotMatch(history, /DROP TABLE|DELETE FROM users|DELETE FROM conversations\b/);
});

test("auto-searches all currently accessible tenant and system knowledge bases", async () => {
  const [retrieve, stream] = await Promise.all([
    source("../app/api/chat/retrieve/route.ts"),
    source("../app/api/chat/stream/route.ts"),
  ]);
  assert.match(retrieve, /knowledge_base_ids: \[\]/);
  assert.match(stream, /knowledge_base_ids: \[\]/);
  assert.doesNotMatch(`${retrieve}\n${stream}`, /workspace|knowledgeBaseIds/);
});

test("renders safe Markdown, evidence metrics, cache provenance, and clickable citations", async () => {
  const [chat, css, packageJson] = await Promise.all([
    source("../components/chat-view.tsx"),
    source("../app/globals.css"),
    source("../package.json"),
  ]);
  assert.match(chat, /from "react-markdown"/);
  assert.match(chat, /from "remark-gfm"/);
  assert.match(chat, /skipHtml/);
  assert.match(chat, /citation-chip/);
  assert.match(chat, /融合分/);
  assert.match(chat, /关键词相似度/);
  assert.match(chat, /向量相似度/);
  assert.match(chat, /命中精准缓存/);
  assert.match(chat, /命中语义缓存/);
  assert.match(css, /\.answer-copy table/);
  assert.match(css, /\.answer-copy pre/);
  assert.match(packageJson, /"react-markdown": "10\.1\.0"/);
});

test("keeps streaming answers isolated per conversation with a two-run limit", async () => {
  const chat = await source("../components/chat-view.tsx");
  assert.match(chat, /MAX_PARALLEL_ANSWERS = 2/);
  assert.match(chat, /Map<string, ConversationRun>/);
  assert.match(chat, /updateConversationView\(runKey/);
  assert.match(chat, /runsRef\.current\.size >= MAX_PARALLEL_ANSWERS/);
  assert.match(chat, /stopConversation\(activeConversationKey\)/);
  assert.doesNotMatch(chat, /if \(busy\) return|disabled=\{busy\}/);
});
