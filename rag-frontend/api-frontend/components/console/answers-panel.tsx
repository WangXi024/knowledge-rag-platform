"use client";

/** 问答面板：支持历史消息、非流式/流式答案、引用、取消和原始响应诊断。 */

import {
  Ban,
  Bot,
  CircleStop,
  MessageSquareText,
  Radio,
  Send,
  Sparkles,
} from "lucide-react";
import { FormEvent, useRef, useState } from "react";
import {
  directIdentityHeaders,
  gatewayRequest,
  parseJsonObject,
  payloadFrom,
  splitIds,
} from "@/lib/client-api";
import { Card, Empty, JsonOutput, PageHead, Status } from "./shared";
import type { ResourceIds } from "./use-resources";

type Citation = {
  citation_id: string;
  label: string;
  document_name: string;
  quote: string;
  page_start?: number;
};

type StreamEvent = {
  event: string;
  data: Record<string, unknown>;
};

/**
 * 记录“用户原话”和“真正用于检索的查询”之间的关系及动态检索预算。
 * 例如中文问题会转成英文检索问题，以便命中英文知识库；问题类型与三阶段上限则
 * 说明本次请求为何采用较宽或较窄的召回范围，避免开发人员把最终候选数误当 Top-K。
 */
type QueryTrace = {
  original: string;
  effective: string;
  detectedLanguage?: string;
  variants?: Record<string, string>;
  knowledgeLanguages?: string[];
  routes?: string[];
  queryType?: "fact" | "list" | "comparison" | "summary" | "multi_hop";
  retrievalTopK?: number;
  rerankTopN?: number;
  contextTopN?: number;
};

function parseMessages(value: string): Array<{ role: string; content: string }> {
  if (!value.trim()) return [];
  const parsed = JSON.parse(value) as unknown;
  if (!Array.isArray(parsed)) throw new Error("历史消息必须是 JSON 数组");
  return parsed as Array<{ role: string; content: string }>;
}

export function AnswersPanel({
  resources,
  setResources,
}: {
  resources: ResourceIds;
  setResources: (patch: Partial<ResourceIds>) => void;
}) {
  const [knowledgeBaseIds, setKnowledgeBaseIds] = useState(resources.knowledgeBaseId);
  const [mode, setMode] = useState<"stream" | "rest">("stream");
  const [answer, setAnswer] = useState("");
  const [citations, setCitations] = useState<Citation[]>([]);
  const [events, setEvents] = useState<StreamEvent[]>([]);
  const [result, setResult] = useState<unknown>();
  const [queryTrace, setQueryTrace] = useState<QueryTrace>();
  const [busy, setBusy] = useState(false);
  const [requestId, setRequestId] = useState(resources.requestId);
  const controllerRef = useRef<AbortController | null>(null);

  function requestPayload(form: FormData) {
    const metadata = String(form.get("metadataFilter") || "");
    return {
      knowledge_base_ids: splitIds(knowledgeBaseIds),
      query: String(form.get("query")),
      messages: parseMessages(String(form.get("messages") || "")),
      metadata_filter: metadata ? parseJsonObject(metadata) : null,
      retrieval_profile: String(form.get("retrievalProfile")),
      generation_profile: String(form.get("generationProfile")),
      response_language: String(form.get("responseLanguage")),
    };
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setAnswer("");
    setCitations([]);
    setEvents([]);
    setResult(undefined);
    setQueryTrace(undefined);
    const form = new FormData(event.currentTarget);
    try {
      const payload = requestPayload(form);
      setQueryTrace({ original: String(payload.query), effective: "" });
      if (mode === "rest") {
        const response = await gatewayRequest<{
          request_id: string;
          data: {
            answer?: string;
            citations?: Citation[];
            original_query?: string;
            effective_query?: string;
            query_plan?: {
              detected_language?: string;
              variants?: Record<string, string>;
              knowledge_languages?: string[];
              retrieval_routes?: string[];
              query_type?: QueryTrace["queryType"];
              retrieval_top_k?: number;
              rerank_top_n?: number;
              context_top_n?: number;
            };
          };
        }>("v1/answers", { method: "POST", body: payload });
        setAnswer(response.data.answer ?? "");
        setCitations(response.data.citations ?? []);
        setQueryTrace({
          original: response.data.original_query ?? String(payload.query),
          effective: response.data.effective_query ?? String(payload.query),
          detectedLanguage: response.data.query_plan?.detected_language,
          variants: response.data.query_plan?.variants,
          knowledgeLanguages: response.data.query_plan?.knowledge_languages,
          routes: response.data.query_plan?.retrieval_routes,
          queryType: response.data.query_plan?.query_type,
          retrievalTopK: response.data.query_plan?.retrieval_top_k,
          rerankTopN: response.data.query_plan?.rerank_top_n,
          contextTopN: response.data.query_plan?.context_top_n,
        });
        setRequestId(response.request_id);
        setResources({ requestId: response.request_id });
        setResult(response);
      } else {
        await stream(payload);
      }
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") {
        setResult({ status: "本地流已停止", request_id: requestId || null });
      } else {
        setResult(payloadFrom(error));
      }
    } finally {
      setBusy(false);
      controllerRef.current = null;
    }
  }

  async function stream(payload: Record<string, unknown>) {
    const controller = new AbortController();
    controllerRef.current = controller;
    const response = await fetch("/api/gateway/v1/answers/stream", {
      method: "POST",
      headers: directIdentityHeaders({
        accept: "text/event-stream",
        "content-type": "application/json",
      }),
      body: JSON.stringify(payload),
      signal: controller.signal,
    });
    if (!response.ok || !response.body) {
      const failed = await response.text();
      throw new Error(failed || `流式请求失败 (${response.status})`);
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let fullAnswer = "";
    const collected: StreamEvent[] = [];
    while (true) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value ?? new Uint8Array(), { stream: !done });
      const blocks = buffer.split(/\r?\n\r?\n/u);
      buffer = blocks.pop() ?? "";
      for (const block of blocks) {
        const eventName =
          block
            .split(/\r?\n/u)
            .find((line) => line.startsWith("event:"))
            ?.slice(6)
            .trim() ?? "message";
        const dataText = block
          .split(/\r?\n/u)
          .filter((line) => line.startsWith("data:"))
          .map((line) => line.slice(5).trimStart())
          .join("\n");
        if (!dataText) continue;
        const data = JSON.parse(dataText) as Record<string, unknown>;
        const streamEvent = { event: eventName, data };
        collected.push(streamEvent);
        setEvents([...collected]);
        if (data.request_id) {
          const id = String(data.request_id);
          setRequestId(id);
          setResources({ requestId: id });
        }
        if (eventName === "answer.delta" && data.delta) {
          fullAnswer += String(data.delta);
          setAnswer(fullAnswer);
        }
        if (eventName === "citation" && data.citation) {
          setCitations((current) => [...current, data.citation as Citation]);
        }
        if (eventName === "retrieval.completed") {
          const original = data.original_query;
          const effective = data.effective_query;
          const plan = data.query_plan as {
            detected_language?: string;
            variants?: Record<string, string>;
            knowledge_languages?: string[];
            retrieval_routes?: string[];
            query_type?: QueryTrace["queryType"];
            retrieval_top_k?: number;
            rerank_top_n?: number;
            context_top_n?: number;
          } | undefined;
          if (typeof original === "string" && typeof effective === "string") {
            setQueryTrace({
              original,
              effective,
              detectedLanguage: plan?.detected_language,
              variants: plan?.variants,
              knowledgeLanguages: plan?.knowledge_languages,
              routes: plan?.retrieval_routes,
              queryType: plan?.query_type,
              retrievalTopK: plan?.retrieval_top_k,
              rerankTopN: plan?.rerank_top_n,
              contextTopN: plan?.context_top_n,
            });
          }
        }
        if (eventName === "error") setResult(data);
        if (eventName === "completed") setResult(data);
      }
      if (done) break;
    }
    if (!collected.some((item) => item.event === "completed") && !result) {
      setResult({ request_id: requestId, events: collected.length, status: "stream_closed" });
    }
  }

  async function cancel() {
    controllerRef.current?.abort();
    if (!requestId) return;
    try {
      const response = await gatewayRequest(`v1/requests/${requestId}/cancel`, {
        method: "POST",
        idempotent: true,
      });
      setResult(response);
    } catch (error) {
      setResult(payloadFrom(error));
    }
  }

  return (
    <>
      <PageHead
        eyebrow="Generation"
        title="问答实验室"
        description="最终答案由自研 Gateway 调用当前配置的 Chat 模型生成。可在普通 JSON 与 SSE 流式模式之间切换，并检查引用、缓存与取消传播。"
        actions={
          <div className="toolbar" style={{ margin: 0 }}>
            <button className={`button ${mode === "rest" ? "" : "secondary"}`} onClick={() => setMode("rest")}><MessageSquareText size={14} /> 普通</button>
            <button className={`button ${mode === "stream" ? "" : "secondary"}`} onClick={() => setMode("stream")}><Radio size={14} /> SSE 流式</button>
          </div>
        }
      />

      <div className="chat-layout">
        <section className="card chat-window">
          <div className="card-head">
            <div><h2>RAG 对话</h2><p>{mode === "stream" ? "实时接收 answer.delta、citation、usage 与 completed 事件" : "一次返回完整 AnswerResponse"}</p></div>
            {busy ? <Status tone="warning">生成中</Status> : <Status tone="neutral">待命</Status>}
          </div>
          <div className="chat-messages">
            {!answer && !busy ? (
              <Empty title="提出一个有依据的问题" description="回答必须基于已解析文档并带有效引用；无依据时应明确拒答。" />
            ) : (
              <div className="message">
                <div className="message-label">RAG ASSISTANT · QWEN3.7-MAX</div>
                <div className="message-bubble">{answer || "正在检索证据…"}</div>
              </div>
            )}
          </div>
          <form className="chat-composer" onSubmit={submit}>
            <div className="field-grid">
              <div className="field">
                <label>Knowledge Base IDs</label>
                <input className="input" value={knowledgeBaseIds} onChange={(event) => setKnowledgeBaseIds(event.target.value)} required placeholder="UUID，多个用逗号分隔" />
              </div>
              <div className="field">
                <label>响应语言</label>
                <select className="select" name="responseLanguage" defaultValue="zh-CN">
                  <option value="zh-CN">简体中文</option>
                  <option value="en-US">English</option>
                </select>
                <div className="hint">仅控制最终答案和引用修复语言；检索仍自动使用中英双路查询。</div>
              </div>
            </div>
            <div className="field">
              <label>问题</label>
              <textarea className="textarea" name="query" required placeholder="根据知识库说明，系统的默认检索策略是什么？" />
            </div>
            <details>
              <summary className="hint" style={{ cursor: "pointer", marginBottom: 12 }}>高级参数：Profile、历史与 Metadata Filter</summary>
              <div className="field-grid">
                <div className="field">
                  <label>Retrieval Profile</label>
                  <input className="input" name="retrievalProfile" defaultValue="hybrid-default@2" />
                </div>
                <div className="field">
                  <label>Generation Profile</label>
                  <input className="input" name="generationProfile" defaultValue="answer-openai-compatible@4" />
                </div>
              </div>
              <div className="field">
                <label>历史消息 JSON</label>
                <textarea className="textarea" name="messages" placeholder={'[{"role":"user","content":"上一轮问题"},{"role":"assistant","content":"上一轮回答"}]'} style={{ minHeight: 68 }} />
              </div>
              <div className="field">
                <label>Metadata Filter JSON</label>
                <textarea className="textarea" name="metadataFilter" placeholder={'{"logic":"and","conditions":[] }'} style={{ minHeight: 68 }} />
              </div>
            </details>
            <div className="composer-row">
              <button className="button" disabled={busy || !knowledgeBaseIds.trim()}>
                {busy ? <span className="spinner" /> : <Send size={15} />}
                {mode === "stream" ? "开始流式回答" : "生成完整回答"}
              </button>
              {busy ? (
                <button className="button danger" type="button" onClick={() => void cancel()}>
                  <CircleStop size={15} /> 停止并取消
                </button>
              ) : null}
            </div>
          </form>
        </section>

        <div className="grid" style={{ alignContent: "start" }}>
          <Card title="请求状态" description={requestId ? `Request ${requestId}` : "提交后自动记录 Request ID"} action={<Sparkles size={16} color="#70817b" />}>
            <div className="coverage-list">
              <div className="coverage-item"><span>传输模式</span><Status>{mode === "stream" ? "SSE" : "JSON"}</Status></div>
              <div className="coverage-item"><span>事件数量</span><Status tone="neutral">{events.length}</Status></div>
              <div className="coverage-item"><span>引用数量</span><Status tone={citations.length ? "ok" : "neutral"}>{citations.length}</Status></div>
            </div>
            {requestId ? (
              <button className="button danger small" style={{ marginTop: 12 }} onClick={() => void cancel()}>
                <Ban size={13} /> 调用取消接口
              </button>
            ) : null}
          </Card>
          <Card
            title="Query Rewrite"
            description="根据知识库语言生成中英文检索问题，并以加权 RRF 融合向量与 BM25 路线"
            action={queryTrace?.effective ? (
              <Status tone={queryTrace.original === queryTrace.effective ? "neutral" : "ok"}>
                {queryTrace.original === queryTrace.effective ? "未改写" : "已改写"}
              </Status>
            ) : <Status tone="neutral">等待检索</Status>}
          >
            {queryTrace ? (
              <>
                <div className="citation-card">
                  <strong>原始问题</strong>
                  <p>{queryTrace.original}</p>
                </div>
                <div className="citation-card">
                  <strong>实际检索问题</strong>
                  <p>{queryTrace.effective || "等待 Query Rewrite 完成…"}</p>
                </div>
                {queryTrace.variants ? Object.entries(queryTrace.variants).map(([language, query]) => (
                  <div className="citation-card" key={language}>
                    <strong>{language} 检索问题</strong>
                    <p>{query}</p>
                  </div>
                )) : null}
                <div className="hint">
                  问题语言：{queryTrace.detectedLanguage ?? "—"} · 知识库语言：{queryTrace.knowledgeLanguages?.join(", ") || "—"}
                </div>
                <div className="hint">
                  问题类型：{queryTrace.queryType ?? "—"} · 召回 Top-K：{queryTrace.retrievalTopK ?? "—"} · Rerank：{queryTrace.rerankTopN ?? "—"} · 上下文：{queryTrace.contextTopN ?? "—"}
                </div>
                {queryTrace.routes?.length ? (
                  <div className="citation-card">
                    <strong>实际检索路线</strong>
                    <p>{queryTrace.routes.join(" · ")}</p>
                  </div>
                ) : null}
              </>
            ) : (
              <div className="inline-note"><MessageSquareText size={16} /> 提交问答后在这里查看 Rewrite 前后的问题。</div>
            )}
          </Card>
          <Card title="引用" description="引用会在缓存命中后重新校验知识库边界">
            {citations.length ? citations.map((citation) => (
              <div className="citation-card" key={citation.citation_id}>
                <strong>{citation.label} · {citation.document_name}</strong>
                <p>{citation.quote}</p>
                <div className="hint">Page {citation.page_start ?? "—"} · {citation.citation_id}</div>
              </div>
            )) : (
              <div className="inline-note"><Bot size={16} /> 成功回答后在此展示已验证引用。</div>
            )}
          </Card>
          <Card title="完成事件 / 原始响应">
            <JsonOutput value={result ?? (events.length ? events[events.length - 1] : undefined)} busy={busy} />
          </Card>
        </div>
      </div>
    </>
  );
}
