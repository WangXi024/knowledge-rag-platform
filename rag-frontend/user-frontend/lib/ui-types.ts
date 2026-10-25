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

export type EvidenceBundle = {
  candidates: Candidate[];
  citations: Citation[];
  durationMs: number | null;
  total: number;
  cacheType: CacheType | null;
};

export type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  evidence?: EvidenceBundle;
  requestId?: string | null;
  streaming?: boolean;
};

export type ConversationSummary = { id: string; title: string; createdAt: number; updatedAt: number };
