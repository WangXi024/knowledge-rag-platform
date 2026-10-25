"use client";

/** 跨面板资源 Hook：并发刷新知识库与文档列表，并统一 loading/error 状态。 */

import { useCallback, useState } from "react";

export type ResourceIds = {
  knowledgeBaseId: string;
  documentId: string;
  jobId: string;
  requestId: string;
  citationId: string;
};

const empty: ResourceIds = {
  knowledgeBaseId: "",
  documentId: "",
  jobId: "",
  requestId: "",
  citationId: "",
};

export function useResources() {
  const [resources, setResourcesState] = useState<ResourceIds>(() => {
    if (typeof window === "undefined") return empty;
    try {
      const saved = window.localStorage.getItem("rag-console-recent-resources");
      return saved ? { ...empty, ...JSON.parse(saved) } : empty;
    } catch {
      return empty;
    }
  });

  const setResources = useCallback((patch: Partial<ResourceIds>) => {
    setResourcesState((current) => {
      const next = { ...current, ...patch };
      window.localStorage.setItem(
        "rag-console-recent-resources",
        JSON.stringify(next),
      );
      return next;
    });
  }, []);

  const resetResources = useCallback(() => {
    // Tenant 变化会改变可访问的租户共享资源；User 变化时也清空最近记录，避免把
    // 浏览器中的上一主体操作状态带入新请求。Gateway 仍会在服务端校验资源范围。
    setResourcesState(empty);
    window.localStorage.removeItem("rag-console-recent-resources");
  }, []);

  return { resources, setResources, resetResources };
}
