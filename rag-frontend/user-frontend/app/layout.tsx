/** 用户站根布局：设置中文页面元数据并加载全局样式。 */
import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "LazyTT · 私人知识助手",
  description: "上传文档，通过可溯源的智能问答获取答案。",
  icons: {
    icon: "/favicon.svg",
    shortcut: "/favicon.svg",
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
