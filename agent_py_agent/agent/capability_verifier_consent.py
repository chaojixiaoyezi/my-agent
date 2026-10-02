# LLM: 能力包 v2 第 11 条的“执行同意”：声明了检查程序的内容包，启用前要给管理员看宿主会自动跑哪些包内程序，
#   凭确认码启用，并把同意摘要写进内容激活（PluginContentActivation.verifier_consent_sha256）。宿主运行检查程序前
#   用 verifier_consent_sha256 重算比对；包、成员摘要、参数或超时任何一项变了，旧同意就不再有效。
#   确认码只是事实摘要，不是密钥。改动须同步 test_capability_verifier_consent.py 与 plugin_enable_tool。
# 模块用途: 生成检查程序的启用确认内容、确认码、给人看的说明和同意摘要，供启用和运行两处共用同一口径。

from __future__ import annotations

import hashlib
import json

CONSENT_KIND = "capability_verifiers"


# LLM: 只收录声明和安装事实：包身份、每个检查程序的成员路径与 sha、运行方式、参数模板、超时、基线参数名；
#   不含本机路径、用户信息或设置值。成员 sha 取包声明的 files，宿主运行时还会按安装 blob 复核。
# 函数用途: 为一个声明了检查程序的内容包生成启用前确认内容。
def verifier_confirmation_details(manifest, package_sha256: str) -> dict:
    verification = manifest.capability.verification
    digests = {item.path: item.sha256 for item in manifest.files}
    return {
        "kind": CONSENT_KIND, "plugin_id": manifest.plugin_id, "version": manifest.version,
        "package_sha256": package_sha256,
        "verifiers": [{
            "id": item.id, "member": item.member, "member_sha256": digests.get(item.member, ""),
            "runtime": item.runtime, "applies_to": item.applies_to, "args": list(item.args),
            "timeout_seconds": item.timeout_seconds,
            "baseline_flag": item.baseline.flag if item.baseline is not None else "",
        } for item in verification.verifiers],
    }


# LLM: 和 v6 插件确认码同一算法（规范 JSON 的 sha256 前 12 位），用户在自己的命令里原样输入才继续。
# 函数用途: 由确认内容生成 12 位确认码。
def verifier_confirmation_code(details: dict) -> str:
    return verifier_consent_sha256(details)[:12]


# LLM: 完整摘要写进内容激活；运行前用同一函数重算比对，不能只比 12 位确认码。
# 函数用途: 计算确认内容的完整 sha256，作为持久同意摘要。
def verifier_consent_sha256(details: dict) -> str:
    return hashlib.sha256(json.dumps(details, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


# LLM: 判断“这一代激活记录的同意”是否还覆盖当前包声明；任何不一致都返回 False，宿主据此不跑检查程序。
# 函数用途: 运行检查程序前核对管理员当初同意的内容与当前安装是否一致。
def verifier_consent_matches(manifest, package_sha256: str, consent_sha256: str) -> bool:
    if not consent_sha256 or manifest.capability is None or manifest.capability.verification is None:
        return False
    return verifier_consent_sha256(verifier_confirmation_details(manifest, package_sha256)) == consent_sha256


# LLM: 只把确认内容里的结构化事实排成中文给人看，不参与机器判断；最后一行给出带确认码的命令。
# 函数用途: 生成 TUI/IM 里“启用前请确认”的说明。
def verifier_confirmation_message(confirmation: dict) -> str:
    lines = ["启用前需要你确认：这个能力包声明了检查程序，启用后宿主会在你的任务里自动运行它们，"
             "检查交付物（断网、只读交付物、只写临时目录）。",
             f"能力包：{confirmation.get('plugin_id')} {confirmation.get('version')}"
             f"（包摘要 {str(confirmation.get('package_sha256'))[:16]}…）"]
    for item in confirmation.get("verifiers") or []:
        baseline = f"，对照原件参数 {item['baseline_flag']}" if item.get("baseline_flag") else ""
        lines.append(f"  - {item.get('id')}：用 {item.get('runtime')} 运行包内 {item.get('member')}"
                     f"（sha256 {str(item.get('member_sha256'))[:16]}…），参数 {' '.join(item.get('args') or [])}，"
                     f"超时 {item.get('timeout_seconds')} 秒{baseline}")
    lines.append(f"确认无误后输入：/plugins enable {confirmation.get('plugin_id')} --confirm {confirmation.get('confirm_code')}")
    return "\n".join(lines)
