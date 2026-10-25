/** 退出当前联调身份：只清除浏览器 Cookie，不需要删除账号或服务端 Session。 */
import { expiredIdentityCookie } from "@/lib/auth";

export async function POST(request: Request): Promise<Response> {
  return Response.json({ ok: true }, { headers: { "Set-Cookie": expiredIdentityCookie(request) } });
}
