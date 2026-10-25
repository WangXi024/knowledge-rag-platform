'use client';

/**
 * 管理工作区：基于当前管理员固定范围，完成知识库 CRUD、多文件智能路由上传、
 * 文档状态查看、失败重试、重新解析和异步删除。页面只呈现真实 API 数据。
 */

import {
  BookOpen,
  Building2,
  CheckCircle2,
  File,
  FileStack,
  FolderOpen,
  Globe2,
  Layers3,
  LoaderCircle,
  Pencil,
  Plus,
  RefreshCw,
  RotateCcw,
  Search,
  Trash2,
  UploadCloud,
  X,
} from 'lucide-react';
import { FormEvent, ReactNode, useCallback, useEffect, useMemo, useState } from 'react';
import type {
  AcceptedDocument,
  AcceptedJob,
  AdminSession,
  ApiEnvelope,
  Capabilities,
  JobState,
  KnowledgeBase,
  KnowledgeDocument,
  PageEnvelope,
  ScopeType,
} from '@/lib/contracts';
import { errorMessage, GatewayError, gatewayRequest } from '@/lib/gateway-api';
import { DocumentChunks } from '@/components/document-chunks';

export type AdminSection = 'overview' | 'knowledge' | 'documents';
type Dialog = 'create' | 'edit' | 'upload' | 'reparse' | 'chunks' | null;

const EMPTY_CAPABILITIES: Capabilities = {
  file_extensions: [],
  parser_profiles: [],
  chunk_profiles: [],
  retrieval_profiles: [],
  features: {},
};
const TERMINAL_JOB_STATUSES = new Set(['SUCCEEDED', 'FAILED', 'CANCELLED']);
const TERMINAL_DOCUMENT_STATUSES = new Set(['READY', 'INDEXED', 'FAILED', 'CANCELLED', 'DELETED']);
const MAX_UPLOAD_FILE_BYTES = 100 * 1024 * 1024;

/**
 * 在异步删除、重试或重解析后轮询任务。达到时间上限只停止前端等待，
 * 不取消服务端任务，管理员仍可通过刷新查看最终结果。
 */
async function waitForJob(
  session: AdminSession,
  jobId: string,
  onProgress: (message: string) => void,
): Promise<JobState | null> {
  for (let attempt = 0; attempt < 30; attempt += 1) {
    const response = await gatewayRequest<ApiEnvelope<JobState>>(session, `v1/management/jobs/${jobId}`);
    const progress = Math.round((response.data.progress ?? 0) * 100);
    onProgress(`任务 ${response.data.status} · ${progress}%`);
    if (TERMINAL_JOB_STATUSES.has(response.data.status)) return response.data;
    await new Promise((resolve) => window.setTimeout(resolve, 1000));
  }
  return null;
}

/** 管理端主工作区；父组件只负责身份和导航，资源操作与上传弹窗的临时选择在此收敛。 */
export function AdminWorkspace({
  session,
  section,
  onSectionChange,
}: {
  session: AdminSession;
  section: AdminSection;
  onSectionChange: (section: AdminSection) => void;
}) {
  const scope: ScopeType = session.role === 'TENANT_ADMIN' ? 'TENANT' : 'SYSTEM';
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBase[]>([]);
  const [documents, setDocuments] = useState<KnowledgeDocument[]>([]);
  const [capabilities, setCapabilities] = useState<Capabilities>(EMPTY_CAPABILITIES);
  const [selectedKnowledgeBaseId, setSelectedKnowledgeBaseId] = useState('');
  const [selectedDocument, setSelectedDocument] = useState<KnowledgeDocument | null>(null);
  const [query, setQuery] = useState('');
  const [dialog, setDialog] = useState<Dialog>(null);
  const [busy, setBusy] = useState('');
  const [notice, setNotice] = useState<{ tone: 'success' | 'error' | 'info'; message: string } | null>(null);
  const [uploadMode, setUploadMode] = useState<'AUTO' | 'MANUAL' | 'VISION' | 'MINERU'>('AUTO');
  const [uploadProgress, setUploadProgress] = useState('');
  // 这里只保存名称和字节数元数据；实际文件仍由 input 持有，不为展示读取文件正文。
  const [selectedUploadFiles, setSelectedUploadFiles] = useState<{ name: string; size: number }[]>([]);

  const selectedKnowledgeBase = knowledgeBases.find((item) => item.id === selectedKnowledgeBaseId) ?? null;
  const filteredKnowledgeBases = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase('zh-CN');
    if (!normalized) return knowledgeBases;
    return knowledgeBases.filter((item) =>
      `${item.name} ${item.description ?? ''} ${item.external_id ?? ''}`.toLocaleLowerCase('zh-CN').includes(normalized),
    );
  }, [knowledgeBases, query]);

  const loadKnowledgeBases = useCallback(async (quiet = false) => {
    if (!quiet) setBusy('load-kb');
    try {
      const [knowledgeResponse, capabilitiesResponse] = await Promise.all([
        gatewayRequest<PageEnvelope<KnowledgeBase>>(
          session,
          `v1/management/knowledge-bases?page=1&page_size=100&scope_type=${scope}`,
        ),
        gatewayRequest<ApiEnvelope<Capabilities>>(session, 'v1/capabilities'),
      ]);
      // scope_type 查询是 UX 边界；仍在浏览器侧二次过滤，避免异常响应混入错误范围。
      const scopedItems = (knowledgeResponse.data ?? []).filter((item) => item.scope_type === scope);
      setKnowledgeBases(scopedItems);
      setCapabilities(capabilitiesResponse.data ?? EMPTY_CAPABILITIES);
      setSelectedKnowledgeBaseId((current) =>
        scopedItems.some((item) => item.id === current) ? current : scopedItems[0]?.id ?? '',
      );
      if (!quiet) setNotice(null);
    } catch (error) {
      setNotice({ tone: 'error', message: errorMessage(error) });
    } finally {
      if (!quiet) setBusy('');
    }
  }, [scope, session]);

  const loadDocuments = useCallback(async (quiet = false) => {
    if (!selectedKnowledgeBaseId) {
      setDocuments([]);
      return;
    }
    if (!quiet) setBusy('load-docs');
    try {
      const response = await gatewayRequest<PageEnvelope<KnowledgeDocument>>(
        session,
        `v1/management/knowledge-bases/${selectedKnowledgeBaseId}/documents?page=1&page_size=100`,
      );
      setDocuments(response.data ?? []);
    } catch (error) {
      setNotice({ tone: 'error', message: errorMessage(error) });
    } finally {
      if (!quiet) setBusy('');
    }
  }, [selectedKnowledgeBaseId, session]);

  useEffect(() => {
    const timeout = window.setTimeout(() => void loadKnowledgeBases(), 0);
    return () => window.clearTimeout(timeout);
  }, [loadKnowledgeBases]);

  useEffect(() => {
    const timeout = window.setTimeout(() => void loadDocuments(), 0);
    return () => window.clearTimeout(timeout);
  }, [loadDocuments]);

  useEffect(() => {
    if (!documents.some((item) => !TERMINAL_DOCUMENT_STATUSES.has(item.status))) return;
    const timeout = window.setTimeout(() => void loadDocuments(true), 2500);
    return () => window.clearTimeout(timeout);
  }, [documents, loadDocuments]);

  const defaultParser = chooseDefault(capabilities.parser_profiles, 'office-default@1', 'deepdoc-default@1');
  const defaultChunk = chooseDefault(capabilities.chunk_profiles, 'general-256@2', 'prose-384@1');
  const defaultRetrieval = chooseDefault(capabilities.retrieval_profiles, 'hybrid-default@2', 'hybrid-default@1');
  const documentTotal = knowledgeBases.reduce((total, item) => total + (item.document_count ?? 0), 0);
  const processingTotal = documents.filter((item) => !TERMINAL_DOCUMENT_STATUSES.has(item.status)).length;

  /** 创建范围由角色直接决定，不把 scope_type 暴露成可切换字段。 */
  async function createKnowledgeBase(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    setBusy('create');
    try {
      const response = await gatewayRequest<ApiEnvelope<KnowledgeBase>>(session, 'v1/management/knowledge-bases', {
        method: 'POST',
        idempotent: true,
        body: {
          scope_type: scope,
          external_id: String(form.get('externalId') || '').trim() || null,
          name: String(form.get('name') || '').trim(),
          description: String(form.get('description') || '').trim() || null,
          parser_profile: String(form.get('parserProfile')),
          chunk_profile: String(form.get('chunkProfile')),
          embedding_profile: 'embedding-openai-compatible@3',
          retrieval_profile: String(form.get('retrievalProfile')),
        },
      });
      setDialog(null);
      setNotice({ tone: 'success', message: `知识库“${response.data.name}”创建成功。` });
      await loadKnowledgeBases(true);
      setSelectedKnowledgeBaseId(response.data.id);
    } catch (error) {
      setNotice({ tone: 'error', message: errorMessage(error) });
    } finally {
      setBusy('');
    }
  }

  /** 更新知识库名称、描述和可变 Profile；Embedding 按后端契约保持不可编辑。 */
  async function updateKnowledgeBase(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selectedKnowledgeBase) return;
    const form = new FormData(event.currentTarget);
    setBusy('edit');
    try {
      await gatewayRequest(session, `v1/management/knowledge-bases/${selectedKnowledgeBase.id}`, {
        method: 'PATCH',
        body: {
          name: String(form.get('name') || '').trim(),
          description: String(form.get('description') || '').trim() || null,
          parser_profile: String(form.get('parserProfile')),
          chunk_profile: String(form.get('chunkProfile')),
          retrieval_profile: String(form.get('retrievalProfile')),
        },
      });
      setDialog(null);
      setNotice({ tone: 'success', message: '知识库设置已保存。' });
      await loadKnowledgeBases(true);
    } catch (error) {
      setNotice({ tone: 'error', message: errorMessage(error) });
    } finally {
      setBusy('');
    }
  }

  /**
   * 删除知识库前按文档数量确认，提交后等待异步 Job。
   * 若 Job 因目标已软删除而返回特定 404，仅在同一管理员范围确认目标不存在且列表刷新成功后展示完成；
   * 其他错误保留原提示，不重新发起删除或修改服务端状态。
   */
  async function deleteKnowledgeBase(item: KnowledgeBase) {
    const message = item.document_count > 0
      ? `“${item.name}”含 ${item.document_count} 个文档。确认强制删除知识库、文档和索引内容吗？`
      : `确认删除空知识库“${item.name}”吗？`;
    if (!window.confirm(message)) return;
    setBusy(`delete-kb-${item.id}`);
    try {
      const response = await gatewayRequest<ApiEnvelope<AcceptedJob>>(
        session,
        `v1/management/knowledge-bases/${item.id}?force=${item.document_count > 0}`,
        { method: 'DELETE', idempotent: true },
      );
      const result = await waitForJob(session, response.data.job_id, (value) =>
        setNotice({ tone: 'info', message: `正在删除“${item.name}”：${value}` }),
      );
      setNotice(result
        ? {
            tone: result.status !== 'SUCCEEDED' ? 'error' : 'success',
            message: result.status !== 'SUCCEEDED' ? result.error?.message ?? `知识库删除未成功（${result.status}）。` : '知识库删除任务已完成。',
          }
        : { tone: 'info', message: '删除任务仍在后台执行，请稍后刷新。' });
      await loadKnowledgeBases(true);
    } catch (error) {
      const payload = error instanceof GatewayError ? error.payload : null;
      const code = payload && typeof payload === 'object' && 'error' in payload
        ? (payload.error as { code?: string } | null)?.code : undefined;
      if (error instanceof GatewayError && error.status === 404 && code === 'RAG-JOB-001') {
        try {
          // 删除 Job 可能因知识库软删除而返回 404；按同一身份查询目标 ID，避免列表分页误判。
          await gatewayRequest<ApiEnvelope<KnowledgeBase>>(session, `v1/management/knowledge-bases/${item.id}`);
        } catch (lookupError) {
          const lookupPayload = lookupError instanceof GatewayError ? lookupError.payload : null;
          const lookupCode = lookupPayload && typeof lookupPayload === 'object' && 'error' in lookupPayload
            ? (lookupPayload.error as { code?: string } | null)?.code : undefined;
          if (lookupError instanceof GatewayError && lookupError.status === 404 && lookupCode === 'RAG-KB-001') {
            try {
              const response = await gatewayRequest<PageEnvelope<KnowledgeBase>>(
                session,
                `v1/management/knowledge-bases?page=1&page_size=100&scope_type=${scope}`,
              );
              const visible = response.data.filter((base) => base.scope_type === scope);
              if (visible.some((base) => base.id === item.id)) throw new Error('知识库列表仍包含待删除目标');
              setKnowledgeBases(visible);
              setSelectedKnowledgeBaseId((current) =>
                visible.some((base) => base.id === current) ? current : visible[0]?.id ?? '',
              );
              setNotice({ tone: 'success', message: '知识库已删除。' });
              return;
            } catch {
              // 列表刷新失败时不声称删除完成，继续展示原始任务错误。
            }
          }
        }
      }
      setNotice({ tone: 'error', message: errorMessage(error) });
    } finally {
      setBusy('');
    }
  }

  /** 打开全新上传弹窗，清除上次文件元数据；不读取文件正文，也不请求后端。 */
  function openUploadDialog() {
    setSelectedUploadFiles([]);
    setUploadMode('AUTO');
    setDialog('upload');
  }

  /** 取消或上传成功后清空文件元数据并卸载控件，不影响已发送到后端的文件。 */
  function closeUploadDialog() {
    setSelectedUploadFiles([]);
    setDialog(null);
  }

  /**
   * 一次可选择多个文件，但每个文件仍调用一次单文件 API，并生成独立幂等键。
   * AUTO 模式仅在能力列表包含 MinerU 策略时为 PDF 提交 Parser，其余文件交给后端路由；
   * MANUAL 提交管理员选定值；VISION 和 MINERU 显式覆盖解析器，分块仍由后端选择。
   */
  async function uploadDocuments(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selectedKnowledgeBase) return;
    const form = new FormData(event.currentTarget);
    const input = event.currentTarget.elements.namedItem('files') as HTMLInputElement | null;
    const files = Array.from(input?.files ?? []);
    if (!files.length) {
      setNotice({ tone: 'error', message: '请至少选择一个文件。' });
      return;
    }
    // 管理端最多接收 100 MiB 单文件；如果当前后端公布了更小限制，则以前后端
    // 较严格的值为准，避免浏览器上传完成后才被 Gateway 拒绝。
    const uploadLimit = Math.min(capabilities.max_file_bytes ?? MAX_UPLOAD_FILE_BYTES, MAX_UPLOAD_FILE_BYTES);
    const tooLarge = files.find((file) => file.size > uploadLimit);
    if (tooLarge) {
      setNotice({ tone: 'error', message: `${tooLarge.name} 超过单文件 ${formatBytes(uploadLimit)} 上限。` });
      return;
    }
    // 视觉解析仅支持 PDF；整批上传前校验，避免混选文件导致部分上传成功。
    if (uploadMode === 'VISION' && (!capabilities.parser_profiles.includes('vision-pdf@1') || files.some((file) => !file.name.toLowerCase().endsWith('.pdf')))) {
      setNotice({ tone: 'error', message: 'PDF 视觉模型解析仅支持 PDF 文件，且需要后端启用该策略。' });
      return;
    }
    // 每个文件按扩展名检查对应策略；图片由后端转为单页 PDF，整批校验后才上传。
    if (uploadMode === 'MINERU' && files.some((file) => !(/\.(pdf|png|jpe?g)$/i.test(file.name)) || !capabilities.parser_profiles.includes(file.name.toLowerCase().endsWith('.pdf') ? 'mineru-cloud@1' : 'mineru-image@1'))) {
      setNotice({ tone: 'error', message: 'MinerU 支持 PDF、PNG、JPG/JPEG；后端需已导入对应策略。' });
      return;
    }
    const metadata = String(form.get('metadata') || '').trim();
    if (metadata) {
      try {
        const parsed = JSON.parse(metadata) as unknown;
        if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') throw new Error();
      } catch {
        setNotice({ tone: 'error', message: 'Metadata 必须是合法的 JSON 对象。' });
        return;
      }
    }

    setBusy('upload');
    let completed = 0;
    try {
      for (const file of files) {
        setUploadProgress(`正在上传 ${completed + 1}/${files.length}：${file.name}`);
        const body = new FormData();
        body.set('file', file);
        body.set('auto_parse', 'true');
        if (metadata) body.set('metadata', metadata);
        if (uploadMode === 'AUTO' && file.name.toLowerCase().endsWith('.pdf') && capabilities.parser_profiles.includes('mineru-cloud@1')) body.set('parser_profile', 'mineru-cloud@1');
        if (uploadMode === 'VISION') body.set('parser_profile', 'vision-pdf@1');
        if (uploadMode === 'MINERU') body.set('parser_profile', file.name.toLowerCase().endsWith('.pdf') ? 'mineru-cloud@1' : 'mineru-image@1');
        if (uploadMode === 'MANUAL') {
          body.set('parser_profile', String(form.get('parserProfile')));
          body.set('chunk_profile', String(form.get('chunkProfile')));
        }
        await gatewayRequest<ApiEnvelope<AcceptedDocument>>(
          session,
          `v1/management/knowledge-bases/${selectedKnowledgeBase.id}/documents`,
          { method: 'POST', idempotent: true, body },
        );
        completed += 1;
      }
      closeUploadDialog();
      setNotice({ tone: 'success', message: `${completed} 个文件已接收，正在后台解析并建立索引。` });
      await Promise.all([loadDocuments(true), loadKnowledgeBases(true)]);
    } catch (error) {
      setNotice({ tone: 'error', message: `已上传 ${completed}/${files.length} 个文件；${errorMessage(error)}` });
      await loadDocuments(true);
    } finally {
      setUploadProgress('');
      setBusy('');
    }
  }

  /** 重试只沿用后端记录的失败阶段，不在前端猜测应重跑哪个 Worker。 */
  async function retryDocument(item: KnowledgeDocument) {
    setBusy(`retry-${item.id}`);
    try {
      const response = await gatewayRequest<ApiEnvelope<AcceptedJob>>(
        session,
        `v1/management/documents/${item.id}/retry`,
        { method: 'POST', idempotent: true },
      );
      const result = await waitForJob(session, response.data.job_id, (value) =>
        setNotice({ tone: 'info', message: `正在重试“${item.filename}”：${value}` }),
      );
      setNotice(result
        ? {
            tone: result.status !== 'SUCCEEDED' ? 'error' : 'success',
            message: result.status !== 'SUCCEEDED' ? result.error?.message ?? `文档重试未成功（${result.status}）。` : '文档重试任务已完成。',
          }
        : { tone: 'info', message: '重试任务仍在后台执行，请稍后刷新。' });
      await loadDocuments(true);
    } catch (error) {
      setNotice({ tone: 'error', message: errorMessage(error) });
    } finally {
      setBusy('');
    }
  }

  /** 允许管理员显式选择新的 Parser/Chunk 后重解析现有文件。 */
  async function reparseDocument(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selectedDocument) return;
    const form = new FormData(event.currentTarget);
    setBusy('reparse');
    try {
      const response = await gatewayRequest<ApiEnvelope<AcceptedJob>>(
        session,
        `v1/management/documents/${selectedDocument.id}/reparse`,
        {
          method: 'POST',
          idempotent: true,
          body: {
            parser_profile: String(form.get('parserProfile')),
            chunk_profile: String(form.get('chunkProfile')),
          },
        },
      );
      setDialog(null);
      const result = await waitForJob(session, response.data.job_id, (value) =>
        setNotice({ tone: 'info', message: `正在重新解析“${selectedDocument.filename}”：${value}` }),
      );
      setNotice(result
        ? {
            tone: result.status !== 'SUCCEEDED' ? 'error' : 'success',
            message: result.status !== 'SUCCEEDED' ? result.error?.message ?? `文档重解析未成功（${result.status}）。` : '文档重解析任务已完成。',
          }
        : { tone: 'info', message: '重解析任务仍在后台执行，请稍后刷新。' });
      await loadDocuments(true);
    } catch (error) {
      setNotice({ tone: 'error', message: errorMessage(error) });
    } finally {
      setBusy('');
    }
  }

  /** 文档删除会移除索引；处理中状态同时请求取消活动任务。 */
  async function deleteDocument(item: KnowledgeDocument) {
    if (!window.confirm(`确认删除“${item.filename}”及其全部索引内容吗？`)) return;
    setBusy(`delete-doc-${item.id}`);
    try {
      const cancelActive = !TERMINAL_DOCUMENT_STATUSES.has(item.status);
      const response = await gatewayRequest<ApiEnvelope<AcceptedJob>>(
        session,
        `v1/management/documents/${item.id}?cancel_active=${cancelActive}`,
        { method: 'DELETE', idempotent: true },
      );
      const result = await waitForJob(session, response.data.job_id, (value) =>
        setNotice({ tone: 'info', message: `正在删除“${item.filename}”：${value}` }),
      );
      setNotice(result
        ? {
            tone: result.status !== 'SUCCEEDED' ? 'error' : 'success',
            message: result.status !== 'SUCCEEDED' ? result.error?.message ?? `文档删除未成功（${result.status}）。` : '文档删除任务已完成。',
          }
        : { tone: 'info', message: '文档删除任务仍在后台执行，请稍后刷新。' });
      await Promise.all([loadDocuments(true), loadKnowledgeBases(true)]);
    } catch (error) {
      setNotice({ tone: 'error', message: errorMessage(error) });
    } finally {
      setBusy('');
    }
  }

  /** 批量删除只提交勾选文件；最多三条删除链并行，单项失败不阻止其他文件。 */
  async function deleteDocuments(items: KnowledgeDocument[]) {
    if (busy || !items.length) return;
    if (!window.confirm(`确认删除选中的 ${items.length} 个文件及其全部索引？处理中任务会请求取消。`)) return;
    setBusy('delete-docs');
    const results: string[] = [];
    let cursor = 0;
    let succeeded = 0;
    let pending = 0;
    async function consume() {
      while (cursor < items.length) {
        const item = items[cursor++];
        try {
          const response = await gatewayRequest<ApiEnvelope<AcceptedJob>>(
            session,
            `v1/management/documents/${item.id}?cancel_active=${!TERMINAL_DOCUMENT_STATUSES.has(item.status)}`,
            { method: 'DELETE', idempotent: true },
          );
          const result = await waitForJob(session, response.data.job_id, () => {});
          if (!result) {
            pending++;
            results.push(`${item.filename}：后台处理中`);
          } else if (result.status === 'SUCCEEDED') {
            succeeded++;
            results.push(`${item.filename}：删除完成`);
          } else {
            results.push(`${item.filename}：${result.error?.message ?? result.status}`);
          }
        } catch (error) {
          results.push(`${item.filename}：${errorMessage(error)}`);
        }
        setNotice({ tone: 'info', message: `已处理 ${results.length}/${items.length} 个文件。${results.join('；')}` });
      }
    }
    try {
      await Promise.all(Array.from({ length: Math.min(3, items.length) }, consume));
      await Promise.all([loadDocuments(true), loadKnowledgeBases(true)]);
      const failed = items.length - succeeded - pending;
      setNotice({
        tone: failed ? 'error' : pending ? 'info' : 'success',
        message: `删除完成 ${succeeded} 个，后台处理中 ${pending} 个，未成功 ${failed} 个。${results.join('；')}`,
      });
    } finally {
      setBusy('');
    }
  }

  return (
    <div className="content">
      {notice && (
        <div className={`notice ${notice.tone}`} role="status">
          {notice.tone === 'success' ? <CheckCircle2 size={17} /> : notice.tone === 'info' ? <LoaderCircle className="spin" size={17} /> : <X size={17} />}
          <span>{notice.message}</span>
          <button onClick={() => setNotice(null)} aria-label="关闭提示"><X size={15} /></button>
        </div>
      )}

      {section === 'overview' && (
        <>
          <PageHeading
            eyebrow="KNOWLEDGE OVERVIEW"
            title={scope === 'TENANT' ? '企业共享知识' : '系统全局知识'}
            description={scope === 'TENANT' ? '管理当前租户内所有成员可访问的知识库。' : '管理所有租户用户共同访问的系统知识库。'}
            actions={
              <>
                <button className="secondary-button" onClick={() => void loadKnowledgeBases()} disabled={Boolean(busy)}><RefreshCw size={17} />刷新</button>
                <button className="primary-button" onClick={() => setDialog('create')}><Plus size={18} />新建知识库</button>
              </>
            }
          />
          <section className="metric-grid" aria-label="知识库概览">
            <Metric icon={<BookOpen size={20} />} label="共享知识库" value={knowledgeBases.length} note="当前管理范围" tone="green" />
            <Metric icon={<FileStack size={20} />} label="知识文档" value={documentTotal} note="全部知识库" tone="blue" />
            <Metric icon={<RefreshCw size={20} />} label="处理中" value={processingTotal} note="当前选中知识库" tone="amber" />
            <Metric icon={<CheckCircle2 size={20} />} label="可用知识库" value={knowledgeBases.filter((item) => item.status === 'ACTIVE').length} note="状态为 ACTIVE" tone="violet" />
          </section>
          <KnowledgeTable
            title="最近知识库"
            items={filteredKnowledgeBases.slice(0, 5)}
            scope={scope}
            busy={busy}
            selectedId={selectedKnowledgeBaseId}
            onSelect={setSelectedKnowledgeBaseId}
            onEdit={(item) => { setSelectedKnowledgeBaseId(item.id); setDialog('edit'); }}
            onDocuments={(item) => { setSelectedKnowledgeBaseId(item.id); onSectionChange('documents'); }}
            onDelete={(item) => void deleteKnowledgeBase(item)}
            onCreate={() => setDialog('create')}
          />
        </>
      )}

      {section === 'knowledge' && (
        <>
          <PageHeading
            eyebrow="KNOWLEDGE BASES"
            title={scope === 'TENANT' ? '租户共享知识库' : '系统共享知识库'}
            description="创建、配置和维护当前管理员范围内的共享知识库。"
            actions={<button className="primary-button" onClick={() => setDialog('create')}><Plus size={18} />新建知识库</button>}
          />
          <div className="list-toolbar">
            <label className="search-box"><Search size={16} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="按名称、描述或外部 ID 搜索" /></label>
            <span>{filteredKnowledgeBases.length} 个知识库</span>
            <button className="secondary-button icon-only" onClick={() => void loadKnowledgeBases()} aria-label="刷新"><RefreshCw size={17} /></button>
          </div>
          <KnowledgeTable
            title="知识库列表"
            items={filteredKnowledgeBases}
            scope={scope}
            busy={busy}
            selectedId={selectedKnowledgeBaseId}
            onSelect={setSelectedKnowledgeBaseId}
            onEdit={(item) => { setSelectedKnowledgeBaseId(item.id); setDialog('edit'); }}
            onDocuments={(item) => { setSelectedKnowledgeBaseId(item.id); onSectionChange('documents'); }}
            onDelete={(item) => void deleteKnowledgeBase(item)}
            onCreate={() => setDialog('create')}
          />
        </>
      )}

      {section === 'documents' && (
        <>
          <PageHeading
            eyebrow="DOCUMENT OPERATIONS"
            title="文档与解析任务"
            description="上传文件、跟踪解析状态，并对失败文档重试或重新选择 Profile。"
            actions={
              <>
                <button className="secondary-button" onClick={() => void loadDocuments()} disabled={!selectedKnowledgeBase || Boolean(busy)}><RefreshCw size={17} />刷新</button>
                <button className="primary-button" onClick={openUploadDialog} disabled={!selectedKnowledgeBase}><UploadCloud size={18} />上传文件</button>
              </>
            }
          />
          <section className="target-card">
            <div className={`scope-square ${scope.toLowerCase()}`}>{scope === 'TENANT' ? <Building2 size={20} /> : <Globe2 size={20} />}</div>
            <div className="target-copy"><span>当前知识库</span><strong>{selectedKnowledgeBase?.name ?? '尚未创建知识库'}</strong></div>
            <select value={selectedKnowledgeBaseId} onChange={(event) => setSelectedKnowledgeBaseId(event.target.value)} disabled={!knowledgeBases.length || Boolean(busy)}>
              {knowledgeBases.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
            </select>
            {selectedKnowledgeBase && <div className="target-profile"><span>{selectedKnowledgeBase.parser_profile}</span><span>{selectedKnowledgeBase.chunk_profile}</span></div>}
          </section>
          <DocumentTable
            key={`${scope}:${selectedKnowledgeBaseId}`}
            onDeleteSelected={(items) => void deleteDocuments(items)}
            items={documents}
            busy={busy}
            hasKnowledgeBase={Boolean(selectedKnowledgeBase)}
            onUpload={openUploadDialog}
            onViewChunks={(item) => { setSelectedDocument(item); setDialog('chunks'); }}
            onRetry={(item) => void retryDocument(item)}
            onReparse={(item) => { setSelectedDocument(item); setDialog('reparse'); }}
            onDelete={(item) => void deleteDocument(item)}
          />
        </>
      )}

      {dialog === 'create' && (
        <ProfileDialog
          title={`新建${scope === 'TENANT' ? '租户' : '系统'}共享知识库`}
          scope={scope}
          capabilities={capabilities}
          defaults={{ parser: defaultParser, chunk: defaultChunk, retrieval: defaultRetrieval }}
          busy={busy === 'create'}
          onClose={() => setDialog(null)}
          onSubmit={createKnowledgeBase}
        />
      )}
      {dialog === 'edit' && selectedKnowledgeBase && (
        <ProfileDialog
          title="编辑知识库"
          scope={scope}
          capabilities={capabilities}
          defaults={{ parser: selectedKnowledgeBase.parser_profile, chunk: selectedKnowledgeBase.chunk_profile, retrieval: selectedKnowledgeBase.retrieval_profile }}
          knowledgeBase={selectedKnowledgeBase}
          busy={busy === 'edit'}
          onClose={() => setDialog(null)}
          onSubmit={updateKnowledgeBase}
        />
      )}
      {dialog === 'upload' && selectedKnowledgeBase && (
        <Modal title="上传知识文件" description={`目标：${selectedKnowledgeBase.name}`} onClose={() => !busy && closeUploadDialog()}>
          <form onSubmit={uploadDocuments}>
            <label className="upload-zone">
              <UploadCloud size={28} />
              <strong>选择一个或多个文件</strong>
              <span>每个文件使用独立幂等键，单文件上限 100 MiB，可同时选择 PDF、Office 和图片</span>
              <input name="files" type="file" multiple required accept={capabilities.file_extensions.join(',')} onChange={(event) => setSelectedUploadFiles(Array.from(event.currentTarget.files ?? [], (file) => ({ name: file.name, size: file.size })))} />
            </label>
            {/* 文件名作为普通文本持续显示；选择变化时更新数量，不依赖浏览器悬停提示。 */}
            <div aria-live="polite" aria-atomic="true">
              {selectedUploadFiles.length > 0 && (
                <div className="selected-upload-files">
                  <strong>已选择 {selectedUploadFiles.length} 个文件</strong>
                  <ul aria-label="已选择的文件">
                    {selectedUploadFiles.map((file, index) => <li key={`${index}-${file.name}`}><span>{file.name}</span><small>{formatBytes(file.size)}</small></li>)}
                  </ul>
                </div>
              )}
            </div>
            <div className="field">
              <span>解析策略</span>
              <select aria-label="解析策略" value={uploadMode} onChange={(event) => setUploadMode(event.target.value as 'AUTO' | 'MANUAL' | 'VISION' | 'MINERU')}>
                <option value="AUTO">按文件类型自动路由{capabilities.parser_profiles.includes('mineru-cloud@1') ? '（PDF 默认 MinerU）' : '（推荐）'}</option>
                <option value="VISION" disabled={!capabilities.parser_profiles.includes('vision-pdf@1')}>PDF 视觉模型解析</option>
                <option value="MINERU" disabled={!capabilities.parser_profiles.includes('mineru-cloud@1')}>MinerU 云端解析{!capabilities.parser_profiles.includes('mineru-cloud@1') ? '（后端策略未导入）' : ''}</option>
                <option value="MANUAL">手动选择解析与分块策略</option>
              </select>
              <small>{uploadMode === 'VISION'
                ? '仅支持 PDF：页面图片会发送至已配置的视觉模型，引用定位为整页范围。'
                : uploadMode === 'MINERU'
                  ? '支持 PDF、PNG、JPG/JPEG；图片自动转为单页 PDF（静态图片，最多 1600 万像素），发送至 MinerU 云端解析。'
                : uploadMode === 'MANUAL'
                  ? '所选策略应用于本次所有文件，请确保解析器支持文件类型。'
                  : capabilities.parser_profiles.includes('mineru-cloud@1')
                    ? 'PDF 默认使用 MinerU 云端解析；其他文件按知识库默认策略和文件类型自动选择。'
                    : '优先使用知识库默认策略，不兼容时按文件类型自动选择。'}</small>
            </div>
            {uploadMode === 'MANUAL' && (
              <div className="form-grid">
                <ProfileSelect label="Parser Profile" name="parserProfile" values={capabilities.parser_profiles} defaultValue={selectedKnowledgeBase.parser_profile} />
                <ProfileSelect label="Chunk Profile" name="chunkProfile" values={capabilities.chunk_profiles} defaultValue={selectedKnowledgeBase.chunk_profile} />
              </div>
            )}
            <label className="field"><span>Metadata（可选 JSON 对象）</span><textarea name="metadata" placeholder={'例如 {"source":"internal"}'} /></label>
            {uploadProgress && <div className="inline-progress"><LoaderCircle className="spin" size={16} />{uploadProgress}</div>}
            <DialogActions busy={busy === 'upload'} submitLabel="开始上传" onCancel={closeUploadDialog} />
          </form>
        </Modal>
      )}
      {dialog === 'chunks' && selectedDocument && (
        // 身份、文档或修订变化时重新挂载，同时清理旧切块与 PDF 阅读会话。
        <DocumentChunks key={JSON.stringify([session.role, session.tenantId, session.userId, selectedDocument.id, selectedDocument.document_revision])}
          session={session} documentId={selectedDocument.id} filename={selectedDocument.filename} chunkCount={selectedDocument.chunk_count} documentRevision={selectedDocument.document_revision}
          onClose={() => { setDialog(null); setSelectedDocument(null); }} />
      )}
      {dialog === 'reparse' && selectedDocument && (
        <Modal title="重新解析文档" description={selectedDocument.filename} onClose={() => !busy && setDialog(null)}>
          <form onSubmit={reparseDocument}>
            <div className="warning-box">重新解析会替换现有文本块。请确认新 Parser 支持该文件类型。</div>
            <div className="form-grid">
              <ProfileSelect label="Parser Profile" name="parserProfile" values={capabilities.parser_profiles} defaultValue={selectedDocument.parser_profile} />
              <ProfileSelect label="Chunk Profile" name="chunkProfile" values={capabilities.chunk_profiles} defaultValue={selectedDocument.chunk_profile} />
            </div>
            <DialogActions busy={busy === 'reparse'} submitLabel="创建重解析任务" onCancel={() => setDialog(null)} />
          </form>
        </Modal>
      )}
    </div>
  );
}

/** 从能力列表中选稳定默认值；能力接口为空时仍保留代码内兼容基线。 */
function chooseDefault(values: string[], preferred: string, fallback: string): string {
  return values.includes(preferred) ? preferred : values[0] ?? fallback;
}

/** 页面标题和动作区域。 */
function PageHeading({ eyebrow, title, description, actions }: { eyebrow: string; title: string; description: string; actions: ReactNode }) {
  return <div className="page-heading"><div><span className="eyebrow">{eyebrow}</span><h1>{title}</h1><p>{description}</p></div><div className="heading-actions">{actions}</div></div>;
}

/** 总览统计卡片。 */
function Metric({ icon, label, value, note, tone }: { icon: ReactNode; label: string; value: number; note: string; tone: string }) {
  return <article className="metric-card"><span className={`metric-icon ${tone}`}>{icon}</span><div><p>{label}</p><strong>{value}</strong><span>{note}</span></div></article>;
}

/** 知识库表格只显示当前 scope 数据，并提供编辑、文档和删除入口。 */
function KnowledgeTable({
  title, items, scope, busy, selectedId, onSelect, onEdit, onDocuments, onDelete, onCreate,
}: {
  title: string;
  items: KnowledgeBase[];
  scope: ScopeType;
  busy: string;
  selectedId: string;
  onSelect: (id: string) => void;
  onEdit: (item: KnowledgeBase) => void;
  onDocuments: (item: KnowledgeBase) => void;
  onDelete: (item: KnowledgeBase) => void;
  onCreate: () => void;
}) {
  return (
    <section className="data-card">
      <div className="data-card-head"><div><h2>{title}</h2><p>{scope === 'TENANT' ? '仅当前租户可见' : '所有租户用户可见'}</p></div><span className={`scope-pill ${scope.toLowerCase()}`}>{scope === 'TENANT' ? <Building2 size={13} /> : <Globe2 size={13} />}{scope === 'TENANT' ? '租户共享' : '系统共享'}</span></div>
      {busy === 'load-kb' ? <LoadingRows /> : items.length ? (
        <div className="table-scroll"><table className="data-table"><thead><tr><th>知识库</th><th>归属</th><th>文档</th><th>状态</th><th>默认 Profile</th><th>操作</th></tr></thead><tbody>{items.map((item) => (
          <tr key={item.id} className={selectedId === item.id ? 'selected' : ''} onClick={() => onSelect(item.id)}>
            <td><div className="resource-name"><span><FolderOpen size={17} /></span><div><strong>{item.name}</strong><small>{item.description || '暂无描述'}</small></div></div></td>
            <td><span className={`ownership ${item.scope_type.toLowerCase()}`}>{item.scope_type === 'TENANT' ? '租户共享' : '系统共享'}</span></td>
            <td>{item.document_count}</td>
            <td><Status value={item.status} /></td>
            <td><div className="profile-stack"><span>{item.parser_profile}</span><span>{item.chunk_profile}</span></div></td>
            <td><div className="row-actions" onClick={(event) => event.stopPropagation()}><button title="编辑" onClick={() => onEdit(item)}><Pencil size={15} /></button><button title="管理文档" onClick={() => onDocuments(item)}><FileStack size={15} /></button><button className="danger" title="删除" disabled={Boolean(busy)} onClick={() => onDelete(item)}><Trash2 size={15} /></button></div></td>
          </tr>
        ))}</tbody></table></div>
      ) : <EmptyState icon={<BookOpen size={27} />} title="还没有共享知识库" description="创建第一个知识库后即可上传并解析知识文档。" action={<button className="primary-button" onClick={onCreate}><Plus size={17} />创建知识库</button>} />}
    </section>
  );
}

/** 文档表格展示解析状态、进度和恢复操作；已完成解析的文档可只读查看切块。 */
function DocumentTable({ onDeleteSelected, items, busy, hasKnowledgeBase, onUpload, onViewChunks, onRetry, onReparse, onDelete }: { onDeleteSelected: (items: KnowledgeDocument[]) => void; items: KnowledgeDocument[]; busy: string; hasKnowledgeBase: boolean; onUpload: () => void; onViewChunks: (item: KnowledgeDocument) => void; onRetry: (item: KnowledgeDocument) => void; onReparse: (item: KnowledgeDocument) => void; onDelete: (item: KnowledgeDocument) => void }) {
  // 选择仅属于当前知识库和当前已加载列表，切换知识库由组件 key 清空。
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const selected = items.filter((item) => selectedIds.includes(item.id));
  const allSelected = items.length > 0 && selected.length === items.length;
  return (
    <section className="data-card document-card">
      <div className="data-card-head"><div><h2>知识文档</h2><p>上传、解析、切块和索引状态</p></div><div className="row-actions"><span>{items.length} 个文件 · 已选 {selected.length} 个</span><button className="secondary-button" disabled={Boolean(busy) || !selected.length} onClick={() => onDeleteSelected(selected)}><Trash2 size={16} />批量删除</button></div></div>
      {busy === 'load-docs' ? <LoadingRows /> : items.length ? (
        <div className="table-scroll"><table className="data-table"><thead><tr><th><input type="checkbox" aria-label="全选当前列表" checked={allSelected} disabled={Boolean(busy)} ref={(node) => { if (node) node.indeterminate = selected.length > 0 && !allSelected; }} onChange={(event) => setSelectedIds(event.target.checked ? items.map((item) => item.id) : [])} /></th><th>文件</th><th>状态与进度</th><th>索引统计</th><th>解析策略</th><th>更新时间</th><th>操作</th></tr></thead><tbody>{items.map((item) => (
          <tr key={item.id}>
            <td><input type="checkbox" aria-label={`选择 ${item.filename}`} checked={selectedIds.includes(item.id)} disabled={Boolean(busy)} onChange={(event) => setSelectedIds((ids) => event.target.checked ? [...ids, item.id] : ids.filter((id) => id !== item.id))} /></td>
            <td><div className="resource-name"><span className="file-icon"><File size={17} /></span><div><strong>{item.filename}</strong><small>{formatBytes(item.file_size_bytes)}</small></div></div></td>
            <td><Status value={item.status} /><div className="progress-track"><span style={{ width: `${Math.round(item.progress * 100)}%` }} /></div><small>{Math.round(item.progress * 100)}%</small>{item.last_error?.message && <div className="error-copy">{item.last_error.message}</div>}</td>
            <td><strong>{item.chunk_count}</strong><small> chunks · {item.token_count} tokens</small></td>
            <td><div className="profile-stack"><span>{item.parser_profile}</span><span>{item.chunk_profile}</span></div></td>
            <td>{formatDate(item.updated_at)}</td>
            <td><div className="row-actions"><button className="view-chunks-button" disabled={Boolean(busy) || !['READY', 'INDEXED'].includes(item.status)} onClick={() => onViewChunks(item)}><Layers3 size={15} />查看切块</button><button title="重新解析" disabled={Boolean(busy)} onClick={() => onReparse(item)}><RotateCcw size={15} /></button>{item.status === 'FAILED' && <button title="重试失败任务" disabled={Boolean(busy)} onClick={() => onRetry(item)}><RefreshCw size={15} /></button>}<button className="danger" title="删除" disabled={Boolean(busy)} onClick={() => onDelete(item)}><Trash2 size={15} /></button></div></td>
          </tr>
        ))}</tbody></table></div>
      ) : <EmptyState icon={<FileStack size={27} />} title={hasKnowledgeBase ? '这个知识库还没有文档' : '请先创建知识库'} description={hasKnowledgeBase ? '支持一次选择多个 PDF、Office 文件和图片，系统会逐个创建解析任务。' : '文档必须归属于一个共享知识库。'} action={hasKnowledgeBase ? <button className="primary-button" onClick={onUpload}><UploadCloud size={17} />上传文件</button> : undefined} />}
    </section>
  );
}

/** 创建与编辑复用同一份 Profile 表单，但创建时额外显示可选 External ID。 */
function ProfileDialog({ title, scope, capabilities, defaults, knowledgeBase, busy, onClose, onSubmit }: { title: string; scope: ScopeType; capabilities: Capabilities; defaults: { parser: string; chunk: string; retrieval: string }; knowledgeBase?: KnowledgeBase; busy: boolean; onClose: () => void; onSubmit: (event: FormEvent<HTMLFormElement>) => void }) {
  return (
    <Modal title={title} description={`固定归属：${scope === 'TENANT' ? '当前租户共享' : '系统全局共享'}`} onClose={() => !busy && onClose()}>
      <form onSubmit={onSubmit}>
        <label className="field"><span>知识库名称</span><input name="name" required maxLength={128} defaultValue={knowledgeBase?.name ?? ''} placeholder="例如 产品技术资料库" /></label>
        {!knowledgeBase && <label className="field"><span>External ID（可选）</span><input name="externalId" maxLength={128} placeholder="留空即可；填写时需保持业务唯一" /><small>它是外部业务系统的稳定引用，不是知识库名称，也不要在不同请求中复用幂等键。</small></label>}
        <label className="field"><span>描述（可选）</span><textarea name="description" maxLength={2048} defaultValue={knowledgeBase?.description ?? ''} placeholder="说明内容范围与维护用途" /></label>
        <div className="form-grid"><ProfileSelect label="默认 Parser" name="parserProfile" values={capabilities.parser_profiles} defaultValue={defaults.parser} /><ProfileSelect label="默认 Chunk" name="chunkProfile" values={capabilities.chunk_profiles} defaultValue={defaults.chunk} /></div>
        <ProfileSelect label="Retrieval Profile" name="retrievalProfile" values={capabilities.retrieval_profiles} defaultValue={defaults.retrieval} />
        <div className="immutable-field"><span>Embedding Profile</span><strong>embedding-openai-compatible@3</strong><small>由当前后端部署固定并在启动时校验。</small></div>
        <DialogActions busy={busy} submitLabel={knowledgeBase ? '保存修改' : '创建知识库'} onCancel={onClose} />
      </form>
    </Modal>
  );
}

/** 标准 Profile 下拉框使用能力发现接口返回值，不写死后端未开放的选项。 */
function ProfileSelect({ label, name, values, defaultValue }: { label: string; name: string; values: string[]; defaultValue: string }) {
  const options = values.length ? values : [defaultValue];
  return <label className="field"><span>{label}</span><select name={name} defaultValue={defaultValue}>{options.map((value) => <option key={value} value={value}>{value}</option>)}</select></label>;
}

/** 无路由依赖的轻量模态框，关闭按钮始终在可见标题区。 */
function Modal({ title, description, onClose, children }: { title: string; description: string; onClose: () => void; children: ReactNode }) {
  return <div className="modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}><section className="modal" role="dialog" aria-modal="true" aria-labelledby="modal-title"><header><div><h2 id="modal-title">{title}</h2><p>{description}</p></div><button onClick={onClose} aria-label="关闭"><X size={19} /></button></header><div className="modal-body">{children}</div></section></div>;
}

/** 模态框底部动作；busy 时禁止重复提交导致同一业务动作并发。 */
function DialogActions({ busy, submitLabel, onCancel }: { busy: boolean; submitLabel: string; onCancel: () => void }) {
  return <div className="dialog-actions"><button type="button" className="secondary-button" onClick={onCancel} disabled={busy}>取消</button><button type="submit" className="primary-button" disabled={busy}>{busy ? <LoaderCircle className="spin" size={17} /> : null}{busy ? '处理中…' : submitLabel}</button></div>;
}

/** 标准状态标签；失败/取消醒目提示，其余处理中状态使用中性颜色。 */
function Status({ value }: { value: string }) {
  const tone = value === 'ACTIVE' || value === 'READY' || value === 'INDEXED' || value === 'SUCCEEDED'
    ? 'ok'
    : value === 'FAILED' || value === 'CANCELLED'
      ? 'error'
      : 'working';
  return <span className={`status-badge ${tone}`}><i />{value}</span>;
}

/** 空状态明确给出下一步操作。 */
function EmptyState({ icon, title, description, action }: { icon: ReactNode; title: string; description: string; action?: ReactNode }) {
  return <div className="empty-state"><span>{icon}</span><h3>{title}</h3><p>{description}</p>{action}</div>;
}

/** 初次请求占位，避免渲染虚构业务记录。 */
function LoadingRows() {
  return <div className="loading-rows"><LoaderCircle className="spin" size={21} /><span>正在读取最新数据…</span></div>;
}

/** 文件大小转换为便于阅读的二进制单位。 */
function formatBytes(value: number): string {
  if (value >= 1024 * 1024) return `${(value / 1024 / 1024).toFixed(2)} MiB`;
  if (value >= 1024) return `${Math.ceil(value / 1024)} KiB`;
  return `${value} B`;
}

/** 服务端时间统一按当前浏览器中文时区显示。 */
function formatDate(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false });
}
