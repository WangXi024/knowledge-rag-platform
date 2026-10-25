"use client";

/** 控制台外壳：负责 Direct Tenant/User、侧边导航、资源刷新和面板切换。 */

import {
  Activity,
  BookOpen,
  Braces,
  Database,
  FileText,
  GitBranch,
  MessageSquareText,
  Quote,
  ShieldCheck,
} from "lucide-react";
import { useEffect, useState } from "react";
import {
  DEFAULT_DIRECT_TENANT_ID,
  DEFAULT_DIRECT_USER_ID,
  getDirectTenantId,
  getDirectUserId,
  setDirectTenantId,
  setDirectUserId,
} from "@/lib/client-api";
import { AnswersPanel } from "./answers-panel";
import { ApiConsolePanel } from "./api-console-panel";
import { DashboardPanel } from "./dashboard-panel";
import { DocumentsPanel } from "./documents-panel";
import { KnowledgePanel } from "./knowledge-panel";
import { RequestsPanel } from "./requests-panel";
import { RetrievalPanel } from "./retrieval-panel";
import { SystemPanel } from "./system-panel";
import { useResources } from "./use-resources";

type PanelKey =
  | "dashboard"
  | "knowledge"
  | "documents"
  | "retrieval"
  | "answers"
  | "requests"
  | "system"
  | "api";

const navItems = [
  { key: "dashboard", label: "开发总览", icon: Activity, group: "Workspace" },
  { key: "knowledge", label: "知识库", icon: BookOpen, group: "Workspace" },
  { key: "documents", label: "文档与解析", icon: FileText, group: "Workspace" },
  { key: "retrieval", label: "混合检索", icon: GitBranch, group: "Online" },
  { key: "answers", label: "问答实验室", icon: MessageSquareText, group: "Online" },
  { key: "requests", label: "请求与反馈", icon: Quote, group: "Quality" },
  { key: "system", label: "系统状态", icon: Database, group: "Quality" },
  { key: "api", label: "全接口控制台", icon: Braces, group: "Quality", count: "30" },
] as const;

const panelTitles: Record<PanelKey, string> = {
  dashboard: "开发总览",
  knowledge: "知识库",
  documents: "文档与解析",
  retrieval: "混合检索",
  answers: "问答实验室",
  requests: "请求与反馈",
  system: "系统状态",
  api: "全接口控制台",
};

export function ConsoleApp() {
  const [tenantId, setTenantIdState] = useState(DEFAULT_DIRECT_TENANT_ID);
  const [userId, setUserIdState] = useState(DEFAULT_DIRECT_USER_ID);
  const [active, setActive] = useState<PanelKey>("dashboard");
  const { resources, setResources, resetResources } = useResources();

  useEffect(() => {
    setTenantIdState(getDirectTenantId());
    setUserIdState(getDirectUserId());
  }, []);

  function navigate(target: string) {
    setActive(target as PanelKey);
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function persistIdentity() {
    const previousTenant = getDirectTenantId();
    const previous = getDirectUserId();
    const nextTenant = setDirectTenantId(tenantId);
    const next = setDirectUserId(userId);
    setTenantIdState(nextTenant);
    setUserIdState(next);
    if (nextTenant !== previousTenant || next !== previous) resetResources();
  }

  const groups = ["Workspace", "Online", "Quality"];

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark"><ShieldCheck size={20} /></div>
          <div>
            <strong>LazyTT RAG</strong>
            <span>Developer Console</span>
          </div>
        </div>
        <div className="environment-pill">
          <span className="environment-dot" />
          <span>本地测试环境 · 禁止生产使用</span>
        </div>

        {groups.map((group) => (
          <div key={group}>
            <div className="nav-label">{group}</div>
            <nav className="nav-list">
              {navItems
                .filter((item) => item.group === group)
                .map((item) => {
                  const Icon = item.icon;
                  return (
                    <button
                      className={`nav-item ${active === item.key ? "active" : ""}`}
                      key={item.key}
                      onClick={() => navigate(item.key)}
                    >
                      <Icon size={16} />
                      <span>{item.label}</span>
                      {"count" in item ? <span className="nav-count">{item.count}</span> : null}
                    </button>
                  );
                })}
            </nav>
          </div>
        ))}

        <div className="sidebar-spacer" />
        <div className="user-card">
          <div className="avatar">{userId.slice(0, 1).toUpperCase() || "U"}</div>
          <div className="user-copy">
            <strong>Direct Identity</strong>
            <input
              aria-label="Direct Tenant ID"
              onBlur={persistIdentity}
              onChange={(event) => setTenantIdState(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") event.currentTarget.blur();
              }}
              placeholder="Tenant ID"
              value={tenantId}
            />
            <input
              aria-label="Direct User ID"
              onBlur={persistIdentity}
              onChange={(event) => setUserIdState(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") event.currentTarget.blur();
              }}
              value={userId}
              placeholder="User ID"
            />
          </div>
        </div>
      </aside>

      <main className="main">
        <header className="topbar">
          <div className="breadcrumb">RAG Console&nbsp; / &nbsp;<strong>{panelTitles[active]}</strong></div>
          <div className="scope-chip" title={`X-Tenant-ID: ${tenantId}; X-User-ID: ${userId}`}>
            <Database size={13} />
            <span>{tenantId}</span>
            <span style={{ color: "#a1aca8" }}>/</span>
            <span>{userId}</span>
          </div>
        </header>
        <div className="content">
          {active === "dashboard" ? <DashboardPanel navigate={navigate} /> : null}
          {active === "knowledge" ? <KnowledgePanel resources={resources} setResources={setResources} navigate={navigate} /> : null}
          {active === "documents" ? <DocumentsPanel resources={resources} setResources={setResources} /> : null}
          {active === "retrieval" ? <RetrievalPanel resources={resources} /> : null}
          {active === "answers" ? <AnswersPanel resources={resources} setResources={setResources} /> : null}
          {active === "requests" ? <RequestsPanel resources={resources} setResources={setResources} /> : null}
          {active === "system" ? <SystemPanel /> : null}
          {active === "api" ? <ApiConsolePanel /> : null}
        </div>
      </main>
    </div>
  );
}
