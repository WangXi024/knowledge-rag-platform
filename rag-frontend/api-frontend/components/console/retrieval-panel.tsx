"use client";

/** 检索面板：配置知识库/文档范围、元数据过滤和返回选项，展示候选与各路分数。 */

import { Filter, Search, SlidersHorizontal } from "lucide-react";
import { FormEvent, useState } from "react";
import {
  gatewayRequest,
  parseJsonObject,
  payloadFrom,
  splitIds,
} from "@/lib/client-api";
import { Card, Empty, JsonOutput, PageHead, Status } from "./shared";
import type { ResourceIds } from "./use-resources";

type Candidate = {
  chunk_id: string;
  document_name: string;
  document_id: string;
  content: string;
  heading_path?: string[];
  scores?: {
    score?: number;
    vector_score?: number | null;
    term_score?: number | null;
    rerank_score?: number | null;
  };
  metadata?: {
    gateway_retrieval_routes?: string[];
  };
};

type RerankState = "unknown" | "enabled" | "fallback";

export function RetrievalPanel({
  resources,
}: {
  resources: ResourceIds;
}) {
  const [knowledgeBaseIds, setKnowledgeBaseIds] = useState(resources.knowledgeBaseId);
  const [result, setResult] = useState<unknown>();
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [busy, setBusy] = useState(false);
  const [rerankState, setRerankState] = useState<RerankState>("unknown");

  async function retrieve(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    setBusy(true);
    try {
      const metadata = String(form.get("metadataFilter") || "");
      const response = await gatewayRequest<{
        data: { candidates?: Candidate[]; warnings?: string[] };
      }>("v1/retrieval", {
        method: "POST",
        body: {
          knowledge_base_ids: splitIds(knowledgeBaseIds),
          document_ids: splitIds(String(form.get("documentIds") || "")),
          query: String(form.get("query")),
          metadata_filter: metadata ? parseJsonObject(metadata) : null,
          retrieval_profile: String(form.get("retrievalProfile")),
          options: {
            page_size: Number(form.get("pageSize")),
            include_content: Boolean(form.get("includeContent")),
            include_positions: Boolean(form.get("includePositions")),
          },
        },
      });
      const nextCandidates = response.data?.candidates ?? [];
      const warnings = response.data?.warnings ?? [];
      setCandidates(nextCandidates);
      // 以本次真实响应为准展示 Rerank：任一候选有 Cross-Encoder 分数即为启用；
      // 明确出现 rerank fallback Warning 则显示降级，避免继续写死“本地关闭”。
      setRerankState(
        nextCandidates.some((candidate) => candidate.scores?.rerank_score != null)
          ? "enabled"
          : warnings.some((warning) => warning.toLowerCase().includes("rerank"))
            ? "fallback"
            : "unknown",
      );
      setResult(response);
    } catch (error) {
      setCandidates([]);
      setRerankState("unknown");
      setResult(payloadFrom(error));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <PageHead
        eyebrow="Retrieval"
        title="混合检索实验室"
        description="验证向量检索、BM25、RRF 与可选 Cross-Encoder Rerank；外部服务故障时应稳定降级且不返回 500。"
      />

      <div className="grid cols-2">
        <Card title="检索参数" description="POST /v1/retrieval">
          <form onSubmit={retrieve}>
            <div className="field">
              <label>Knowledge Base IDs</label>
              <textarea className="textarea" value={knowledgeBaseIds} onChange={(event) => setKnowledgeBaseIds(event.target.value)} required placeholder="多个 UUID 可用逗号或换行分隔" style={{ minHeight: 68 }} />
            </div>
            <div className="field">
              <label>问题</label>
              <textarea className="textarea" name="query" required placeholder="这份测试文档主要说明了什么？" />
            </div>
            <div className="field">
              <label>Document IDs（可选）</label>
              <input className="input" name="documentIds" placeholder="限制到指定文档 UUID" />
            </div>
            <div className="field-grid">
              <div className="field">
                <label>Retrieval Profile</label>
                <input className="input" name="retrievalProfile" defaultValue="hybrid-default@2" required />
              </div>
              <div className="field">
                <label>返回条数</label>
                <input className="input" name="pageSize" type="number" min={1} max={50} defaultValue={10} />
              </div>
            </div>
            <div className="field">
              <label>Metadata Filter JSON（可选）</label>
              <textarea
                className="textarea"
                name="metadataFilter"
                placeholder={'{"logic":"and","conditions":[{"field":"language","operator":"eq","value":"zh-CN"}]}'}
                style={{ minHeight: 74 }}
              />
            </div>
            <div className="toolbar">
              <label className="inline-note"><input type="checkbox" name="includeContent" defaultChecked /> 返回正文</label>
              <label className="inline-note"><input type="checkbox" name="includePositions" defaultChecked /> 返回位置</label>
            </div>
            <button className="button" disabled={busy || !knowledgeBaseIds.trim()}>
              <Search size={15} /> 执行混合检索
            </button>
          </form>
        </Card>

        <Card
          title="融合策略"
          description="以本次检索响应中的真实分数和 Warning 为准"
          action={
            <Status tone={rerankState === "enabled" ? "ok" : rerankState === "fallback" ? "warning" : "neutral"}>
              {rerankState === "enabled" ? "Rerank 已启用" : rerankState === "fallback" ? "Rerank 已降级" : "等待检索"}
            </Status>
          }
        >
          <div className="coverage-list">
            {[
              ["向量语义召回", "启动时已验证的模型与维度", "ok"],
              ["BM25 全文召回", "关键词与精确术语", "ok"],
              ["RRF 融合", "合并两路候选", "ok"],
              [
                "Cross-Encoder Rerank",
                rerankState === "enabled" ? "兼容端点真实重排" : rerankState === "fallback" ? "故障时保留 RRF 排名" : "执行检索后确认",
                rerankState === "enabled" ? "ok" : "warning",
              ],
            ].map(([title, detail, tone]) => (
              <div className="coverage-item" key={title}>
                <span style={{ display: "flex", gap: 10, alignItems: "center" }}>
                  <span className="metric-icon" style={{ width: 32, height: 32 }}>
                    {title.includes("Rerank") ? <SlidersHorizontal size={15} /> : title.includes("BM25") ? <Filter size={15} /> : <Search size={15} />}
                  </span>
                  <span><strong style={{ display: "block", fontSize: 11 }}>{title}</strong><span className="hint">{detail}</span></span>
                </span>
                <Status tone={tone as "ok" | "warning"}>{tone === "ok" ? "启用" : rerankState === "unknown" && title.includes("Rerank") ? "待确认" : "降级"}</Status>
              </div>
            ))}
          </div>
          <div className="divider" />
          <JsonOutput value={result} busy={busy} />
        </Card>
      </div>

      <section className="card" style={{ marginTop: 17 }}>
        <div className="card-head">
          <div><h2>候选 Chunks</h2><p>按融合得分展示，并分别保留向量、词项与 Rerank 分数</p></div>
          <Status tone={candidates.length ? "ok" : "neutral"}>{candidates.length} 条</Status>
        </div>
        {candidates.length ? (
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th>来源</th><th>内容</th><th>融合分</th><th>向量 / BM25 / Rerank</th></tr></thead>
              <tbody>
                {candidates.map((candidate) => (
                  <tr key={candidate.chunk_id}>
                    <td>
                      <div className="row-title">{candidate.document_name}</div>
                      <code>{candidate.chunk_id}</code>
                      <div className="row-subtitle">{candidate.heading_path?.join(" / ")}</div>
                      {/* 完整路线来自 Gateway 候选 Metadata。它只用于开发验收，帮助
                          判断同一块是否同时被向量、BM25、摘要、实体或 Parent 命中。 */}
                      {candidate.metadata?.gateway_retrieval_routes?.length ? (
                        <div className="row-subtitle" style={{ marginTop: 6 }}>
                          路线：{candidate.metadata.gateway_retrieval_routes.join(" · ")}
                        </div>
                      ) : null}
                    </td>
                    <td><div className="row-subtitle" style={{ color: "#43504c", maxWidth: 550 }}>{candidate.content}</div></td>
                    <td>{candidate.scores?.score?.toFixed(4) ?? "—"}</td>
                    <td>
                      <div className="row-subtitle">V {candidate.scores?.vector_score?.toFixed(4) ?? "—"}</div>
                      <div className="row-subtitle">B {candidate.scores?.term_score?.toFixed(4) ?? "—"}</div>
                      <div className="row-subtitle">R {candidate.scores?.rerank_score?.toFixed(4) ?? (rerankState === "fallback" ? "已降级" : "—")}</div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty title="等待一次真实检索" description="选择已完成解析的知识库，输入问题后执行检索；结果会在这里按候选展示。" />
        )}
      </section>
    </>
  );
}
