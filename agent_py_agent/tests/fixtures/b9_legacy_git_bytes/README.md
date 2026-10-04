# LLM: 这两份夹具是"历史 Git 字节"的只读存档，用来让测试不再依赖本地对象库里有旧提交。
#   车道容器/CI 只拉一个提交时拿不到这些对象，从源码包装的环境连 git 都没有，用例会直接报
#   "Not a valid object name"。夹具旁边记下来源提交和 sha256，任何人可以复核字节来源。
# 模块用途: 说明 fixtures/b9_legacy_git_bytes 下每份夹具的来源提交与内容摘要。

## 这是什么

两个测试原本用 `git show <提交>:<路径>` 读取"旧版"字节作为对照基线：

- `test_python_builder_preserves_project_license` 经 `_legacy_python_project` 读旧 Python 模板；
- `test_existing_packages_match_baseline_bytes` 经 `_baseline_builder` 读旧构建脚本。

这些字节是**输入基线**，逻辑上属于测试资产，不是运行期数据。存成夹具后，
测试不再要求本地对象库里有那两个提交。

## 夹具清单（全部按原字节存，未做任何改写）

来源提交 `35f445792`（旧 Python 模板，提交内路径前缀
`agent_py_agent/skills/builtin/plugins/write-my-agent-plugin/templates/python/`）：

| 夹具路径 | 来源路径（相对上述前缀） | sha256 |
| --- | --- | --- |
| `legacy_python_template/pyproject.toml` | `pyproject.toml` | `6893535a87072b1e92faf3c96bb31331a283e5ed9ae877265bc7dcf955c49edf` |
| `legacy_python_template/src/plugin_template/__init__.py` | `src/plugin_template/__init__.py` | `40e4437d7d8a3eaca503a5da6c509842a0881f696dbb34bb94676282534f9c2f` |
| `legacy_python_template/src/plugin_template/__main__.py` | `src/plugin_template/__main__.py` | `b3a0f6b766cc11490da40744420efc6b05fdd1e6536bcf4196386f3811dae6c9` |
| `legacy_python_template/src/plugin_template/declaration.json` | `src/plugin_template/declaration.json` | `00a375d364a19b771e0f46ecabae2a5466a7ea6a2ec248f00f085370f79c06f7` |

来源提交 `b453f8883`（旧构建脚本）：

| 夹具路径 | 来源路径 | sha256 |
| --- | --- | --- |
| `legacy_build_script/build_plugin_package.py.txt` | `scripts/build_plugin_package.py` | `c72e793f38739aeafe95c269c564aa6d3e369c599d462422aed59a13f47f2b49` |

构建脚本夹具刻意带 `.txt` 后缀：它是"历史字节"而不是在运的产品源码，
避免被代码尺寸扫描当成产品代码统计（`.py` 会被扫）。用例只读它的字节再
`compile`/`exec`，扩展名不影响语义。Python 模板那四个文件是**数据**配置
（只有在被复制的临时工程里才参与构建），保留原名即可。

## 怎么用与怎么复核

用例直接读夹具目录，不再调用 `git show`。另有一条校验
（`test_legacy_fixture_matches_git_object_when_available`）：**本地对象库里确实有这两个提交时**，
逐字节核对夹具与 `git show` 的结果一致；**对象库里没有时只跳过这一条校验**，
不影响两条原用例照常运行。
