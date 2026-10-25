/**
 * 服务端 Gateway 客户端：把当前联调身份的 Tenant ID 与 User ID 转成受控请求头，
 * 再转发给当前采用 Direct Identity 模式的 RAG Gateway，并统一解析友好错误。
 */
import type { User } from "./auth";
import { getRuntimeEnvironment } from "./runtime-env";

export class GatewayError extends Error {
  status: number;
  payload: unknown;

  constructor(status: number, message: string, payload: unknown) {
    super(message);
    this.status = status;
    this.payload = payload;
  }
}

export async function gatewayFetch(user: User, path: string, init: RequestInit = {}): Promise<Response> {
  /**
   * Tenant/User 请求头只能取自经过格式校验的 HttpOnly 身份 Cookie，不能相信各业务
   * API 请求体临时传入的值。当前 Cookie 不证明身份所有权，正式 Identity 接入后仍应
   * 在这里使用验签后的主体覆盖两个 Header。
   * 同时主动删除调用方可能附带的旧 Authorization，避免前端自签 JWT 与后端当前
   * Direct Identity 协议混用；Gateway 会由 Tenant ID 与 User ID 派生
   * security_scope_id，从而保持同租户不同用户、不同租户同名用户之间的数据隔离。
   */
  const base = getRuntimeEnvironment().GATEWAY_BASE_URL || "http://127.0.0.1:8080";
  const headers = new Headers(init.headers);
  headers.delete("Authorization");
  headers.set("X-Tenant-ID", user.tenantId);
  headers.set("X-User-ID", user.userId);
  headers.set("Accept", headers.get("Accept") || "application/json");
  return fetch(new URL(path, base), { ...init, headers, redirect: "manual" });
}

export async function gatewayJson<T>(user: User, path: string, init: RequestInit = {}): Promise<T> {
  // 对非 2xx 响应抛出 GatewayError；成功响应按调用方类型返回解析 JSON。
  const response = await gatewayFetch(user, path, init);
  const text = await response.text();
  let payload: unknown;
  try {
    payload = text ? JSON.parse(text) : {};
  } catch {
    payload = { message: text || response.statusText };
  }
  if (!response.ok) {
    const record = payload as { error?: { message?: string }; message?: string };
    throw new GatewayError(response.status, record.error?.message || record.message || "服务暂时不可用", payload);
  }
  return payload as T;
}

export function friendlyError(error: unknown): { error: string } {
  if (error instanceof GatewayError) return { error: error.message };
  return { error: "服务暂时不可用，请稍后重试" };
}
