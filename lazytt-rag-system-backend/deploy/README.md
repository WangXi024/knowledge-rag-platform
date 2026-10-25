# 后端部署入口

- [本地一键部署](local/README.md)：`./deploy/local/start-full.sh`；停止用 `./deploy/local/stop-full.sh`。
- [ECS 开发部署](ecs/README.md)：GitHub Actions 构建、prepare、release；管理员显式执行数据库与首次账号/模型配置。
- `ragflow/`：两种环境共用的官方 RAGFlow PostgreSQL 配置模板。

根目录 `compose.local.yml` 和 `compose.dev.yml` 分别对应本地运行时与 ECS 后端。
本地 Store Compose 位于 `local/docker-compose.storage.yml`；ECS 复用已有共享 Store。
迁移 SQL 只保存在根目录 `migrations/`，角色、权限、注释和验收 SQL 在 `ecs/database/`。
前端不属于本次部署改造。
