#!/usr/bin/env bash
# LLM: 对仓库已有 bubblewrap 0.8.0 及对应发行补丁重新构建；Ubuntu 20.04 链接确保低 glibc 基线。
# 模块用途: 生成内网离线可加载的沙箱程序，随包保留对应源码、许可和构建说明，不需要目标机器编译器。
set -euo pipefail
root="${1:?传入构建目录}"
cd "$root"
tar -xf bubblewrap-0.8.0.tar.xz
cd bubblewrap-0.8.0
patch -p1 < ../backport-Add--bind-fd-and--ro-bind-fd-to-let-you-bind-a-O_PATH-fd.patch
./configure --with-priv-mode=none --disable-man --disable-selinux
make -j2
mkdir -p "$root/bundle/native/lib" "$root/bundle/native/source"
cp bwrap "$root/bundle/native/bwrap"
cp -L /lib/x86_64-linux-gnu/libcap.so.2 "$root/bundle/native/lib/libcap.so.2"
patchelf --set-rpath '$ORIGIN/lib' "$root/bundle/native/bwrap"
cp COPYING "$root/bundle/native/COPYING.bubblewrap"
cp /usr/share/doc/libcap2/copyright "$root/bundle/native/COPYRIGHT.libcap2"
cp /etc/ssl/certs/ca-certificates.crt "$root/bundle/native/ca-certificates.crt"
cp "$root/bubblewrap-0.8.0.tar.xz" "$root/"*.patch "$root/bundle/native/source/"
cp "$0" "$root/bundle/native/source/build.sh"
printf '%s\n' 'Ubuntu 20.04 x86_64 / glibc 2.31；bubblewrap 0.8.0 + 原 openEuler --bind-fd/--ro-bind-fd 补丁。' \
  '与仓库版本的功能来源一致；重新链接并设置局部 libcap 的 RPATH；源码及 LGPL 许可随包。' \
  '构建依赖：gcc make pkg-config autoconf automake libtool libcap-dev patchelf patch。' \
  '运行时系统 bwrap 优先，无系统 bwrap 时使用本包；不设 setuid，不放宽原沙箱检查。' \
  > "$root/bundle/native/PROVENANCE.txt"
"$root/bundle/native/bwrap" --version
ldd "$root/bundle/native/bwrap"
