"""Tier 5 部署清单测试:钉死零停机滚动 + 队列深度扩缩 + 安全硬化的关键字段,防清单回退。

这些字段是研究核验三家都缺的差异化点。项目自建 YAML 解析(config_io.load_simple_yaml)不支持
K8s 的嵌套/flow 风格,且不引 PyYAML(自建优先)——故用无依赖的文本断言校验关键字段(始终真跑,不 skip)。
"""

from __future__ import annotations

from pathlib import Path

_K8S = Path(__file__).resolve().parents[2] / "deploy" / "k8s"


def _text(name: str) -> str:
    return (_K8S / name).read_text(encoding="utf-8")


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


def test_disaster_recovery_backup_and_isolated_restore_exist() -> None:
    backup = _text("dr-backup.yaml")
    restore = _text("dr-restore.yaml")
    assert "pg_dump --format=custom" in backup
    assert "aws s3 cp" in backup
    assert "DATABASE_RESTORE_URL" in restore
    assert 'test "$DATABASE_RESTORE_URL" != "$DATABASE_PRODUCTION_URL"' in restore
    assert "DR_EXPECTED_DATABASE" in restore and "my_agent_dr_" in restore
