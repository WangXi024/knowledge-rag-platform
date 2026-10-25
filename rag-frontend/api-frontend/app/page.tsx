/** 测试控制台首页：挂载覆盖全部 Gateway API 的交互式 ConsoleApp。 */
import type { Metadata } from "next";
import { ConsoleApp } from "@/components/console/console-app";

export const metadata: Metadata = {
  title: "开发验证总览 · LazyTT RAG Console",
};

export default function Home() {
  return <ConsoleApp />;
}
