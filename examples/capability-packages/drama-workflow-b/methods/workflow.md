# 资料先对齐，再交接制作

本方法参考 shuohao-skills 的分段资料与稳定 ID 设计，改写为独立内容包的小切片。它与 drama-text-a 的原文改编路径不同，不自动覆盖或融合。

| 资料 | 关键字段 | 下游消费 |
| --- | --- | --- |
| 分集目标 | episodes.id/hook/ending/target_seconds | 场次引用 episode_id；人物和事件变化要落在场次里 |
| 人物 | characters.id/name/visual_anchor | 场次 character_ids、对白 character_id、角色参考图 |
| 美术 | locations 与 props 的 id/name/continuity_note | 场次 location_id/prop_ids、地点/道具参考图 |
| 剧本 | scenes.id 与 beats.id/kind/text | 每个动作/对白节拍有单独编号；说话人必须在场 |
| 分镜 | shots.scene_id/beat_ids/seconds/reference_ids | 明确承接哪些节拍，不能只写一段通用视频提示词 |
| 参考资料 | references.id/kind/subject_id/state | planned 表示计划；provided 只是输入声明，仍需核对真实媒体 |

角色名和显示文字可以修改，资料引用继续使用 ID。修改上游 ID 或删除对象后，所有下游引用都要复查。
合并另一能力包的输出时显式制作一次转换清单，保存输入引用和转换结果；不能把同名字段或编号假定为同一对象。

这些是制作资料的方法，不是宿主执行合同。机械检查发现断链应提示修订；创作深度、镜头美感和节奏效果继续由模型/人审阅，不加入通用宿主硬门。
