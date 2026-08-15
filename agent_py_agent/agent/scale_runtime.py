"""正式规模化部署配置与启动硬门。

``MY_AGENT_DEPLOYMENT_MODE=scale`` 是唯一规模模式开关。进入该模式后，运行进程不得
静默回退到 SQLite、进程内限流、无导出的自建 trace 或 stub worker。配置缺口在领取请求
前失败，让编排层摘除未就绪副本。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlsplit


class ScaleConfigurationError(RuntimeError):
    """规模部署配置不完整或仍指向本地回退。"""


class ScaleRole(str, Enum):
    INGRESS = "ingress"
    WORKER = "worker"
    MIGRATE = "migrate"
    MONITOR = "monitor"


def _value(env: Mapping[str, str], name: str) -> str:
    return str(env.get(name) or "").strip()


def _is_postgres_url(url: str) -> bool:
    scheme = urlsplit(url).scheme.lower()
    return scheme in {"postgres", "postgresql", "postgresql+psycopg"}


def _is_redis_url(url: str) -> bool:
    return urlsplit(url).scheme.lower() in {"redis", "rediss", "unix"}


@dataclass(frozen=True)
class ScaleRuntimeConfig:
    role: ScaleRole
    deployment_mode: str
    database_url: str
    migration_database_url: str
    database_app_role: str
    redis_url: str
    otlp_endpoint: str
    worker_handler: str
    worker_downstream: str
    tenant_id: str
    agent_config_path: str
    agent_home: str
    workspace_root: str
    release_channel: str
    owner_store: str
    owner_s3_bucket: str
    owner_s3_prefix: str
    owner_s3_endpoint: str
    owner_s3_region: str
    owner_s3_sse: str
    owner_s3_kms_key: str
    feishu_app_id: str
    feishu_app_secret: str
    feishu_encrypt_key: str
    feishu_verification_token: str

    @property
    def is_scale(self) -> bool:
        return self.deployment_mode == "scale"

    @classmethod
    def from_env(
        cls,
        role: ScaleRole | str,
        env: Mapping[str, str],
    ) -> ScaleRuntimeConfig:
        resolved_role = ScaleRole(role)
        mode = _value(env, "MY_AGENT_DEPLOYMENT_MODE").lower() or "local"
        if mode not in {"local", "scale"}:
            raise ScaleConfigurationError("MY_AGENT_DEPLOYMENT_MODE 只允许 local 或 scale")
        config = cls(
            role=resolved_role,
            deployment_mode=mode,
            database_url=_value(env, "DATABASE_URL"),
            migration_database_url=_value(env, "DATABASE_MIGRATION_URL"),
            database_app_role=_value(env, "DATABASE_APP_ROLE"),
            redis_url=_value(env, "REDIS_URL"),
            otlp_endpoint=(
                _value(env, "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
                or _value(env, "OTEL_EXPORTER_OTLP_ENDPOINT")
            ),
            worker_handler=_value(env, "WORKER_HANDLER"),
            worker_downstream=_value(env, "WORKER_DOWNSTREAM"),
            tenant_id=_value(env, "MY_AGENT_TENANT_ID"),
            agent_config_path=_value(env, "MY_AGENT_CONFIG"),
            agent_home=_value(env, "MY_AGENT_HOME"),
            workspace_root=_value(env, "MY_AGENT_SCALE_WORKSPACE_ROOT"),
            release_channel=_value(env, "MY_AGENT_RELEASE_CHANNEL").lower() or "stable",
            owner_store=_value(env, "MY_AGENT_OWNER_STORE").lower(),
            owner_s3_bucket=_value(env, "MY_AGENT_OWNER_S3_BUCKET"),
            owner_s3_prefix=_value(env, "MY_AGENT_OWNER_S3_PREFIX").strip("/"),
            owner_s3_endpoint=_value(env, "MY_AGENT_OWNER_S3_ENDPOINT"),
            owner_s3_region=_value(env, "MY_AGENT_OWNER_S3_REGION") or "us-east-1",
            owner_s3_sse=_value(env, "MY_AGENT_OWNER_S3_SSE") or "AES256",
            owner_s3_kms_key=_value(env, "MY_AGENT_OWNER_S3_KMS_KEY"),
            feishu_app_id=_value(env, "FEISHU_APP_ID"),
            feishu_app_secret=_value(env, "FEISHU_APP_SECRET"),
            feishu_encrypt_key=_value(env, "FEISHU_ENCRYPT_KEY"),
            feishu_verification_token=_value(env, "FEISHU_VERIFICATION_TOKEN"),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if not self.is_scale:
            return
        if self.release_channel not in {"stable", "canary"}:
            raise ScaleConfigurationError("MY_AGENT_RELEASE_CHANNEL 只允许 stable 或 canary")
        database_url = self.migration_database_url if self.role is ScaleRole.MIGRATE else self.database_url
        if not _is_postgres_url(database_url):
            name = "DATABASE_MIGRATION_URL" if self.role is ScaleRole.MIGRATE else "DATABASE_URL"
            raise ScaleConfigurationError(f"scale/{self.role.value} 要求 {name}=PostgreSQL URL，禁止 SQLite 回退")
        if not self.database_app_role or not self.database_app_role.replace("_", "").isalnum():
            raise ScaleConfigurationError("scale 模式要求合法 DATABASE_APP_ROLE（非超级用户、无 BYPASSRLS）")
        if self.role in {ScaleRole.INGRESS, ScaleRole.WORKER, ScaleRole.MONITOR}:
            if not _is_redis_url(self.redis_url):
                raise ScaleConfigurationError(f"scale/{self.role.value} 要求 REDIS_URL")
            if not self.otlp_endpoint:
                raise ScaleConfigurationError(
                    f"scale/{self.role.value} 要求 OTEL_EXPORTER_OTLP_ENDPOINT 或 OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"
                )
        if self.role is ScaleRole.MONITOR and not self.agent_home:
            raise ScaleConfigurationError("scale/monitor 要求 MY_AGENT_HOME")
        if self.role is ScaleRole.INGRESS and not (
            self.feishu_encrypt_key or self.feishu_verification_token
        ):
            raise ScaleConfigurationError("scale/ingress 要求飞书 encrypt key 或 verification token")
        if self.role is ScaleRole.INGRESS and not self.tenant_id:
            raise ScaleConfigurationError("scale/ingress 要求 MY_AGENT_TENANT_ID，禁止把所有请求落到 default 租户")
        if self.role is ScaleRole.WORKER:
            if not self.worker_handler:
                raise ScaleConfigurationError("scale/worker 要求显式 WORKER_HANDLER，禁止 stub handler")
            if self.worker_handler == "agent_py_agent.agent.worker_entry:_stub_handler":
                raise ScaleConfigurationError("scale/worker 禁止 stub handler")
            if not self.worker_downstream:
                raise ScaleConfigurationError("scale/worker 要求 WORKER_DOWNSTREAM，禁止空下游")
            if not self.agent_home or not self.workspace_root:
                raise ScaleConfigurationError("scale/worker 要求 MY_AGENT_HOME 与 MY_AGENT_SCALE_WORKSPACE_ROOT")
            if self.owner_store != "s3" or not self.owner_s3_bucket:
                raise ScaleConfigurationError(
                    "scale/worker 要求 MY_AGENT_OWNER_STORE=s3 与 MY_AGENT_OWNER_S3_BUCKET，禁止 RWX 事实源回退"
                )
            if self.owner_s3_sse not in {"AES256", "aws:kms"}:
                raise ScaleConfigurationError("MY_AGENT_OWNER_S3_SSE 只允许 AES256 或 aws:kms")
            if self.owner_s3_sse == "aws:kms" and not self.owner_s3_kms_key:
                raise ScaleConfigurationError("aws:kms owner store 要求 MY_AGENT_OWNER_S3_KMS_KEY")
            if not self.feishu_app_id or not self.feishu_app_secret:
                raise ScaleConfigurationError("scale/worker 要求 FEISHU_APP_ID/FEISHU_APP_SECRET 用于可靠回执")


__all__ = ["ScaleConfigurationError", "ScaleRole", "ScaleRuntimeConfig"]
