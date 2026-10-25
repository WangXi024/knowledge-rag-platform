/**
 * 开发联调身份：接收 Tenant ID 与 User ID，放入 HttpOnly Cookie，并生成仅供 D1
 * 历史隔离使用的稳定主体键。本模块不验证身份所有权，因此不能被描述为正式认证。
 */
import { getRuntimeEnvironment } from "./runtime-env";

const IDENTITY_COOKIE = "lazytt_direct_identity";
const DEFAULT_IDENTITY_TTL_SECONDS = 43_200;
const MAX_ID_LENGTH = 128;
const encoder = new TextEncoder();

export type User = {
  /** Tenant/User 组合的固定摘要，只用于本地历史记录隔离，绝不代替外部 User ID。 */
  id: string;
  tenantId: string;
  userId: string;
  displayName: string;
};

type IdentityCookiePayload = {
  version: 1;
  tenantId: string;
  userId: string;
};

function bytesToHex(bytes: Uint8Array): string {
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function bytesToBase64Url(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replaceAll("=", "");
}

function base64UrlToBytes(value: string): Uint8Array {
  const base64 = value.replaceAll("-", "+").replaceAll("_", "/");
  const padded = base64.padEnd(Math.ceil(base64.length / 4) * 4, "=");
  const binary = atob(padded);
  return Uint8Array.from(binary, (character) => character.charCodeAt(0));
}

async function principalId(tenantId: string, userId: string): Promise<string> {
  // 与后端主体 Scope 使用同一不可混淆分隔方式；这不是密码哈希或认证签名。
  const canonical = `principal:v1\u001f${tenantId}\u001fuser:${userId}`;
  const digest = await crypto.subtle.digest("SHA-256", encoder.encode(canonical));
  return bytesToHex(new Uint8Array(digest));
}

function normalizeIdentityId(value: unknown, field: "tenantId" | "userId"): string {
  /**
   * ID 是上层 Identity 的不透明值，所以不强制 UUID、邮箱或大小写；这里只与 Gateway
   * 一致地去除首尾空白，并拒绝空值、控制字符和超长输入。
   */
  const normalized = String(value ?? "").trim();
  if (!normalized) throw new Error(field === "tenantId" ? "TENANT_ID_REQUIRED" : "USER_ID_REQUIRED");
  if (normalized.length > MAX_ID_LENGTH) throw new Error(field === "tenantId" ? "TENANT_ID_TOO_LONG" : "USER_ID_TOO_LONG");
  if (/\p{Cc}/u.test(normalized)) throw new Error(field === "tenantId" ? "TENANT_ID_INVALID" : "USER_ID_INVALID");
  return normalized;
}

function identityTtlSeconds(): number {
  const configured = Number.parseInt(getRuntimeEnvironment().SESSION_TTL_SECONDS || "", 10);
  if (!Number.isFinite(configured) || configured < 300 || configured > 604_800) {
    return DEFAULT_IDENTITY_TTL_SECONDS;
  }
  return configured;
}

function encodeIdentity(payload: IdentityCookiePayload): string {
  return bytesToBase64Url(encoder.encode(JSON.stringify(payload)));
}

function readCookie(request: Request, name: string): string | null {
  const cookieHeader = request.headers.get("cookie") ?? "";
  for (const part of cookieHeader.split(";")) {
    const [key, ...value] = part.trim().split("=");
    if (key === name) {
      try { return decodeURIComponent(value.join("=")); } catch { return null; }
    }
  }
  return null;
}

async function toUser(tenantId: string, userId: string): Promise<User> {
  return {
    id: await principalId(tenantId, userId),
    tenantId,
    userId,
    displayName: userId,
  };
}

export async function createDirectIdentity(
  tenantIdInput: unknown,
  userIdInput: unknown,
): Promise<{ user: User; cookieValue: string; maxAge: number }> {
  const tenantId = normalizeIdentityId(tenantIdInput, "tenantId");
  const userId = normalizeIdentityId(userIdInput, "userId");
  return {
    user: await toUser(tenantId, userId),
    cookieValue: encodeIdentity({ version: 1, tenantId, userId }),
    maxAge: identityTtlSeconds(),
  };
}

export async function getAuthenticatedUser(request: Request): Promise<User | null> {
  /**
   * Cookie 只是用户刚才选择的联调身份，不是可信凭证。重新执行字段校验可以阻止手工
   * 修改后的畸形值进入请求头；未来接入正式 Identity 时可保留 User 类型和 BFF 调用点。
   */
  const encoded = readCookie(request, IDENTITY_COOKIE);
  if (!encoded) return null;
  try {
    const payload = JSON.parse(new TextDecoder().decode(base64UrlToBytes(encoded))) as Partial<IdentityCookiePayload>;
    if (payload.version !== 1) return null;
    const tenantId = normalizeIdentityId(payload.tenantId, "tenantId");
    const userId = normalizeIdentityId(payload.userId, "userId");
    return toUser(tenantId, userId);
  } catch {
    return null;
  }
}

export function identityCookie(value: string, maxAge: number, request: Request): string {
  const secure = new URL(request.url).protocol === "https:" ? "; Secure" : "";
  return `${IDENTITY_COOKIE}=${encodeURIComponent(value)}; Path=/; HttpOnly; SameSite=Lax; Max-Age=${maxAge}${secure}`;
}

export function expiredIdentityCookie(request: Request): string {
  return identityCookie("", 0, request);
}
