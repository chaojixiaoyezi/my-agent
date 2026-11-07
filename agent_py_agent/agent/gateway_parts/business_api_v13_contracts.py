"""v13 共用的不可变调用与动作声明；不导入注册器或执行领域逻辑。"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class V13Call:
    action: str
    params: dict
    request_id: str
    owner_id: str
    subject_id: str
    context: object = field(repr=False)
    services: object = field(repr=False)
    owner_home: Path
    idempotency_key: str = ''
    expected_revision: int | None = None
    clock: object = field(default=time.time, repr=False)


@dataclass(frozen=True)
class V13Action:
    name: str
    effect: str
    params_schema: dict
    result_schema: dict
    handler: object = field(repr=False)
    accepted_result_schema: dict | None = None
    ready: object = field(default=None, repr=False)
    admin: bool = False
    idempotency_required: bool = False
    expected_revision_required: bool = False
    aliases: tuple[str, ...] = ()
