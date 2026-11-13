# GraphRAG 一期检查点验收记录

记录日期：2026-11-13。代码检查点：`e79a9fe`，分支
`feature/WangXi024/1/native-graphrag`。计划对应 GitHub Issue #1。

**结论：代码与下列自动检查通过，一期整体验收尚未完成。** 本记录不能替代真实
RAGFlow v0.26.4 的构图闭环验收；目前没有为本期创建或合并 PR，二期尚未开始。
合并目标按项目所有者指示使用 `main`，GitHub Actions 保持关闭，手动检查串行执行。

## 已验证内容

| 检查 | 结果与边界 |
| --- | --- |
| 后端单元与 HTTP/OpenAPI 契约 | 454 passed；1 项 POSIX ECS 检查在 Windows 跳过；4 项 POSIX 0600 权限用例显式排除 |
| 领域/应用覆盖率 | 行与分支合并覆盖率 81.38%，达到当前 80% 门禁；不代表每个模块都达到 80% |
| PostgreSQL 18.4 | 23 passed；真实 SQL 迁移、Scope/FK、CAS、单在途约束、排队取消恢复、旧终态/后继入队的单事务提交 |
| canonical SQL 部署验收 | 1 passed；V1.1→V1.2 带合成业务行升级无损，重复执行拒绝，注释与权限门禁 |
| Redis Stack 7.4.0-v8 | 2 passed；真实向量索引、TTL、同键原子覆盖、过滤摘要/图谱版本/模式隔离 |
| 管理前端 | TypeScript、ESLint、51 项组件/代理模拟回归和生产构建通过；最终版本文案调整后 6 项图谱用例以并发 1 复验通过 |
| 静态门禁 | Ruff、71 个源文件 Mypy、规格路径/引用校验、Git 空白检查通过 |
| 凭据检查 | 私有文件 Git 忽略；Windows 使用原生 DACL 检查，完整 monorepo 跟踪文件与本机敏感值对照，不输出密钥 |
| 四类真实模型端点 | Chat、Embedding、Graph Chat、Vision 最小合成请求分别成功；Embedding 返回 1024 维，OCR 识别出合成图片文字 |

数据库与缓存测试分开运行，测试容器分别限制 1 核、384 MB，采用临时内存挂载；
检查结束即停止并清理。没有并行运行前端构建和后端测试，没有启动项目完整栈。
数据库并发正确性用例仅使用必要的两个事务；Redis 同键用例仅使用两个小写入。

## 可复验命令

Windows 本机使用 Git 忽略的隔离测试环境；以下以 `python` 表示该环境的解释器，
命令工作目录为 `rag-system-backend`。真实密钥和连接串不写入本文。

```powershell
python -m pytest tests/unit tests/contract --cov --cov-report=term-missing -q `
  --deselect=tests/unit/test_prepare_ragflow_env.py::test_first_run_generates_seven_distinct_passwords_and_default_email `
  --deselect=tests/unit/test_prepare_ragflow_env.py::test_repeat_run_preserves_all_existing_values `
  --deselect=tests/unit/test_ragflow_account_bootstrap.py::test_api_key_update_preserves_passwords_and_forces_private_permissions `
  --deselect=tests/unit/test_ragflow_cloud_model_bootstrap.py::test_probe_results_are_written_without_changing_secrets
python -m ruff check src migrations tests tools deploy/ecs rag-development-spec/tools
python -m mypy src
python rag-development-spec/tools/validate_spec.py
python tools/check_tracked_secrets.py
```

上述四项 POSIX 文件模式断言仍须在 Linux 主机复验，不把排除视为已通过。Windows
本机文件权限由独立 DACL 检查确认，不以 Windows 的 `stat` 位冒充 POSIX 0600。
媒体预检单测的可执行存在性夹具改为 Python 路径，避免依赖 Unix 专有的 `true` 命令；
这不修改生产媒体处理逻辑，也不证明真实 FFmpeg/ASR 已验收。

PostgreSQL 测试必须指向独立 `rag_app_test`，禁止使用开发或业务库：

```powershell
python -u -c "import asyncio,pytest; asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy()); raise SystemExit(pytest.main(['tests/integration/test_postgres_scope.py','-q']))"
python -m pytest tests/integration/test_redis_stack_cache.py -q
```

前者通过私有进程环境的 `TEST_DATABASE_DSN`，后者通过 `TEST_CACHE_REDIS_URL` 指向
各自当次的临时容器。Docker 完整镜像、端口和合成凭据不应成为生产配置默认值。

## 尚未验收及合并前提

- 尚无真实 v0.26.4 的“注册原生 Chat→解析合成文档→构图→原文检索与引用”完整证据或脱敏原生响应 Fixture。
- 四类最小 API 请求成功仅证明被测形态可用。原有参数对临时端点曾返回 400；成功探测使用的其他请求形态没有写入运行配置。不为临时 Kimi K3 修改代码或 Token 参数。
- 尚未真实验证文档更新、候选/退役物理版本、删除、回滚与原生图谱的组合行为。适配器严格阻断物理 ID 集合不一致，而不放宽证据边界。
- v0.26.4 原生取消会删除任务记录；取消请求受理不能证明执行器停止。无法确认时保持 `CANCELLING` 并阻断新构图，不能强行标记成功或自动重复收费。需按真实环境手动核实该限制。
- 尚无图谱开关前后的真实召回、引用正确率、耗时和模型成本对照，不宣称业务效果或容量稳定性。

保留以上门禁；不能仅凭 Mock、数据库和缓存检查就关闭 Issue #1、合并 PR 或开始
依赖一期交付的二期。完整操作与人工检查清单见 [NATIVE_GRAPHRAG.md](NATIVE_GRAPHRAG.md)。
