# Docs Index

这个目录用于放长期会增长的项目文档。旧的根目录文档仍然保留原位置；新的模块型文档优先放到 `docs/modules/`。

## 入口

- [guides/MODEL_GUIDE.md](guides/MODEL_GUIDE.md)：给用户的《模型管理使用说明》，覆盖新增、切换、默认、`/effort`、删除、共享、派子代理选模、语义记忆的向量模型与对话式管理。
- [guides/CAPABILITY_PACK_GUIDE.md](guides/CAPABILITY_PACK_GUIDE.md)：给用户的《能力包使用说明》，覆盖装/启用/停用/更新/回退/卸载、发现与使用、版本固定、派子代理授权、自制包与安全边界。
- [modules/README.md](modules/README.md)：按功能模块组织的“四件套”规范和模块索引。
- [design/README.md](design/README.md)：较长的模块设计文档索引，承接 `DESIGN_LEDGER.md` 里的长篇设计细节。
- [design/P1_MAINLINE_CONVERGENCE.md](design/P1_MAINLINE_CONVERGENCE.md)：P1 主链收敛的事实、边界与验收证据。
- [design/P2_SCALE_MAINLINE.md](design/P2_SCALE_MAINLINE.md)：P2 正式 scale profile、真依赖验证和未完成的规模证明。
- [design/P2_SCALE_ROLLOUT_DR_OWNER_STORE.md](design/P2_SCALE_ROLLOUT_DR_OWNER_STORE.md)：灰度/灾备、Owner 对象事实源和正在运行的 24 小时真实来源 proof。

## 维护原则

- 根目录文档适合放全局入口、验收记录、运行日志和跨模块索引。
- 模块目录适合放某个功能自己的讨论、推进、初心和结构说明。
- 不要为了整理而搬空旧文档；第一版先建立入口和链接，后续在实际开发时逐步补齐。
