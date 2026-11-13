"""PostgreSQL 基础设施入口；导入图谱映射，保证 Base.metadata 始终包含新表。

图谱仓库与原有 Repository 共用 Session 和事务，包初始化只注册映射，不连接数据库。
"""

from rag_service.infrastructure.postgres.graph_models import GraphStateRow

__all__ = ["GraphStateRow"]
