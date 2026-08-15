"""Tier 5 部署清单测试:钉死零停机滚动 + 队列深度扩缩 + 安全硬化的关键字段,防清单回退。

这些字段是研究核验三家都缺的差异化点。项目自建 YAML 解析(config_io.load_simple_yaml)不支持
K8s 的嵌套/flow 风格,且不引 PyYAML(自建优先)——故用无依赖的文本断言校验关键字段(始终真跑,不 skip)。
"""

from __future__ import annotations

from pathlib import Path

_K8S = Path(__file__).resolve().parents[2] / "deploy" / "k8s"


def _text(name: str) -> str:
    return (_K8S / name).read_text(encoding="utf-8")


def test_ingress_zero_downtime_fields() -> None:
    t = _text("ingress.yaml")
    assert "maxUnavailable: 0" in t  # 新 pod 就绪前不减旧 = 零停机
    assert "path: /readyz" in t  # 就绪门
    assert "preStop:" in t and '["sleep", "5"]' in t  # 退出前漏排
    assert "terminationGracePeriodSeconds: 60" in t
    assert "kind: PodDisruptionBudget" in t  # 防维护期全干掉
    assert "kind: HorizontalPodAutoscaler" in t


def test_worker_keda_queue_depth_scaling() -> None:
    t = _text("worker.yaml")
    assert "kind: ScaledObject" in t  # KEDA 扩缩
    assert "type: postgresql" in t
    assert "ingress_messages" in t and "pending" in t  # 按真实队列待处理积压扩缩
    assert "terminationGracePeriodSeconds: 120" in t  # 给在途 LLM turn 跑完余量
    assert "kind: PodDisruptionBudget" in t


def test_security_hardening_present() -> None:
    t = _text("ingress.yaml")
    assert "readOnlyRootFilesystem: true" in t
    assert "allowPrivilegeEscalation: false" in t
    assert 'drop: ["ALL"]' in t  # 丢弃所有 capability


def test_both_tiers_run_as_nonroot() -> None:
    for name in ("ingress.yaml", "worker.yaml"):
        assert "runAsNonRoot: true" in _text(name)


def test_manifests_reference_real_entrypoints() -> None:
    # 清单的 command 必须指向真实存在的入口模块(防引用不存在模块导致起容器即崩)
    assert "agent_py_agent.agent.asgi_entry" in _text("ingress.yaml")
    assert "agent_py_agent.agent.worker_entry" in _text("worker.yaml")
    for mod in ("asgi_entry", "worker_entry"):
        assert (Path(__file__).resolve().parents[1] / "agent" / f"{mod}.py").exists()


def test_scale_profile_and_online_migration_are_formal_manifests() -> None:
    config = _text("config.yaml")
    migration = _text("migration.yaml")
    assert 'MY_AGENT_DEPLOYMENT_MODE: "scale"' in config
    assert "DATABASE_APP_ROLE" in config
    assert "WORKER_DOWNSTREAM" in config
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" in config
    assert "agent_py_agent.agent.migrate_entry" in migration
    assert "DATABASE_MIGRATION_URL" in migration


def test_scale_pods_require_redis_and_application_database_url() -> None:
    for name in ("ingress.yaml", "worker.yaml"):
        text = _text(name)
        assert "my-agent-scale" in text
        assert "DATABASE_URL" in text
        assert "REDIS_URL" in text


def test_worker_owner_state_uses_s3_and_ephemeral_cache_not_rwx() -> None:
    worker = _text("worker.yaml")
    storage = _text("storage.yaml")
    config = _text("config.yaml")
    assert "my-agent-owner-store" in worker
    assert "owner-cache" in worker and "emptyDir:" in worker
    assert "MY_AGENT_OWNER_STORE: \"s3\"" in config
    assert "ReadWriteMany" not in worker + storage


def test_continuous_monitor_is_a_production_process_not_watch_harness() -> None:
    monitor = _text("monitor.yaml")
    assert "agent_py_agent.agent.continuous_monitor_entry" in monitor
    assert "scripts/watch_harness" not in monitor
    assert "my-agent-monitor-state" in monitor


def test_canary_routes_ingress_and_worker_through_same_channel() -> None:
    canary = _text("canary.yaml")
    route = _text("canary-route.yaml")
    assert canary.count('MY_AGENT_RELEASE_CHANNEL, value: "canary"') == 2
    assert "release_channel = 'canary'" in canary
    assert "my-agent-ingress-canary" in route and "weight: 0" in route


def test_disaster_recovery_backup_and_isolated_restore_exist() -> None:
    backup = _text("dr-backup.yaml")
    restore = _text("dr-restore.yaml")
    assert "pg_dump --format=custom" in backup
    assert "aws s3 cp" in backup
    assert "DATABASE_RESTORE_URL" in restore
    assert 'test "$DATABASE_RESTORE_URL" != "$DATABASE_PRODUCTION_URL"' in restore
    assert "DR_EXPECTED_DATABASE" in restore and "my_agent_dr_" in restore
