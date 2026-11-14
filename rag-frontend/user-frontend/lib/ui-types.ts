/** 用户站共享公开 DTO；当前只携带联调身份字段，不包含密码、JWT 或内部主体摘要。 */
export type PublicUser = { tenantId: string; userId: string; displayName: string };

export type UserFile = {
  id: string;
  filename: string;
  mimeType: string;
  size: number;
  status: string;
  progress: number;
  chunks: number;
  tokens: number;
  createdAt: string;
  updatedAt: string;
  error: string | null;
};

export type UserKnowledgeBase = {
  id: string;
  scopeType: "TENANT" | "SYSTEM";
  name: string;
  description: string | null;
  status: string;
  documentCount: number;
  createdAt: string;
  updatedAt: string;
};

export type UserKnowledgeDocument = UserFile & {
  knowledgeBaseId: string;
};

export type UserDocumentChunk = {
  id: string;
  content: string;
  pages: number[];
};

export type PageMeta = {
  page: number;
  pageSize: number;
  total: number;
};

export type Citation = {
  citation_id: string;
  label: string;
  document_id: string;
  document_name: string;
  quote: string;
  page_start?: number | null;
  page_end?: number | null;
  score?: number | null;
};

export type Candidate = {
  chunkId: string;
  documentId: string;
  documentName: string;
  headingPath: string[];
  content: string;
  page: number | null;
  score: number;
  vectorScore: number | null;
  termScore: number | null;
  /** 真实 Cross-Encoder 返回的相关度；为空表示本候选未完成重排，界面不得声称已重排。 */
  rerankScore: number | null;
  citationLabel?: string;
};

export type CacheType = "none" | "exact" | "semantic";

export type EvidenceMode = "original" | "community";

/** 聚合来源仍是当前活动版本的真实原文；不把生成摘要塞进 Citation.quote。 */
export type AggregateSource = {
  knowledge_base_id: string;
  document_id: string;
  document_name: string;
  document_revision: number;
  active_version_id: string | null;
  chunk_id: string;
  quote: string;
  positions: Array<{ page: number | null; bbox?: [number, number, number, number] | null }>;
};

export type AggregateEvidence = {
  aggregate_id: string;
  label: string;
  kind: "community_aggregate";
  derived: true;
  origin: "gateway_generated";
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
  sources: AggregateSource[];
};

export type EvidenceBundle = {
  candidates: Candidate[];
  citations: Citation[];
  durationMs: number | null;
  total: number;
  cacheType: CacheType | null;
  aggregateEvidence?: AggregateEvidence[];
  evidenceMode?: EvidenceMode;
};

export type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  evidence?: EvidenceBundle;
  requestId?: string | null;
  streaming?: boolean;
  evidenceMode?: EvidenceMode;
};

export type ConversationSummary = { id: string; title: string; createdAt: number; updatedAt: number };
