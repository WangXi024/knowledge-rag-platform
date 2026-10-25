/**
 * 共享知识库列表 BFF：分页返回当前 Tenant 可读的租户共享库与系统共享库，并提供
 * 两类总数。该接口只调用普通用户 GET 路由，不创建工作区，也不调用 management。
 */
import { getAuthenticatedUser } from "@/lib/auth";
import { gatewayJson } from "@/lib/gateway";

type ScopeFilter = "ALL" | "TENANT" | "SYSTEM";
type GatewayKnowledgeBase = {
  id: string;
  scope_type: "TENANT" | "SYSTEM";
  name: string;
  description?: string | null;
  status: string;
  document_count: number;
  created_at: string;
  updated_at: string;
};
type GatewayPage = {
  data: GatewayKnowledgeBase[];
  meta: { page: number; page_size: number; total: number };
};

function positiveInteger(value: string | null, fallback: number, maximum: number): number {
  /** 把浏览器分页参数收敛到 Gateway 契约允许的范围。 */
  const parsed = Number.parseInt(value ?? "", 10);
  if (!Number.isFinite(parsed) || parsed < 1) return fallback;
  return Math.min(parsed, maximum);
}

function scopeFilter(value: string | null): ScopeFilter {
  return value === "TENANT" || value === "SYSTEM" ? value : "ALL";
}

function gatewayListPath(page: number, pageSize: number, scope: ScopeFilter): string {
  const query = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
  if (scope !== "ALL") query.set("scope_type", scope);
  return `/v1/knowledge-bases?${query.toString()}`;
}

export async function GET(request: Request): Promise<Response> {
  const user = await getAuthenticatedUser(request);
  if (!user) return Response.json({ error: "身份信息已失效" }, { status: 401 });
  try {
    const url = new URL(request.url);
    const page = positiveInteger(url.searchParams.get("page"), 1, 10_000);
    const pageSize = positiveInteger(url.searchParams.get("page_size"), 12, 100);
    const scope = scopeFilter(url.searchParams.get("scope"));

    // 当前页与两类计数分开读取，因此即使知识库很多，页面仍只加载一页卡片，同时能
    // 准确展示全部/租户共享/系统共享三个筛选数量。
    const [listed, tenantCount, systemCount] = await Promise.all([
      gatewayJson<GatewayPage>(user, gatewayListPath(page, pageSize, scope)),
      gatewayJson<GatewayPage>(user, gatewayListPath(1, 1, "TENANT")),
      gatewayJson<GatewayPage>(user, gatewayListPath(1, 1, "SYSTEM")),
    ]);
    return Response.json({
      data: listed.data.map((item) => ({
        id: item.id,
        scopeType: item.scope_type,
        name: item.name,
        description: item.description ?? null,
        status: item.status,
        documentCount: item.document_count,
        createdAt: item.created_at,
        updatedAt: item.updated_at,
      })),
      meta: {
        page: listed.meta.page,
        pageSize: listed.meta.page_size,
        total: listed.meta.total,
      },
      counts: {
        all: tenantCount.meta.total + systemCount.meta.total,
        tenant: tenantCount.meta.total,
        system: systemCount.meta.total,
      },
    });
  } catch {
    // 普通用户只需要产品级错误；下游服务地址、协议内容和请求头不能穿透到浏览器。
    return Response.json({ error: "知识库暂时不可用，请稍后重试" }, { status: 503 });
  }
}
