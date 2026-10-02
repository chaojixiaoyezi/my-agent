# shuohao-novel-gates 来源与修改说明

- 上游： https://github.com/eternityspring/shuohao-skills
- 固定提交： `7ebef4f2f53159ee1eaaec2793271a114a8be8cc`
- 上游 Apache-2.0 LICENSE 与 NOTICE 原文随包附带。自己写的 `src/{server,gates,cast,arguments,errors,workspace_files}.js` 包装代码按主仓库 Apache-2.0。
- `src/workspace_read.js`、`conformance.js` 来自主仓库 `plugins/hello-node/`（基线 f15b0a19f）；只给缺少 LLM 层的 helper 补中文契约注释，读取算法保持。
- 以下所有上游文件按字节原样复制，没有修改函数、门、格式或 NUL。`characters` 含一个真实 NUL；.gitattributes 的 -text 禁止文本转换。
- 自检、样例与三张最小镜头卡只在仓库测试，**不进插件 ZIP**；完整私有镜头库未迁入。
- 原 NOTICE 的 `skills/storycast/examples/渡口.txt` 是过时路径，实际在 `skills/novel-characters/examples/渡口.txt`；NOTICE 保持原文。
- 不迁 install.sh、出图调用路线、作者图片。上游 CLI 中未暴露的写命令/HTML renderer 随原文件保留，但不由插件调用；模型只见五个只读声明，参数层拒绝未知操作与写参数。
- `report.mjs` 仅在测试夹具供自检和平台沙箱子进程验证，不进插件包、不对外提供 report 写工具（M-B2 待实施）。
- 五工具同进程调用导出的纯计算函数；不调用 CLI main 和 storyboard logGates，等价于 CLI --no-log。CLI 对照测试显式传 --no-log。全部文件读取先由宿主下发的上下文裁决，再 no-follow 打开并复核；不调用上游隐式 fs 读取助手。

## 原样文件逐项摘要

同份机器清单在 `UPSTREAM.json`。destination 为主仓库相对路径，测试文件只在源码仓库。

| 上游路径 | 本地路径 | SHA-256（上游与本地相同） | 改动 |
| --- | --- | --- | --- |
| `skills/novel-outline/scripts/novel-outline.mjs` | `plugins/shuohao-novel-gates/upstream/skills/novel-outline/scripts/novel-outline.mjs` | `33f1469123333b98b5e56f4b81cd64df80b58e1878e0cf7212b585f6e8ac8597` | 无 |
| `skills/novel-outline/scripts/selftest.mjs` | `agent_py_agent/tests/fixtures/shuohao_skills/skills/novel-outline/scripts/selftest.mjs` | `b900b1d0aff7d3af922f5b59805b3de6f9f01e8c8b6db506e5201b8580fee633` | 无 |
| `skills/novel-outline/examples/渡口-outline.json` | `agent_py_agent/tests/fixtures/shuohao_skills/skills/novel-outline/examples/渡口-outline.json` | `cd257e64909c7ceaef7701b826c3801870abd3c0ded37591ef5825e71627e5c0` | 无 |
| `skills/novel-art/scripts/novel-art.mjs` | `plugins/shuohao-novel-gates/upstream/skills/novel-art/scripts/novel-art.mjs` | `714a5d369c196f463e1e3db4c448b3ecbb69ca956f25094118fc8d5554552eae` | 无 |
| `skills/novel-art/scripts/selftest.mjs` | `agent_py_agent/tests/fixtures/shuohao_skills/skills/novel-art/scripts/selftest.mjs` | `703292498a5f909e38245e12b23a77ac26e12979cae57f69a5aec109eb55c608` | 无 |
| `skills/novel-art/examples/渡口-art.json` | `agent_py_agent/tests/fixtures/shuohao_skills/skills/novel-art/examples/渡口-art.json` | `5bd45de84d31798d6bcc673cef8a727105be16b0e2afefd31e0eb8475ec3afde` | 无 |
| `skills/novel-script/scripts/novel-script.mjs` | `plugins/shuohao-novel-gates/upstream/skills/novel-script/scripts/novel-script.mjs` | `833511f3dcc01373687f12065cb8c843e505958fb344d015535ffb4c928bc7bc` | 无 |
| `skills/novel-script/scripts/selftest.mjs` | `agent_py_agent/tests/fixtures/shuohao_skills/skills/novel-script/scripts/selftest.mjs` | `fc91d7e895f8353d4c98980cf0f65366413c4b5fd290a12ce9e5dd4f24ad6c9c` | 无 |
| `skills/novel-script/examples/渡口-script.json` | `agent_py_agent/tests/fixtures/shuohao_skills/skills/novel-script/examples/渡口-script.json` | `74dd50ba759f86644ca92e150f75b3a4fbddf8708106782f4ff6adcfebc95417` | 无 |
| `skills/novel-storyboard/scripts/novel-storyboard.mjs` | `plugins/shuohao-novel-gates/upstream/skills/novel-storyboard/scripts/novel-storyboard.mjs` | `4df0886f8235ab151a55e052ac1fa81ab59e9c9692fee5c6b2e52f2ce393403b` | 无 |
| `skills/novel-storyboard/scripts/selftest.mjs` | `agent_py_agent/tests/fixtures/shuohao_skills/skills/novel-storyboard/scripts/selftest.mjs` | `53df29c8a8babf062556fe8e028c78ccb55d876d34307a92f194730cfa7499b8` | 无 |
| `skills/novel-storyboard/examples/渡口-storyboard.json` | `agent_py_agent/tests/fixtures/shuohao_skills/skills/novel-storyboard/examples/渡口-storyboard.json` | `4f20333beb43d9431a338b7b3d0be1fff001fb81b8eccea462bd54776e1859dd` | 无 |
| `skills/novel-characters/scripts/novel-characters.mjs` | `plugins/shuohao-novel-gates/upstream/skills/novel-characters/scripts/novel-characters.mjs` | `146cef28dbbe21a2f800de61ca10052cf3d13f79375aaae57c25ce398e15b864` | 无 |
| `skills/novel-characters/scripts/selftest.mjs` | `agent_py_agent/tests/fixtures/shuohao_skills/skills/novel-characters/scripts/selftest.mjs` | `f227c41d5a05c9c6af977fe98625b282150e98291c6f15bc76a4b53fe27794af` | 无 |
| `skills/novel-characters/examples/渡口-cast.json` | `agent_py_agent/tests/fixtures/shuohao_skills/skills/novel-characters/examples/渡口-cast.json` | `f23f692a18749d91253b63b43e4de15d78467e4f87950de15da8fd07a46682e7` | 无 |
| `skills/novel-characters/examples/渡口.txt` | `agent_py_agent/tests/fixtures/shuohao_skills/skills/novel-characters/examples/渡口.txt` | `faa58b6088c49578005591040b44746cf5348e52cb5237850c4f5f07d5168730` | 无 |
| `skills/novel-storyboard/references/test-fixtures/shot-recipes/hands-tell.md` | `agent_py_agent/tests/fixtures/shuohao_skills/skills/novel-storyboard/references/test-fixtures/shot-recipes/hands-tell.md` | `d3d7f1cc14f2d4b7c6a5ad5f91cbeb5bc5042ee45740d29c62c634e1421f2a37` | 无 |
| `skills/novel-storyboard/references/test-fixtures/shot-recipes/insert-beat.md` | `agent_py_agent/tests/fixtures/shuohao_skills/skills/novel-storyboard/references/test-fixtures/shot-recipes/insert-beat.md` | `ccabaabf21a42eacf40e27684e56d40d39c9754601b728d301fe08b1b2032aee` | 无 |
| `skills/novel-storyboard/references/test-fixtures/shot-recipes/ots-shot-reverse.md` | `agent_py_agent/tests/fixtures/shuohao_skills/skills/novel-storyboard/references/test-fixtures/shot-recipes/ots-shot-reverse.md` | `090939b70e2c856926f2d417e69a1a0b34bd5c94781783b6355d8ab73f408885` | 无 |
| `LICENSE` | `plugins/shuohao-novel-gates/LICENSE` | `b1d2870f1a00e4d7f56576e5f0870cba109e041f8af66866cf5b499478e654e7` | 无 |
| `LICENSE` | `agent_py_agent/tests/fixtures/shuohao_skills/LICENSE` | `b1d2870f1a00e4d7f56576e5f0870cba109e041f8af66866cf5b499478e654e7` | 无 |
| `NOTICE` | `plugins/shuohao-novel-gates/NOTICE` | `4a439a29366f485f5ef493a804a4c77a23b41d31eaf97eb66b921e5484bc597d` | 无 |
| `NOTICE` | `agent_py_agent/tests/fixtures/shuohao_skills/NOTICE` | `4a439a29366f485f5ef493a804a4c77a23b41d31eaf97eb66b921e5484bc597d` | 无 |
| `scripts/report.mjs` | `agent_py_agent/tests/fixtures/shuohao_skills/scripts/report.mjs` | `30a1bf139bcbce40b8c0f2ea8c864e9316d429a74fb715e70506de8ae833754c` | 无 |
| `scripts/report-selftest.mjs` | `agent_py_agent/tests/fixtures/shuohao_skills/scripts/report-selftest.mjs` | `2b2e0b4b7ece3533edbda5ce40f1d21ec758feb5892d3be775b3bd7de9441b7c` | 无 |
