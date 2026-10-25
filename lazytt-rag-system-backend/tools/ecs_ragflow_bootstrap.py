"""ECS 显式账号/模型管理任务，复用现有官方 API 适配代码，不访问数据库。

只由管理员通过候选镜像执行。账号输入从 stdin 读取已用官方工具加密的密码；输出
可能含新 API Key，只允许父发布工具捕获并原子保存到受限配置，禁止直接写终端。
模型任务仅返回非敏感兼容字段，供父工具保存；普通 release 不调用本模块。
"""

from __future__ import annotations

import json
import os
import sys

import httpx
from tools.ragflow_account_bootstrap import bootstrap_account
from tools.ragflow_cloud_model_bootstrap import (
    _contract,
    ensure_ragflow_models,
    validate_cloud_models,
)


def main() -> None:
    """执行一个显式管理任务，成功返回所需配置；异常只输出通用状态码。"""
    mode = sys.argv[1]
    if mode == "account":
        request = json.load(sys.stdin)
        with httpx.Client(base_url=os.environ["RAGFLOW_BASE_URL"], timeout=30) as client:
            key, _summary = bootstrap_account(
                client,
                email=request["email"],
                password="",
                configured_api_key=os.getenv("RAGFLOW_API_KEY", ""),
                encrypt_password=lambda _password: request["encrypted_password"],
            )
        configuration = {"RAGFLOW_API_KEY": key}
    elif mode == "models":
        contract = _contract()
        dimension, compatibility = validate_cloud_models(contract, {"steps": []})
        ensure_ragflow_models(contract, dimension, {"steps": []})
        configuration = {
            "EMBEDDING_DIMENSION": str(dimension),
            "RAGFLOW_EMBEDDING_MODEL": contract.embedding_ref,
            "RAGFLOW_VISION_MODEL": contract.vision_ref,
            "RAGFLOW_VISION_RUNTIME_MODEL": contract.vision_model,
            "LLM_OMIT_SAMPLING_PARAMETERS": str(compatibility.omit_sampling_parameters).lower(),
            "LLM_MAX_OUTPUT_TOKENS_FIELD": compatibility.max_output_tokens_field,
            "LLM_EXTRA_BODY_JSON": json.dumps(compatibility.normalized_extra_body()),
        }
    else:
        raise ValueError("unsupported management task")
    print(json.dumps(configuration))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit("RAGFlow 管理任务失败；检查账号、权限与模型契约。") from None
