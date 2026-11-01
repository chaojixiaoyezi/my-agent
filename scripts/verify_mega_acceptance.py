"""从冻结派工清单去重运行各片focused；原始失败、skip、退出码均保留。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath

_reference_files = frozenset({
    'agent_py_agent/tests/_sandbox_capabilities.py',
    'agent_py_agent/tests/fixtures/constants_catalog_pending_fixes.json',
})


def focused_matrix(manifest):
    """只消费声明的测试路径，不接受命令参数、仓外路径或任意执行脚本。"""
    files = set()
    for stream in manifest['workstreams']:
        for name in [*stream['tests'], *stream.get('read_only_tests', [])]:
            if name in _reference_files:
                continue
            path = PurePosixPath(name)
            if (path.is_absolute() or '..' in path.parts or path.parts[:2] != ('agent_py_agent', 'tests')
                    or not path.name.startswith('test_') or path.suffix != '.py'):
                raise ValueError('验收矩阵只能包含仓内明确测试文件: ' + name)
            files.add(name)
    if not files:
        raise ValueError('focused清单不能为空，不能退回全仓隐式收集')
    return sorted(files)


def main(argv=None):
    parser = argparse.ArgumentParser(description='串行执行冻结候选的去重focused矩阵，非产品发行签字。')
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--collect-only', action='store_true')
    args = parser.parse_args(argv)
    repo, out = args.repo.resolve(), args.output_dir.resolve()
    if out.is_relative_to(repo):
        parser.error('输出目录必须在代码仓外')
    manifest_bytes = args.manifest.read_bytes()
    manifest = json.loads(manifest_bytes)
    files = focused_matrix(manifest)
    references = sorted({name for stream in manifest['workstreams']
                         for name in [*stream['tests'], *stream.get('read_only_tests', [])]
                         if name in _reference_files})
    for name in [*files, *references]:
        path = (repo / name).resolve(strict=True)
        if not path.is_relative_to(repo):
            parser.error('测试路径不能经符号链接离开仓库')
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo, text=True).strip()
    if subprocess.check_output(['git', 'status', '--porcelain=v1', '--untracked-files=all'], cwd=repo, text=True):
        parser.error('候选必须先提交并保持干净，不能把未提交字节标记成HEAD证据')
    out.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.pop('PYTHONSAFEPATH', None)
    env.pop('MY_AGENT_CONFIG', None)
    env['PYTHONPATH'] = str(repo)
    env['MY_AGENT_HOME'] = str(out / 'home')
    command = [sys.executable, '-m', 'pytest', *files, '--basetemp=' + str(out / 'pytest'),
        '-q', '--tb=short', '--junitxml=' + str(out / 'matrix.xml'), '-o', 'junit_family=xunit1']
    if args.collect_only:
        command.append('--collect-only')
    started = time.monotonic()
    with (out / 'matrix.log').open('w') as log:
        result = subprocess.run(command, cwd=repo, env=env, stdout=log, stderr=subprocess.STDOUT, check=False)
    after = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo, text=True).strip()
    clean_after = not subprocess.check_output(
        ['git', 'status', '--porcelain=v1', '--untracked-files=all'], cwd=repo, text=True)
    report = {'schema_version': 'mega-focused-run.v1', 'candidate_sha': head,
        'same_head_after_run': head == after, 'manifest_sha256': hashlib.sha256(manifest_bytes).hexdigest(),
        'worktree_clean_before_run': True, 'worktree_clean_after_run': clean_after,
        'files': files, 'command': command, 'exit_code': result.returncode,
        'non_executable_references': [{'path': name,
            'sha256': hashlib.sha256((repo / name).read_bytes()).hexdigest(),
            'role': 'helper_or_fixture_not_a_pytest_target'} for name in references],
        'wall_seconds': time.monotonic() - started, 'collect_only': args.collect_only,
        'product_validation': 'NOT_RUN' if args.collect_only else 'PARTIAL_NOT_RELEASE_PASS',
        'release_verdict': 'NOT_ISSUED', 'junit': str(out / 'matrix.xml'), 'log': str(out / 'matrix.log'),
        'not_verified': ['real_provider_cache_and_TTFT', 'native_Linux_bwrap', 'native_macos_Seatbelt',
                         'Windows', 'staging_user_path', 'independent_release_audits']}
    (out / 'matrix-run.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False))
    return result.returncode if head == after and clean_after else 2


if __name__ == '__main__':
    raise SystemExit(main())
