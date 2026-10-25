// 生成 Git 忽略的本地 Worker 环境文件；这里只写 Gateway 地址，不保存任何身份凭据。
import { chmod, writeFile } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const scriptDirectory = dirname(fileURLToPath(import.meta.url));
const destination = resolve(scriptDirectory, '../.env.local');

await writeFile(destination, 'GATEWAY_BASE_URL=http://127.0.0.1:8080\n', {
  encoding: 'utf8',
  mode: 0o600,
});
await chmod(destination, 0o600);
console.log('已生成 admin-frontend/.env.local（仅包含本地 Gateway 地址）。');
