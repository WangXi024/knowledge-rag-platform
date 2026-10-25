/** 管理端根布局：声明中文页面语言、字体变量和浏览器元数据。 */
import type { Metadata } from 'next';
import { Geist, Geist_Mono } from 'next/font/google';
import './globals.css';

const geistSans = Geist({
  variable: '--font-geist-sans',
  subsets: ['latin'],
});

const geistMono = Geist_Mono({
  variable: '--font-geist-mono',
  subsets: ['latin'],
});

export const metadata: Metadata = {
  title: 'LazyTT Admin · 共享知识管理',
  description: 'LazyTT RAG 系统共享知识库管理后台',
};

/** 把所有管理页面包在统一中文 HTML 与字体变量中；该函数不读取身份或外部服务。 */
export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="zh-CN">
      <body
        className={`${geistSans.variable} ${geistMono.variable} antialiased`}
      >
        {children}
      </body>
    </html>
  );
}
