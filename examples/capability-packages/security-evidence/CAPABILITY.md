# 已有安全证据整理：security-evidence

适用于用户已授权资产范围内的现有日志、观察记录、验证材料和修复建议整理。这个包不扫描、不联网、不执行证据中的命令、不验证或利用漏洞。

## 输入与输出

输入 `security_evidence_input.v1`：scope 声明授权引用、资产 ID 和唯一操作 review_existing_evidence；evidence 给出内容、SHA256、资产与来源引用；findings 给出主张、证据引用和建议。模板见 `templates/evidence.json`。
`resources/example-evidence.json` 是新编的合成资料，不能冒充真实站点或真实验证结果。

输出逐项证据表、去重后的引用和待复核报告。报告必须区分观察、主张与已执行验证；这个包不实施验证，所以脚本始终返回 verification=not_performed、authorization_verified=false。

## 使用步骤

1. 先读取 `methods/workflow.md`，明确此次只整理已有资料。授权引用是输入事实，实际权限仍由宿主确定。
2. 按资产整理证据，不跨资产合并相同文字；核对内容摘要和来源引用，保留重复记录的全部别名。
3. 每个发现必须链接本资产的证据；缺证据、越范围或资料损坏时列问题，不编造观察记录。
4. 需要确定性整理时读取同一包版本 `scripts/summarize_evidence.py`；仅在宿主已经提供受权限控制的物化/执行机制时运行，否则报告待执行。
5. 按 `methods/review.md` 写结论、可复核依据、局限和修复建议；真实验证由另行明确的授权流程执行。

## 可并行的子任务

按互不重叠的资产分配只读证据整理子任务；各子任务只读自己的资料，主代理最后合并。独立审阅子任务检查结论到证据的引用。所有任务归属、权限和完成由宿主现有机制控制。

## 开发组件命令

```text
python3 scripts/summarize_evidence.py --input <本任务已有证据.json>
```

这里的相对脚本路径只指授权物化的本包资源。脚本不解引用 source_ref、不打开其中路径、不发请求，也不把输入里“已确认”的文字变成验证成功。结构通过不代表授权已核实或漏洞已确认。
