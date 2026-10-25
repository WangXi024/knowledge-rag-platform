/**
 * 共享知识库文档接口：返回当前 Tenant/User 可读知识库中的简化文档分页。
 * 该接口只读，不允许浏览器选择 Parser、上传文件或修改知识库。
 */
import { getAuthenticatedUser } from "@/lib/auth";
import { GatewayError, gatewayJson } from "@/lib/gateway";

type Context = { params: Promise<{ knowledgeBaseId: string }> };
type GatewayDocument = {
  id: string;
  knowledge_base_id: string;
  filename: string;
  mime_type: string;
  file_size_bytes: number;
  status: string;
  progress: number;
  chunk_count: number;
  token_count: number;
  created_at: string;
  updated_at: string;
  last_error?: { message?: string } | null;
};
type GatewayPage = {
  data: GatewayDocument[];
  meta: { page: number; page_size: number; total: number };
};

function positiveInteger(value: string | null, fallback: number, maximum: number): number {
  /** 把不可信查询参数收敛到正整数范围，避免向 Gateway 发送超大分页请求。 */
  const parsed = Number.parseInt(value ?? "", 10);
  if (!Number.isFinite(parsed) || parsed < 1) return fallback;
  return Math.min(parsed, maximum);
}

export async function GET(request: Request, context: Context): Promise<Response> {
  /**
   * BFF 不维护知识库白名单副本，而是把路径 ID 交给共享读取策略判定。跨租户、未来
   * ACL 不可见和真正不存在均由 Gateway 返回 404，避免前端把权限规则写死。
   */
  const user = await getAuthenticatedUser(request);
  if (!user) return Response.json({ error: "身份信息已失效" }, { status: 401 });
  try {
    const { knowledgeBaseId } = await context.params;
    const url = new URL(request.url);
    const page = positiveInteger(url.searchParams.get("page"), 1, 10_000);
    const pageSize = positiveInteger(url.searchParams.get("page_size"), 20, 100);
    const response = await gatewayJson<GatewayPage>(
      user,
      `/v1/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/documents?page=${page}&page_size=${pageSize}`,
    );
    const data = response.data.map((item) => ({
      id: item.id,
      knowledgeBaseId: item.knowledge_base_id,
      filename: item.filename,
      mimeType: item.mime_type,
      size: item.file_size_bytes,
      status: item.status,
      progress: item.progress,
      chunks: item.chunk_count,
      tokens: item.token_count,
      createdAt: item.created_at,
      updatedAt: item.updated_at,
      error: item.last_error?.message ?? null,
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
    // 404 可以安全表达当前 Scope 内不存在；其他下游细节统一隐藏为产品级服务故障。
    if (error instanceof GatewayError && error.status === 404) {
      return Response.json({ error: "知识库不存在" }, { status: 404 });
    }
    return Response.json({ error: "文档暂时不可用，请稍后重试" }, { status: 503 });
  }
}
