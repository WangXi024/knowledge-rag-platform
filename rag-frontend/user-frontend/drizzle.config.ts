/** 用户站 Drizzle Kit 配置；保留生成目录与 D1 方言，供未来显式迁移使用。 */
import { defineConfig } from "drizzle-kit";

export default defineConfig({
  out: "./drizzle",
  schema: "./db/schema.ts",
  dialect: "sqlite",
});
