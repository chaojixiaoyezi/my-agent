# drama-media-shell 来源说明

## 上游

- 项目：`zenstory-ai/drama-skills`
- 固定地址：<https://github.com/zenstory-ai/drama-skills/tree/0e8929881bb59248618c4f402707c64723adc017>
- 固定提交：`0e8929881bb59248618c4f402707c64723adc017`
- 上游许可：MIT；原文随包保存为 `LICENSE.drama-skills`
- 上游没有 NOTICE 文件。

## 来源文件与固定 SHA-256

| 上游路径 | SHA-256 | 本仓用途 |
| --- | --- | --- |
| `skills/short-drama-image-prompts/scripts/image_prompt_check.py` | `a6e3217410e366f0626766218e41e5d41501efde87f5f2283c65c7e7adb901f4` | `image_prompt_check.py` 的校验核心 |
| `skills/short-drama-video-prompts/scripts/container_check.py` | `74650a2e5c9fa982f82df17a4b5796f2f80280a1d4a6af69cf67fcafcb351837` | `container_check.py` 的容器核对核心 |
| `skills/short-drama-video-prompts/scripts/motion_timing_check.py` | `3124ae7c2a317028059336e67ca65a117b2c1f585edf3b6abe4f7589c1fea7c5` | `motion_timing_check.py` 的时序核对核心 |
| `skills/short-drama-video-prompts/scripts/music_spec_check.py` | `abcbd5d3fc55b281b5f05989b81919a0a8477bb85d4c580d47cc7de22b95c493` | `music_spec_check.py` 的音乐规范校验核心 |
| `skills/short-drama-produce/scripts/production_tool.py` | `560808219145a958723ff70841d8bac6280eed0ea4d55feb18646444f8661e3e` | prepare/confirm/run/status/audit/collect 状态机合同来源 |
| `skills/short-drama-produce/scripts/fixture_adapter.py` | `f3695da2aa5281b30991515d36863322f93f3d85de574cf30fe81664aec8a730` | `fixture_adapter.py` 固定 PNG、MP4 与静音 WAV 夹具来源 |
| `skills/short-drama-image-prompts/scripts/selftest.py` | `9cc21ea2b835df84289d3d1f6bd363ed20c2cb2f81870a69e75bd53052c01968` | 仓库一致性测试来源 |
| `skills/short-drama-video-prompts/scripts/selftest.py` | `8ba38a8a49c90e0e21b53515bdc34fc5b787a9931530d52c1fe97f8d6880a828` | 仓库一致性测试来源 |
| `skills/short-drama-produce/scripts/selftest.py` | `d93f6717f3a347be3188d0619946002d1e49e86db5315fb550e7cd8c314bcac3` | 外壳状态机回归测试来源 |
| `LICENSE` | `840bdb5ba503ca4397f5a6049e6e8da182330f83bb70006d5656d7dd00674e9b` | `LICENSE.drama-skills` 原文 |

## 改动说明

1. 四个检查器保留上游结构化校验、错误码、计数和状态语义；删除 CLI、`argparse` 和直接路径读取，由 MCP 包装器通过 Python SDK 0.2.0 的逐次读取上下文传入已解析数据。
2. `production_tool.py` 不再寻找或写入上游 `.short-drama` 项目账；冻结作业、确认和运行记录只写宿主提供的 `MY_AGENT_PLUGIN_DATA_DIR`。
3. 工作区输出只经 `WorkspaceWriteContext.check`、`anchor` 和 SDK no-follow 原子写入，随后读回核验。
4. 唯一可执行适配器是包内 `fixture_adapter.py`。夹具完成状态始终带 `fixture=true`、`generation_success=false` 和“夹具，不是真实生成”；它不表示真实媒体生成成功。
5. 真实供应商名称可在作业计划中记录，但 run/collect 会结构化返回“未配置供应商，本插件不调用付费生成”，且不会联网、读取凭据或发起付费请求。
6. 新增的 MCP、SDK 上下文、私有存储和审计包装代码按主仓库许可分发。
7. MP4 夹具（视频模态的最小 `ftyp isom` 容器字节）是本仓新增的，不是上游 `fixture_adapter.py` 的固定字节；上游只有 PNG 与静音 WAV 两种固定夹具。

## 明确未迁移

- `provider_adapters.py`：**未迁移**；它包含 OpenAI、火山方舟和 MiniMax 的付费 HTTPS 调用。
- Remotion 模板、依赖和字幕路线：**未迁移**。
- `examples/creator-first/`、`evaluations/让你管账号/` 及评测小说派生样例：**未迁移**。
- 其他短剧项目生命周期工具和宿主任务账：**未迁移**。
