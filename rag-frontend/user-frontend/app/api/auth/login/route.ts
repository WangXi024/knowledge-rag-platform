/** 开发联调身份入口：校验 Tenant/User 格式并写入 HttpOnly Cookie，不验证身份所有权。 */
import { createDirectIdentity, identityCookie } from "@/lib/auth";

const IDENTITY_ERRORS: Record<string, string> = {
  TENANT_ID_REQUIRED: "请输入 Tenant ID",
  USER_ID_REQUIRED: "请输入 User ID",
  TENANT_ID_TOO_LONG: "Tenant ID 不能超过 128 个字符",
  USER_ID_TOO_LONG: "User ID 不能超过 128 个字符",
  TENANT_ID_INVALID: "Tenant ID 不能包含控制字符",
  USER_ID_INVALID: "User ID 不能包含控制字符",
};

export async function POST(request: Request): Promise<Response> {
  try {
    const body = (await request.json()) as Record<string, unknown>;
    const result = await createDirectIdentity(body.tenantId, body.userId);
    return Response.json(
      {
        user: {
          tenantId: result.user.tenantId,
          userId: result.user.userId,
          displayName: result.user.displayName,
        },
      },
      { headers: { "Set-Cookie": identityCookie(result.cookieValue, result.maxAge, request) } },
    );
  } catch (error) {
    const message = error instanceof Error ? IDENTITY_ERRORS[error.message] : undefined;
    return Response.json({ error: message || "身份信息格式无效" }, { status: 400 });
  }
}
