"use client";

/**
 * 文档管理面板：上传、筛选、查看进度、解析、版本更新、回滚、删除和浏览文本块。
 *
 * “上传新版本”不会覆盖当前可检索文件。后端会把候选文件作为独立物理文档解析，
 * 成功后才切换活动版本；失败时旧版本继续服务。开发控制台保留版本列表和原始响应，
 * 便于验收这个发布边界，而不是把普通重解析误当成安全文件更新。
 */

import {
  Ban,
  Eye,
  FileCode2,
  FileUp,
  ListTree,
  Play,
  RefreshCw,
  RotateCcw,
  Trash2,
} from "lucide-react";
import { FormEvent, useCallback, useEffect, useState } from "react";
import {
  gatewayRequest,
  parseJsonObject,
  payloadFrom,
} from "@/lib/client-api";
import { Card, Empty, JsonOutput, PageHead, Status } from "./shared";
import type { ResourceIds } from "./use-resources";

type DocumentItem = {
  id: string;
  filename: string;
  status: string;
  progress: number;
  chunk_count: number;
  token_count: number;
  file_size_bytes: number;
  parser_profile: string;
  chunk_profile: string;
  created_at: string;
  active_version_id?: string | null;
  active_version_number?: number;
  document_revision?: number;
  last_error?: unknown;
};

type DocumentVersionItem = {
  id: string;
  document_id: string;
  version_number: number;
  filename: string;
  mime_type: string;
  file_size_bytes: number;
  content_sha256: string;
  metadata: Record<string, unknown>;
  parser_profile: string;
  chunk_profile: string;
  status: string;
  requested_mode: string;
  effective_mode: string;
  chunk_count: number;
  token_count: number;
  created_at: string;
  updated_at: string;
  activated_at?: string | null;
  retired_at?: string | null;
  last_error?: unknown;
};

type VersionJobWatch = {
  /** 版本发布或回滚对应的异步 Job；逻辑文档在它完成前仍指向旧活动版本。 */
  jobId: string;
  documentId: string;
  operation: "UPLOAD_VERSION" | "ROLLBACK_VERSION";
  status: string;
};

const MAX_UPLOAD_FILE_BYTES = 100 * 1024 * 1024;

function formatFileSize(size: number): string {
  if (size >= 1024 * 1024) return `${(size / (1024 * 1024)).toFixed(2)} MiB`;
  return `${Math.ceil(size / 1024)} KiB`;
}

function formatDate(value?: string | null): string {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString("zh-CN");
}

export function DocumentsPanel({
  resources,
  setResources,
}: {
  resources: ResourceIds;
  setResources: (patch: Partial<ResourceIds>) => void;
}) {
  const [knowledgeBaseId, setKnowledgeBaseId] = useState(resources.knowledgeBaseId);
  const [documentId, setDocumentId] = useState(resources.documentId);
  const [jobId, setJobId] = useState(resources.jobId);
  const [items, setItems] = useState<DocumentItem[]>([]);
  const [versions, setVersions] = useState<DocumentVersionItem[]>([]);
  const [result, setResult] = useState<unknown>();
  const [busy, setBusy] = useState(false);
  const [selectedFile, setSelectedFile] = useState("");
  const [selectedVersionFile, setSelectedVersionFile] = useState("");
  const [versionIdempotencyKey, setVersionIdempotencyKey] = useState("");
  const [lastMutationKey, setLastMutationKey] = useState("");
  const [versionJobWatch, setVersionJobWatch] = useState<VersionJobWatch | null>(null);

  const rememberResponse = useCallback(
    (response: unknown) => {
      const data =
        response && typeof response === "object"
          ? (response as { data?: Record<string, unknown> }).data
          : undefined;
      const patch: Partial<ResourceIds> = {};
      if (data?.document_id) {
        patch.documentId = String(data.document_id);
        setDocumentId(patch.documentId);
      }
      if (data?.job_id) {
        patch.jobId = String(data.job_id);
        setJobId(patch.jobId);
      }
      if (Object.keys(patch).length) setResources(patch);
      setResult(response);
    },
    [setResources],
  );

  const list = useCallback(async (showResponse = true) => {
    if (!knowledgeBaseId.trim()) return;
    setBusy(true);
    setResources({ knowledgeBaseId: knowledgeBaseId.trim() });
    try {
      const response = await gatewayRequest<{ data: DocumentItem[] }>(
        `v1/management/knowledge-bases/${knowledgeBaseId.trim()}/documents?page=1&page_size=100`,
      );
      setItems(response.data ?? []);
      if (showResponse) rememberResponse(response);
    } catch (error) {
      setResult(payloadFrom(error));
    } finally {
      setBusy(false);
    }
  }, [knowledgeBaseId, rememberResponse, setResources]);

  useEffect(() => {
    if (!knowledgeBaseId) return;
    const timeout = window.setTimeout(() => void list(), 0);
    return () => window.clearTimeout(timeout);
  }, [knowledgeBaseId, list]);

  async function upload(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!knowledgeBaseId.trim()) {
      setResult({ error: "请先输入或选择知识库 ID" });
      return;
    }
    const form = new FormData(event.currentTarget);
    const file = form.get("file");
    if (!(file instanceof File) || !file.size) {
      setResult({ error: "请选择一个最小非业务测试文件" });
      return;
    }
    if (file.size > MAX_UPLOAD_FILE_BYTES) {
      setResult({
        error: {
          code: "LOCAL-UPLOAD-001",
          message: `文件大小为 ${formatFileSize(file.size)}，超过 100 MiB 单文件上限`,
          retryable: false,
          details: [],
        },
      });
      return;
    }
    try {
      const metadata = String(form.get("metadata") || "").trim();
      if (metadata) parseJsonObject(metadata);
      setBusy(true);
      const body = new FormData();
      body.set("file", file);
      body.set("auto_parse", form.get("autoParse") ? "true" : "false");
      for (const name of ["externalId", "metadata", "parserProfile", "chunkProfile"]) {
        const value = String(form.get(name) || "").trim();
        if (value) {
          const apiName =
            name === "externalId"
              ? "external_id"
              : name === "parserProfile"
                ? "parser_profile"
                : name === "chunkProfile"
                  ? "chunk_profile"
                  : name;
          body.set(apiName, value);
        }
      }
      const response = await gatewayRequest(
        `v1/management/knowledge-bases/${knowledgeBaseId.trim()}/documents`,
        { method: "POST", idempotent: true, body },
      );
      rememberResponse(response);
      await list(false);
    } catch (error) {
      setResult(payloadFrom(error));
    } finally {
      setBusy(false);
    }
  }

  const listVersions = useCallback(async (
    targetDocumentId = documentId,
    showResponse = true,
    foreground = true,
  ) => {
    if (!targetDocumentId.trim()) {
      setResult({ error: "请先在文档列表选择一个逻辑文档" });
      return;
    }
    if (foreground) setBusy(true);
    setDocumentId(targetDocumentId.trim());
    setResources({ documentId: targetDocumentId.trim() });
    try {
      const response = await gatewayRequest<{ data: DocumentVersionItem[] }>(
        `v1/management/documents/${targetDocumentId.trim()}/versions?page=1&page_size=100`,
      );
      setVersions(response.data ?? []);
      if (showResponse) rememberResponse(response);
    } catch (error) {
      setResult(payloadFrom(error));
    } finally {
      if (foreground) setBusy(false);
    }
  }, [documentId, rememberResponse, setResources]);

  const watchedJobId = versionJobWatch?.jobId;
  const watchedDocumentId = versionJobWatch?.documentId;

  useEffect(() => {
    if (!watchedJobId || !watchedDocumentId) return;
    let stopped = false;
    let timeout: number | undefined;
    const terminalStatuses = new Set(["SUCCEEDED", "FAILED", "CANCELLED"]);

    /**
     * 版本变更是异步发布：这里每 2.5 秒查询一次 Job，同时静默刷新版本历史。
     * 只有 Job 结束后才刷新逻辑文档列表，因此页面能明确展示“候选版本处理中”，
     * 又不会错误地把当前仍可检索的活动文档标成失败或不可用。
     */
    const poll = async () => {
      try {
        const response = await gatewayRequest<{ data?: { status?: string } }>(
          `v1/management/jobs/${watchedJobId}`,
        );
        if (stopped) return;
        const status = String(response.data?.status ?? "UNKNOWN");
        setVersionJobWatch((current) =>
          current && current.jobId === watchedJobId ? { ...current, status } : current,
        );
        setResult(response);
        await listVersions(watchedDocumentId, false, false);
        if (terminalStatuses.has(status)) {
          await list(false);
          if (!stopped) setVersionJobWatch(null);
          return;
        }
      } catch (error) {
        if (!stopped) setResult(payloadFrom(error));
      }
      if (!stopped) timeout = window.setTimeout(() => void poll(), 2500);
    };

    void poll();
    return () => {
      stopped = true;
      if (timeout !== undefined) window.clearTimeout(timeout);
    };
  }, [list, listVersions, watchedDocumentId, watchedJobId]);

  async function uploadVersion(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!documentId.trim()) {
      setResult({ error: "请先在文档列表选择要更新的逻辑文档" });
      return;
    }
    const form = new FormData(event.currentTarget);
    const file = form.get("versionFile");
    if (!(file instanceof File) || !file.size) {
      setResult({ error: "请选择新版本文件" });
      return;
    }
    if (file.size > MAX_UPLOAD_FILE_BYTES) {
      setResult({
        error: {
          code: "LOCAL-UPLOAD-001",
          message: `文件大小为 ${formatFileSize(file.size)}，超过 100 MiB 单文件上限`,
          retryable: false,
          details: [],
        },
      });
      return;
    }
    try {
      const metadata = String(form.get("versionMetadata") || "").trim();
      if (metadata) parseJsonObject(metadata);
      setBusy(true);
      const body = new FormData();
      body.set("file", file);
      body.set("mode", String(form.get("versionMode") || "AUTO"));
      if (metadata) body.set("metadata", metadata);
      for (const [formName, apiName] of [
        ["versionParserProfile", "parser_profile"],
        ["versionChunkProfile", "chunk_profile"],
      ] as const) {
        const value = String(form.get(formName) || "").trim();
        if (value) body.set(apiName, value);
      }
      // 输入框留空时自动生成；保留在页面中后，可用同一个 Key 重放完全相同的请求。
      const requestKey = versionIdempotencyKey.trim() || crypto.randomUUID();
      setVersionIdempotencyKey(requestKey);
      setLastMutationKey(requestKey);
      const response = await gatewayRequest<{
        data?: { job_id?: string; status?: string };
      }>(
        `v1/management/documents/${documentId.trim()}/versions`,
        { method: "POST", idempotencyKey: requestKey, body },
      );
      rememberResponse(response);
      if (response.data?.job_id) {
        setVersionJobWatch({
          jobId: response.data.job_id,
          documentId: documentId.trim(),
          operation: "UPLOAD_VERSION",
          status: String(response.data.status ?? "PENDING"),
        });
      }
      await listVersions(documentId.trim(), false);
    } catch (error) {
      setResult(payloadFrom(error));
    } finally {
      setBusy(false);
    }
  }

  async function rollbackVersion(versionId: string) {
    if (!documentId.trim()) return;
    if (!window.confirm("确认把这个退役版本重新设为活动版本？当前版本会保留并可再次回滚。")) return;
    setBusy(true);
    try {
      // 每次回滚都是新的业务动作，自动生成新 Key，并显示出来供诊断和重放。
      const requestKey = crypto.randomUUID();
      setLastMutationKey(requestKey);
      const response = await gatewayRequest<{
        data?: { job_id?: string; status?: string };
      }>(
        `v1/management/documents/${documentId.trim()}/versions/${versionId}/rollback`,
        { method: "POST", idempotencyKey: requestKey },
      );
      rememberResponse(response);
      if (response.data?.job_id) {
        setVersionJobWatch({
          jobId: response.data.job_id,
          documentId: documentId.trim(),
          operation: "ROLLBACK_VERSION",
          status: String(response.data.status ?? "PENDING"),
        });
      }
      await listVersions(documentId.trim(), false);
    } catch (error) {
      setResult(payloadFrom(error));
    } finally {
      setBusy(false);
    }
  }

  async function documentAction(
    id: string,
    action: "get" | "parse" | "reparse" | "retry" | "chunks" | "delete",
  ) {
    if (action === "delete" && !window.confirm("确认删除这个文档及其索引内容？")) return;
    setBusy(true);
    setDocumentId(id);
    setResources({ documentId: id });
    const suffix =
      action === "get" || action === "delete"
        ? ""
        : action === "chunks"
          ? "/chunks?page=1&page_size=100"
          : `/${action}`;
    const method =
      action === "get" || action === "chunks"
        ? "GET"
        : action === "delete"
          ? "DELETE"
          : "POST";
    try {
      const response = await gatewayRequest(`v1/management/documents/${id}${suffix}`, {
        method,
        idempotent: !["get", "chunks"].includes(action),
        body: action === "reparse" ? {} : undefined,
      });
      rememberResponse(response);
      if (action === "delete") await list(false);
    } catch (error) {
      setResult(payloadFrom(error));
    } finally {
      setBusy(false);
    }
  }

  async function inspectJob(cancel = false) {
    if (!jobId.trim()) {
      setResult({ error: "请输入 Job ID" });
      return;
    }
    setBusy(true);
    setResources({ jobId: jobId.trim() });
    try {
      const response = await gatewayRequest(
        `v1/management/jobs/${jobId.trim()}${cancel ? "/cancel" : ""}`,
        { method: cancel ? "POST" : "GET", idempotent: cancel },
      );
      rememberResponse(response);
    } catch (error) {
      setResult(payloadFrom(error));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <PageHead
        eyebrow="Ingestion"
        title="文档与解析任务"
        description="上传最小非业务 Fixture，跟踪解析进度，检查 Chunk，并验证重试、重解析、取消和删除链路。"
        actions={
          <button className="button secondary" onClick={() => void list()} disabled={busy || !knowledgeBaseId}>
            <RefreshCw size={15} /> 刷新文档
          </button>
        }
      />

      <Card title="目标知识库" description="文档列表与上传都作用于这个知识库 ID">
        <div className="toolbar" style={{ marginBottom: 0 }}>
          <input
            className="input"
            style={{ flex: 1 }}
            value={knowledgeBaseId}
            onChange={(event) => setKnowledgeBaseId(event.target.value)}
            placeholder="Knowledge Base UUID"
          />
          <button className="button secondary" onClick={() => void list()} disabled={!knowledgeBaseId || busy}>加载文档</button>
        </div>
      </Card>

      <div className="grid cols-2" style={{ marginTop: 17 }}>
        <Card title="上传测试文件" description="POST /v1/management/knowledge-bases/{id}/documents">
          <form onSubmit={upload}>
            <div className="field">
              <label>文件</label>
              <input
                className="input"
                name="file"
                type="file"
                required
                onChange={(event) => {
                  const file = event.currentTarget.files?.[0];
                  setSelectedFile(file ? `${file.name} · ${formatFileSize(file.size)}` : "");
                }}
              />
              <span className="hint">
                {selectedFile ? `${selectedFile}。` : ""}单文件上限 100 MiB；支持格式以系统能力接口为准。
              </span>
            </div>
            <div className="field-grid">
              <div className="field">
                <label>外部 ID（可选）</label>
                <input className="input" name="externalId" placeholder="fixture-doc-001" />
              </div>
              <div className="field">
                <label>自动解析</label>
                <label className="inline-note" style={{ alignItems: "center", minHeight: 44 }}>
                  <input type="checkbox" name="autoParse" defaultChecked /> 上传后立即创建解析任务
                </label>
              </div>
            </div>
            <div className="field">
              <label>Metadata JSON（可选）</label>
              <textarea className="textarea" name="metadata" defaultValue={'{"source":"local-ui-fixture","language":"zh-CN"}'} style={{ minHeight: 70 }} />
            </div>
            <div className="field-grid">
              <div className="field">
                <label>Parser 覆盖（可选）</label>
                <input className="input" name="parserProfile" placeholder="例如 office-default@1" />
                <span className="hint">
                  不填会继承知识库默认值；本验收使用的 .txt Fixture 必须使用 office-default@1。
                </span>
              </div>
              <div className="field">
                <label>Chunk 覆盖（可选）</label>
                <input className="input" name="chunkProfile" placeholder="general-256@2" />
                <span className="hint">
                  留空继承知识库设置；general-256@2 表示约 256 Token、相邻块重叠 10%。
                </span>
              </div>
            </div>
            <button className="button" disabled={busy || !knowledgeBaseId}>
              <FileUp size={15} /> 上传文件
            </button>
          </form>
        </Card>

        <Card title="任务检查" description="GET /v1/management/jobs/{id} · POST /v1/management/jobs/{id}/cancel">
          <div className="field">
            <label>Job ID</label>
            <input className="input" value={jobId} onChange={(event) => setJobId(event.target.value)} placeholder="解析或删除任务 UUID" />
          </div>
          <div className="toolbar">
            <button className="button secondary" onClick={() => void inspectJob(false)} disabled={busy || !jobId}>
              <Eye size={15} /> 查询任务
            </button>
            <button className="button danger" onClick={() => void inspectJob(true)} disabled={busy || !jobId}>
              <Ban size={15} /> 取消任务
            </button>
          </div>
          <JsonOutput value={result} busy={busy} />
        </Card>
      </div>

      <section className="card" style={{ marginTop: 17 }}>
        <div className="card-head">
          <div>
            <h2>知识库文档</h2>
            <p>选中文档：{documentId || "无"} · 每个动作都会保留完整响应</p>
          </div>
          <Status tone={items.some((item) => item.status === "INDEXED") ? "ok" : "neutral"}>
            {items.length} 个文档
          </Status>
        </div>
        {items.length ? (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr><th>文档</th><th>状态</th><th>解析结果</th><th>Profile</th><th>操作</th></tr>
              </thead>
              <tbody>
                {items.map((item) => (
                  <tr key={item.id}>
                    <td>
                      <div className="row-title">{item.filename}</div>
                      <div className="row-subtitle">{Math.ceil(item.file_size_bytes / 1024)} KB</div>
                      <code>{item.id}</code>
                    </td>
                    <td>
                      <Status tone={item.status === "INDEXED" ? "ok" : item.last_error ? "error" : "neutral"}>
                        {item.status}
                      </Status>
                      <div className="row-subtitle">{Math.round(item.progress * 100)}%</div>
                    </td>
                    <td>{item.chunk_count} chunks<div className="row-subtitle">{item.token_count} tokens</div></td>
                    <td><div className="row-subtitle">{item.parser_profile}</div><div className="row-subtitle">{item.chunk_profile}</div></td>
                    <td>
                      <div className="row-actions">
                        <button className="icon-button" title="详情" onClick={() => void documentAction(item.id, "get")}><Eye size={14} /></button>
                        <button className="icon-button" title="解析" onClick={() => void documentAction(item.id, "parse")}><Play size={14} /></button>
                        <button className="icon-button" title="重解析" onClick={() => void documentAction(item.id, "reparse")}><RotateCcw size={14} /></button>
                        <button className="icon-button" title="重试" onClick={() => void documentAction(item.id, "retry")}><RefreshCw size={14} /></button>
                        <button className="icon-button" title="查看 Chunks" onClick={() => void documentAction(item.id, "chunks")}><ListTree size={14} /></button>
                        <button className="icon-button" title="版本历史" onClick={() => void listVersions(item.id)}><FileCode2 size={14} /></button>
                        <button className="icon-button" title="删除" onClick={() => void documentAction(item.id, "delete")}><Trash2 size={14} /></button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty title="未加载到文档" description="输入知识库 ID 加载列表，或在上方上传一个最小非业务 Fixture。" />
        )}
      </section>

      <div className="grid cols-2" style={{ marginTop: 17 }}>
        <Card
          title="安全更新文档"
          description="POST /v1/management/documents/{id}/versions · 新版成功前旧版持续可检索"
        >
          <form onSubmit={uploadVersion}>
            <div className="field">
              <label>当前逻辑文档 ID</label>
              <input
                className="input"
                value={documentId}
                onChange={(event) => setDocumentId(event.target.value)}
                placeholder="先在上方文档列表点击“版本历史”"
              />
            </div>
            <div className="field">
              <label>新版本文件</label>
              <input
                className="input"
                name="versionFile"
                type="file"
                required
                onChange={(event) => {
                  const file = event.currentTarget.files?.[0];
                  setSelectedVersionFile(file ? `${file.name} · ${formatFileSize(file.size)}` : "");
                }}
              />
              <span className="hint">
                {selectedVersionFile ? `${selectedVersionFile}。` : ""}当前 AUTO 会明确协商为 SAFE_REPLACE；不会伪装成 Chunk 增量更新。
              </span>
            </div>
            <div className="field">
              <label>新版本 Metadata JSON（可选）</label>
              <textarea className="textarea" name="versionMetadata" placeholder={'{"version":"2"}'} style={{ minHeight: 70 }} />
              <span className="hint">留空时继承当前活动版本 Metadata；填写后由后端按版本更新规则处理。</span>
            </div>
            <div className="field-grid">
              <div className="field">
                <label>更新模式</label>
                <select className="input" name="versionMode" defaultValue="AUTO">
                  <option value="AUTO">AUTO（自动协商）</option>
                  <option value="SAFE_REPLACE">SAFE_REPLACE（安全整版替换）</option>
                </select>
                <span className="hint">当前 AUTO 会协商为 SAFE_REPLACE，不会声称执行了 Chunk 级增量索引。</span>
              </div>
              <div className="field">
                <label>Idempotency-Key</label>
                <div className="toolbar" style={{ marginBottom: 0 }}>
                  <input
                    className="input"
                    style={{ flex: 1 }}
                    value={versionIdempotencyKey}
                    onChange={(event) => setVersionIdempotencyKey(event.target.value)}
                    placeholder="留空则提交时自动生成 UUID"
                  />
                  <button
                    className="button secondary"
                    type="button"
                    onClick={() => setVersionIdempotencyKey(crypto.randomUUID())}
                  >
                    生成
                  </button>
                </div>
                <span className="hint">重发完全相同请求时保留此值；发起另一项业务操作前请生成新值。</span>
              </div>
            </div>
            <div className="field-grid">
              <div className="field">
                <label>Parser 覆盖（可选）</label>
                <input className="input" name="versionParserProfile" placeholder="例如 deepdoc-default@1" />
              </div>
              <div className="field">
                <label>Chunk 覆盖（可选）</label>
                <input className="input" name="versionChunkProfile" placeholder="例如 prose-384@1" />
              </div>
            </div>
            {lastMutationKey ? <div className="inline-note">最近发送的 Idempotency-Key：<code>{lastMutationKey}</code></div> : null}
            {versionJobWatch ? (
              <div className="inline-note">
                自动跟踪 {versionJobWatch.operation}：<code>{versionJobWatch.jobId}</code> · {versionJobWatch.status}
              </div>
            ) : null}
            <div className="toolbar">
              <button className="button" disabled={busy || !documentId}>
                <FileUp size={15} /> 上传候选版本
              </button>
              <button className="button secondary" type="button" onClick={() => void listVersions()} disabled={busy || !documentId}>
                <RefreshCw size={15} /> 刷新版本历史
              </button>
            </div>
          </form>
        </Card>

        <Card title="版本历史" description="GET /v1/management/documents/{id}/versions">
          {versions.length ? (
            <div className="table-wrap">
              <table className="table">
                <thead><tr><th>版本与文件</th><th>状态</th><th>Profile</th><th>解析结果</th><th>时间</th><th>操作</th></tr></thead>
                <tbody>
                  {versions.map((version) => (
                    <tr key={version.id}>
                      <td>
                        <div className="row-title">v{version.version_number} · {version.filename}</div>
                        <div className="row-subtitle">{formatFileSize(version.file_size_bytes)} · {version.mime_type}</div>
                        <code>{version.id}</code>
                        <div className="row-subtitle">SHA-256：<code>{version.content_sha256}</code></div>
                      </td>
                      <td>
                        <Status tone={version.status === "ACTIVE" ? "ok" : version.status === "FAILED" ? "error" : "neutral"}>{version.status}</Status>
                        <div className="row-subtitle">请求 {version.requested_mode} → 实际 {version.effective_mode}</div>
                        {version.last_error ? <div className="row-subtitle">错误：{JSON.stringify(version.last_error)}</div> : null}
                      </td>
                      <td>
                        <div className="row-subtitle">{version.parser_profile}</div>
                        <div className="row-subtitle">{version.chunk_profile}</div>
                      </td>
                      <td>{version.chunk_count} chunks<div className="row-subtitle">{version.token_count} tokens</div></td>
                      <td>
                        <div className="row-subtitle">创建：{formatDate(version.created_at)}</div>
                        <div className="row-subtitle">激活：{formatDate(version.activated_at)}</div>
                        <div className="row-subtitle">退役：{formatDate(version.retired_at)}</div>
                      </td>
                      <td>
                        {version.status === "RETIRED" ? (
                          <button className="icon-button" title="回滚到此版本" onClick={() => void rollbackVersion(version.id)}><RotateCcw size={14} /></button>
                        ) : <span className="row-subtitle">—</span>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <Empty title="尚未加载版本历史" description="从上方文档列表选择文档，或输入逻辑文档 ID 后刷新。" />
          )}
        </Card>
      </div>

      {result ? (
        <div style={{ marginTop: 17 }}>
          <Card title="文档操作原始响应" action={<FileCode2 size={16} color="#70817b" />}>
            <JsonOutput value={result} busy={busy} />
          </Card>
        </div>
      ) : null}
    </>
  );
}
