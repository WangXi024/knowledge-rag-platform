/** 当前联调身份接口：从 HttpOnly Cookie 返回 Tenant ID 与 User ID。 */
import { getAuthenticatedUser } from "@/lib/auth";

export async function GET(request: Request): Promise<Response> {
  const user = await getAuthenticatedUser(request);
  if (!user) return Response.json({ error: "未选择身份" }, { status: 401 });
  return Response.json({
    user: {
      tenantId: user.tenantId,
      userId: user.userId,
      displayName: user.displayName,
    },
  });
}
