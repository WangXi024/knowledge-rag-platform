"""Uvicorn 导入入口；模块加载时创建唯一的生产 FastAPI 应用对象。"""

from rag_service.api.app import get_application

# 部署命令通过 ``rag_service.api.main:app`` 定位该对象。
app = get_application()
