/**
 * 管理端同源代理：只允许 RAG v1、存活和就绪接口，并严格转发白名单 Header。
 * 该代理不提供管理员鉴权；真实角色校验必须在后续统一身份/后端权限层完成。
 */
import { getGatewayBaseUrl } from '@/lib/runtime-env';

type RouteContext = { params: Promise<{ path: string[] }> };

const REQUEST_HEADERS = ['accept', 'content-type', 'idempotency-key', 'x-request-id', 'x-tenant-id', 'x-user-id'];
// PDF 预览也保留禁止类型嗅探响应头；文件继续流式转发，不在 BFF 缓冲。
const RESPONSE_HEADERS = ['cache-control', 'content-disposition', 'content-type', 'etag', 'x-request-id', 'x-content-type-options'];

/** 进行路径、身份格式和 Header 白名单校验后，把请求流式转发给 Gateway。 */
async function proxy(request: Request, context: RouteContext): Promise<Response> {
  const tenantId = request.headers.get('x-tenant-id')?.trim() ?? '';
  const userId = request.headers.get('x-user-id')?.trim() ?? '';
  const invalidIdentity = (value: string) => !value || value.length > 128 || /[\u0000-\u001f\u007f]/u.test(value);

  if (invalidIdentity(tenantId) || invalidIdentity(userId)) {
    return Response.json({ error: 'Tenant ID 或 User ID 缺失/格式无效' }, { status: 400 });
  }

  const { path } = await context.params;
  if (path.some((segment) => segment === '.' || segment === '..' || segment.includes('/') || segment.includes('\\'))) {
    return Response.json({ error: '代理路径不合法' }, { status: 400 });
  }
  const joinedPath = path.join('/');
  if (!joinedPath.startsWith('v1/') && joinedPath !== 'health/live' && joinedPath !== 'health/ready') {
    return Response.json({ error: '不允许代理该路径' }, { status: 403 });
  }

  const requestUrl = new URL(request.url);
  const upstreamUrl = new URL(`/${joinedPath}${requestUrl.search}`, getGatewayBaseUrl());
  const headers = new Headers();
  REQUEST_HEADERS.forEach((name) => {
    const value = request.headers.get(name);
    if (value) headers.set(name, value);
  });

  try {
    const upstream = await fetch(upstreamUrl, {
      method: request.method,
      headers,
      body: request.method === 'GET' || request.method === 'HEAD' ? undefined : request.body,
      redirect: 'manual',
      signal: request.signal,
    });
    const responseHeaders = new Headers();
    RESPONSE_HEADERS.forEach((name) => {
      const value = upstream.headers.get(name);
      if (value) responseHeaders.set(name, value);
    });
    return new Response(upstream.body, { status: upstream.status, statusText: upstream.statusText, headers: responseHeaders });
  } catch (error) {
    return Response.json(
      { error: '无法连接 RAG Gateway', detail: error instanceof Error ? error.message : 'unknown error' },
      { status: 502 },
    );
  }
}

export const GET = proxy;
export const POST = proxy;
export const PATCH = proxy;
export const DELETE = proxy;
