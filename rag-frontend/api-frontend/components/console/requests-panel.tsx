"use client";

/** 请求审计面板：查询请求状态、取消执行、查看引用和提交质量反馈。 */

import { Ban, Eye, MessageSquareHeart, Quote, Search } from "lucide-react";
import { FormEvent, useState } from "react";
import { gatewayRequest, payloadFrom } from "@/lib/client-api";
import { Card, JsonOutput, PageHead } from "./shared";
import type { ResourceIds } from "./use-resources";

export function RequestsPanel({
  resources,
  setResources,
}: {
  resources: ResourceIds;
  setResources: (patch: Partial<ResourceIds>) => void;
}) {
  const [requestId, setRequestId] = useState(resources.requestId);
  const [citationId, setCitationId] = useState(resources.citationId);
  const [result, setResult] = useState<unknown>();
  const [busy, setBusy] = useState(false);

  async function requestAction(action: "get" | "cancel" | "citations") {
    if (!requestId.trim()) {
      setResult({ error: "请输入 Request ID" });
      return;
    }
    setBusy(true);
    setResources({ requestId: requestId.trim() });
    try {
      const suffix =
        action === "cancel" ? "/cancel" : action === "citations" ? "/citations" : "";
      const response = await gatewayRequest(`v1/requests/${requestId.trim()}${suffix}`, {
        method: action === "cancel" ? "POST" : "GET",
        idempotent: action === "cancel",
      });
      if (action === "citations") {
        const first = (response as { data?: Array<{ citation_id?: string }> }).data?.[0]?.citation_id;
        if (first) {
          setCitationId(first);
          setResources({ citationId: first });
        }
      }
      setResult(response);
    } catch (error) {
      setResult(payloadFrom(error));
    } finally {
      setBusy(false);
    }
  }

  async function inspectCitation() {
    if (!citationId.trim()) {
      setResult({ error: "请输入 Citation ID，或先查询请求引用" });
      return;
    }
    setBusy(true);
    setResources({ citationId: citationId.trim() });
    try {
      setResult(await gatewayRequest(`v1/citations/${citationId.trim()}`));
    } catch (error) {
      setResult(payloadFrom(error));
    } finally {
      setBusy(false);
    }
  }

  async function feedback(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    setBusy(true);
    try {
      const response = await gatewayRequest("v1/feedback", {
        method: "POST",
        idempotent: true,
        body: {
          request_id: String(form.get("requestId")),
          rating:
            String(form.get("rating")) === "null"
              ? null
              : Number(form.get("rating")),
          feedback_type: String(form.get("feedbackType")),
          comment: String(form.get("comment") || "") || null,
          corrected_answer: String(form.get("correctedAnswer") || "") || null,
        },
      });
      setResult(response);
    } catch (error) {
      setResult(payloadFrom(error));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <PageHead
        eyebrow="Trace & Quality"
        title="请求、引用与反馈"
        description="按 Request ID 回查问答状态、取消尚未完成的请求、读取引用详情，并向质量闭环提交结构化反馈。"
      />

      <div className="grid cols-2">
        <Card title="请求记录" description="GET /v1/requests/{id} · POST /cancel">
          <div className="field">
            <label>Request ID</label>
            <input className="input" value={requestId} onChange={(event) => setRequestId(event.target.value)} placeholder="问答响应中的 request_id" />
          </div>
          <div className="toolbar">
            <button className="button secondary" onClick={() => void requestAction("get")} disabled={busy || !requestId}>
              <Search size={14} /> 查询状态
            </button>
            <button className="button secondary" onClick={() => void requestAction("citations")} disabled={busy || !requestId}>
              <Quote size={14} /> 查询引用
            </button>
            <button className="button danger" onClick={() => void requestAction("cancel")} disabled={busy || !requestId}>
              <Ban size={14} /> 取消请求
            </button>
          </div>
          <div className="divider" />
          <div className="field">
            <label>Citation ID</label>
            <input className="input" value={citationId} onChange={(event) => setCitationId(event.target.value)} placeholder="引用 UUID" />
          </div>
          <button className="button secondary" onClick={() => void inspectCitation()} disabled={busy || !citationId}>
            <Eye size={14} /> 查看引用详情
          </button>
        </Card>

        <Card title="质量反馈" description="POST /v1/feedback">
          <form onSubmit={feedback}>
            <div className="field">
              <label>Request ID</label>
              <input className="input" name="requestId" required value={requestId} onChange={(event) => setRequestId(event.target.value)} />
            </div>
            <div className="field-grid">
              <div className="field">
                <label>Feedback Type</label>
                <select className="select" name="feedbackType" defaultValue="helpful">
                  <option value="helpful">helpful</option>
                  <option value="not_helpful">not_helpful</option>
                  <option value="answer_incorrect">answer_incorrect</option>
                  <option value="citation_incorrect">citation_incorrect</option>
                  <option value="should_answer">should_answer</option>
                  <option value="should_refuse">should_refuse</option>
                  <option value="other">other</option>
                </select>
              </div>
              <div className="field">
                <label>Rating</label>
                <select className="select" name="rating" defaultValue="1">
                  <option value="1">+1</option>
                  <option value="-1">-1</option>
                  <option value="null">不评分</option>
                </select>
              </div>
            </div>
            <div className="field">
              <label>备注（可选）</label>
              <textarea className="textarea" name="comment" placeholder="说明为何有效或无效" style={{ minHeight: 70 }} />
            </div>
            <div className="field">
              <label>修正答案（可选）</label>
              <textarea className="textarea" name="correctedAnswer" placeholder="期望答案" style={{ minHeight: 70 }} />
            </div>
            <button className="button" disabled={busy || !requestId}>
              <MessageSquareHeart size={15} /> 提交反馈
            </button>
          </form>
        </Card>
      </div>

      <div style={{ marginTop: 17 }}>
        <Card title="原始响应" description="请求、引用和反馈返回都会显示在这里">
          <JsonOutput value={result} busy={busy} />
        </Card>
      </div>
    </>
  );
}
