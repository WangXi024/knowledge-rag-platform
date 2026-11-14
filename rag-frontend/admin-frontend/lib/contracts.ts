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

/** 原生图谱只提供受控知识库配置，模型凭据不属于浏览器契约。 */
export interface GraphConfiguration {
  enabled: boolean;
  profile_ref: 'native-graph-light@1' | 'native-graph-with-summaries@1';
  auto_build: boolean;
  retrieval_enabled: boolean;
}

export type GraphStatus = 'DISABLED' | 'NOT_BUILT' | 'QUEUED' | 'BUILDING' | 'READY' | 'STALE' | 'FAILED' | 'CANCELLING';

/** 保留内容版本、构图代数和任务诊断；202 接收不能被解释为图谱就绪。 */
export interface KnowledgeGraphState {
  knowledge_base_id: string;
  configuration: GraphConfiguration;
  status: GraphStatus;
  target_content_revision: number;
  built_for_revision: number | null;
  graph_generation: number;
  configuration_version: number;
  job_id: string | null;
  last_error: { code: string; message: string; retryable?: boolean } | null;
  queued_at: string | null;
  started_at: string | null;
  completed_at: string | null;
  updated_at: string;
}

/** 来源仅由 Gateway 映射到当前活动文档；摘要与节点描述不冒充原文。 */
export interface GraphSource {
  knowledge_base_id: string;
  document_id: string;
  document_name: string;
  document_revision: number;
  active_version_id: string | null;
  chunk_id: string;
  quote: string;
  positions: SourcePosition[];
}

export interface AggregateEvidence {
  aggregate_id: string;
  label: string;
  kind: 'community_aggregate';
  derived: true;
  origin: 'gateway_generated';
  knowledge_base_id: string;
  community_id: string;
  title: string;
  summary: string;
  graph_generation: number;
  content_revision: number;
  configuration_version: number;
  graph_profile_ref: string;
  generator_model_ref: string;
  strategy_ref: string;
  sources: GraphSource[];
}

export interface GraphNode {
  id: string;
  name: string;
  entity_type: string;
  description: string;
  generated: true;
  sources: GraphSource[];
  source_status: 'available' | 'unavailable';
}

export interface GraphEdge {
  id: string;
  source: string;
  target: string;
  description: string;
  generated: true;
  sources: GraphSource[];
  source_status: 'available' | 'unavailable';
}

export interface GraphView {
  knowledge_base_id: string;
  status: GraphStatus;
  graph_generation: number;
  content_revision: number;
  profile_ref: string;
  nodes: GraphNode[];
  edges: GraphEdge[];
  truncated: boolean;
  warnings: string[];
}

export interface GraphCommunities {
  knowledge_base_id: string;
  status: GraphStatus;
  graph_generation: number;
  content_revision: number;
  communities: AggregateEvidence[];
  warnings: string[];
}
