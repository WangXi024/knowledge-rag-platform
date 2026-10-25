/** Cloudflare Worker 运行环境只需要 Gateway 地址；本地未配置时使用仓库默认端口。 */
import { env } from 'cloudflare:workers';

export function getGatewayBaseUrl(): string {
  const value = (env as unknown as { GATEWAY_BASE_URL?: string }).GATEWAY_BASE_URL?.trim();
  return value || 'http://127.0.0.1:8080';
}
