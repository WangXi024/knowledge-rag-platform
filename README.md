# Knowledge RAG Platform

文档知识库与智能问答平台，包含 Python 后端和三个独立 Web 前端。

| 目录 | 职责 | 默认端口 |
|---|---|---|
| [rag-system-backend](rag-system-backend/README.md) | FastAPI Gateway、异步摄取、混合检索、问答、缓存和引用 | 8080 |
| [rag-frontend/api-frontend](rag-frontend/api-frontend/README.md) | 开发测试控制台 | 3000 |
| [rag-frontend/user-frontend](rag-frontend/user-frontend/README.md) | 共享知识浏览与流式问答 | 3001 |
| [rag-frontend/admin-frontend](rag-frontend/admin-frontend/README.md) | 共享知识库与文档管理 | 3002 |

## 运行与身份

后端部署入口是 `rag-system-backend/deploy/local/start-full.sh`，需在后端目录执行。
详细配置见[本地部署手册](rag-system-backend/deploy/local/README.md)。
前端位于本仓库的 `rag-frontend` 目录，各自安装依赖并按对应 README 运行。
部分历史文档保留原仓库路径，应以上表中的实际目录为准。

当前使用 `X-Tenant-ID` 和 `X-User-ID` 进行开发联调，尚未接入正式身份认证。

## GitHub Actions

本仓库创建后、首次推送前已关闭仓库级 GitHub Actions，不运行自动构建或部署。
原部署 workflow 作为源码保留在各子项目的 `.github/workflows` 中。

## 源码快照

本仓库导入本地工作区当前源码（包括当前未提交的源码改动），不导入原仓库 Git 历史。
本机 `.env`、密钥、运行数据库、依赖目录、缓存和构建产物不上传。
