/**
 * 将锁定版本的 PDF.js worker、字体、字符映射及解码器复制到同源静态资源目录。
 * 只复制依赖资源，不读取业务文件；供 dev/build 前置步骤调用，不依赖外部 CDN。
 */
import { cp, mkdir, readFile } from 'node:fs/promises';

const dependency = new URL('../node_modules/pdfjs-dist/', import.meta.url);
const { version } = JSON.parse(await readFile(new URL('package.json', dependency), 'utf8'));
const target = new URL(`../public/pdfjs/${version}/`, import.meta.url);
await mkdir(target, { recursive: true });
await Promise.all(['build/pdf.worker.min.mjs', 'cmaps', 'standard_fonts', 'wasm'].map(async (name) => {
  const destination = new URL(name.replace('build/', ''), target);
  await cp(new URL(name, dependency), destination, { recursive: true });
}));
