/** Next/vinext 配置：启用必要实验特性并声明服务器外部包边界。 */
import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  experimental: {
    serverActions: {
      // vinext currently classifies every multipart POST as a possible
      // progressive Server Action before dispatching Route Handlers. Leave
      // 1 MiB of envelope space above the Gateway's 100 MiB file limit so the
      // authenticated streaming proxy can receive supported uploads.
      bodySizeLimit: "101mb",
    },
  },
};

export default nextConfig;
