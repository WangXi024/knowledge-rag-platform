/**
 * 共享文档切块接口：由后端访问策略验证文档可读后，分页返回纯文本和页码。
 * RAGFlow Chunk ID、坐标、Metadata 与内部错误不会穿透到浏览器。
 */
import { getAuthenticatedUser } from "@/lib/auth";
import { GatewayError, gatewayJson } from "@/lib/gateway";

type Context = { params: Promise<{ documentId: string }> };
type GatewayDocumentResponse = { data: { id: string; knowledge_base_id: string } };
type GatewayChunkPage = {
  data: Array<{
    id: string;
    content: string;
    positions?: Array<{ page?: number | null }>;
  }>;
  meta: { page: number; page_size: number; total: number };
};

function positiveInteger(value: string | null, fallback: number, maximum: number): number {
  /** 把分页参数限制在 Gateway 契约范围内，防止一次把全部切块正文读入内存。 */
  const parsed = Number.parseInt(value ?? "", 10);
  if (!Number.isFinite(parsed) || parsed < 1) return fallback;
  return Math.min(parsed, maximum);
}

export async function GET(request: Request, context: Context): Promise<Response> {
  /**
   * 先读取文档公开详情确认它通过共享访问策略，再分页读取切块。两次 Gateway 调用都
   * 携带同一 Tenant/User；未来成员 ACL 收窄后，BFF 无需增加本地权限判断。
   */
  const user = await getAuthenticatedUser(request);
  if (!user) return Response.json({ error: "身份信息已失效" }, { status: 401 });
  try {
    const { documentId } = await context.params;
    await gatewayJson<GatewayDocumentResponse>(
      user,
      `/v1/documents/${encodeURIComponent(documentId)}`,
    );
    const url = new URL(request.url);
    const page = positiveInteger(url.searchParams.get("page"), 1, 10_000);
    const pageSize = positiveInteger(url.searchParams.get("page_size"), 20, 100);
    const response = await gatewayJson<GatewayChunkPage>(
      user,
      `/v1/documents/${encodeURIComponent(documentId)}/chunks?page=${page}&page_size=${pageSize}`,
    );
    const data = response.data.map((item) => ({
      id: item.id,
      content: item.content,
      pages: Array.from(new Set(
        (item.positions ?? [])
          .map((position) => position.page)
          .filter((value): value is number => typeof value === "number" && value > 0),
      )).sort((left, right) => left - right),
    }));
    return Response.json({
      data,
      meta: {
        page: response.meta.page,
        pageSize: response.meta.page_size,
        total: response.meta.total,
      },
    });
  } catch (error) {
    // 文档不存在可以直接反馈；其余错误不携带下游名称、Token 状态或协议正文。
    if (error instanceof GatewayError && error.status === 404) {
      return Response.json({ error: "文档不存在" }, { status: 404 });
    }
    return Response.json({ error: "内容片段暂时不可用，请稍后重试" }, { status: 503 });
  }
}
