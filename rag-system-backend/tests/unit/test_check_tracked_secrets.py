"""密钥扫描字段分类的回归测试。

该工具会读取本机 Git 忽略环境中的敏感值，再确认这些值没有出现在任何跟踪文件。
这里仅验证字段名分类，不读取真实环境，也不构造真实密钥，防止通用模型新增的
``MAX_OUTPUT_TOKENS_FIELD`` 被误当成访问令牌，同时确保密码和 API Key 仍受保护。
"""

from __future__ import annotations

from pathlib import Path

from tools.check_tracked_secrets import SENSITIVE_NAME, _sensitive_values


def test_sensitive_name_matches_credentials_but_not_token_count_configuration() -> None:
    """完整凭据字段必须命中，而复数 Token 计数字段不能造成误报。"""
    assert SENSITIVE_NAME.search("LLM_API_KEY")
    assert SENSITIVE_NAME.search("RAGFLOW_ADMIN_PASSWORD")
    assert SENSITIVE_NAME.search("SERVICE_TOKEN")
    assert SENSITIVE_NAME.search("JWT_PRIVATE_KEY")
    assert not SENSITIVE_NAME.search("LLM_MAX_OUTPUT_TOKENS_FIELD")
    assert not SENSITIVE_NAME.search("TOKENIZER_MODEL")


def test_scan_samples_ignore_generation_placeholders_and_unquote_real_values(
    tmp_path: Path,
) -> None:
    """生成占位值不冒充真实密钥；带引号的真实样本仍须按完整值匹配跟踪内容。"""
    directory = tmp_path / "deploy/local"
    directory.mkdir(parents=True)
    (directory / ".env.ragflow").write_text(
        "POSTGRES_PASSWORD=change-me-auto-generated\n"
        "LLM_API_KEY='fixture-private-value-for-scan'\n"
        "LLM_MODEL=public-model-name\n",
        encoding="utf-8",
    )
    assert _sensitive_values(tmp_path) == {b"fixture-private-value-for-scan"}
