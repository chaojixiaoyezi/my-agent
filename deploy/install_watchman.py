"""构建期安装固定官方 Watchman 资产；不替换 Python 运行镜像或自动选择旧发行包。"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import stat
import subprocess
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

ARCHIVE_URL = 'https://github.com/facebook/watchman/releases/download/v2026.07.27.00/watchman-v2026.07.27.00-linux.zip'
ARCHIVE_SHA256 = 'e02227ea30fc07af4131f4c00b851d270dd231c24f86b670d46fa7c9aa95b022'


def install(archive: Path, prefix: Path, library_dir: Path, expected_sha256: str):
    if archive.stat().st_size > 32 * 1024 * 1024:
        raise RuntimeError('Watchman 压缩资产超过构建预算')
    if hashlib.sha256(archive.read_bytes()).hexdigest() != expected_sha256:
        raise RuntimeError('Watchman 官方资产 SHA256 不匹配')
    with zipfile.ZipFile(archive) as bundle:
        if sum(item.file_size for item in bundle.infolist()) > 128 * 1024 * 1024:
            raise RuntimeError('Watchman 资产超过构建解包预算')
        for item in bundle.infolist():
            path = PurePosixPath(item.filename)
            if path.is_absolute() or '..' in path.parts or '\\' in item.filename or stat.S_ISLNK(item.external_attr >> 16):
                raise RuntimeError('Watchman 资产含危险解包路径')
            if item.is_dir():
                continue
            if len(path.parts) != 3 or path.parts[1] not in {'bin', 'lib'}:
                raise RuntimeError('Watchman 资产布局与已验证格式不符')
            target = prefix / path.parts[1] / path.parts[2]
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(item) as source, target.open('wb') as destination:
                shutil.copyfileobj(source, destination)
            target.chmod(0o755 if path.parts[1] == 'bin' else 0o644)
    if not (prefix / 'bin/watchman').is_file():
        raise RuntimeError('Watchman 资产缺少执行文件')
    library_dir.mkdir(parents=True, exist_ok=True)
    for source in (prefix / 'lib').iterdir():
        shutil.copyfile(source, library_dir / source.name)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default=ARCHIVE_URL)
    parser.add_argument('--sha256', default=ARCHIVE_SHA256)
    parser.add_argument('--target-arch', default='amd64')
    parser.add_argument('--prefix', type=Path, default=Path('/opt/owner-observer'))
    parser.add_argument('--system-library-dir', type=Path, default=Path('/usr/local/lib'))
    args = parser.parse_args()
    if args.target_arch != 'amd64' and args.url == ARCHIVE_URL:
        raise RuntimeError('固定官方资产仅支持 Linux amd64；其它架构须提供适配的官方资产 URL 与 SHA256')
    with tempfile.TemporaryDirectory(prefix='watchman-build-') as directory:
        archive = Path(directory) / 'watchman.zip'
        with urllib.request.urlopen(args.url, timeout=30) as response, archive.open('wb') as target:
            total = 0
            while block := response.read(65536):
                total += len(block)
                if total > 32 * 1024 * 1024:
                    raise RuntimeError('Watchman 下载超过构建预算')
                target.write(block)
        install(archive, args.prefix, args.system_library_dir, args.sha256)
    # 真实执行才能证明 ELF/共享库 ABI；不把 ldd 的展示文案当机器验收协议。
    subprocess.run([str(args.prefix / 'bin/watchman'), '--version'], check=True, timeout=10)


if __name__ == '__main__':
    main()
