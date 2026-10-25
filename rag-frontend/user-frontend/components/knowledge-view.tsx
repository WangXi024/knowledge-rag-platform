"use client";

/**
 * 共享知识库浏览页：分页展示当前租户共享库与系统共享库，并允许只读查看文档和解析
 * 切块。页面不包含创建、上传、删除、重试或管理配置入口。
 */

import {
  AlertCircle,
  ArrowLeft,
  Building2,
  ChevronLeft,
  ChevronRight,
  FileSpreadsheet,
  FileText,
  Globe2,
  Layers3,
  LibraryBig,
  RefreshCw,
  X,
} from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import type {
  PageMeta,
  UserDocumentChunk,
  UserKnowledgeBase,
  UserKnowledgeDocument,
} from "@/lib/ui-types";

type ScopeFilter = "ALL" | "TENANT" | "SYSTEM";
type KnowledgeCounts = { all: number; tenant: number; system: number };

const EMPTY_PAGE: PageMeta = { page: 1, pageSize: 20, total: 0 };
const EMPTY_COUNTS: KnowledgeCounts = { all: 0, tenant: 0, system: 0 };

async function responseData<T>(response: Response): Promise<T> {
  /** 401 表示联调身份 Cookie 已失效；其余失败只展示 BFF 的安全产品文案。 */
  if (response.status === 401) {
    window.location.href = "/login";
    throw new Error("身份信息已失效");
  }
  const payload = await response.json() as T & { error?: string };
  if (!response.ok) throw new Error(payload.error || "暂时无法读取知识库");
  return payload;
}

function knowledgeIcon(scopeType: UserKnowledgeBase["scopeType"]) {
  /** 归属图标只依赖服务端 scope_type，不根据名称猜测。 */
  return scopeType === "SYSTEM" ? <Globe2 size={24} /> : <Building2 size={24} />;
}

function fileIcon(filename: string) {
  /** 扩展名仅用于视觉提示，不参与文档格式或权限判断。 */
  const extension = filename.split(".").pop()?.toLowerCase();
  if (["xls", "xlsx"].includes(extension || "")) return <FileSpreadsheet size={19} />;
  return <FileText size={19} />;
}

function statusLabel(status: string): string {
  /** 未知新状态原样显示，避免把未来状态错误标成“可用”。 */
  const labels: Record<string, string> = {
    ACTIVE: "可使用",
    READY: "可查看",
    RECEIVED: "已接收",
    UPLOADING: "上传中",
    UPLOADED: "等待解析",
    QUEUED: "排队中",
    PARSING: "解析中",
    INDEXING: "建立索引",
    RETRYING: "重试中",
    FAILED: "处理失败",
    DELETING: "删除中",
    DELETE_FAILED: "删除失败",
  };
  return labels[status] || status;
}

function formatSize(value: number): string {
  if (value >= 1024 * 1024) return `${(value / (1024 * 1024)).toFixed(1)} MB`;
  return `${Math.max(1, Math.ceil(value / 1024))} KB`;
}

function pageLabel(pages: number[]): string {
  if (!pages.length) return "未标注来源页";
  if (pages.length === 1) return `第 ${pages[0]} 页`;
  return `第 ${pages[0]}–${pages[pages.length - 1]} 页`;
}

function scopeLabel(scopeType: UserKnowledgeBase["scopeType"], tenantId: string): string {
  return scopeType === "SYSTEM" ? "系统共享" : `租户共享 · ${tenantId}`;
}

export function KnowledgeView({ tenantId }: { tenantId: string }) {
  const [scope, setScope] = useState<ScopeFilter>("ALL");
  const [knowledgeBases, setKnowledgeBases] = useState<UserKnowledgeBase[]>([]);
  const [knowledgePage, setKnowledgePage] = useState<PageMeta>({ ...EMPTY_PAGE, pageSize: 12 });
  const [knowledgeCounts, setKnowledgeCounts] = useState<KnowledgeCounts>(EMPTY_COUNTS);
  const [selectedKnowledgeBaseId, setSelectedKnowledgeBaseId] = useState("");
  const [documents, setDocuments] = useState<UserKnowledgeDocument[]>([]);
  const [documentPage, setDocumentPage] = useState<PageMeta>(EMPTY_PAGE);
  const [selectedDocument, setSelectedDocument] = useState<UserKnowledgeDocument | null>(null);
  const [chunks, setChunks] = useState<UserDocumentChunk[]>([]);
  const [chunkPage, setChunkPage] = useState<PageMeta>(EMPTY_PAGE);
  const [loadingKnowledge, setLoadingKnowledge] = useState(true);
  const [loadingDocuments, setLoadingDocuments] = useState(false);
  const [loadingChunks, setLoadingChunks] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [chunkError, setChunkError] = useState<string | null>(null);

  const loadKnowledgeBases = useCallback(async (nextScope: ScopeFilter, page = 1) => {
    /** 每次只取 12 个卡片；分类总数由 BFF 单独统计，因此大量知识库也能准确分页。 */
    setLoadingKnowledge(true);
    setError(null);
    try {
      const payload = await responseData<{
        data: UserKnowledgeBase[];
        meta: PageMeta;
        counts: KnowledgeCounts;
      }>(await fetch(
        `/api/knowledge-bases?scope=${nextScope}&page=${page}&page_size=12`,
        { cache: "no-store" },
      ));
      setKnowledgeBases(payload.data);
      setKnowledgePage(payload.meta);
      setKnowledgeCounts(payload.counts);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "知识库加载失败");
    } finally {
      setLoadingKnowledge(false);
    }
  }, []);

  const loadDocuments = useCallback(async (knowledgeBaseId: string, page = 1) => {
    /** 文档列表只读；切换分页不会触发解析、重试或任何写操作。 */
    setLoadingDocuments(true);
    setError(null);
    try {
      const payload = await responseData<{ data: UserKnowledgeDocument[]; meta: PageMeta }>(
        await fetch(
          `/api/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/documents?page=${page}&page_size=20`,
          { cache: "no-store" },
        ),
      );
      setDocuments(payload.data);
      setDocumentPage(payload.meta);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "文档加载失败");
    } finally {
      setLoadingDocuments(false);
    }
  }, []);

  const loadChunks = useCallback(async (document: UserKnowledgeDocument, page = 1) => {
    /** 只有用户主动打开 READY 文档时才读取正文，关闭抽屉后会清除已加载内容。 */
    setLoadingChunks(true);
    setChunkError(null);
    try {
      const payload = await responseData<{ data: UserDocumentChunk[]; meta: PageMeta }>(
        await fetch(
          `/api/documents/${encodeURIComponent(document.id)}/chunks?page=${page}&page_size=20`,
          { cache: "no-store" },
        ),
      );
      setChunks(payload.data);
      setChunkPage(payload.meta);
    } catch (failure) {
      setChunkError(failure instanceof Error ? failure.message : "内容片段加载失败");
    } finally {
      setLoadingChunks(false);
    }
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(() => void loadKnowledgeBases(scope, 1), 0);
    return () => window.clearTimeout(timer);
  }, [loadKnowledgeBases, scope]);

  function openKnowledgeBase(item: UserKnowledgeBase) {
    setSelectedKnowledgeBaseId(item.id);
    setDocuments([]);
    setDocumentPage(EMPTY_PAGE);
    setSelectedDocument(null);
    setChunks([]);
    void loadDocuments(item.id, 1);
  }

  function leaveKnowledgeBase() {
    setSelectedKnowledgeBaseId("");
    setDocuments([]);
    setDocumentPage(EMPTY_PAGE);
    setSelectedDocument(null);
    setChunks([]);
    setError(null);
  }

  function openChunks(document: UserKnowledgeDocument) {
    if (document.status !== "READY" || document.chunks < 1) return;
    setSelectedDocument(document);
    setChunks([]);
    setChunkPage(EMPTY_PAGE);
    void loadChunks(document, 1);
  }

  function closeChunks() {
    setSelectedDocument(null);
    setChunks([]);
    setChunkPage(EMPTY_PAGE);
    setChunkError(null);
  }

  const selectedKnowledgeBase = knowledgeBases.find((item) => item.id === selectedKnowledgeBaseId);
  const knowledgePages = Math.max(1, Math.ceil(knowledgePage.total / knowledgePage.pageSize));
  const documentPages = Math.max(1, Math.ceil(documentPage.total / documentPage.pageSize));
  const chunkPages = Math.max(1, Math.ceil(chunkPage.total / chunkPage.pageSize));
  const filters: Array<{ value: ScopeFilter; label: string; count: number }> = [
    { value: "ALL", label: "全部", count: knowledgeCounts.all },
    { value: "TENANT", label: "租户共享", count: knowledgeCounts.tenant },
    { value: "SYSTEM", label: "系统共享", count: knowledgeCounts.system },
  ];

  return (
    <div className="page-content knowledge-page">
      <header className="page-title-row">
        <div>
          <span className="eyebrow">SHARED KNOWLEDGE BASES</span>
          <h1>{selectedKnowledgeBase?.name || "共享知识库"}</h1>
          <p>{selectedKnowledgeBase ? "只读查看这个知识库中的文档和解析内容。" : "当前租户共享知识与系统共享知识统一展示，并明确标注归属。"}</p>
        </div>
        <button
          className="secondary-action"
          onClick={() => selectedKnowledgeBaseId
            ? void loadDocuments(selectedKnowledgeBaseId, documentPage.page)
            : void loadKnowledgeBases(scope, knowledgePage.page)}
          disabled={loadingKnowledge || loadingDocuments}
        >
          <RefreshCw size={16} className={loadingKnowledge || loadingDocuments ? "spin" : ""} /> 刷新
        </button>
      </header>

      {error ? <div className="notice error"><AlertCircle size={17} />{error}</div> : null}

      {!selectedKnowledgeBase ? (
        <>
          <div className="knowledge-filters" aria-label="知识库归属筛选">
            {filters.map((filter) => (
              <button
                key={filter.value}
                className={scope === filter.value ? "active" : ""}
                aria-pressed={scope === filter.value}
                onClick={() => setScope(filter.value)}
              ><span>{filter.label}</span><strong>{filter.count}</strong></button>
            ))}
          </div>
          <section className="knowledge-grid" aria-busy={loadingKnowledge}>
            {knowledgeBases.map((item) => (
              <button className="knowledge-card" key={item.id} onClick={() => openKnowledgeBase(item)}>
                <span className={`knowledge-card-icon knowledge-${item.scopeType.toLowerCase()}`}>{knowledgeIcon(item.scopeType)}</span>
                <span className="knowledge-card-copy">
                  <span className={`scope-badge scope-${item.scopeType.toLowerCase()}`}>{scopeLabel(item.scopeType, tenantId)}</span>
                  <strong>{item.name}</strong>
                  <small>{item.description || (item.scopeType === "SYSTEM" ? "平台统一维护的系统知识" : "当前企业租户共享知识")}</small>
                </span>
                <span className="knowledge-card-count"><strong>{item.documentCount}</strong><small>份文档</small></span>
                <ChevronRight size={19} />
              </button>
            ))}
            {!loadingKnowledge && knowledgeBases.length === 0 ? (
              <div className="empty-state knowledge-empty">
                <LibraryBig size={34} />
                <h3>当前分类还没有共享知识库</h3>
                <p>知识库由平台统一维护，请稍后刷新查看。</p>
              </div>
            ) : null}
            {loadingKnowledge && knowledgeBases.length === 0 ? (
              <div className="knowledge-loading"><RefreshCw className="spin" size={21} />正在加载共享知识库…</div>
            ) : null}
          </section>
          {knowledgePage.total > knowledgePage.pageSize ? (
            <div className="knowledge-pagination page-controls">
              <button aria-label="上一页知识库" disabled={loadingKnowledge || knowledgePage.page <= 1} onClick={() => void loadKnowledgeBases(scope, knowledgePage.page - 1)}><ChevronLeft size={17} /></button>
              <span>第 {knowledgePage.page} / {knowledgePages} 页</span>
              <button aria-label="下一页知识库" disabled={loadingKnowledge || knowledgePage.page >= knowledgePages} onClick={() => void loadKnowledgeBases(scope, knowledgePage.page + 1)}><ChevronRight size={17} /></button>
            </div>
          ) : null}
        </>
      ) : (
        <>
          <div className="knowledge-detail-context">
            <button className="knowledge-back" onClick={leaveKnowledgeBase}><ArrowLeft size={16} /> 返回全部知识库</button>
            <span className={`scope-badge scope-${selectedKnowledgeBase.scopeType.toLowerCase()}`}>{scopeLabel(selectedKnowledgeBase.scopeType, tenantId)}</span>
          </div>
          <section className="knowledge-documents-card" aria-busy={loadingDocuments}>
            <div className="list-toolbar">
              <div><h2>知识库文档</h2><p>{documentPage.total} 份文档，点击“查看内容”浏览解析切块</p></div>
              {documentPage.total > documentPage.pageSize ? (
                <div className="page-controls">
                  <button aria-label="上一页文档" disabled={loadingDocuments || documentPage.page <= 1} onClick={() => void loadDocuments(selectedKnowledgeBase.id, documentPage.page - 1)}><ChevronLeft size={17} /></button>
                  <span>{documentPage.page} / {documentPages}</span>
                  <button aria-label="下一页文档" disabled={loadingDocuments || documentPage.page >= documentPages} onClick={() => void loadDocuments(selectedKnowledgeBase.id, documentPage.page + 1)}><ChevronRight size={17} /></button>
                </div>
              ) : null}
            </div>
            {documents.length ? (
              <div className="knowledge-document-list">
                {documents.map((document) => (
                  <article className="knowledge-document-row" key={document.id}>
                    <span className="file-icon">{fileIcon(document.filename)}</span>
                    <div className="file-main">
                      <strong>{document.filename}</strong>
                      <span>{formatSize(document.size)} · {new Date(document.createdAt).toLocaleString("zh-CN")}</span>
                      {document.error ? <small className="file-error">{document.error}</small> : null}
                    </div>
                    <div className="file-result"><span>{document.chunks || "—"}</span><small>内容片段</small></div>
                    <span className={`status-pill status-${document.status.toLowerCase()}`}>{statusLabel(document.status)}</span>
                    <button className="chunk-open" disabled={document.status !== "READY" || document.chunks < 1} onClick={() => openChunks(document)}><Layers3 size={15} /> {document.status === "READY" && document.chunks > 0 ? "查看内容" : "内容未就绪"}</button>
                  </article>
                ))}
              </div>
            ) : !loadingDocuments ? (
              <div className="empty-state"><FileText size={32} /><h3>这个知识库还没有文档</h3><p>共享内容由平台统一维护。</p></div>
            ) : (
              <div className="knowledge-loading"><RefreshCw className="spin" size={21} />正在加载文档…</div>
            )}
          </section>
        </>
      )}

      {selectedDocument ? (
        <>
          <button className="knowledge-drawer-backdrop" aria-label="关闭内容片段" onClick={closeChunks} />
          <aside className="knowledge-drawer" role="dialog" aria-modal="true" aria-labelledby="chunk-drawer-title">
            <header>
              <div><span>文档内容</span><h2 id="chunk-drawer-title">{selectedDocument.filename}</h2><p>{chunkPage.total || selectedDocument.chunks} 个内容片段</p></div>
              <button aria-label="关闭" onClick={closeChunks}><X size={19} /></button>
            </header>
            <div className="chunk-list">
              {chunkError ? <div className="notice error"><AlertCircle size={17} />{chunkError}</div> : null}
              {chunks.map((chunk, index) => (
                <article className="chunk-card" key={chunk.id}>
                  <div><strong>片段 {(chunkPage.page - 1) * chunkPage.pageSize + index + 1}</strong><span>{pageLabel(chunk.pages)}</span></div>
                  <p>{chunk.content}</p>
                </article>
              ))}
              {loadingChunks ? <div className="knowledge-loading"><RefreshCw className="spin" size={20} />正在读取内容…</div> : null}
              {!loadingChunks && !chunkError && chunks.length === 0 ? <div className="empty-state"><Layers3 size={30} /><h3>没有可显示的内容片段</h3></div> : null}
            </div>
            {chunkPage.total > chunkPage.pageSize ? (
              <footer className="chunk-pagination">
                <button disabled={loadingChunks || chunkPage.page <= 1} onClick={() => void loadChunks(selectedDocument, chunkPage.page - 1)}><ChevronLeft size={16} /> 上一页</button>
                <span>{chunkPage.page} / {chunkPages}</span>
                <button disabled={loadingChunks || chunkPage.page >= chunkPages} onClick={() => void loadChunks(selectedDocument, chunkPage.page + 1)}>下一页 <ChevronRight size={16} /></button>
              </footer>
            ) : null}
          </aside>
        </>
      ) : null}
    </div>
  );
}
