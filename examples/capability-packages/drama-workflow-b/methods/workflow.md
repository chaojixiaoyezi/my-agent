# 资料先对齐，再交接制作

本方法参考 shuohao-skills 的分段资料与稳定 ID 设计，改写为独立内容包的小切片。它与 drama-text-a 的原文改编路径不同，不自动覆盖或融合。

| 资料 | 关键字段 | 下游消费 |
| --- | --- | --- |
| 分集目标 | episodes.id/hook/ending/target_seconds | 场次引用 episode_id；按场次归集镜头秒数，与每集显式目标对账 |
| 人物 | characters.id/name/visual_anchor | 场次 character_ids、对白 character_id、角色参考图 |
| 美术 | locations 与 props 的 id/name/continuity_note | 场次 location_id/prop_ids、地点/道具参考图 |
| 剧本 | scenes.id/episode_id/location_id/character_ids/prop_ids 与 beats.id/kind/text；对白另有 character_id | 每个动作/对白节拍有单独编号；说话人必须在场 |
| 分镜 | shots.id/scene_id/beat_ids/seconds/reference_ids | 明确承接哪些节拍，不能只写一段通用视频提示词 |
| 参考资料 | references.id/kind/subject_id/state | kind 为 character/location/prop，subject_id 指向对应表；镜头只引用 references.id |

从 `templates/project.json` 复制所需数量的行并填写本次事实；空字符串、空引用及 null 是待填标记，不能原样交付。`beats` 的动作/对白两行展示各自字段，只保留实际存在的节拍。没有道具时可用 `props=[]`、场次 `prop_ids=[]`，不为占位行虚构道具。角色名和显示文字可以修改，资料引用继续使用 ID；修改上游 ID 或删除对象后，所有下游引用都要复查。
每集 `target_seconds` 与每镜 `seconds` 都用有限正数。检查器以 `shots.scene_id → scenes.episode_id` 为唯一分集计量关系，不读取额外 `episodes.scene_ids` 或场次 `seconds` 来替代实际镜头合计。缺少目标记 `episode_target_missing` 警告，不猜目标；显式空值、布尔值、字符串、非有限数或非正数记错误。合计与目标使用 `rel_tol=1e-12, abs_tol=0.0` 消除相对数值舍入误差，不给极小的合法时长额外绝对误差额度，这不是“约若干秒”的创作容差。实际计划仍有偏差时，据实调整资料或列出差值，不伪改检查结果。

没有参考条目时用 `references=[]` 和镜头 `reference_ids=[]`。确有参考制作计划但没有媒体时建立 `state=planned` 的条目；只有已有输入资料时才声明 `provided`，且仍需独立核对媒体。参考条目 ID、角色/道具 ID、来源段落 ID 是不同关系，不能互相代填。

## 阶段与跨包交接

在本次工作区填写 `templates/handoff.json` 的副本，所有列表都可按实际数量增减；没有省略、新增或未决差异时用空列表，不留下空占位行。

- `files`：为每份实际输入和阶段产物分配唯一资料 ID，写可定位的工作区文件路径与完整 64 位 `sha256`。摘要须来自该文件读取时的真实字节，不能用 `file_version`、包摘要或重新序列化 JSON 的摘要代替；资料修改后重新计算并保留本次交接对应版本。这里是业务文件路径，不是包内资源路径。
- `stages`：记录阶段 ID、工作范围、实际消费的 `input_file_ids`、实际产出的 `output_file_ids` 和已审阅范围；文件 ID 均指向 `files`。没有实际产物就不把计划中的文件列为已产出。
- `object_mappings`：逐项填写来源文件/字段/对象 ID、目标文件/字段/对象 ID 与转换理由。一对多或多对一用多行表示，不因名称相同省略核对。
- `omissions` / `additions`：分别记录省略的来源对象、新增的目标对象及理由；新增内容不能冒充原输入已提供的事实。
- `unresolved_differences`：保留关联文件、对象、具体差异与下一步审阅动作，不能因 final project 可解析就删除未决问题。

这是一份供模型/人审阅的制作资料。其 `schema` 只标识文档格式，不是宿主 task、run、授权、pin 或完成账；填写 `review_notes` 也不证明检查器执行。当前检查器只读取 `drama_workflow_project.v1`，不读取此交接文档，不验证文件摘要、跨文件映射或阶段聚合。需逐项回到明确输入核对，不能把单个 project 的 `structure_valid=true` 称为交接全链已验证。

以文本改编资料转入本包为例：

- `cast.id` 与 `characters.id` 显式对应；逐场核对 `character_ids`，不能只复制角色名字。
- 原 `scenes.source_ids` 保留为来源依据清单，逐项回到原文核对；本包检查器不读取原文，不能替代来源检查，更不能把它们填进 `reference_ids`。
- 原场次和镜头 ID 显式映射到本包 `scenes.id` 与 `shots.scene_id`；补齐本包需要的场次 `episode_id/location_id/character_ids/prop_ids`。
- 原镜头动作需要明确整理成场次内 `beats`，分别填写 `id/kind/text`，对白增加 `character_id`；镜头的 `beat_ids` 逐项引用本场真实节拍。另一包没有这些字段时，不能只造一列节拍编号而没有正文。
- 参考资料单独建立条目，逐镜核对 `reference_ids → references.id → subject_id`；镜头秒数在转换后重新按集汇总。

完整制作资料形成后，用同代脚本资源的 `source_ref` 原样物化，通过现有执行工具运行 `check_continuity.py --project <本次资料>`，记录实际输出、退出码及所检文件引用。发现错误就修订资料后重跑原脚本，不改弱检查器。未执行可以如实交付未验证资料；执行并无结构错误也只说明本脚本覆盖的客观项目，警告、创作与媒体审阅仍单列。

这些是制作资料的方法，不是宿主执行合同。机械检查发现断链应提示修订；创作深度、镜头美感和节奏效果继续由模型/人审阅，不加入通用宿主硬门。
