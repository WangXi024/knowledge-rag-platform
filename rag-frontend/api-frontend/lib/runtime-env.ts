/** 对 Cloudflare Worker Bindings 做集中类型声明与运行时必需项检查。 */
import { env } from "cloudflare:workers";

export type RuntimeEnvironment = {
  GATEWAY_BASE_URL: string;
};

export function getRuntimeEnvironment(): RuntimeEnvironment {
  // 开发控制台只需要 Gateway 地址；身份范围由请求中的 Tenant/User Header 明确传入。
  return env as unknown as RuntimeEnvironment;
}
