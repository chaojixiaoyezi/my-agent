# bubblewrap 随包材料

my-agent 的许可证不替代第三方组件许可证。本目录随 wheel/sdist 分发，不参与模型上下文。

- 二进制：`../bin/bwrap.linux-x86_64`，bubblewrap 0.8.0，Linux x86_64；没有本项目修改。
- 许可：`COPYING`（GNU Library General Public License v2，原发行包原文）。
- 对应源码：`bubblewrap-0.8.0-2.oe2403sp3.src.rpm`，含上游源码、发行版补丁和构建 spec。
- 上游项目：https://github.com/containers/bubblewrap/tree/v0.8.0
- 发行版原包：https://repo.openeuler.org/openEuler-24.03-LTS-SP3/OS/x86_64/Packages/bubblewrap-0.8.0-2.oe2403sp3.x86_64.rpm
- 对应源码原包：https://repo.openeuler.org/openEuler-24.03-LTS-SP3/source/Packages/bubblewrap-0.8.0-2.oe2403sp3.src.rpm

2026-09-10 已从上述发行包提取 `usr/bin/bwrap`，与本仓库二进制逐字节哈希一致：

| 材料 | SHA256 |
| --- | --- |
| bwrap 二进制 | `fd4f98cc36c1b326f7dedafb6f134c6f6bc51771ff2f984651f9a256e8c3c33d` |
| 发行版二进制 RPM | `807a10536244f36841c808c088334e8889457c55d7b6a12d1143db6743477b54` |
| 随包源码 RPM | `3e619886b93170cf20a6dc87dd0bbf2f1e09e3a9c68b6a112dbadfd385cbb7ab` |

源码 RPM 可用 `bsdtar -xf` 解包阅读；在匹配的 openEuler 构建环境安装 spec 中的 BuildRequires 后，
可使用 `rpmbuild --rebuild bubblewrap-0.8.0-2.oe2403sp3.src.rpm` 构建。此处记录对应源码，不声称
已经在所有平台完成可复现构建或安全审计。升级二进制必须同步源码、许可、哈希、包验收和 Linux 隔离测试。

运行时优先使用操作系统维护的 bubblewrap；随包版本只作匹配架构的离线备用，安全自检失败仍拒绝执行。
