# 同领域合并

学第二个同领域 agent 时（比如已经有短剧分场包，又学了一个短剧配音方法）：

1. 用 `skill_search` 找到已有的包，读它的 `CAPABILITY.md`。只能并进你自己做的包：打包回执里 `installed_by_me` 为 true 才是你做的；回执 `warnings` 里有 `PACKAGE_BUILD_ID_TAKEN`，就是这个包名被别处的包占着，装不上，只能换个包名单独成包（跟用户说清楚）。
2. 问用户：“并进已有的 <包名>（新增一个方向），还是单独成包？”这只决定做成哪种。
3. 并进去：
   - 先用 `skill_search`（action=get、package_id）把已有包的入口和要保留的文件读出来，原样写进新的包目录；
   - 用同一个 `plugin_id`，版本号要比现在装着的大（看打包回执的 `installed_version`，如 0.1.0 → 0.2.0）；回执的 `warnings` 里有 `PACKAGE_BUILD_VERSION_REUSED`（忘了升）、`PACKAGE_BUILD_VERSION_OLDER`（比装着的旧）、`PACKAGE_BUILD_FILES_DROPPED`（旧方向的文件没带上）就改好重新打包；
   - `CAPABILITY.md` 按方向分节（如“## 方向一：分场”“## 方向二：台词润色”），每节写清来源和许可证；
   - 新方向的文件放到自己的子目录（如 `methods/dialogue/`），LICENSE 和 PROVENANCE 按来源分开保留（如 `provenance/scenes.md`、`provenance/dialogue.md`）；
   - `keywords` 合并两边的领域词，`description` 改成覆盖两个方向；
   - `package_build` 的 `origin`、`license` 按方向列全，一行写完，如 `origin`：“分场：github.com/a/drama-agent@abc；台词润色：github.com/b/dialogue-agent@def”，`license`：“分场 MIT；台词润色 Apache-2.0”。各自最多 200 个字，放不下就用短提交号，完整的写在 `provenance/` 里。
4. 单独成包：换一个新的 `plugin_id`，按正常流程做。
5. 装不装照旧看开关：开关关着就把回执里的确认行原样交给用户。
6. 并错了：用户发 `/plugins#<包名> 退回`，宿主会先说明退到哪一版，再发回执最后那一行就回到合并前那一版。
