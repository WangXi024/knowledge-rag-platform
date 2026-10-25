/**
 * 生成 Git 忽略的本地用户端配置。
 *
 * Tenant ID 与 User ID 由联调人员进入页面时填写，不读取账号数据库或 JWT 私钥。
 * 该脚本只写 Gateway 地址和身份 Cookie 有效期，并收紧文件权限。
 */
import { chmod, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const scriptDirectory = dirname(fileURLToPath(import.meta.url));
const destinationPath = resolve(scriptDirectory, "../.env.local");

function runtimeValue(name, fallback) {
  // 环境值必须保持单行，避免通过换行向生成文件注入额外配置项。
  const value = process.env[name]?.trim() || fallback;
  if (/[\r\n]/u.test(value)) throw new Error(`${name} must be a single-line value`);
  return value;
}

const destination = [
  `GATEWAY_BASE_URL=${runtimeValue("GATEWAY_BASE_URL", "http://127.0.0.1:8080")}`,
  `SESSION_TTL_SECONDS=${runtimeValue("SESSION_TTL_SECONDS", "43200")}`,
  "",
].join("\n");

await writeFile(destinationPath, destination, { encoding: "utf8", mode: 0o600 });
await chmod(destinationPath, 0o600);
console.log("Prepared ignored user-frontend/.env.local for Tenant/User direct identity mode.");
