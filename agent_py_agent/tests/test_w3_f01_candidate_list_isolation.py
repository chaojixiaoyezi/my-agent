"""One unreadable memory candidate body must not hide every other candidate from /memory candidates.

Live clone (2026-11-17, W3 F01 seeded fault): the body file of one blocked candidate was replaced with bytes
that fail the digest check. Candidate writes, exact previews and the curator kept working, but the
`/memory candidates` list answered only MEMORY_CONTROL_UNAVAILABLE because CandidateService.list() parses the
whole ledger strictly and the first unreadable body aborted it.
"""
from __future__ import annotations

from agent_py_agent.agent.memory_store import candidates as candidate_store
from agent_py_agent.agent.memory_store.candidate_ledger import CandidateRows, read_manifest
from agent_py_agent.tests.test_memory_candidate_control import (
    memory_control,  # noqa: F401 - pytest fixture
)


def _break_body(control, candidate_id):
    service = control.agent.memory_candidates
    rows = CandidateRows(service.path, read_manifest(service.path))
    position = rows.index_for(candidate_id, None)
    identifier = rows.index.get(rows.root, 'pos/' + str(position))
    entry = rows.index.get(rows.root, 'row/' + identifier)
    body = service.path.parent / (service.path.name + '.records') / entry['record']['ref']
    original = body.read_bytes()
    body.write_bytes(b'\xff\xfe' + original[2:])
    # A restarted gateway reads the ledger cold; drop this process's parsed-ledger cache the same way.
    with candidate_store._CANDIDATE_LOAD_CACHE_LOCK:
        candidate_store._CANDIDATE_LOAD_CACHE.clear()
    return body, original


def _second_candidate(control):
    from agent_py_agent.agent.capability.memory_tool import RememberTool
    RememberTool(control.agent).execute(dict(content='W3 F01 健康候选', kind='fact', origin='model_inferred',
                                             subject_key='w3.f01.healthy'))
    return next(row for row in control.agent.memory_candidates.list() if row.subject_key == 'w3.f01.healthy')


def test_list_skips_only_the_unreadable_candidate_and_names_it(memory_control):  # noqa: F811
    c = memory_control
    healthy = _second_candidate(c)
    body, original = _break_body(c, c.candidate.candidate_id)
    code, result = c.send('/memory candidates')
    assert code == 200 and result['ok'], result
    listed = {item['candidate_id'] for item in result['details']['candidates']}
    assert healthy.candidate_id in listed and c.candidate.candidate_id not in listed
    assert result['details']['unreadable'][0]['candidate_id'] == c.candidate.candidate_id
    assert c.candidate.candidate_id in result['message'] and '读不出' in result['message']
    assert body.read_bytes() != original and body.read_bytes()[2:] == original[2:]
    code, preview = c.send('/memory candidates preview ' + healthy.candidate_id)
    assert code == 200 and preview['ok'], preview


def test_healthy_ledger_list_is_unchanged(memory_control):  # noqa: F811
    c = memory_control
    code, result = c.send('/memory candidates')
    assert code == 200 and result['ok'] and 'unreadable' not in result['details']
    assert [item['candidate_id'] for item in result['details']['candidates']] == [c.candidate.candidate_id]
