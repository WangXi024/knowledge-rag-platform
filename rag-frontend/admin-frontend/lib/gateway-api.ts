'use client';

/** 浏览器侧 API 工具：统一身份 Header、幂等键、JSON/FormData 与错误解析。 */

import type { AdminSession } from './contracts';

type JsonObject = Record<string, unknown>;

export class GatewayError extends Error {
  constructor(message: string, readonly status: number, readonly payload: unknown) {
    super(message);
  }
}

/**
 * 为一次写操作生成 UUID v4，不接收业务数据，也不访问网络或持久化存储。
 * HTTPS/localhost 优先用原生 randomUUID；局域网 HTTP 缺少它时，使用仍可用的
 * getRandomValues 生成安全随机字节。随机源失败会抛错并阻止请求，不使用弱随机数兜底。
 */
function createIdempotencyKey(): string {
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID();

  const bytes = crypto.getRandomValues(new Uint8Array(16));
  // 固定 UUID v4 的版本位与变体位，其余位保留随机值，兼容后端既有幂等键格式。
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('');
  return [hex.slice(0, 8), hex.slice(8, 12), hex.slice(12, 16), hex.slice(16, 20), hex.slice(20)].join('-');
}

/**
 * 所有浏览器请求都先访问本站同源代理，避免 CORS，同时只向 Gateway 传递白名单 Header。
 * 当前 Tenant/User 只是开发身份标识；服务端接入统一身份后应由可信会话覆盖它们。
 * 写操作按 options 生成兼容 HTTP/HTTPS 的独立幂等键；失败时抛出异常交由页面显示。
 */
export async function gatewayRequest<T>(
  session: AdminSession,
  path: string,
  options: {
    method?: 'GET' | 'POST' | 'PATCH' | 'DELETE';
    body?: JsonObject | FormData;
    idempotent?: boolean;
  } = {},
): Promise<T> {
  const headers = new Headers({
    accept: 'application/json',
    'X-Tenant-ID': session.tenantId,
    'X-User-ID': session.userId,
  });
  let body: BodyInit | undefined;

  if (options.body instanceof FormData) {
    body = options.body;
  } else if (options.body) {
    headers.set('content-type', 'application/json');
    body = JSON.stringify(options.body);
  }
  if (options.idempotent) {
    // 每次按钮操作生成新键；不会复用上一次上传的键，避免“幂等键与不同请求冲突”。
    headers.set('Idempotency-Key', createIdempotencyKey());
  }

  const response = await fetch(`/api/gateway/${path.replace(/^\/+/, '')}`, {
    method: options.method ?? 'GET',
    headers,
    body,
  });
  const contentType = response.headers.get('content-type') ?? '';
  const payload: unknown = contentType.includes('application/json')
    ? await response.json()
    : await response.text();

  if (!response.ok) {
    const error = payload && typeof payload === 'object'
      ? (payload as { error?: { message?: string } | string }).error
      : undefined;
    const message = typeof error === 'string'
      ? error
      : error?.message ?? `请求失败（HTTP ${response.status}）`;
    throw new GatewayError(message, response.status, payload);
  }
  return payload as T;
}

/** 从异常中提取适合管理员查看的简短说明，不在页面暴露整个上游响应。 */
export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : '发生未知错误，请稍后重试。';
}
