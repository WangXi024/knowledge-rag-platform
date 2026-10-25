"use client";

/** 浏览器侧 Gateway 调用工具：统一 Direct Tenant/User、JSON、错误结构和输入解析。 */

export type JsonRecord = Record<string, unknown>;

/**
 * 开发控制台把 Tenant ID 与 User ID 保存在当前浏览器中，便于开发者切换隔离空间。
 * 两个值都只是临时身份标识，不是认证凭据；生产环境必须由上游 Identity 系统提供。
 */
export const DIRECT_TENANT_STORAGE_KEY = "lazytt-rag-direct-tenant-id";
export const DIRECT_USER_STORAGE_KEY = "lazytt-rag-direct-user-id";
export const DEFAULT_DIRECT_TENANT_ID = "local-console-tenant";
export const DEFAULT_DIRECT_USER_ID = "local-console-user";

export function getDirectTenantId(): string {
  if (typeof window === "undefined") return DEFAULT_DIRECT_TENANT_ID;
  return localStorage.getItem(DIRECT_TENANT_STORAGE_KEY)?.trim() || DEFAULT_DIRECT_TENANT_ID;
}

export function setDirectTenantId(value: string): string {
  const normalized = value.trim() || DEFAULT_DIRECT_TENANT_ID;
  localStorage.setItem(DIRECT_TENANT_STORAGE_KEY, normalized);
  return normalized;
}

export function getDirectUserId(): string {
  if (typeof window === "undefined") return DEFAULT_DIRECT_USER_ID;
  return localStorage.getItem(DIRECT_USER_STORAGE_KEY)?.trim() || DEFAULT_DIRECT_USER_ID;
}

export function setDirectUserId(value: string): string {
  const normalized = value.trim() || DEFAULT_DIRECT_USER_ID;
  localStorage.setItem(DIRECT_USER_STORAGE_KEY, normalized);
  return normalized;
}

/** 为普通 JSON 请求和 SSE 请求添加完全一致的租户、用户隔离头。 */
export function directIdentityHeaders(initial?: HeadersInit): Headers {
  const headers = new Headers(initial);
  headers.set("X-Tenant-ID", getDirectTenantId());
  headers.set("X-User-ID", getDirectUserId());
  return headers;
}

export class GatewayError extends Error {
  /** 保存 HTTP 状态和 Gateway 原始错误体，便于面板同时显示友好/诊断信息。 */
  constructor(
    message: string,
    readonly status: number,
    readonly payload: unknown,
  ) {
    super(message);
  }
}

export async function gatewayRequest<T = JsonRecord>(
  path: string,
  options: {
    method?: "GET" | "POST" | "PATCH" | "DELETE";
    body?: JsonRecord | FormData;
    idempotent?: boolean;
    idempotencyKey?: string;
    signal?: AbortSignal;
  } = {},
): Promise<T> {
  // 所有请求走本站 /api/gateway，浏览器不跨域访问 Gateway。
  const method = options.method ?? "GET";
  const headers = directIdentityHeaders({ accept: "application/json" });
  let body: BodyInit | undefined;
  if (options.body instanceof FormData) {
    body = options.body;
  } else if (options.body) {
    headers.set("content-type", "application/json");
    body = JSON.stringify(options.body);
  }
  // 开发控制台允许显式传入同一个 Key 来复现幂等重放；普通页面只声明
  // idempotent 即可，由浏览器为每次新的业务动作生成独立 UUID。
  if (options.idempotencyKey?.trim()) {
    headers.set("idempotency-key", options.idempotencyKey.trim());
  } else if (options.idempotent) {
    headers.set("idempotency-key", crypto.randomUUID());
  }
  const response = await fetch(`/api/gateway/${path.replace(/^\/+/u, "")}`, {
    method,
    headers,
    body,
    signal: options.signal,
  });
  const contentType = response.headers.get("content-type") ?? "";
  const payload = contentType.includes("application/json")
    ? await response.json()
    : await response.text();
  if (!response.ok) {
    const message =
      typeof payload === "object" && payload
        ? String(
            (payload as { error?: { message?: string } | string }).error &&
              typeof (payload as { error?: unknown }).error === "object"
              ? (
                  (payload as { error?: { message?: string } }).error?.message ??
                  `请求失败 (${response.status})`
                )
              : (payload as { error?: string }).error ??
                  `请求失败 (${response.status})`,
          )
        : `请求失败 (${response.status})`;
    throw new GatewayError(message, response.status, payload);
  }
  return payload as T;
}

export function payloadFrom(error: unknown): unknown {
  return error instanceof GatewayError
    ? error.payload
    : { error: error instanceof Error ? error.message : "未知错误" };
}

export function pretty(value: unknown): string {
  if (value === undefined) return "尚未执行请求";
  if (typeof value === "string") return value;
  return JSON.stringify(value, null, 2);
}

export function splitIds(value: string): string[] {
  return value
    .split(/[\n,\s]+/u)
    .map((item) => item.trim())
    .filter(Boolean);
}

export function parseJsonObject(value: string): JsonRecord | undefined {
  if (!value.trim()) return undefined;
  const parsed = JSON.parse(value) as unknown;
  if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") {
    throw new Error("请输入 JSON 对象");
  }
  return parsed as JsonRecord;
}
