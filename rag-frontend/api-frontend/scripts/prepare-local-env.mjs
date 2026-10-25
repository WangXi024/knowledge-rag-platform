// 生成前端本地 Worker 环境文件；Direct User 模式不再复制 JWT 私钥或账号配置。
import { chmod, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const scriptDirectory = dirname(fileURLToPath(import.meta.url));
const destinationPath = resolve(scriptDirectory, "../.env.local");

const destination = [
  "GATEWAY_BASE_URL=http://127.0.0.1:8080",
  "",
].join("\n");

await writeFile(destinationPath, destination, { encoding: "utf8", mode: 0o600 });
await chmod(destinationPath, 0o600);
console.log("Prepared ignored frontend/.env.local for Direct Tenant/User mode.");
