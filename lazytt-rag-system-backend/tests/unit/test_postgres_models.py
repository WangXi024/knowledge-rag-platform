"""PostgreSQL ORM 约束测试：防止领域枚举与数据库检查条件再次漂移。

这组测试不需要启动 PostgreSQL，因此会在每次普通单元测试中执行。真实数据库集成
测试仍负责验证 Migration 后的写入和回读；这里提供更早、更便宜的静态防线，避免
因为集成环境未配置而再次漏掉新增节点类型。
"""

from __future__ import annotations

import re

from rag_service.domain.models import IndexNodeType
from rag_service.infrastructure.postgres.models import DocumentIndexNodeRow


def test_index_node_check_constraint_matches_every_domain_node_type() -> None:
    """ORM CHECK 中允许的值必须与 IndexNodeType 完全相同，不能缺少或多出类型。"""
    constraint = next(
        item
        for item in DocumentIndexNodeRow.__table__.constraints
        if item.name == "chk_index_node_type"
    )
    # SQLAlchemy 在这里保留的是项目声明的 SQL 表达式。只提取单引号内的大写枚举值，
    # 再与领域枚举做集合等值比较；这样未来新增节点时必须同步数据库约束。
    allowed_values = set(re.findall(r"'([A-Z]+)'", str(constraint.sqltext)))

    assert allowed_values == {node_type.value for node_type in IndexNodeType}
