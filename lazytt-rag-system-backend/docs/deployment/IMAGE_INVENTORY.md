# 本地验收镜像清单

记录日期：2026-07-27。Digest 来自本次 Apple Silicon 本机实际拉取/构建结果。
许可证列记录上游项目声明，不能替代公司法务、SBOM 和漏洞扫描。

| 用途 | Tag | 本次 Repo Digest / Image ID | 上游来源 | 上游许可证 |
|---|---|---|---|---|
| RAGFlow | `infiniflow/ragflow:v0.26.4` | `sha256:16d24d1968ab59e2715a85d2590f1569c9539e0362344a42f3a23e8be06a655b` | Docker Hub / infiniflow/ragflow | Apache-2.0 |
| PostgreSQL（两套） | `postgres:18.4-bookworm` | `sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296` | Docker Official Image | PostgreSQL License |
| Unified Redis Stack | `redis/redis-stack-server:7.4.0-v8` | `sha256:798ab84d9f266936b034ab11c4d04a2b8e4b441884c5aa7d17ac951eefdf742a` | Docker Hub / Redis Stack | DB 0 缓存、DB 1 RAGFlow、DB 2 Celery；Redis Stack 模块许可组合，生产需法务复核 |
| Elasticsearch | `elasticsearch:8.11.3` | `sha256:58a3a280935d830215802322e9a0373faaacdfd646477aa7e718939c2f29292a` | Elastic Registry/Docker Hub | Elastic License 2.0 / SSPL 1.0 |
| MinIO | `pgsty/minio:RELEASE.2026-03-25T00-00-00Z` | `sha256:a72bf37c235a83a73890d2a46c5b36801fed61c335175e0396070bf84a8bbb98` | Docker Hub / PGSTY distribution | MinIO 上游 AGPLv3；分发镜像需额外供应链审核 |
| 自研运行时 | `python:3.12.10-slim-bookworm` | 构建时解析并记录到镜像 Provenance | Docker Official Image / Python | PSF License；Debian 包按各自许可 |
| 构建工具 | `ghcr.io/astral-sh/uv:0.11.28` | 构建时解析并记录到镜像 Provenance | GHCR / Astral uv | MIT / Apache-2.0 |
| 契约 Fixture | `python:3.12-alpine` | 仅 `smoke` Profile | Docker Official Image / Python | PSF License；Alpine 包按各自许可 |
| 自研服务 | `local/lazytt-rag-service:dev` | `sha256:c2f2e4d5830ad607a16b743806ce0f4aea1e337d51df6110a2d86f2bcfa2c440` | 本仓库 Dockerfile | 公司项目许可证待仓库所有者确认 |

## 发布要求

- 本地开发 Compose 保留可读 Tag；生产发布将全部 Tag 改为审核后的 Registry
  Digest。
- 生产流水线生成 SBOM、镜像签名和漏洞扫描结果，并由许可证责任人复核 Redis、
  Redis Stack、Elasticsearch 与 PGSTY MinIO 分发镜像。
- RAGFlow 只使用官方固定 v0.26.4，不引用本机同级 `ragflow-main` 源码。
