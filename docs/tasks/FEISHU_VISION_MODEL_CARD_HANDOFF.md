# Workstream Handoff：飞书模型卡输入模态

## 基本信息

- workstream：飞书原生识图的模型声明表单
- branch：`codex/feishu-native-images`
- worktree：本任务已附唯一共享源码树
- owner：模型卡开发线
- date：2026-10-15
- baseline：`2812638850ff02c091c5bc980019f5c9474a9b30`

## 本线目标

让用户通过原模型卡新增、编辑并查看模型的输入模态；明确声明 `image` 才表示档案声明支持图像，未知不能被展示成纯文本。

## 实际完成

- 原私人表单增加 `input_modalities` 文本输入，编辑预填逗号分隔的原档案声明；密钥仍不预填。
- 保存复用 `validate_input_modalities` 和原 `add/save_profile`，允许原开放标识符；非法输入在写目录前拒绝。
- 新表单明确留空会清为未知，`text` 保持显式纯文本声明；不按模型名或 `native` 工具调用协议推断图像能力，不探测供应商。
- 原模型列表和选择卡展示输入模态；部署默认行只复制原配置 `model_input_modalities`，不建立第二能力源。
- 原有效期内已发出的五字段签名表单仍可提交；其缺字段表示未修改该声明，编辑时保留原档案。仅显式六字段空值清为未知。原签名、30分钟有效期、目录版本及提交摘要仍生效。
- 原个人/群 owner、操作者、私人收集、群配置授权、父卡取消、目录锁与密钥保留规则保持；同卡同参重投幂等，改模态参数重投拒绝。

## 改动文件

- `agent_py_agent/agent/gateway_parts/model_control_cards.py`
- `agent_py_agent/agent/gateway_parts/model_profile_service.py`
- `agent_py_agent/agent/settings/model_profiles.py`：仅 `public_model_profiles` 默认公开行投影及相关注释。
- `agent_py_agent/tests/test_feishu_vision_model_cards.py`
- 本交接文档。

## 测试命令和结果

```bash
python -B -m pytest agent_py_agent/tests/test_feishu_vision_model_cards.py agent_py_agent/tests/test_im_model_cards.py agent_py_agent/tests/test_model_text_control.py agent_py_agent/tests/test_gateway_model_profiles.py agent_py_agent/tests/test_model_profiles.py agent_py_agent/tests/test_compact_media_vision.py::test_input_modalities_are_validated_persisted_and_reach_runtime_config -q --tb=short
ruff check agent_py_agent/agent/gateway_parts/model_control_cards.py agent_py_agent/agent/gateway_parts/model_profile_service.py agent_py_agent/agent/settings/model_profiles.py agent_py_agent/tests/test_feishu_vision_model_cards.py
git diff --check
```

结果：95 passed，0 failed/error/skip；新直接文件16项，覆盖原个人/群签名入口、显示/新增/编辑/清空/旧卡/开放值/非法零写/密钥不外泄/重放。Ruff 与 diff 检查通过。

先红后绿证据：原卡缺字段/拒保存/无展示导致8失败，修复后13通过；部署默认投影另3失败，修复后新增文件16通过。原始 JUnit 已保留在本任务仓外 `model-card` 证据目录，不把模拟结果当真实飞书成功。

## 影响范围

仅原显式模型管理操作，无新工具、配置、网络请求或供应商识别；图片真实下载、解码、入站、委派和模型发送属于其他开发线。本线未提交、推送、发行或操作服务，未读取真实配置或凭据。

## 需要主线重点复查

- 新增/编辑的模态字段沿原签名控制入口，保存目录仍属于原 owner；群私人表单不能改变群授权。
- `[]`/缺声明展示为未知，不等于 `['text']`，不以型号或工具协议补能力。
- 旧五字段只为尚有效的既有签名卡保留其原含义，不另开权限入口或重放账。

## 需要其他线协调

根代理统一同步全局文档、模块 progress/structure 与文件树，并运行严格 gate；本线不并发写这些文件。当前变更未做发行或真实租户识图，须后续按准确提交验收。

## 剩余风险

卡片 JSON 与回调通过隔离 transport 验证，未在真实飞书客户端提交新字段；声明是用户配置，不证明供应商实际支持。未知档案的运行时处理仍由原输入媒体/后端合同负责，卡线不增加硬门。

## 建议下一步

主线先合并完整图片链并运行 focused/strict gate，再用一次普通识图需求验原请求实际图片块；只读跨层安全审查可并行。保持真实配置、服务和平台操作由根独占，不以模态声明替代供应商成功证据。
