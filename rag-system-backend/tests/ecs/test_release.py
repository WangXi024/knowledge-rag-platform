"""注入发布阶段故障，验证旧服务保留、兼容恢复和秘密写回边界。"""

import json
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import pytest
from deploy.ecs import release as ecs


@pytest.mark.parametrize(
    ("service_name", "host", "allowed"),
    [
        ("rag-gateway", "127.0.0.1", True),
        ("rag-gateway", "0.0.0.0", True),  # noqa: S104 -- 仅测试配置边界，不创建监听。
        ("rag-gateway", "::", False),
        ("ragflow-server", "127.0.0.1", True),
        ("ragflow-server", "0.0.0.0", False),  # noqa: S104 -- 验证管理端公网被拒绝。
        ("rag-worker", "0.0.0.0", False),  # noqa: S104 -- 验证后台服务公网被拒绝。
    ],
)
def test_only_gateway_can_publish_an_explicit_ipv4_endpoint(
    tmp_path: Path, service_name: str, host: str, allowed: bool
) -> None:
    """模拟已解析 Compose，验证公网能力只授予 Gateway，管理及后台服务仍受保护。"""
    image = "ghcr.io/test/rag@sha256:" + uuid4().hex + uuid4().hex
    services = {
        name: {
            "image": image,
            "platform": "linux/amd64",
            "cpus": "1",
            "mem_limit": 1024,
            "pids_limit": 32,
            "logging": {"driver": "json-file"},
            "restart": "always",
            "healthcheck": {"test": ["CMD", "true"]},
        }
        for name in ecs.SERVICES
    }
    services[service_name]["ports"] = [{"host_ip": host}]
    candidate = ecs.Deployment(tmp_path, "")
    candidate.command = Mock(return_value=json.dumps({"name": ecs.PROJECT, "services": services}))
    if allowed:
        assert candidate.validate()["service_count"] == len(ecs.SERVICES)
    else:
        with pytest.raises(ecs.ReleaseError, match="endpoint binding"):
            candidate.validate()


def test_candidate_schema_failure_keeps_live_compose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """只读前检失败时，不能覆盖现有定义或停止服务。"""
    candidate = ecs.Deployment(tmp_path, "new")
    path = tmp_path / "compose.yml"
    path.write_text("old")
    old = Mock()
    old.compose = "old"
    old.running.return_value = ["rag-gateway"]
    monkeypatch.setattr(ecs, "Deployment", Mock(return_value=old))
    candidate.one_shot = Mock(side_effect=ecs.ReleaseError("schema"))
    candidate.command = Mock()
    with pytest.raises(ecs.ReleaseError, match="schema"):
        ecs.release(candidate, path)
    assert path.read_text() == "old"
    candidate.command.assert_not_called()
    old.start.assert_not_called()


@pytest.mark.parametrize("compatible", [True, False])
def test_release_recovers_only_compatible_prior_services(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, compatible: bool
) -> None:
    """更新失败时恢复旧文件；旧镜像无法接受当前结构则保持停止，不回滚 SQL。"""
    path = tmp_path / "compose.yml"
    path.write_text("old")
    old = Mock()
    old.compose = "old"
    old.running.return_value = ["rag-gateway", "ragflow-server"]
    if not compatible:
        old.one_shot.side_effect = ecs.ReleaseError("incompatible")
    candidate = ecs.Deployment(tmp_path, "new")
    candidate.one_shot = Mock()
    candidate.start = Mock(side_effect=ecs.ReleaseError("health"))
    candidate.command = Mock()
    monkeypatch.setattr(ecs, "Deployment", Mock(return_value=old))
    with pytest.raises(ecs.ReleaseError, match="release failed"):
        ecs.release(candidate, path)
    assert path.read_text() == "old"
    candidate.command.assert_called_once_with("stop", *ecs.SERVICES)
    old.one_shot.assert_called_once_with("rag-app-schema-check")
    if compatible:
        old.start.assert_called_once_with("rag-gateway", "ragflow-server")
    else:
        old.start.assert_not_called()


def test_configuration_quoting_and_permissions(tmp_path: Path) -> None:
    """写回只允许既定字段，并保持 Shell 元字符为字面值、文件权限为 0600。"""
    path = tmp_path / "rag.env"
    path.write_text("export KEEP=unchanged\nexport RAGFLOW_API_KEY=old\n")
    path.chmod(0o600)
    value = "test-only; $(printf never-run) ' quoted"
    ecs.save_configuration(path, {"RAGFLOW_API_KEY": value})
    assert path.stat().st_mode & 0o777 == 0o600
    assert "export KEEP=unchanged" in path.read_text()
    assert ecs.shlex.split(path.read_text().splitlines()[-1]) == [
        "export",
        "RAGFLOW_API_KEY=" + value,
    ]
    with pytest.raises(ecs.ReleaseError, match="unexpected"):
        ecs.save_configuration(path, {"ADMIN_PASSWORD": "test-only"})
    path.chmod(0o644)
    with pytest.raises(ecs.ReleaseError, match="0600"):
        ecs.save_configuration(path, {"RAGFLOW_API_KEY": "test-only"})


def test_bootstrap_checks_configuration_before_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """不合规配置文件必须在账号或模型 API 产生副作用之前被拒绝。"""
    path = tmp_path / "rag.env"
    path.write_text("")
    path.chmod(0o644)
    command = Mock()
    monkeypatch.setattr(ecs, "run", command)
    with pytest.raises(ecs.ReleaseError, match="0600"):
        ecs.bootstrap(tmp_path, "bootstrap-models", path)
    command.assert_not_called()


def test_prepare_and_migrate_do_not_require_model_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """首装数据库阶段不依赖未来才生成的 RAGFlow Key 或模型引用。"""
    monkeypatch.delenv("EMBEDDING_DIMENSION", raising=False)
    ecs.validate_environment("prepare")
    ecs.validate_environment("migrate")
    with pytest.raises(ecs.ReleaseError, match="EMBEDDING_DIMENSION"):
        ecs.validate_environment("release")


def test_digest_pinning_does_not_expand_secrets() -> None:
    """候选文件只固化镜像，不把秘密值写入持久 Compose。"""
    image = "ghcr.io/test/backend@sha256:" + "a" * 64
    upstream = "infiniflow/ragflow@sha256:" + "b" * 64
    result = ecs.candidate_text(
        "${RAG_APP_IMAGE} ${RAGFLOW_IMAGE} ${APP_DATABASE_DSN}", image, upstream
    )
    assert result == f"{image} {upstream} ${{APP_DATABASE_DSN}}"
    with pytest.raises(ecs.ReleaseError, match="digests"):
        ecs.candidate_text("", "ghcr.io/test/backend:latest", upstream)


def test_unknown_existing_resource_is_not_adopted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同名网络归属未知时停止，不更改或接管它。"""
    command = Mock(side_effect=[f"{ecs.PROJECT}_ragflow-backplane\n", '[{"Labels": {}}]'])
    monkeypatch.setattr(ecs, "run", command)
    with pytest.raises(ecs.ReleaseError, match="ownership"):
        ecs.Deployment(tmp_path, "").check_ownership()
    assert command.call_count == 2


@pytest.mark.parametrize("matching_revision", [True, False])
def test_prepare_preserves_live_file_and_checks_image_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, matching_revision: bool
) -> None:
    """prepare 固化 ECS 模板路径并保留运行定义；标签 SHA 不一致时拒绝成功回执。"""
    revision = "a" * 40
    image = "ghcr.io/test/rag@sha256:" + "b" * 64
    source = tmp_path / "source.yml"
    source.write_text(
        "image: ${RAG_APP_IMAGE}\nvolumes:\n"
        "- ./deploy/ecs/ragflow/service_conf.postgres.yaml.template:"
        "/ragflow/conf/service_conf.yaml.template:ro\n"
    )
    template = tmp_path / "source.template"
    template.write_text("postgres: {}\n")
    directory = tmp_path / "installed"
    directory.mkdir()
    live = directory / "compose.yml"
    live.write_text("old-running-candidate")
    candidate = Mock()
    deployment_factory = Mock(return_value=candidate)
    monkeypatch.setattr(ecs, "Deployment", deployment_factory)
    monkeypatch.setenv("RAGFLOW_IMAGE", "infiniflow/ragflow@sha256:" + "c" * 64)
    monkeypatch.setattr(
        ecs,
        "run",
        Mock(
            return_value=json.dumps(
                [
                    {
                        "Architecture": "amd64",
                        "Os": "linux",
                        "Config": {
                            "Labels": {
                                "org.opencontainers.image.revision": (
                                    revision if matching_revision else "d" * 40
                                )
                            }
                        },
                    }
                ]
            )
        ),
    )
    monkeypatch.setattr(
        ecs.sys,
        "argv",
        [
            "release.py",
            "--mode",
            "prepare",
            "--directory",
            str(directory),
            "--source-compose",
            str(source),
            "--source-template",
            str(template),
            "--image",
            image,
            "--revision",
            revision,
        ],
    )
    if matching_revision:
        ecs.main()
        receipt = json.loads((directory / "candidate.json").read_text())
        assert receipt["revision"] == revision
    else:
        with pytest.raises(ecs.ReleaseError, match="revision"):
            ecs.main()
        assert not (directory / "candidate.json").exists()
    assert live.read_text() == "old-running-candidate"
    # 部署后的挂载必须指向版本目录，不能残留仓库内的相对路径。
    rendered = deployment_factory.call_args.args[1]
    installed_template = directory / "config" / revision / template.name
    installed_template = installed_template.with_name("service_conf.postgres.yaml.template")
    assert str(installed_template) in rendered
    assert "./deploy/ecs/ragflow/" not in rendered
    assert installed_template.read_text() == template.read_text()
    candidate.start.assert_not_called()
    candidate.one_shot.assert_not_called()


@pytest.mark.parametrize("enabled", [True, False])
def test_mineru_release_registers_before_application(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    """填写 Token 后先启动并登记解析器；留空时不调用云端注册入口。"""
    monkeypatch.setenv("MINERU_CLOUD_API_TOKEN", "test-only" if enabled else "")
    candidate = ecs.Deployment(tmp_path, "services:\n  mineru-cloud-adapter:\n")
    calls = Mock()
    candidate.start = calls.start
    candidate.one_shot = calls.one_shot
    ecs.release(candidate, tmp_path / "compose.yml")
    names = [str(call) for call in calls.mock_calls]
    registration = [i for i, name in enumerate(names) if "mineru_cloud.register" in name]
    assert bool(registration) is enabled
    if enabled:
        assert registration[0] < next(i for i, name in enumerate(names) if "rag-gateway" in name)


def test_mineru_registration_failure_restores_previous_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """模型注册失败时停止候选适配器，并恢复旧版实际运行的适配器。"""
    monkeypatch.setenv("MINERU_CLOUD_API_TOKEN", "test-only")
    path = tmp_path / "compose.yml"
    path.write_text("old")
    old = Mock()
    old.compose = "old"
    old.running.return_value = ["rag-gateway", ecs.MINERU_SERVICE]
    candidate = ecs.Deployment(tmp_path, "services:\n  mineru-cloud-adapter:\n")
    candidate.start = Mock()
    candidate.command = Mock()
    candidate.one_shot = Mock(side_effect=["", ecs.ReleaseError("registration")])
    monkeypatch.setattr(ecs, "Deployment", Mock(return_value=old))
    with pytest.raises(ecs.ReleaseError, match="restored"):
        ecs.release(candidate, path)
    candidate.command.assert_called_once_with("stop", *ecs.SERVICES, ecs.MINERU_SERVICE)
    old.start.assert_called_once_with("rag-gateway", ecs.MINERU_SERVICE)
    assert path.read_text() == "old"


def test_disabling_mineru_stops_previous_adapter_after_application(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """清空 Token 后发布后端成功才停止旧适配器，不删除模型登记或数据。"""
    monkeypatch.setenv("MINERU_CLOUD_API_TOKEN", "")
    path = tmp_path / "compose.yml"
    path.write_text("old")
    old = Mock()
    old.compose = "old"
    old.running.return_value = [ecs.MINERU_SERVICE]
    candidate = ecs.Deployment(tmp_path, "services:\n  mineru-cloud-adapter:\n")
    candidate.start = Mock()
    candidate.one_shot = Mock()
    monkeypatch.setattr(ecs, "Deployment", Mock(return_value=old))
    ecs.release(candidate, path)
    old.command.assert_called_once_with("--profile", "mineru", "stop", ecs.MINERU_SERVICE)
    candidate.start.assert_called_with("rag-gateway", "rag-worker", "rag-scheduler")
