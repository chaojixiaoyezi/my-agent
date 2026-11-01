"""业务81项的逐项证据核对。JUnit只是所给运行记录，不是发行认证。"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from collections import Counter
from io import BytesIO
from pathlib import Path


def build_acceptance_report(cases, junit_path, candidate_sha):
    """只接纳同候选、同观察层级的明确case绑定；缺失、失败、skip分别保留。"""
    if not re.fullmatch(r'[0-9a-f]{40}', candidate_sha):
        raise ValueError('candidate_sha必须是完整Git SHA')
    ids = [case['id'] for case in cases]
    if len(set(ids)) != len(ids):
        raise ValueError('重复验收case ID')
    observations = {}
    for node in ET.parse(junit_path).getroot().iter('testcase'):
        props = {p.attrib['name']: p.attrib.get('value', '') for p in node.findall('./properties/property')}
        case_id = props.get('case_id')
        if not case_id:
            continue
        if case_id not in ids:
            raise ValueError('JUnit包含未声明验收case')
        observations.setdefault(case_id, []).append((node, props))
    rows = []
    for case in cases:
        rows_for_case = observations.get(case['id'], [])
        observed, reason = 'NOT_RUN', 'no_bound_observation'
        evidence = []
        for node, props in rows_for_case:
            evidence.append({'test': node.attrib.get('classname', '') + '::' + node.attrib.get('name', ''),
                'candidate_sha': props.get('candidate_sha'), 'level': props.get('validation_level'),
                'observation_point': props.get('observation_point')})
            if props.get('candidate_sha') != candidate_sha:
                status, why = 'NOT_VERIFIED', 'candidate_mismatch'
            elif node.find('failure') is not None or node.find('error') is not None:
                status, why = 'FAILED', 'test_failed'
            elif node.find('skipped') is not None:
                status, why = 'NOT_RUN', 'test_skipped'
            elif props.get('validation_level') != case['minimum_validation_level']:
                status, why = 'NOT_VERIFIED', 'observation_level_mismatch'
            else:
                status, why = 'PASS', 'matching_bound_test_passed'
            # 一个通过样本不能覆盖同case的失败、缺平台或旧候选证据。
            if not evidence[:-1] or {'PASS': 0, 'NOT_RUN': 1, 'NOT_VERIFIED': 2, 'FAILED': 3}[status] > {
                    'PASS': 0, 'NOT_RUN': 1, 'NOT_VERIFIED': 2, 'FAILED': 3}[observed]:
                observed, reason = status, why
        rows.append({'id': case['id'], 'scenario': case.get('scenario', ''),
            'minimum_validation_level': case['minimum_validation_level'],
            'status': observed, 'reason': reason, 'evidence': evidence})
    counts = dict(Counter(row['status'] for row in rows))
    return {'schema_version': 'business-acceptance-evidence.v1', 'candidate_sha': candidate_sha,
        'basis': 'provided_junit_with_explicit_case_bindings_not_release_attestation',
        'product_validation': 'PASS' if rows and counts.get('PASS') == len(rows) else 'PARTIAL_NOT_RELEASE_PASS',
        'release_verdict': 'NOT_ISSUED', 'counts': counts, 'cases': rows}


def main(argv=None):
    parser = argparse.ArgumentParser(description='逐项核对业务81项；局部绿灯不替代UI、真实模型或staging。')
    parser.add_argument('--acceptance', type=Path, required=True)
    parser.add_argument('--junit', type=Path, required=True)
    parser.add_argument('--candidate', required=True)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    acceptance_bytes, junit_bytes = args.acceptance.read_bytes(), args.junit.read_bytes()
    cases = json.loads(acceptance_bytes)['cases']
    if {case['id'] for case in cases} != {f'V12-{i:03}' for i in range(1, 82)}:
        parser.error('必须提供完整81项原始验收，不接受缩小清单')
    report = build_acceptance_report(cases, BytesIO(junit_bytes), args.candidate)
    report['input_refs'] = {
        'acceptance': {'path': str(args.acceptance.resolve()),
                       'sha256': hashlib.sha256(acceptance_bytes).hexdigest()},
        'junit': {'path': str(args.junit.resolve()), 'sha256': hashlib.sha256(junit_bytes).hexdigest()},
    }
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded)
    print(encoded, end='')
    return 0 if report['product_validation'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
