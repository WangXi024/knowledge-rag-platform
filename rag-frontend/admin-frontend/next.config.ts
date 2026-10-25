/** 管理端 Next/vinext 配置：允许同源代理接收后端支持的 100 MiB 单文件上传。 */
import type { NextConfig } from 'next';

const nextConfig: NextConfig = {
  experimental: {
    serverActions: {
      // vinext 会先把 multipart POST 识别为可能的 Server Action；额外预留
      // 1 MiB multipart 封装空间，确保 100 MiB 文件可以继续流式转发给 Gateway。
      bodySizeLimit: '101mb',
    },
  },
};

export default nextConfig;
