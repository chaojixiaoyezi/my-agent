# LLM: 环境计划是原 operation 的不可变资源身份，不是第二套安装状态；声明必须先经原执行器 claim，准备器才可写文件。
# 模块用途: 冷读取计划不加载环境执行器；启动前固定包、解释器、配置版本和候选地址，避免扫描目录猜归属。

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from dataclasses import asdict, dataclass


# LLM: 所有字段都是宿主结构化身份，不能含配置正文或个人路径；scope 是完整计划的规范身份，不能只保存不可逆摘要。
# 类用途: 将同一次启用准备的版本和最终环境引用写进原工具资源声明。
@dataclass(frozen=True)
class PluginEnvironmentPlan:
    operation_id: str
    plugin_id: str
    package_sha256: str
    installation_revision: int
    settings_revision: int
    interpreter_fingerprint: str

    # LLM: 标识与版本严格校验，不将布尔值当版本；解释器指纹只用于同一计划核对，不替代执行权限。
    # 函数用途: 在进入操作账前拒绝不完整的环境计划。
    def __post_init__(self) -> None:
        from .plugin_installation import validate_install_identity

        validate_install_identity(self.operation_id, self.installation_revision)
        if not isinstance(self.plugin_id, str) or re.fullmatch(r"[A-Za-z][A-Za-z0-9._-]{0,63}", self.plugin_id) is None:
            raise ValueError("环境计划插件身份无效")
        if (self.installation_revision < 1 or type(self.settings_revision) is not int
                or not 0 <= self.settings_revision <= self.installation_revision):
            raise ValueError("环境计划版本无效")
        for value in (self.package_sha256, self.interpreter_fingerprint):
            if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
                raise ValueError("环境计划摘要无效")

    # LLM: 同操作/包/解释器对应固定地址，重试不得换新地址掩盖旧候选；地址不含用户可控路径片段。
    # 函数用途: 计算 owner 插件目录下的唯一候选名。
    @property
    def environment_ref(self) -> str:
        return hashlib.sha256(json.dumps([
            self.operation_id, self.package_sha256, self.interpreter_fingerprint,
        ]).encode()).hexdigest()

    # LLM: 复用既有 logical resource scope 的持久机制；完整有版本结构作为身份，读回可以恢复计划且不引入新表。
    # 函数用途: 给原 ToolRuntimePolicy 提供可在 claim 前冻结的资源声明。
    @property
    def resource_scope(self) -> str:
        return "logical:plugin_environment:" + json.dumps({
            "schema": "plugin_environment_plan.v1", **asdict(self), "environment_ref": self.environment_ref,
        }, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


# LLM: 只读宿主 Python，不能在生成计划时创建候选或启动进程；调用方须把返回值写原 operation 后再准备。
# 函数用途: 根据同次安装快照和原管理操作生成固定的环境身份。
def plan_plugin_environment(installation, operation_id: str, *, deadline: float | None = None) -> PluginEnvironmentPlan:
    return PluginEnvironmentPlan(
        operation_id, installation.manifest.plugin_id, installation.package_sha256,
        installation.revision, installation.settings_revision, interpreter_fingerprint(deadline),
    )


# LLM: 指纹涵盖宿主版本、解释器内容与位置摘要；只有实际计划生成才加载期限检查，冷读计划不加载准备执行器。
# 函数用途: 拒绝计划生成后被替换的 Python，读取期间继续响应取消和准备期限。
def interpreter_fingerprint(deadline: float | None = None) -> str:
    from .plugin_environment_process import check_preparation_deadline

    digest = hashlib.sha256()
    digest.update(json.dumps([sys.version, sys.implementation.cache_tag, os.path.realpath(sys.executable)]).encode())
    with open(sys.executable, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            if deadline is not None:
                check_preparation_deadline(deadline)
            digest.update(chunk)
    return digest.hexdigest()
