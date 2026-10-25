/** 用户站 Next/vinext 运行配置。 */
import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  experimental: {
    serverActions: {
      bodySizeLimit: "101mb",
    },
  },
};

export default nextConfig;
