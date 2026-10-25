"use client";

/** 仪表盘：汇总当前身份可访问资源、系统健康和常用操作入口。 */

import {
  Activity,
  ArrowRight,
  BookOpen,
  Database,
  GitBranch,
  MessageSquareText,
  RefreshCw,
  ServerCog,
} from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { gatewayRequest, payloadFrom } from "@/lib/client-api";
import { Card, JsonOutput, PageHead, Status } from "./shared";

type DashboardSnapshot = {
  live?: unknown;
  ready?: unknown;
  version?: unknown;
  capabilities?: unknown;
  knowledgeBases?: unknown;
  error?: unknown;
};

function dataOf(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object") return {};
  const data = (value as { data?: unknown }).data;
  return data && typeof data === "object" && !Array.isArray(data)
    ? (data as Record<string, unknown>)
    : {};
}

export function DashboardPanel({
  navigate,
}: {
  navigate: (target: string) => void;
}) {
  const [snapshot, setSnapshot] = useState<DashboardSnapshot>({});
  const [busy, setBusy] = useState(true);

  const refresh = useCallback(async () => {
    setBusy(true);
    const paths = [
      ["live", "health/live"],
      ["ready", "health/ready"],
      ["version", "v1/version"],
      ["capabilities", "v1/capabilities"],
      ["knowledgeBases", "v1/management/knowledge-bases?page=1&page_size=1"],
    ] as const;
    const values = await Promise.all(
      paths.map(async ([key, path]) => {
        try {
          return [key, await gatewayRequest(path)] as const;
        } catch (error) {
          return [key, payloadFrom(error)] as const;
        }
      }),
    );
    setSnapshot(Object.fromEntries(values));
    setBusy(false);
  }, []);

  useEffect(() => {
    const timeout = window.setTimeout(() => void refresh(), 0);
    return () => window.clearTimeout(timeout);
  }, [refresh]);

  const ready = dataOf(snapshot.ready);
  const version = dataOf(snapshot.version);
  const capabilities = dataOf(snapshot.capabilities);
  const kbMeta: { total?: number } | undefined =
    snapshot.knowledgeBases &&
    typeof snapshot.knowledgeBases === "object" &&
    !Array.isArray(snapshot.knowledgeBases)
      ? (snapshot.knowledgeBases as { meta?: { total?: number } }).meta
      : undefined;
  const features =
    capabilities.features && typeof capabilities.features === "object"
      ? (capabilities.features as Record<string, boolean>)
      : {};

  return (
    <>
      <PageHead
        eyebrow="Overview"
        title="开发验证总览"
        description="从这里观察 Gateway 状态、当前能力和测试资源。所有业务请求都会自动使用当前账号的 Tenant 与 RAG Scope。"
        actions={
          <button className="button secondary" onClick={() => void refresh()} disabled={busy}>
            <RefreshCw size={15} /> 刷新状态
          </button>
        }
      />

      <div className="grid cols-4">
        <section className="card metric">
          <div className="metric-icon"><Activity size={19} /></div>
          <div className="metric-value">{ready.status === "ok" ? "Ready" : busy ? "…" : String(ready.status ?? "未知")}</div>
          <div className="metric-label">Gateway 就绪状态</div>
        </section>
        <section className="card metric">
          <div className="metric-icon"><Database size={19} /></div>
          <div className="metric-value">{kbMeta?.total ?? "—"}</div>
          <div className="metric-label">当前 Scope 知识库</div>
        </section>
        <section className="card metric">
          <div className="metric-icon"><GitBranch size={19} /></div>
          <div className="metric-value">{String(version.api_version ?? "—")}</div>
          <div className="metric-label">Gateway API 版本</div>
        </section>
        <section className="card metric">
          <div className="metric-icon"><ServerCog size={19} /></div>
          <div className="metric-value">27</div>
          <div className="metric-label">已接入测试接口</div>
        </section>
      </div>

      <div className="grid cols-2" style={{ marginTop: 17 }}>
        <Card title="核心能力状态" description="读取 /v1/capabilities 的真实运行配置">
          <div className="coverage-list">
            {[
              ["流式答案", "answer_streaming"],
              ["精确缓存", "exact_cache"],
              ["语义缓存", "semantic_cache"],
              ["向量 + BM25 + RRF", "retrieval_vector_bm25_rrf"],
              ["独立 Rerank", "rerank"],
            ].map(([label, key]) => (
              <div className="coverage-item" key={key}>
                <span style={{ fontSize: 12 }}>{label}</span>
                <Status tone={features[key] ? "ok" : key === "rerank" ? "warning" : "neutral"}>
                  {features[key] ? "已启用" : key === "rerank" ? "本地关闭" : "未启用"}
                </Status>
              </div>
            ))}
          </div>
        </Card>

        <Card title="快速开始" description="进入最常用的端到端验证路径">
          <div className="coverage-list">
            {[
              [BookOpen, "知识库与文档", "创建知识库，上传并解析非业务 Fixture", "knowledge"],
              [GitBranch, "混合检索", "验证向量、BM25 与 RRF 融合结果", "retrieval"],
              [MessageSquareText, "问答实验室", "测试普通回答与 SSE 流式输出", "answers"],
            ].map(([Icon, title, text, target]) => {
              const IconComponent = Icon as typeof BookOpen;
              return (
                <button
                  className="coverage-item"
                  style={{ background: "white", textAlign: "left" }}
                  key={String(target)}
                  onClick={() => navigate(String(target))}
                >
                  <span style={{ display: "flex", alignItems: "center", gap: 11 }}>
                    <span className="metric-icon" style={{ width: 34, height: 34 }}>
                      <IconComponent size={16} />
                    </span>
                    <span>
                      <strong style={{ display: "block", fontSize: 12 }}>{String(title)}</strong>
                      <span className="hint">{String(text)}</span>
                    </span>
                  </span>
                  <ArrowRight size={15} color="#79908a" />
                </button>
              );
            })}
          </div>
        </Card>
      </div>

      <div style={{ marginTop: 17 }}>
        <Card title="就绪探针原始响应" description="依赖状态、延迟与错误会完整保留">
          <JsonOutput value={snapshot.ready} busy={busy} />
        </Card>
      </div>
    </>
  );
}
