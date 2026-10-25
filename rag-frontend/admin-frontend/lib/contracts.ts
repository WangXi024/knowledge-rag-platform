/**
 * 管理端与 RAG Gateway 之间使用的最小契约集合。
 * 字段名称保持 OpenAPI 原样，避免在 UI 层重复做不必要的映射。
 */

export type AdminRole = 'TENANT_ADMIN' | 'SYSTEM_ADMIN';
export type ScopeType = 'TENANT' | 'SYSTEM';

export interface AdminSession {
  role: AdminRole;
  tenantId: string;
  userId: string;
}

export interface ApiEnvelope<T> {
  request_id: string;
  data: T;
}

export interface PageEnvelope<T> extends ApiEnvelope<T[]> {
  meta: { page: number; page_size: number; total: number };
}

export interface KnowledgeBase {
  id: string;
  scope_type: ScopeType;
  external_id?: string | null;
  name: string;
  description?: string | null;
  status: string;
  revision: number;
  parser_profile: string;
  chunk_profile: string;
  embedding_profile: string;
  retrieval_profile: string;
  document_count: number;
  reindex_required?: boolean;
  created_at: string;
  updated_at: string;
}

export interface KnowledgeDocument {
  id: string;
  knowledge_base_id: string;
  external_id?: string | null;
  filename: string;
  mime_type: string;
  file_size_bytes: number;
  document_revision: number;
  parser_profile: string;
  chunk_profile: string;
  status: string;
  progress: number;
  chunk_count: number;
  token_count: number;
  last_error?: { code?: string; message?: string } | null;
  created_at: string;
  updated_at: string;
  indexed_at?: string | null;
}

/** RAGFlow 原文位置：一基页码；bbox 按左、右、上、下排列，单位为 72 DPI 页面坐标。 */
export interface SourcePosition {
  page: number | null;
  bbox?: [number, number, number, number] | null;
}

/** 管理端只读切块；缺失页码/坐标不能通过文本相似度伪造精确定位。 */
export interface DocumentChunk {
  id: string;
  document_id: string;
  content: string;
  positions: SourcePosition[];
}

export interface Capabilities {
  file_extensions: string[];
  max_file_bytes?: number | null;
  parser_profiles: string[];
  chunk_profiles: string[];
  retrieval_profiles: string[];
  features: Record<string, boolean>;
}

export interface AcceptedJob {
  job_id: string;
  status: string;
}

export interface AcceptedDocument extends AcceptedJob {
  document_id: string;
}

export interface JobState {
  id: string;
  type: string;
  status: string;
  progress: number;
  error?: { code?: string; message?: string } | null;
}
