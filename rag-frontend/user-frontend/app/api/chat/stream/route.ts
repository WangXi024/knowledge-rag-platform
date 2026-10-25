/** 流式问答接口：自动检索共享知识库，转发 Gateway SSE 并清洗事件内容。 */
import { getAuthenticatedUser } from "@/lib/auth";
import { friendlyError, gatewayFetch } from "@/lib/gateway";
import { sanitizedSseBody } from "@/lib/sse";

export async function POST(request: Request): Promise<Response> {
  const user = await getAuthenticatedUser(request);
  if (!user) return Response.json({ error: "身份信息已失效" }, { status: 401 });
  try {
    const body = (await request.json()) as Record<string, unknown>;
    const query = String(body.query ?? "").trim();
    if (!query || query.length > 8000) return Response.json({ error: "请输入有效问题" }, { status: 400 });
    const responseLanguage = body.responseLanguage === "en-US" ? "en-US" : "zh-CN";
    const messages = Array.isArray(body.messages) ? body.messages.slice(-12) : [];
    const upstream = await gatewayFetch(user, "/v1/answers/stream", {
      method: "POST",
      headers: { "Accept": "text/event-stream", "Content-Type": "application/json" },
      body: JSON.stringify({
        knowledge_base_ids: [],
        query,
        messages,
        metadata_filter: null,
        retrieval_profile: "hybrid-default@2",
        // 普通用户端只引用稳定的通用 Profile；真实供应商、地址和密钥全部由后端环境注入。
        generation_profile: "answer-openai-compatible@4",
        response_language: responseLanguage,
      }),
      signal: request.signal,
    });
    if (!upstream.ok || !upstream.body) {
      const text = await upstream.text();
      let message = "回答服务暂时不可用";
      try {
        const payload = JSON.parse(text) as { error?: { message?: string } };
        message = payload.error?.message || message;
      } catch {}
      return Response.json({ error: message }, { status: upstream.status || 502 });
    }
    return new Response(sanitizedSseBody(upstream.body), {
      status: 200,
      headers: {
        "Content-Type": "text/event-stream; charset=utf-8",
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
      },
    });
  } catch (error) {
    return Response.json(friendlyError(error), { status: 503 });
  }
}
