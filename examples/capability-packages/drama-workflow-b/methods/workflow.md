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

### 分开整理时（角色、道具、镜头参考各写各的）

用户要求把角色、道具和镜头参考分开整理时：

- **各表独立**：人物进 `characters`、道具进 `props`、镜头参考进 `references` 与各镜头的 `reference_ids`，各自维护、不互相代填；不要用文字描述或来源段落 ID 顶替参考条目。
- **每个镜头都要有人物参考**：镜头引用的节拍里出现的说话人和动作角色，这一镜的 `reference_ids` 里要有对应的人物参考条目；项目里有人物参考计划时，缺一条就报 `shot_character_reference_missing` 提醒。
- **缺的单独列出**：还没有的参考和素材在 `unresolved_differences` 或交付说明的缺项清单里逐条写明（缺什么、下一步谁补），不留下空引用、空 ID 或 null 占位；写进清单前先回产物逐项复核，确认真的没有——已存在的条目不能写成缺项，表格与结论必须一致。

## 阶段与跨包交接

在本次工作区写出**一份** `drama_workflow_handoff.v2` 交接文件（建议 `output/handoff.json`）。v1 的自由字段不会被猜成 v2 地址；旧文件保留原结果，需要按新合同显式重新编写。列表按实际数量增减；没有映射、省略、新增或未决差异时用空列表，不留下空占位行。

### 交接文件清单（写在哪、叫什么、怎么绑进标准链）

1. **只保留一份**：整个工作区里只能有一份 `schema` 为 `drama_workflow_handoff.v2` 的 JSON 文件。不要把模板另存到 `tmp/`、不要把草稿写到第二个位置、不要在交付后再留同名副本。宿主按“本回合写出的、字段匹配的交接文件恰好一份”自动找它；出现两份（或零份）时交接不会进入标准检查链，基线对比会把所有改动当成“没列出”。
2. **写在交付物旁边**：放在本次工作区内、与交付物同目录最稳（例如 `output/handoff.json`）；不要在包安装目录或工作区外写。
3. **六个键都写上**：`files`、`stages`、`object_mappings`、`omissions`、`additions`、`unresolved_differences`；没有内容的用 `[]`。
4. **摘要来自真实字节**：`files[].sha256` 用该文件读取时的完整 64 位小写 SHA-256；资料改动后重新计算，再更新交接。
5. **手动检查时显式绑定**：自己运行检查器时传 `--handoff output/handoff.json`，并对 `files` 里每个 ID 传一个 `--input-file FILE_ID=PATH`；绑定路径与 `files[].path` 按同一 cwd 比较。
6. **交给宿主自动核验**：宿主开启包检查时，写出交付物和交接后，工具回执里会附宿主用本包原版检查程序得出的结论（`pack_verification`）；按它修订，不要复制、改写或自写检查程序。

完整示例（公开合成故事《最后一班夜车》，假设把 `inputs/project.json` 整理成 `output/project.json`；示例里的路径、摘要和文字都要换成本次任务的真实值，摘要不能照抄）：

```json
{
  "schema": "drama_workflow_handoff.v2",
  "files": [
    {"id": "F01", "path": "inputs/project.json", "sha256": "<F01 文件真实字节的 64 位小写 sha256>"},
    {"id": "F02", "path": "output/project.json", "sha256": "<F02 文件真实字节的 64 位小写 sha256>"}
  ],
  "stages": [
    {
      "id": "S01",
      "scope": "整理《最后一班夜车》的动作节拍角色和镜头人物参考",
      "input_file_ids": ["F01"],
      "output_file_ids": ["F02"],
      "review_notes": "已用同版本检查器自检；参考图仍未制作"
    }
  ],
  "object_mappings": [
    {
      "stage_id": "S01",
      "source": {"file_id": "F01", "pointer": "/scenes/0/beats/0", "object_id": "B01"},
      "target": {"file_id": "F02", "pointer": "/scenes/0/beats/0", "object_id": "B01"},
      "reason": "缺角色归属 → 补 character_ids: [\"C01\"]：站务员拦车动作"
    }
  ],
  "omissions": [],
  "additions": [
    {
      "stage_id": "S01",
      "target": {"file_id": "F02", "pointer": "/shots/2/reference_ids/0"},
      "reason": "SH03 出镜的乘客没有人物参考 → 新增 REF-C02"
    }
  ],
  "unresolved_differences": [
    {
      "stage_id": "S01",
      "refs": [{"file_id": "F02", "pointer": "/references/1", "object_id": "REF-C02"}],
      "difference": "参考图尚未制作（state=planned）",
      "next_step": "拍摄前由美术补充乘客造型图"
    }
  ]
}
```

- `files`：为每份实际输入和阶段产物分配唯一资料 ID，写可定位的工作区文件路径与完整 64 位 `sha256`。摘要须来自该文件读取时的真实字节，不能用 `file_version`、包摘要或重新序列化 JSON 的摘要代替；资料修改后重新计算并保留本次交接对应版本。这里是业务文件路径，不是包内资源路径。
- `stages`：记录唯一阶段 ID、`scope`、实际消费的 `input_file_ids`、实际产出的非空 `output_file_ids` 和 `review_notes`；文件 ID 指向 `files`，列表不重复。没有文件输入的原创阶段可用空输入列表，没有实际产物就不把计划列为已产出。填写这些字段不证明实际执行。
- `object_mappings`：每行填写 `stage_id/source/target/reason`；source 只能引用该阶段输入文件，target 只能引用其输出。一对多或多对一用多行表示，不把多个 ID 拼成一个字符串。
- `omissions` / `additions`：分别填写 `stage_id/source/reason` 或 `stage_id/target/reason`；新增内容不能冒充原输入已提供的事实。
- `unresolved_differences`：填写 `stage_id/refs/difference/next_step`，非空 `refs` 只指向该阶段输入或输出，保留具体差异和下一步审阅动作；未决项是警告，不因结构通过而删除。

`source/target/refs[]` 使用相同地址：`{"file_id":"F02","pointer":"/shots/0","object_id":"SH01"}`。`pointer` 是严格 JSON Pointer，按实际 JSON 字段和从零开始的数组下标定位；`~0` 表示键中的波浪号，`~1` 表示斜线，空字符串明确表示整份 JSON。不接受表达式、通配符、URI fragment、`-` 或 `01` 数组下标。若填写 `object_id`，目标必须是含相同 `id` 的对象；定位标量字段（例如 `/shots/0/seconds`）时省略 `object_id`，不能留下空占位。每个地址只指一个实际位置；理由可以说明合并、拆分或派生，但检查器不证明理由为真，也不假定来源值等于目标值。

交接本身不授予读取其它文件的权限。运行脚本时逐一传入 `--input-file FILE_ID=PATH`；每个 `files.id` 必须恰有一项明确绑定，不能缺项、多余或重复。`files.path` 与绑定路径都按**本次命令 cwd**词法规范化后比较，不以 handoff 文件父目录为基准，不展开 `~` 或环境变量。正文路径只有比较作用，绝不拿来打开文件；不一致在读取绑定资料前拒绝。所有绑定文件须是普通 UTF-8 JSON 文件，拒绝叶子符号链接、FIFO 等特殊文件。命令行本身仍须服从本次任务已有工具权限，包不能借绑定扩大宿主授权。

每份资料最多 4 MiB，最多 32 个绑定文件、交接文件总字节最多 32 MiB；每类交接列表最多 4096 行，JSON 嵌套最多 64 层，地址最多 2048 字符/64 段。输入摘要和 JSON 来自同一次有界读取，完全相同的绑定路径复用本次快照；不同路径表达各自读取，不能因词法折叠 `..` 看似相同而跳过不存在的目录或中间链接指向的实际文件。检查过程中检测文件身份与大小/改写时间变化，结果只描述这次取得的字节；它不是文件锁，不保证检查后文件不会再变。超限应拆分真实制作阶段，不能截断资料后谎称全量验证。未绑定/路径不符、不存在、权限不足、非普通文件、读取变化和格式错误有不同错误码；不读取错误信息中的任意路径来恢复。

声明、地址语法和授权先全量检查，失败时不打开任何绑定资料。文件读取或摘要失败后停止后续绑定读取；`files_checked` 只计本次已取得且摘要相符的文件，少于 `files_declared` 时余项不能视为通过。解析失败等情形不能确定完整读取计量，不把缺报当零字节后继续消费预算。显式 project 和 handoff 文档自身也各受 4 MiB 限制，不能用绑定预算替代它们的上限。

这仍是供模型/人审阅的制作资料，不是宿主 task、run、授权、pin 或完成账。检查器可验证字段、原字节 SHA、阶段输入输出范围及实际对象地址；不会自动判断阶段先后是否符合故事、所有应有映射是否已经声明、映射理由/来源忠实/道具语义是否正确，或阶段是否真的执行。未声明的语义遗漏仍须逐项回到原文核对，不能把机械通过称为交接全链质量通过。

以文本改编资料转入本包为例：

- `cast.id` 与 `characters.id` 显式对应；逐场核对 `character_ids`，不能只复制角色名字。
- 原 `scenes.source_ids` 保留为来源依据清单，逐项回到原文核对；本包检查器不读取原文，不能替代来源检查，更不能把它们填进 `reference_ids`。
- 原场次 `scenes[].id` 与本包场次 `scenes[].id`、原镜头 `shots[].id` 与本包镜头 `shots[].id` 分别建立显式对应，不要求转换前后的编号相同。一对多或多对一沿前述 `object_mappings` 分行记录，不拼接多个 ID。
- 本包每个镜头的 `shots[].scene_id` 只填写其所属的本包场次 `scenes[].id`，不能填镜头自身 ID；补齐该场次的 `episode_id/location_id/character_ids/prop_ids`。
- 原镜头动作需要明确整理成场次内 `beats`，分别填写 `id/kind/text`，对白增加 `character_id`；镜头的 `beat_ids` 逐项引用本场真实节拍。另一包没有这些字段时，不能只造一列节拍编号而没有正文。
- 参考资料单独建立条目，逐镜核对 `reference_ids → references.id → subject_id`；镜头秒数在转换后重新按集汇总。

完整制作资料形成后，用同代脚本资源的 `source_ref` 原样物化，通过现有执行工具运行：

```text
python3 scripts/check_continuity.py --project output/project.json
python3 scripts/check_continuity.py --project output/project.json --handoff output/handoff.json --input-file F01=inputs/source.json --input-file F02=output/project.json
python3 scripts/check_continuity.py --project output/project.json --baseline-project inputs/project-before.json
```

F01/F02 只是命令示例，必须对应本次交接清单并绑定全部文件；需要静态 HTML 时添加 `--format html`。入口仍是一个独立标准库脚本，不需要另取隐藏辅助文件。

CLI 报告升级为 `drama_workflow_check.v2`：`checks.project` 与 `checks.handoff` 分别记录 `passed/failed`，未请求交接时后者为 `not_requested` 并有 `handoff_not_checked` 警告。顶层 `structure_valid` 仅汇总**本次请求的**机械检查，`errors/warnings` 用 `scope` 区分项目和交接；原分集计量仍在 `metrics`，交接计量在 `handoff_metrics`。原 `check_project()` 函数和项目 v1 格式的含义不变。任何请求检查失败退出 1，无错误退出 0；未决语义、创作与媒体限制继续作为警告展示，不变成宿主硬门。

记录实际输出、退出码及所检文件版本。发现错误就修订资料后重算相关 SHA、重跑原脚本，不改弱检查器。未执行可以如实交付未验证资料；执行并无结构错误也只说明本脚本覆盖的客观项目，警告、创作与媒体审阅仍单列。

这些是制作资料的方法，不是宿主执行合同。机械检查发现断链应提示修订；创作深度、镜头美感和节奏效果继续由模型/人审阅，不加入通用宿主硬门。

## 0.3.0 起新增写法和检查

- **表和外键齐全**：七张表缺了哪张报 `missing_table`；场次缺 `episode_id`/`location_id`、镜头缺 `scene_id`、参考缺 `subject_id` 这几个键报 `missing_foreign_key`。键在但值不对，仍是原来的 `unknown_reference` 等错误。
- **动作节拍写角色（字段名 `character_ids`，列表）**：动作节拍的角色写进 `character_ids` 列表，列出参与动作的本场角色；对白节拍仍用单数 `character_id`。完全没写角色字段报 `beat_character_missing` 提醒（纯环境动作可以没有角色）；写成单数 `character_id`（非空）报 `beat_character_id_singular` 错误（0.3.1 起，会触发返工）；写了不是本场角色报错。
- **出镜人物要有参考**：项目里有人物参考时，一个镜头引用的节拍里出现的角色（说话人和动作角色），这一镜的 `reference_ids` 里要有他的人物参考，否则报 `shot_character_reference_missing` 提醒。项目完全没有人物参考计划时不提醒。
- **不编造参考 ID**：检查器从本项目 `references` 的 ID 推出写法（如 `REF-`），在整份项目和交接的所有文字里找这种写法的 ID，表里没有就报 `unknown_reference_mention` 错误。
- **改动前后对比更严**：带 `--baseline-project` 时，下面这些改动没在交接里列出就是错误，`checks.baseline` 记 `failed`：
  - `baseline_beat_changed`：节拍增删（包括重新编号）或台词改动；
  - `baseline_relation_changed`：镜头和节拍、参考的对应关系变了（不看顺序）；
  - `baseline_schema_or_duration_changed`：schema 名、镜头秒数或分集目标秒数变了。
  - “列出”指交接里有一条地址（`source`、`target` 或 `refs`）点到这处改动，文件按 `files[].sha256` 对应到改前或改后的项目。换说话人、节拍类型、对象删除等仍只是提醒。
- **交接只列真实改动**：对象映射两端在已读文件里、值完全相同，报 `handoff_claim_without_change` 错误；声称新增却原样已有、声称省略却原样还在，仍报原来的错误。
- **交接模板**：`templates/handoff.json` 里 `<…>` 是填写提示；改动写成“基线值 → 新值：为什么改”。整段照抄提示报 `placeholder_text` 错误。
- **交接摘要过期**：带基线时交接 `files` 里没有一条摘要等于本次项目的实际字节，报 `handoff_target_not_matched` 提醒：项目改完后要重算交接里的摘要，否则交接里的地址都对不上，所有改动都会算“没列出”。
- **宿主模式**：宿主开启包检查时，用 `--host-json` 跑本检查器，交接文件按摘要对应宿主交来的项目和基线；对不上的文件报 `handoff_file_not_available` 提醒，不读、不影响结论。

## 和改动前的项目对比（0.2.0）

在已有项目上修改时（比如用户给了原制作资料、要求改一部分），检查时加上 `--baseline-project <改动前的项目文件>`。这个参数也是显式授权，读取方式和上限与 `--input-file` 相同。

- **报告内容**：`checks.baseline` 和 `baseline_metrics` 会按稳定 ID 列出新增、删除、修改的对象（每类最多 100 条，另给省略数），并给出这些提醒：
  - `dialogue_speaker_changed`：同一节拍换了说话人，或者同场一句一字不差的台词换了人（常见于重新编号时顺手换了人）。
  - `beat_kind_changed`：节拍的类型变了。
  - `baseline_object_removed`：原有对象没了。
- **只是提醒**：这些都是 warning。用户确实要求改的，在交付说明或交接里写明即可；检查器不知道用户说了什么，也不判断改得对不对。（0.3.0 起节拍、台词、对应关系、schema 和时长的改动没在交接里列出是错误，见上一节。）
- **交接文件**：带 `--handoff` 时，同一阶段里输入、输出都是本包项目的，检查器会逐对象比较。
  - 真实改了但没有任何交接行点到的，报 `change_not_declared_in_handoff`。
  - 交接声称新增的整对象在输入里原样已有，报 error；声称省略的整对象在输出里原样还在，也报 error。
  - 根地址 `""` 不算覆盖全部改动。
- **原检查器身份**：报告带 `checker.script_sha256`，应等于本包 `scripts/check_continuity.py` 资源的摘要。自写或改过的脚本给出的结果，不能说成"原检查器通过"。
