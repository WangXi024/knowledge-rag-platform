"use client";

/** 控制台共用的标题、卡片、状态、空态、JSON、HTTP 方法和加载组件。 */

import { Braces, Inbox, LoaderCircle } from "lucide-react";
import { ReactNode } from "react";
import { pretty } from "@/lib/client-api";

export function PageHead({
  eyebrow,
  title,
  description,
  actions,
}: {
  eyebrow: string;
  title: string;
  description: string;
  actions?: ReactNode;
}) {
  return (
    <div className="page-head">
      <div>
        <span className="eyebrow">{eyebrow}</span>
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
      {actions ? <div className="head-actions">{actions}</div> : null}
    </div>
  );
}

export function Card({
  title,
  description,
  action,
  children,
}: {
  title?: string;
  description?: string;
  action?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="card">
      {title ? (
        <div className="card-head">
          <div>
            <h2>{title}</h2>
            {description ? <p>{description}</p> : null}
          </div>
          {action}
        </div>
      ) : null}
      <div className="card-body">{children}</div>
    </section>
  );
}

export function Status({
  children,
  tone = "ok",
}: {
  children: ReactNode;
  tone?: "ok" | "warning" | "error" | "neutral";
}) {
  return (
    <span className={`status ${tone === "ok" ? "" : tone}`}>
      <span className="status-dot" />
      {children}
    </span>
  );
}

export function Empty({
  title,
  description,
}: {
  title: string;
  description: string;
}) {
  return (
    <div className="empty">
      <div>
        <div className="empty-icon" style={{ marginInline: "auto" }}>
          <Inbox size={21} />
        </div>
        <strong>{title}</strong>
        <span>{description}</span>
      </div>
    </div>
  );
}

export function JsonOutput({
  value,
  label = "响应",
  busy = false,
}: {
  value: unknown;
  label?: string;
  busy?: boolean;
}) {
  return (
    <div>
      <div className="result-meta">
        {busy ? <LoaderCircle className="spinner" size={13} /> : <Braces size={13} />}
        {busy ? "正在请求…" : label}
      </div>
      <pre className="json-output">{pretty(value)}</pre>
    </div>
  );
}

export function Method({
  method,
  path,
}: {
  method: "GET" | "POST" | "PATCH" | "DELETE";
  path: string;
}) {
  return (
    <span className="endpoint">
      <span className={`method ${method}`}>{method}</span>
      <span>{path}</span>
    </span>
  );
}

export function LoadingScreen() {
  return (
    <div className="loading-screen">
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <span className="spinner" />
        正在进入本地测试工作台…
      </div>
    </div>
  );
}
