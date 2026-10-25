/** 会话历史接口：按当前 Tenant/User 联调主体列出、读取、保存或删除对话。 */
import { getAuthenticatedUser } from "@/lib/auth";
import { deleteConversation, getConversation, listConversations, saveTurn } from "@/lib/history";

export async function GET(request: Request): Promise<Response> {
  const user = await getAuthenticatedUser(request);
  if (!user) return Response.json({ error: "身份信息已失效" }, { status: 401 });
  const id = new URL(request.url).searchParams.get("id");
  if (!id) return Response.json({ data: await listConversations(user) });
  const data = await getConversation(user, id);
  if (!data) return Response.json({ error: "对话不存在" }, { status: 404 });
  return Response.json({ data });
}

export async function POST(request: Request): Promise<Response> {
  const user = await getAuthenticatedUser(request);
  if (!user) return Response.json({ error: "身份信息已失效" }, { status: 401 });
  try {
    const body = (await request.json()) as Record<string, unknown>;
    const question = String(body.question ?? "").trim();
    const answer = String(body.answer ?? "").trim();
    if (!question || !answer) return Response.json({ error: "对话内容不能为空" }, { status: 400 });
    const conversationId = await saveTurn(user, {
      conversationId: body.conversationId ? String(body.conversationId) : undefined,
      question,
      answer,
      evidence: body.evidence && typeof body.evidence === "object" ? body.evidence : [],
      requestId: body.requestId ? String(body.requestId) : undefined,
    });
    return Response.json({ data: { conversationId } }, { status: 201 });
  } catch (error) {
    if (error instanceof Error && error.message === "CONVERSATION_NOT_FOUND") return Response.json({ error: "对话不存在" }, { status: 404 });
    return Response.json({ error: "保存对话失败" }, { status: 500 });
  }
}

export async function DELETE(request: Request): Promise<Response> {
  const user = await getAuthenticatedUser(request);
  if (!user) return Response.json({ error: "身份信息已失效" }, { status: 401 });
  const id = new URL(request.url).searchParams.get("id");
  if (!id) return Response.json({ error: "缺少对话" }, { status: 400 });
  const deleted = await deleteConversation(user, id);
  return deleted ? Response.json({ ok: true }) : Response.json({ error: "对话不存在" }, { status: 404 });
}
