"""显式注册云端 MinerU 模型引用，供管理员使用现有 Bootstrap 容器运行。

只调用 RAGFlow 官方 Provider API，不修改数据库或租户默认模型；重复执行不重建实例。
官网 Token 不参与注册，RAGFlow 仅保存 Docker 网络内的适配服务地址。
"""

import json
import os
from typing import Any

import httpx

from rag_service.infrastructure.mineru_cloud.client import MinerUError

INSTANCE = "mineru-cloud"
MODEL_REF = "mineru-cloud@mineru-cloud@MinerU"
ADAPTER_URL = "http://mineru-cloud-adapter:8000"


def data(client: httpx.Client, method: str, path: str, **kwargs: Any) -> Any:
    """校验官方响应，只抛固定错误；不打印 API Key、请求或响应正文。"""
    response = client.request(method, "/api/v1" + path, **kwargs)
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict) or body.get("code") != 0:
        raise MinerUError("MINERU_REGISTRATION_REJECTED")
    return body.get("data")


def register(client: httpx.Client) -> None:
    """幂等添加独立 OCR 实例和模型；已有同名模型必须仍声明 OCR 类型。"""
    providers = data(client, "GET", "/providers")
    if not isinstance(providers, list):
        raise MinerUError("MINERU_REGISTRATION_INVALID_RESPONSE")
    if not any(p.get("name") == "MinerU" for p in providers):
        data(client, "PUT", "/providers", json={"provider_name": "MinerU"})
    path = "/providers/MinerU/instances"
    instances = data(client, "GET", path)
    if not isinstance(instances, list):
        raise MinerUError("MINERU_REGISTRATION_INVALID_RESPONSE")
    model = {"model_name": INSTANCE, "model_type": ["ocr"], "max_tokens": 0}
    if not any(i.get("instance_name") == INSTANCE for i in instances):
        data(client, "POST", path, json={
            "instance_name": INSTANCE,
            "api_key": json.dumps({
                "mineru_apiserver": ADAPTER_URL, "mineru_backend": "pipeline",
                "mineru_delete_output": "1",
            }),
            "base_url": "", "region": "cloud", "model_info": [model],
        })
    # 回读实例元数据；当前官方接口不返回 api_key，不能将正常脱敏误判为冲突。
    # 兼容返回配置的版本：只有实际提供配置时才校验地址，不修改已有实例。
    detail = data(client, "GET", f"{path}/{INSTANCE}")
    if not isinstance(detail, dict):
        raise MinerUError("MINERU_REGISTRATION_INVALID_RESPONSE")
    config = detail.get("api_key")
    if isinstance(config, str):
        config = json.loads(config)
    if config is not None and (not isinstance(config, dict) or (
        config.get("mineru_apiserver") != ADAPTER_URL
        or config.get("mineru_backend") != "pipeline"
    )):
        raise MinerUError("MINERU_REGISTRATION_INSTANCE_CONFLICT")
    models = data(client, "GET", f"{path}/{INSTANCE}/models")
    if not isinstance(models, list):
        raise MinerUError("MINERU_REGISTRATION_INVALID_RESPONSE")
    matching = [m for m in models if m.get("name") == INSTANCE]
    if not matching:
        data(client, "POST", f"{path}/{INSTANCE}/models", json=model)
    elif "ocr" not in matching[0].get("model_type", []):
        raise MinerUError("MINERU_REGISTRATION_MODEL_CONFLICT")


def main() -> None:
    """从已有 Bootstrap 环境读取 RAGFlow 连接，成功只输出非敏感模型引用。"""
    token = os.environ.get("RAGFLOW_API_KEY", "")
    base_url = os.environ.get("RAGFLOW_BASE_URL", "")
    if not token or not base_url:
        raise SystemExit("RAGFLOW_BASE_URL and RAGFLOW_API_KEY are required")
    try:
        with httpx.Client(trust_env=False, timeout=10) as probe:
            probe.get(ADAPTER_URL + "/health/ready").raise_for_status()
        with httpx.Client(
            base_url=base_url, headers={"Authorization": f"Bearer {token}"},
            trust_env=False, timeout=180,
        ) as client:
            register(client)
    except (httpx.HTTPError, ValueError, MinerUError):
        raise SystemExit(
            "MinerU registration failed; check adapter readiness and RAGFlow"
        ) from None
    print(f"Registered {MODEL_REF}")


if __name__ == "__main__":
    main()
