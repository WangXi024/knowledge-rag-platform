/** 用户站 Cloudflare Binding 类型与必需配置的集中运行时校验。 */
import { env } from "cloudflare:workers";

export type RuntimeEnvironment = {
  DB: D1Database;
  GATEWAY_BASE_URL: string;
  SESSION_TTL_SECONDS: string;
};

export function getRuntimeEnvironment(): RuntimeEnvironment {
  return env as unknown as RuntimeEnvironment;
}
