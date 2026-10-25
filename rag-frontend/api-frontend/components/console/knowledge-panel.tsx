"use client";

/** 知识库管理面板：创建、筛选、查看、修改 Profile 并发起异步删除。 */

import { BookOpen, Eye, Pencil, Plus, RefreshCw, Trash2 } from "lucide-react";
import { FormEvent, useCallback, useEffect, useState } from "react";
import { gatewayRequest, payloadFrom } from "@/lib/client-api";
import { Card, Empty, JsonOutput, PageHead, Status } from "./shared";
import type { ResourceIds } from "./use-resources";

type KnowledgeBase = {
  id: string;
  scope_type: "TENANT" | "SYSTEM";
  name: string;
  description?: string;
  status: string;
  revision: number;
  parser_profile: string;
  chunk_profile: string;
  embedding_profile: string;
  retrieval_profile: string;
  document_count?: number | null;
  created_at: string;
};

type Capabilities = {
  parser_profiles?: string[];
  chunk_profiles?: string[];
  retrieval_profiles?: string[];
};

export function KnowledgePanel({
  resources,
  setResources,
  navigate,
}: {
  resources: ResourceIds;
  setResources: (patch: Partial<ResourceIds>) => void;
  navigate: (target: string) => void;
}) {
  const [items, setItems] = useState<KnowledgeBase[]>([]);
  const [capabilities, setCapabilities] = useState<Capabilities>({});
  const [result, setResult] = useState<unknown>();
  const [busy, setBusy] = useState(false);
  const [query, setQuery] = useState("");
  const [editing, setEditing] = useState<KnowledgeBase | null>(null);

  const load = useCallback(async () => {
    setBusy(true);
    try {
      const suffix = query ? `&name=${encodeURIComponent(query)}` : "";
      const [page, capabilityResponse] = await Promise.all([
        gatewayRequest<{ data: KnowledgeBase[] }>(
          `v1/management/knowledge-bases?page=1&page_size=100${suffix}`,
        ),
        gatewayRequest<{ data: Capabilities }>("v1/capabilities"),
      ]);
      setItems(page.data ?? []);
      setCapabilities(capabilityResponse.data ?? {});
      setResult(page);
    } catch (error) {
      setResult(payloadFrom(error));
    } finally {
      setBusy(false);
    }
  }, [query]);

  useEffect(() => {
    const timeout = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(timeout);
  }, [load]);

  async function create(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    const formElement = event.currentTarget;
    const form = new FormData(formElement);
    try {
      const response = await gatewayRequest<{ data: KnowledgeBase }>(
        "v1/management/knowledge-bases",
        {
          method: "POST",
          idempotent: true,
          body: {
            scope_type: String(form.get("scopeType")),
            external_id: String(form.get("externalId") || "") || null,
            name: String(form.get("name")),
            description: String(form.get("description") || "") || null,
            parser_profile: String(form.get("parserProfile")),
            chunk_profile: String(form.get("chunkProfile")),
            embedding_profile: String(form.get("embeddingProfile")),
            retrieval_profile: String(form.get("retrievalProfile")),
          },
        },
      );
      setResources({ knowledgeBaseId: response.data.id });
      setResult(response);
      formElement.reset();
      await load();
    } catch (error) {
      setResult(payloadFrom(error));
    } finally {
      setBusy(false);
    }
  }

  async function update(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!editing) return;
    setBusy(true);
    const form = new FormData(event.currentTarget);
    try {
      const response = await gatewayRequest(`v1/management/knowledge-bases/${editing.id}`, {
        method: "PATCH",
        body: {
          name: String(form.get("name")),
          description: String(form.get("description") || "") || null,
          parser_profile: String(form.get("parserProfile")),
          chunk_profile: String(form.get("chunkProfile")),
          retrieval_profile: String(form.get("retrievalProfile")),
        },
      });
      setResult(response);
      setEditing(null);
      await load();
    } catch (error) {
      setResult(payloadFrom(error));
    } finally {
      setBusy(false);
    }
  }

  async function inspect(id: string) {
    setBusy(true);
    setResources({ knowledgeBaseId: id });
    try {
      setResult(await gatewayRequest(`v1/management/knowledge-bases/${id}`));
    } catch (error) {
      setResult(payloadFrom(error));
    } finally {
      setBusy(false);
    }
  }

  async function remove(id: string) {
    if (!window.confirm("确认删除该知识库？有关联文档时可在 API 控制台使用 force=true。")) return;
    setBusy(true);
    try {
      const response = await gatewayRequest(`v1/management/knowledge-bases/${id}`, {
        method: "DELETE",
        idempotent: true,
      });
      const jobId = String((response as { data?: { job_id?: string } }).data?.job_id ?? "");
      setResources({ knowledgeBaseId: "", jobId });
      setResult(response);
      await load();
    } catch (error) {
      setResult(payloadFrom(error));
    } finally {
      setBusy(false);
    }
  }

  const parserProfiles = capabilities.parser_profiles?.length
    ? capabilities.parser_profiles
    : ["deepdoc-default@1", "office-default@1"];
  const chunkProfiles = capabilities.chunk_profiles?.length
    ? capabilities.chunk_profiles
    : ["general-256@2"];
  const retrievalProfiles = capabilities.retrieval_profiles?.length
    ? capabilities.retrieval_profiles
    : ["hybrid-default@2", "hybrid-default@1"];

  return (
    <>
      <PageHead
        eyebrow="Knowledge"
        title="知识库管理"
        description="统一管理租户共享与系统共享知识库。Embedding Profile 固定使用启动时已验证的通用配置。"
        actions={
          <button className="button secondary" onClick={() => void load()} disabled={busy}>
            <RefreshCw size={15} /> 刷新
          </button>
        }
      />

      <div className="grid cols-2">
        <Card title="创建知识库" description="POST /v1/management/knowledge-bases">
          <form onSubmit={create}>
            <div className="field-grid">
              <div className="field">
                <label>共享范围</label>
                <select className="select" name="scopeType" defaultValue="TENANT">
                  <option value="TENANT">当前租户共享</option>
                  <option value="SYSTEM">全局系统共享</option>
                </select>
              </div>
              <div className="field">
                <label>名称</label>
                <input className="input" name="name" required placeholder="本地测试知识库" />
              </div>
              <div className="field">
                <label>外部 ID（可选）</label>
                <input className="input" name="externalId" placeholder="fixture-kb-001" />
              </div>
            </div>
            <div className="field">
              <label>描述</label>
              <textarea className="textarea" name="description" placeholder="说明这个测试知识库的用途" style={{ minHeight: 76 }} />
            </div>
            <div className="field-grid">
              <div className="field">
                <label>Parser Profile</label>
                <select
                  className="select"
                  name="parserProfile"
                  defaultValue={
                    parserProfiles.includes("office-default@1")
                      ? "office-default@1"
                      : parserProfiles[0]
                  }
                >
                  {parserProfiles.map((profile) => <option key={profile}>{profile}</option>)}
                </select>
              </div>
              <div className="field">
                <label>Chunk Profile</label>
                {/* 新建知识库显式选择 256 Token + 10% 重叠版本；旧版本仍保留在选项中，
                    便于查看和回滚历史知识库，不能依赖服务端返回数组的排序决定默认值。 */}
                <select
                  className="select"
                  name="chunkProfile"
                  defaultValue={
                    chunkProfiles.includes("general-256@2")
                      ? "general-256@2"
                      : chunkProfiles[0]
                  }
                >
                  {chunkProfiles.map((profile) => <option key={profile}>{profile}</option>)}
                </select>
              </div>
              <div className="field">
                <label>Embedding Profile</label>
                <input className="input" name="embeddingProfile" readOnly value="embedding-openai-compatible@3" />
              </div>
              <div className="field">
                <label>Retrieval Profile</label>
                <select className="select" name="retrievalProfile">
                  {retrievalProfiles.map((profile) => <option key={profile}>{profile}</option>)}
                </select>
              </div>
            </div>
            <button className="button" disabled={busy}><Plus size={15} /> 创建知识库</button>
          </form>
        </Card>

        {editing ? (
          <Card title="编辑知识库" description={`PATCH /v1/management/knowledge-bases/${editing.id}`}>
            <form onSubmit={update}>
              <div className="field">
                <label>名称</label>
                <input className="input" name="name" required defaultValue={editing.name} />
              </div>
              <div className="field">
                <label>描述</label>
                <textarea className="textarea" name="description" defaultValue={editing.description ?? ""} style={{ minHeight: 70 }} />
              </div>
              <div className="field-grid">
                <div className="field">
                  <label>Parser Profile</label>
                  <select className="select" name="parserProfile" defaultValue={editing.parser_profile}>
                    {parserProfiles.map((profile) => <option key={profile}>{profile}</option>)}
                  </select>
                </div>
                <div className="field">
                  <label>Chunk Profile</label>
                  <select className="select" name="chunkProfile" defaultValue={editing.chunk_profile}>
                    {chunkProfiles.map((profile) => <option key={profile}>{profile}</option>)}
                  </select>
                </div>
              </div>
              <div className="field">
                <label>Retrieval Profile</label>
                <select className="select" name="retrievalProfile" defaultValue={editing.retrieval_profile}>
                  {retrievalProfiles.map((profile) => <option key={profile}>{profile}</option>)}
                </select>
              </div>
              <div className="toolbar" style={{ marginBottom: 0 }}>
                <button className="button" disabled={busy}>保存修改</button>
                <button className="button secondary" type="button" onClick={() => setEditing(null)}>取消</button>
              </div>
            </form>
          </Card>
        ) : (
          <Card title="最近一次响应" description="保留完整 request_id、data 与分页信息">
            <JsonOutput value={result} busy={busy} />
          </Card>
        )}
      </div>

      <section className="card" style={{ marginTop: 17 }}>
        <div className="card-head">
          <div>
            <h2>知识库列表</h2>
            <p>GET /v1/management/knowledge-bases · 当前选中：{resources.knowledgeBaseId || "无"}</p>
          </div>
          <div className="toolbar" style={{ margin: 0 }}>
            <input className="input" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="按名称筛选" />
            <button className="button secondary small" onClick={() => void load()}>查询</button>
          </div>
        </div>
        {items.length ? (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr><th>知识库</th><th>共享范围</th><th>状态</th><th>Profiles</th><th>Revision</th><th>操作</th></tr>
              </thead>
              <tbody>
                {items.map((item) => (
                  <tr key={item.id}>
                    <td>
                      <div className="row-title">{item.name}</div>
                      <div className="row-subtitle">{item.description || "无描述"}</div>
                      <code>{item.id}</code>
                    </td>
                    <td>{item.scope_type === "SYSTEM" ? "系统共享" : "租户共享"}</td>
                    <td><Status tone={item.status === "ACTIVE" ? "ok" : "neutral"}>{item.status}</Status></td>
                    <td>
                      <div className="row-subtitle">{item.parser_profile}</div>
                      <div className="row-subtitle">{item.chunk_profile}</div>
                      <div className="row-subtitle">{item.embedding_profile}</div>
                    </td>
                    <td>
                      {item.revision}
                      <div className="row-subtitle">
                        {item.document_count == null ? "文档数未统计" : `${item.document_count} 个文档`}
                      </div>
                    </td>
                    <td>
                      <div className="row-actions">
                        <button className="icon-button" title="查看" onClick={() => void inspect(item.id)}><Eye size={14} /></button>
                        <button className="icon-button" title="编辑" onClick={() => { setEditing(item); setResources({ knowledgeBaseId: item.id }); }}><Pencil size={14} /></button>
                        <button className="icon-button" title="管理文档" onClick={() => { setResources({ knowledgeBaseId: item.id }); navigate("documents"); }}><BookOpen size={14} /></button>
                        <button className="icon-button" title="删除" onClick={() => void remove(item.id)}><Trash2 size={14} /></button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty title="这个 Scope 还没有知识库" description="在上方创建一个最小测试知识库，然后进入文档管理上传非业务 Fixture。" />
        )}
      </section>
    </>
  );
}
