"""原业务/缓存/Issue 证据组合；不把报告器自己的绿灯当产品通过。"""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def load_script(name):
    path = Path(__file__).resolve().parents[2] / 'scripts' / (name + '.py')
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_acceptance_requires_exact_candidate_and_observation_level(tmp_path):
    module = load_script('verify_business_spaces_v12')
    xml = tmp_path / 'results.xml'
    xml.write_text('''<testsuites><testsuite><testcase classname="tests.case" name="test_one">
      <properties><property name="case_id" value="V12-001"/>
      <property name="candidate_sha" value="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"/>
      <property name="validation_level" value="service"/></properties>
    </testcase></testsuite></testsuites>''')
    cases = [{'id': 'V12-001', 'minimum_validation_level': 'real_model'}]
    report = module.build_acceptance_report(cases, xml, 'a' * 40)
    assert isinstance(report, dict), '必须产生带候选和观察层级的结构化证据'
    assert report['cases'][0]['status'] == 'NOT_VERIFIED'
    assert report['cases'][0]['reason'] == 'observation_level_mismatch'
    report = module.build_acceptance_report(cases, xml, 'b' * 40)
    assert report['cases'][0]['status'] == 'NOT_VERIFIED'
    assert report['cases'][0]['reason'] == 'candidate_mismatch'


def test_failed_or_skipped_evidence_never_becomes_product_pass(tmp_path):
    module = load_script('verify_business_spaces_v12')
    for element, expected in [('<failure message="product defect"/>', 'FAILED'),
                               ('<skipped message="no native sandbox"/>', 'NOT_RUN')]:
        xml = tmp_path / 'results.xml'
        xml.write_text('<testsuites><testsuite><testcase name="test_one"><properties>'
            '<property name="case_id" value="V12-001"/>'
            '<property name="candidate_sha" value="' + 'a' * 40 + '"/>'
            '<property name="validation_level" value="service"/>'
            '</properties>' + element + '</testcase></testsuite></testsuites>')
        report = module.build_acceptance_report([
            {'id': 'V12-001', 'minimum_validation_level': 'service'},
            {'id': 'V12-002', 'minimum_validation_level': 'service'}], xml, 'a' * 40)
        assert isinstance(report, dict), '不能缺少失败与跳过的逐项事实'
        assert report['cases'][0]['status'] == expected
        assert report['cases'][1]['status'] == 'NOT_RUN'
        assert report['product_validation'] != 'PASS'


def test_focus_matrix_is_deduplicated_and_does_not_accept_non_tests():
    module = load_script('verify_mega_acceptance')
    manifest = {'workstreams': [
        {'id': 'W1', 'tests': ['agent_py_agent/tests/test_a.py'], 'read_only_tests': []},
        {'id': 'W15', 'tests': ['agent_py_agent/tests/test_b.py'],
         'read_only_tests': ['agent_py_agent/tests/test_a.py']}]}
    assert module.focused_matrix(manifest) == [
        'agent_py_agent/tests/test_a.py', 'agent_py_agent/tests/test_b.py']
    manifest['workstreams'][0]['tests'] = ['../other.py']
    try:
        module.focused_matrix(manifest)
    except ValueError:
        pass
    else:
        raise AssertionError('验收矩阵不得从外部manifest执行越界路径')


def test_matching_bound_test_is_only_local_evidence_not_release_attestation(tmp_path):
    module = load_script('verify_business_spaces_v12')
    xml = tmp_path / 'results.xml'
    xml.write_text('<testsuites><testsuite><testcase name="test_one"><properties>'
        '<property name="case_id" value="V12-002"/>'
        '<property name="candidate_sha" value="' + 'a' * 40 + '"/>'
        '<property name="validation_level" value="service"/>'
        '</properties></testcase></testsuite></testsuites>')
    report = module.build_acceptance_report([
        {'id': 'V12-002', 'minimum_validation_level': 'service'}], xml, 'a' * 40)
    assert report['cases'][0]['status'] == 'PASS'
    assert report['release_verdict'] == 'NOT_ISSUED'


def test_cli_keeps_all_81_rows_and_fingerprints_the_actual_inputs(tmp_path, capsys):
    import hashlib

    module = load_script('verify_business_spaces_v12')
    cases, junit, output = [tmp_path / name for name in ('cases.json', 'junit.xml', 'report.json')]
    cases.write_text(json.dumps({'cases': [
        {'id': f'V12-{i:03}', 'minimum_validation_level': 'service'} for i in range(1, 82)]}))
    junit.write_text('<testsuites><testsuite/></testsuites>')
    assert module.main(['--acceptance', str(cases), '--junit', str(junit),
        '--candidate', 'a' * 40, '--output', str(output)]) == 1
    capsys.readouterr()
    report = json.loads(output.read_text())
    assert len(report['cases']) == 81 and report['counts'] == {'NOT_RUN': 81}
    assert isinstance(report.get('input_refs'), dict), '必须绑定实际输入，不凭路径或旧文件名签字'
    assert report['input_refs']['junit']['sha256'] == hashlib.sha256(junit.read_bytes()).hexdigest()
    assert report['input_refs']['acceptance']['sha256'] == hashlib.sha256(cases.read_bytes()).hexdigest()


def test_matrix_runner_refuses_uncommitted_candidate_before_running_tests(tmp_path, monkeypatch):
    module = load_script('verify_mega_acceptance')
    repo = tmp_path / 'repo'
    source = repo / 'agent_py_agent/tests/test_one.py'
    source.parent.mkdir(parents=True)
    source.write_text('def test_one(): pass\n')
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({'workstreams': [{'tests': ['agent_py_agent/tests/test_one.py']}]}))
    executed = []

    def git(command, **_kwargs):
        return 'a' * 40 + '\n' if 'rev-parse' in command else '?? uncommitted.py\n'

    def run(*args, **kwargs):
        executed.append(args)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(module.subprocess, 'check_output', git)
    monkeypatch.setattr(module.subprocess, 'run', run)
    with pytest.raises(SystemExit) as failure:
        module.main(['--repo', str(repo), '--manifest', str(manifest),
            '--output-dir', str(tmp_path / 'output')])
    assert failure.value.code == 2
    assert executed == []


def test_empty_focus_cannot_fall_back_to_the_whole_repository():
    with pytest.raises(ValueError):
        load_script('verify_mega_acceptance').focused_matrix({'workstreams': []})


def test_frozen_helper_and_json_reference_are_not_executable_pytest_targets():
    module = load_script('verify_mega_acceptance')
    manifest = {'workstreams': [{'tests': ['agent_py_agent/tests/test_one.py',
            'agent_py_agent/tests/_sandbox_capabilities.py'],
        'read_only_tests': ['agent_py_agent/tests/fixtures/constants_catalog_pending_fixes.json']}]}
    try:
        result = module.focused_matrix(manifest)
    except ValueError:
        result = None
    assert result == ['agent_py_agent/tests/test_one.py'], '冻结清单中的两份只读资料须另记证据，不可当pytest用例执行'
