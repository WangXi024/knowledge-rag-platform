"use client";

/** 系统面板：读取 live/ready、版本和能力接口，展示每个依赖与功能开关。 */

import { Activity, Cpu, RefreshCw, Server, ShieldCheck } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { gatewayRequest, payloadFrom } from "@/lib/client-api";
import { Card, JsonOutput, Method, PageHead, Status } from "./shared";

type Probe = {
  key: string;
  title: string;
  path: string;
  icon: typeof Activity;
  value?: unknown;
};

const definitions = [
  { key: "live", title: "存活探针", path: "health/live", icon: Activity },
  { key: "ready", title: "就绪探针", path: "health/ready", icon: Server },
  { key: "version", title: "版本契约", path: "v1/version", icon: ShieldCheck },
  { key: "capabilities", title: "能力清单", path: "v1/capabilities", icon: Cpu },
] as const;

function statusOf(value: unknown): string {
  if (!value || typeof value !== "object") return "unknown";
  const data = (value as { data?: { status?: string } }).data;
  return data?.status ?? "ok";
}

export function SystemPanel() {
  const [probes, setProbes] = useState<Probe[]>(definitions.map((item) => ({ ...item })));
  const [selected, setSelected] = useState<unknown>();
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    setBusy(true);
    const next = await Promise.all(
      definitions.map(async (definition) => {
        try {
          return { ...definition, value: await gatewayRequest(definition.path) };
        } catch (error) {
          return { ...definition, value: payloadFrom(error) };
        }
      }),
    );
    setProbes(next);
    setSelected(next[1]?.value);
    setBusy(false);
  }, []);

  useEffect(() => {
    const timeout = window.setTimeout(() => void refresh(), 0);
    return () => window.clearTimeout(timeout);
  }, [refresh]);

  return (
    <>
      <PageHead
        eyebrow="Runtime"
        title="系统状态"
        description="集中验证存活、就绪、版本与能力接口。就绪探针会显示 PostgreSQL、Redis、RAGFlow 和当前模型端点等实际依赖状态。"
        actions={
          <button className="button secondary" onClick={() => void refresh()} disabled={busy}>
            <RefreshCw size={15} /> 重新检查
          </button>
        }
      />

      <div className="grid cols-4">
        {probes.map((probe) => {
          const Icon = probe.icon;
          const status = statusOf(probe.value);
          return (
            <button
              key={probe.key}
              className="card metric"
              style={{ textAlign: "left" }}
              onClick={() => setSelected(probe.value)}
            >
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                <div className="metric-icon"><Icon size={19} /></div>
                <Status tone={status === "ok" ? "ok" : status === "degraded" ? "warning" : status === "unknown" ? "neutral" : "error"}>
                  {busy ? "检查中" : status}
                </Status>
              </div>
              <div className="metric-value" style={{ fontSize: 16 }}>{probe.title}</div>
              <div className="metric-label"><Method method="GET" path={`/${probe.path}`} /></div>
            </button>
          );
        })}
      </div>

      <div className="grid cols-2" style={{ marginTop: 17 }}>
        <Card title="依赖与能力详情" description="点击上方卡片切换原始响应">
          <JsonOutput value={selected} busy={busy} />
        </Card>
        <Card title="本地运行边界" description="这套界面专为当前开发环境设计">
          <div className="coverage-list">
            <div className="coverage-item"><span>RAG Console</span><code>http://localhost:3000</code></div>
            <div className="coverage-item"><span>RAG Gateway</span><code>http://127.0.0.1:8080</code></div>
            <div className="coverage-item"><span>RAGFlow UI</span><code>http://127.0.0.1:8081</code></div>
            <div className="coverage-item"><span>身份输入</span><Status tone="neutral">Tenant ID + User ID</Status></div>
            <div className="coverage-item"><span>身份鉴别</span><Status tone="warning">暂未启用</Status></div>
          </div>
          <div className="inline-note" style={{ marginTop: 14 }}>
            <ShieldCheck size={17} />
            当前 Tenant ID 与 User ID 仅用于开发联调和数据隔离，不证明调用者身份；生产环境必须接入公司 Identity。
          </div>
        </Card>
      </div>
    </>
  );
}
