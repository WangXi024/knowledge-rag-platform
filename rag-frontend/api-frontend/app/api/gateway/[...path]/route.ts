/**
 * Gateway 同源代理：转发开发者选择的 X-Tenant-ID 与 X-User-ID，并剥离无关头。
 * 两个 Header 只用于当前无鉴权联调阶段的隔离，不代表调用者已经通过身份认证。
 */
import { getRuntimeEnvironment } from "@/lib/runtime-env";

type RouteContext = { params: Promise<{ path: string[] }> };

const REQUEST_HEADER_ALLOWLIST = [
  "accept",
  "content-type",
  "idempotency-key",
  "x-request-id",
  "x-tenant-id",
  "x-user-id",
];
const RESPONSE_HEADER_ALLOWLIST = [
  "cache-control",
  "content-disposition",
  "content-type",
  "etag",
  "x-accel-buffering",
  "x-request-id",
];

async function proxy(request: Request, context: RouteContext): Promise<Response> {
  const directTenantId = request.headers.get("x-tenant-id")?.trim() ?? "";
  const directUserId = request.headers.get("x-user-id")?.trim() ?? "";
  if (!directTenantId || directTenantId.length > 128 || /[\u0000-\u001f\u007f]/u.test(directTenantId)) {
    return Response.json(
      { error: "X-Tenant-ID 缺失或格式无效" },
      { status: 400 },
    );
  }
  if (!directUserId || directUserId.length > 128 || /[\u0000-\u001f\u007f]/u.test(directUserId)) {
    return Response.json(
      { error: "X-User-ID 缺失或格式无效" },
      { status: 400 },
    );
  }
  const { path } = await context.params;
  if (
    path.some(
      (segment) =>
        segment === "." ||
        segment === ".." ||
        segment.includes("/") ||
        segment.includes("\\"),
    )
  ) {
    return Response.json({ error: "代理路径不合法" }, { status: 400 });
  }
  const joinedPath = path.join("/");
  if (
    !joinedPath.startsWith("v1/") &&
    joinedPath !== "health/live" &&
    joinedPath !== "health/ready"
  ) {
    return Response.json({ error: "不允许代理该路径" }, { status: 403 });
  }

  const runtime = getRuntimeEnvironment();
  const requestUrl = new URL(request.url);
  const upstreamUrl = new URL(
    `/${joinedPath}${requestUrl.search}`,
    runtime.GATEWAY_BASE_URL,
  );
  const headers = new Headers();
  for (const name of REQUEST_HEADER_ALLOWLIST) {
    const value = request.headers.get(name);
    if (value) headers.set(name, value);
  }
  headers.set("X-Tenant-ID", directTenantId);
  headers.set("X-User-ID", directUserId);

  try {
    const upstream = await fetch(upstreamUrl, {
      method: request.method,
      headers,
      body:
        request.method === "GET" || request.method === "HEAD"
          ? undefined
          : request.body,
      redirect: "manual",
      signal: request.signal,
    });
    const responseHeaders = new Headers();
    for (const name of RESPONSE_HEADER_ALLOWLIST) {
      const value = upstream.headers.get(name);
      if (value) responseHeaders.set(name, value);
    }
    responseHeaders.set("x-rag-console-proxy", "local-test");
    return new Response(upstream.body, {
      status: upstream.status,
      statusText: upstream.statusText,
      headers: responseHeaders,
    });
  } catch (error) {
    return Response.json(
      {
        error: "无法连接 RAG Gateway",
        detail: error instanceof Error ? error.message : "unknown error",
      },
      { status: 502 },
    );
  }
}

export const GET = proxy;
export const POST = proxy;
export const PATCH = proxy;
export const DELETE = proxy;
