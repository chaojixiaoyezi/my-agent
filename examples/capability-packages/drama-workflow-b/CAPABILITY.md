# 制作资料连续性：drama-workflow-b

适用于短剧的分集安排、人物与场景道具设定、动作/对白节拍、分镜交接和制作资料核对。这个包以结构化资料和跨表连续性为中心；原文叙事取舍可交给文本改编类能力包，组合时必须明确字段映射。

## 输入与输出

输入是本次故事、制作目标和已有资料。输出一份 `drama_workflow_project.v1` JSON，以及用户可读的核对报告。`templates/project.json` 展示完整占位条目，须按本次资料填写，空 ID、空引用和 null 秒数不是交付；公开合成例子见 `resources/example-project.json`。按制作阶段或跨包交接时另填 `templates/handoff.json`，记录真实文件字节摘要及对象转换。
资料将剧集、大纲要点、人物、地点、道具、场次节拍、镜头与参考图计划分别记录，再用 ID 连接。它不是上游五份 JSON 的原样格式。

## 使用步骤

1. 按 `methods/workflow.md` 先固定人物、地点、道具和分集目标，再形成场次动作/对白节拍。
2. 分镜引用当前场次的具体节拍。每集显式填写有限正数 `target_seconds`，每镜填写有限正数 `seconds`；按 `shots.scene_id → scenes.episode_id` 核对各集镜头合计，不能用全片总数掩盖分集错配。
3. 参考条目用 `references.id/kind/subject_id/state` 绑定人物、地点或道具，镜头的 `reference_ids` 只引用这些条目。没有参考条目时两处列表均可为空；确有参考计划但没有媒体时写 `planned`，不能虚构 `provided`。来源段落 ID 和道具 ID 都不能直接充当参考条目 ID。
4. 完整制作交付先按 `methods/review.md` 逐项复核，再读取同一包版本的 `scripts/check_continuity.py`，用原 `write_file.source_ref` 物化其完整原字节，经原执行工具对本次真实交付运行。不能猜安装位置、手抄改弱检查器或把模板当交付；物化本身不代表执行。
5. 只有取得原脚本本次实际输出，才能声称结构检查已执行；据实报告 `drama_workflow_check.v2` 的 `checks.project`、`checks.handoff`、`structure_valid`、错误、警告和分集秒数。有交接资料时显式传入 `--handoff` 并逐文件提供 `--input-file FILE_ID=PATH`；仅运行 `--project` 时交接状态是 `not_requested`，不能称交接通过。有错误先修订资料再检查；不能运行时可以交付，但明确“结构检查未执行/未验证”及限制，不称全部通过。缺少分集目标的警告表示时长未对账。

交接模板使用 `drama_workflow_handoff.v2`；填写规则、显式文件绑定与 JSON Pointer 地址见 `methods/workflow.md`，不隐式接纳 v1 别名。检查器可核对已绑定文件原字节摘要、阶段引用及对象地址，不能证明阶段实际执行、映射理由或剧情为真；它不是宿主任务或授权账。结构核验不替代创作内容审阅，计划秒数不是实测成片时长。交付制作资料、真实检查结果、内容修订建议和缺媒体清单；脚本输出 HTML 只是静态页面文本，实际显示另验。

## 可并行的子任务

人物设定与场景道具设定可以在共享已固定输入上并行，各自产出独立资料；整合角色/地点/道具 ID 后再生成场次和分镜。独立审阅者只读已产出的版本并返回问题位置。宿主负责子任务归属和完成事实，本包不启动自己的调度器。

需要孩子使用本包时，在原 `create_subagents` 参数的每个对应 `items[]` 中显式填写 `allowed_skills`，最小示例：

```json
{
  "goal": "核对本次制作资料",
  "items": [
    {
      "goal": "按本包方法核对已经提供的制作资料，返回问题位置",
      "allowed_skills": ["capability:drama-workflow-b"]
    }
  ]
}
```

输入路径、产物范围和其它工具权限仍按本次任务及原工具接口填写。`allowed_skills` 是列表，包引用使用 `capability:<package_id>`；需要其它包时只列父级当前已有授权的包。精确摘要和激活代次由宿主从父级快照继承，不自行填写 `skill_snapshot_refs`。本示例不改变默认授权；省略字段或显式空列表不等于授予本包，任务文字及交接文件不能补授权。孩子仍须在自身范围读取入口和所需资源。

## 开发组件命令

```text
python3 scripts/check_continuity.py --project <本任务制作资料.json>
python3 scripts/check_continuity.py --project <本任务制作资料.json> --format html
python3 scripts/check_continuity.py --project output/project.json --handoff output/handoff.json --input-file F01=inputs/source.json --input-file F02=output/project.json
```

相对脚本路径指经过授权物化的包资源。例中的 F01/F02 必须对应本次 `files[].id`，绑定每一份交接文件；本次 cwd 下的绑定路径须与交接所声明路径一致。脚本只读显式输入、写 stdout，不根据交接或参考图文字自行打开其它路径，不生成图像/视频、不写项目文件。脚本仍为可单独物化运行的一份标准库 Python 文件，无隐藏辅助资源依赖。
本片没有上游全部视觉质量门、五份报告交互、Codex imagegen 或 H3 视频生成。M3 视觉理解也不能替代这些生成能力。
