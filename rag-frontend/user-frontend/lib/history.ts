/** 联调对话历史：按 Tenant ID + User ID 的主体摘要隔离，并维护会话标题和更新时间。 */
import type { User } from "./auth";
import { getRuntimeEnvironment } from "./runtime-env";

export type ConversationSummary = { id: string; title: string; createdAt: number; updatedAt: number };
export type ConversationMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  evidence: unknown;
  requestId: string | null;
  createdAt: number;
};

async function ensureHistorySchema(): Promise<void> {
  /**
   * 新表不依赖已经停用的 users 账号表。旧 conversations/account 数据原样保留但不再
   * 读取；这样本次切换不会做破坏性删除，未来正式 Identity 接入时也能单独迁移。
   */
  const { DB } = getRuntimeEnvironment();
  await DB.batch([
    DB.prepare(`
      CREATE TABLE IF NOT EXISTS direct_conversations (
        id TEXT PRIMARY KEY NOT NULL,
        principal_id TEXT NOT NULL,
        tenant_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        title TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
      )
    `),
    DB.prepare(`
      CREATE TABLE IF NOT EXISTS direct_conversation_messages (
        id TEXT PRIMARY KEY NOT NULL,
        conversation_id TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
        content TEXT NOT NULL,
        evidence_json TEXT NOT NULL DEFAULT '[]',
        request_id TEXT,
        created_at INTEGER NOT NULL,
        FOREIGN KEY (conversation_id) REFERENCES direct_conversations(id) ON DELETE CASCADE
      )
    `),
    DB.prepare("CREATE INDEX IF NOT EXISTS direct_conversations_principal_idx ON direct_conversations(principal_id, updated_at DESC)"),
    DB.prepare("CREATE INDEX IF NOT EXISTS direct_messages_conversation_idx ON direct_conversation_messages(conversation_id, created_at)"),
  ]);
}

export async function listConversations(user: User): Promise<ConversationSummary[]> {
  await ensureHistorySchema();
  const result = await getRuntimeEnvironment().DB.prepare(`
      SELECT id, title, created_at, updated_at FROM direct_conversations
      WHERE principal_id = ? ORDER BY updated_at DESC LIMIT 50
    `).bind(user.id).all<Record<string, unknown>>();
  return (result.results ?? []).map((row: Record<string, unknown>) => ({
    id: String(row.id), title: String(row.title), createdAt: Number(row.created_at), updatedAt: Number(row.updated_at),
  }));
}

export async function getConversation(user: User, conversationId: string): Promise<{ conversation: ConversationSummary; messages: ConversationMessage[] } | null> {
  await ensureHistorySchema();
  const conversation = await getRuntimeEnvironment().DB.prepare(`
      SELECT id, title, created_at, updated_at FROM direct_conversations WHERE id = ? AND principal_id = ? LIMIT 1
    `).bind(conversationId, user.id).first<Record<string, unknown>>();
  if (!conversation) return null;
  const result = await getRuntimeEnvironment().DB.prepare(`
      SELECT id, role, content, evidence_json, request_id, created_at
      FROM direct_conversation_messages WHERE conversation_id = ? ORDER BY created_at ASC
    `).bind(conversationId).all<Record<string, unknown>>();
  return {
    conversation: {
      id: String(conversation.id), title: String(conversation.title),
      createdAt: Number(conversation.created_at), updatedAt: Number(conversation.updated_at),
    },
    messages: (result.results ?? []).map((row: Record<string, unknown>) => {
      let evidence: unknown = [];
      try { evidence = JSON.parse(String(row.evidence_json)) as unknown; } catch { evidence = []; }
      return {
        id: String(row.id), role: String(row.role) as "user" | "assistant", content: String(row.content),
        evidence, requestId: row.request_id ? String(row.request_id) : null, createdAt: Number(row.created_at),
      };
    }),
  };
}

export async function saveTurn(user: User, input: {
  conversationId?: string;
  question: string;
  answer: string;
  evidence: unknown;
  requestId?: string;
}): Promise<string> {
  await ensureHistorySchema();
  const { DB } = getRuntimeEnvironment();
  const conversationId = input.conversationId || crypto.randomUUID();
  const now = Date.now();
  const owned = await DB.prepare("SELECT id FROM direct_conversations WHERE id = ? AND principal_id = ? LIMIT 1")
    .bind(conversationId, user.id).first();
  if (!owned) {
    if (input.conversationId) throw new Error("CONVERSATION_NOT_FOUND");
    const title = input.question.replace(/\s+/gu, " ").trim().slice(0, 42) || "新对话";
    await DB.prepare("INSERT INTO direct_conversations (id, principal_id, tenant_id, user_id, title, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)")
      .bind(conversationId, user.id, user.tenantId, user.userId, title, now, now).run();
  }
  await DB.batch([
    DB.prepare("INSERT INTO direct_conversation_messages (id, conversation_id, role, content, evidence_json, request_id, created_at) VALUES (?, ?, 'user', ?, '[]', NULL, ?)")
      .bind(crypto.randomUUID(), conversationId, input.question, now),
    DB.prepare("INSERT INTO direct_conversation_messages (id, conversation_id, role, content, evidence_json, request_id, created_at) VALUES (?, ?, 'assistant', ?, ?, ?, ?)")
      .bind(crypto.randomUUID(), conversationId, input.answer, JSON.stringify(input.evidence), input.requestId || null, now + 1),
    DB.prepare("UPDATE direct_conversations SET updated_at = ? WHERE id = ? AND principal_id = ?").bind(now + 1, conversationId, user.id),
  ]);
  return conversationId;
}

export async function deleteConversation(user: User, conversationId: string): Promise<boolean> {
  await ensureHistorySchema();
  const { DB } = getRuntimeEnvironment();
  const owned = await DB.prepare("SELECT id FROM direct_conversations WHERE id = ? AND principal_id = ? LIMIT 1")
    .bind(conversationId, user.id).first();
  if (!owned) return false;
  await DB.batch([
    DB.prepare("DELETE FROM direct_conversation_messages WHERE conversation_id = ?").bind(conversationId),
    DB.prepare("DELETE FROM direct_conversations WHERE id = ? AND principal_id = ?").bind(conversationId, user.id),
  ]);
  return true;
}
