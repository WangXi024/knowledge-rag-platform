"use client";

/** 通用 API 调试面板：让测试人员选择方法/路径、填写 JSON 并查看原始响应。 */

import { CheckCircle2, Play, TerminalSquare } from "lucide-react";
import { FormEvent, useState } from "react";
import { gatewayRequest, payloadFrom } from "@/lib/client-api";
import { Card, JsonOutput, Method, PageHead, Status } from "./shared";

type HttpMethod = "GET" | "POST" | "PATCH" | "DELETE";
type Endpoint = {
  method: HttpMethod;
  path: string;
  group: string;
  location: string;
  body?: string;
  multipart?: boolean;
};

const endpoints: Endpoint[] = [
  { method: "GET", path: "health/live", group: "System", location: "系统状态" },
  { method: "GET", path: "health/ready", group: "System", location: "系统状态" },
  { method: "GET", path: "v1/version", group: "System", location: "系统状态" },
  { method: "GET", path: "v1/capabilities", group: "System", location: "系统状态" },
  {
    method: "POST",
    path: "v1/management/knowledge-bases",
    group: "Knowledge Bases",
    location: "知识库",
    // 原始请求器的默认示例与产品默认保持一致，创建出的 Fixture 才会真正验证
    // 256 Token 与 10% 重叠，而不是继续沿用历史 general-512@1。
    body: JSON.stringify({ scope_type: "TENANT", name: "fixture-kb", description: "compatible model test", parser_profile: "office-default@1", chunk_profile: "general-256@2", embedding_profile: "embedding-openai-compatible@3", retrieval_profile: "hybrid-default@2" }, null, 2),
  },
  { method: "GET", path: "v1/management/knowledge-bases?page=1&page_size=20", group: "Knowledge Bases", location: "知识库" },
  { method: "GET", path: "v1/management/knowledge-bases/{knowledge_base_id}", group: "Knowledge Bases", location: "知识库" },
  { method: "PATCH", path: "v1/management/knowledge-bases/{knowledge_base_id}", group: "Knowledge Bases", location: "知识库", body: '{\n  "description": "updated by local console"\n}' },
  { method: "DELETE", path: "v1/management/knowledge-bases/{knowledge_base_id}", group: "Knowledge Bases", location: "知识库" },
  { method: "POST", path: "v1/management/knowledge-bases/{knowledge_base_id}/documents", group: "Documents", location: "文档与解析", multipart: true },
  { method: "GET", path: "v1/management/knowledge-bases/{knowledge_base_id}/documents?page=1&page_size=20", group: "Documents", location: "文档与解析" },
  { method: "GET", path: "v1/management/documents/{document_id}", group: "Documents", location: "文档与解析" },
  { method: "DELETE", path: "v1/management/documents/{document_id}", group: "Documents", location: "文档与解析" },
  { method: "POST", path: "v1/management/documents/{document_id}/parse", group: "Documents", location: "文档与解析" },
  { method: "POST", path: "v1/management/documents/{document_id}/reparse", group: "Documents", location: "文档与解析", body: "{}" },
  { method: "POST", path: "v1/management/documents/{document_id}/retry", group: "Documents", location: "文档与解析" },
  { method: "GET", path: "v1/management/documents/{document_id}/chunks?page=1&page_size=20", group: "Documents", location: "文档与解析" },
  // 新版本上传是 multipart，必须到“文档与解析”的安全更新表单选择真实文件；
  // 版本列表和回滚仍可在原始请求器中验证完整响应与错误边界。
  { method: "POST", path: "v1/management/documents/{document_id}/versions", group: "Document Versions", location: "文档与解析", multipart: true },
  { method: "GET", path: "v1/management/documents/{document_id}/versions?page=1&page_size=20", group: "Document Versions", location: "文档与解析" },
  { method: "POST", path: "v1/management/documents/{document_id}/versions/{version_id}/rollback", group: "Document Versions", location: "文档与解析" },
  { method: "GET", path: "v1/management/jobs/{job_id}", group: "Jobs", location: "文档与解析" },
  { method: "POST", path: "v1/management/jobs/{job_id}/cancel", group: "Jobs", location: "文档与解析" },
  { method: "POST", path: "v1/retrieval", group: "Retrieval", location: "混合检索", body: '{\n  "knowledge_base_ids": ["{knowledge_base_id}"],\n  "document_ids": [],\n  "query": "测试问题",\n  "metadata_filter": null,\n  "retrieval_profile": "hybrid-default@2",\n  "options": {"page_size": 10, "include_content": true, "include_positions": true}\n}' },
  { method: "POST", path: "v1/answers", group: "Answers", location: "问答实验室", body: '{\n  "knowledge_base_ids": ["{knowledge_base_id}"],\n  "query": "根据文档回答测试问题",\n  "messages": [],\n  "metadata_filter": null,\n  "retrieval_profile": "hybrid-default@2",\n  "generation_profile": "answer-openai-compatible@4",\n  "response_language": "zh-CN"\n}' },
  { method: "POST", path: "v1/answers/stream", group: "Answers", location: "问答实验室", body: '{\n  "knowledge_base_ids": ["{knowledge_base_id}"],\n  "query": "流式回答测试",\n  "messages": [],\n  "retrieval_profile": "hybrid-default@2",\n  "generation_profile": "answer-openai-compatible@4",\n  "response_language": "zh-CN"\n}' },
  { method: "GET", path: "v1/requests/{request_id}", group: "Requests", location: "请求与反馈" },
  { method: "POST", path: "v1/requests/{request_id}/cancel", group: "Requests", location: "请求与反馈" },
  { method: "GET", path: "v1/requests/{request_id}/citations", group: "Citations", location: "请求与反馈" },
  { method: "GET", path: "v1/citations/{citation_id}", group: "Citations", location: "请求与反馈" },
  { method: "POST", path: "v1/feedback", group: "Feedback", location: "请求与反馈", body: '{\n  "request_id": "{request_id}",\n  "rating": 1,\n  "feedback_type": "helpful",\n  "comment": "local UI test",\n  "corrected_answer": null\n}' },
];

export function ApiConsolePanel() {
  const [method, setMethod] = useState<HttpMethod>("GET");
  const [path, setPath] = useState("v1/capabilities");
  const [body, setBody] = useState("");
  const [idempotent, setIdempotent] = useState(false);
  const [result, setResult] = useState<unknown>();
  const [busy, setBusy] = useState(false);
  const [selectedGroup, setSelectedGroup] = useState("System");

  function choose(endpoint: Endpoint) {
    setMethod(endpoint.method);
    setPath(endpoint.path);
    setBody(endpoint.body ?? "");
    setIdempotent(["POST", "DELETE"].includes(endpoint.method));
    setSelectedGroup(endpoint.group);
    if (endpoint.multipart) {
      setResult({ note: "文件上传需要 multipart/form-data，请前往“文档与解析”使用完整上传表单。" });
    }
  }

  async function execute(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    try {
      const parsedBody =
        method === "GET" || !body.trim()
          ? undefined
          : (JSON.parse(body) as Record<string, unknown>);
      setResult(await gatewayRequest(path, { method, body: parsedBody, idempotent }));
    } catch (error) {
      setResult(payloadFrom(error));
    } finally {
      setBusy(false);
    }
  }

  const groups = Array.from(new Set(endpoints.map((endpoint) => endpoint.group)));

  return (
    <>
      <PageHead
        eyebrow="API Coverage"
        title="全接口控制台"
        description="30 个 Gateway 接口都有产品化测试入口；这里额外提供原始 JSON 请求器，便于验证边界参数和错误响应。"
        actions={<Status>{endpoints.length} / 30 已接入</Status>}
      />

      <div className="grid cols-2">
        <Card title="原始请求器" description="请求经过同源代理，并自动注入当前 X-Tenant-ID 与 X-User-ID">
          <form onSubmit={execute}>
            <div className="field-grid">
              <div className="field">
                <label>Method</label>
                <select className="select" value={method} onChange={(event) => setMethod(event.target.value as HttpMethod)}>
                  <option>GET</option><option>POST</option><option>PATCH</option><option>DELETE</option>
                </select>
              </div>
              <div className="field">
                <label>Idempotency-Key</label>
                <label className="inline-note" style={{ minHeight: 44, alignItems: "center" }}>
                  <input type="checkbox" checked={idempotent} onChange={(event) => setIdempotent(event.target.checked)} /> 自动生成
                </label>
              </div>
            </div>
            <div className="field">
              <label>Gateway Path</label>
              <input className="input" value={path} onChange={(event) => setPath(event.target.value)} required />
            </div>
            <div className="field">
              <label>JSON Body</label>
              <textarea className="textarea" value={body} onChange={(event) => setBody(event.target.value)} disabled={method === "GET"} placeholder={method === "GET" ? "GET 请求不发送 Body" : "{}"} style={{ minHeight: 220, fontFamily: "var(--font-geist-mono)" }} />
            </div>
            <button className="button" disabled={busy || path.includes("{")}>
              <Play size={15} /> 执行请求
            </button>
            {path.includes("{") ? <span className="hint" style={{ marginLeft: 10 }}>请先替换路径中的占位符</span> : null}
          </form>
        </Card>

        <Card title="响应" description="HTTP 错误的 Gateway payload 也会完整显示" action={<TerminalSquare size={16} color="#70817b" />}>
          <JsonOutput value={result} busy={busy} />
        </Card>
      </div>

      <section className="card" style={{ marginTop: 17 }}>
        <div className="card-head">
          <div><h2>接口覆盖矩阵</h2><p>点击任一接口载入原始请求器；文件与 SSE 在对应产品界面有专用测试工具</p></div>
          <div className="toolbar" style={{ margin: 0 }}>
            {groups.map((group) => (
              <button key={group} className={`button small ${selectedGroup === group ? "" : "secondary"}`} onClick={() => setSelectedGroup(group)}>{group}</button>
            ))}
          </div>
        </div>
        <div className="table-wrap">
          <table className="table">
            <thead><tr><th>接口</th><th>产品化入口</th><th>状态</th><th>操作</th></tr></thead>
            <tbody>
              {endpoints.filter((endpoint) => endpoint.group === selectedGroup).map((endpoint) => (
                <tr key={`${endpoint.method}-${endpoint.path}`}>
                  <td><Method method={endpoint.method} path={`/${endpoint.path}`} /></td>
                  <td>{endpoint.location}{endpoint.multipart ? <div className="row-subtitle">multipart 专用表单</div> : endpoint.path.endsWith("/stream") ? <div className="row-subtitle">SSE 专用解析器</div> : null}</td>
                  <td><Status><CheckCircle2 size={11} /> 已接入</Status></td>
                  <td><button className="button secondary small" onClick={() => choose(endpoint)}>载入</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </>
  );
}
