# 在工作区构建并复核最终包

需要已有的、获授权的产品源码副本和 Python；两份 v8 模板只打包文件，不用 pip/setuptools，Node 运行另需已有 node。
产品构建器不随普通 my-agent wheel 提供；不要调用 `my-agent` 管理命令替代构建器，也不联网安装依赖。
下面 `SOURCE`、`WORK` 和 `PY` 都要替换为实际路径：源码、项目、中间文件、产物均在当前授权工作区；
Python 可用当前获授权解释器，不必是产品运行时解释器。脚本只处理作者明确授权的受信源码，不改变安装表。

## 构建命令

```bash
SOURCE='<工作区内产品源码副本>'
WORK='<工作区内插件项目>'
PY='<已有构建环境的 Python>'
mkdir -p "$WORK/tmp" "$WORK/output"
TMPDIR="$WORK/tmp" PYTHONDONTWRITEBYTECODE=1 "$PY" "$SOURCE/scripts/build_plugin_files_package.py" \
  --declaration "$WORK/python/declaration.json" --files-root "$WORK/python" \
  --output "$WORK/output/text-count-python-0.1.0.zip"
TMPDIR="$WORK/tmp" PYTHONDONTWRITEBYTECODE=1 "$PY" "$SOURCE/scripts/build_plugin_files_package.py" \
  --declaration "$WORK/node/declaration.json" --files-root "$WORK/node" \
  --output "$WORK/output/text-count-node-0.1.0.zip"
```

只执行所选语言的命令。两语言都走同一文件构建器，显式 `events/tool_gates/permissions` 选择 v8；不自带 schema_version/sha256。
复制模板后路径按实际项目调整；依赖也须显式列入 `files`，不要给 v8 构建器传 `--wheel`。
输出独占创建，重跑必须选择新文件名或经用户授权处理已有产物；不能把输出存在当作本次构建成功。
文件构建器只读声明和列出的文件，不执行/编译入口、不联网、不安装。引入上游代码要保留真实许可与来源并列入 files，不借模板改许可。
仅制作旧 v1–v5 wheel 工具时才用 `build_plugin_package.py`，离线依赖用 `--wheel`；不能通过旧入口实现 v8 订阅。
该 wheel 构建期工具有意放宽为任意带 `pyproject.toml` 和 `src` 的本地工程，后端会执行工程代码和构建钩子；把工程限制在授权工作区内是会话沙箱和调用方的责任，脚本不做路径围栏，安装/启用不调用它。

## 独立读包检查

构建器发布前已经用产品读包器校验。还可以读回实际路径，检查的是产物而非原始声明：

```bash
PYTHONPATH="$SOURCE" PYTHONDONTWRITEBYTECODE=1 "$PY" - "$WORK/output/text-count-python-0.1.0.zip" <<'PY'
import sys
from pathlib import Path
from agent_py_agent.agent.plugin_package import inspect_plugin_package
package = inspect_plugin_package(Path(sys.argv[1]).read_bytes())
assert package.manifest.to_payload()["schema_version"] == "plugin_package.v8"
assert package.manifest.permissions.network is False  # 原始模板默认，不限制用户明确授权的联网版本
print(package.manifest.to_payload()["schema_version"])
PY
```

Node 换成对应 ZIP。真实读取器核清单、成员、摘要及合法事件/收紧组合，不安装或导入插件。

## 业务组件检查与交付

把**刚构建的包**解到工作区 `tmp/`，Python 用已有解释器 `-I <解包目录>/src/server.py`，Node 用已有 node 执行解出的 `src/server.js`。
用一行一条 JSON 依次输入 `initialize`（`protocolVersion=2024-11-05`）、`notifications/initialized`、`tools/list`、
`tools/call`（`name=count_text`、`arguments.text`）；stdin EOF 应自然结束。
要核对 JSON-RPC id、目录一致、业务返回值、`isError`，不能只看进程退出码。模板统计空文本为 0，`哥哥🙂\n` 为 4 个码点。
至少验证错误类型、未知工具、附加字段、超长输入后还能继续调用；stdout 不得混日志。
初始化回复必须有两个 experimental 能力位的 `versions: ["1"]`，不能仅测试进程退出 0。
随后请求 `my-agent/events.observe`（只用合成提示正文和无正文的工具事实），核 JSON-RPC `result == {}`。
请求 `my-agent/tool-gate.review`，`gate_id=guard-rm`，`call.tool=run_command`，`call.arguments.command="rm -rf build"`：
只把命令当测试数据，核 `verdict=ask, reason_code=RM_RF`；普通 `printf safe` 应 `allow_as_is`，不撤销宿主审批。
未知门返回 `deny`，精确门缺参数返回 `ask`；结果不能含改写参数或执行指令。

仓库回归入口为 `agent_py_agent/tests/test_write_my_agent_plugin_skill.py`，其全部插件样本、历史 wheel 和解包副本都在工作树 `tmp/`，
只调用构建器和独立 stdio，不调用安装或启用服务。缺 Node 时只跳过 Node stdio，不能把跳过写成通过。
修改业务后必须补与用户需求直接相关的测试，模板测试不能证明新增业务正确。

最终报告提供源码和实际 ZIP 路径、当前版本执行结果，明确“宿主安装、启用和真实 TUI/模型调用未验证”。
**B7 前生产启用返回 plugin_events_disabled**；不可通过安装/启用到真实 home 做作者自检。
强制沙箱、默认断网、收窄读、仅 local/main 是 B7 的启用前置合同；本页组件测试没有核对其真实生效。
最后提示用户：“请用 /plugins install <路径> 安装，启用时按界面提示输确认码”。
