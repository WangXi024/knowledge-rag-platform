/** 用户检索接口：由 Gateway 自动选择当前租户与系统共享知识库。 */
import { getAuthenticatedUser } from "@/lib/auth";
import { friendlyError, gatewayJson } from "@/lib/gateway";

type Candidate = {
  chunk_id: string;
  document_id: string;
  document_name: string;
  heading_path?: string[];
  content: string;
  positions?: Array<{ page?: number }>;
  scores: { score: number; vector_score?: number | null; term_score?: number | null; rerank_score?: number | null };
};

export async function POST(request: Request): Promise<Response> {
  const user = await getAuthenticatedUser(request);
  if (!user) return Response.json({ error: "身份信息已失效" }, { status: 401 });
  try {
    const body = (await request.json()) as Record<string, unknown>;
    const query = String(body.query ?? "").trim();
    if (!query || query.length > 8000) return Response.json({ error: "请输入有效问题" }, { status: 400 });
    const startedAt = performance.now();
    const response = await gatewayJson<{
      data: { original_query: string; effective_query: string; candidates: Candidate[]; total: number };
    }>(user, "/v1/retrieval", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        knowledge_base_ids: [],
        document_ids: [],
        query,
        metadata_filter: null,
        retrieval_profile: "hybrid-default@2",
        options: { page_size: 10, include_content: true, include_positions: true },
      }),
    });
    return Response.json({
      data: {
        total: response.data.total,
        durationMs: Math.round((performance.now() - startedAt) * 10) / 10,
        candidates: response.data.candidates.map((candidate) => ({
          // Gateway 已经按真实 Cross-Encoder 分数完成最终排序；这里只把非敏感分数
          // 透传给浏览器用于解释结果，不返回供应商、API Key 或故障诊断信息。
          chunkId: candidate.chunk_id,
          documentId: candidate.document_id,
          documentName: candidate.document_name,
          headingPath: candidate.heading_path ?? [],
          content: candidate.content,
          page: candidate.positions?.find((position) => position.page)?.page ?? null,
          score: candidate.scores.score,
          vectorScore: candidate.scores.vector_score ?? null,
          termScore: candidate.scores.term_score ?? null,
          rerankScore: candidate.scores.rerank_score ?? null,
        })),
      },
    });
  } catch (error) {
    return Response.json(friendlyError(error), { status: 503 });
  }
}
