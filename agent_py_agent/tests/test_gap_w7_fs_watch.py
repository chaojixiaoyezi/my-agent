"""工作区监听的确定性回归：真实 nofollow/epoch 读取与 Event 控制的搜索交错。"""
from __future__ import annotations

import gc
import importlib
import importlib.util
import itertools
import os
import sqlite3
import sys
import threading
import types
import weakref
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts.business_api import bind_business_context
from agent_py_agent.agent.gateway_parts.business_api_v13 import V13Call
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.plugin_host.schemas import validate_object
from agent_py_agent.agent.user_space.owner_epoch import activate_owner_epoch


@pytest.fixture(scope='module')
def contracts():
    return {'actions': [{'name': 'fs_watch.subscribe',
                  'workstream': 'W7',
                  'item': 'G2',
                  'surface': 'client_business_v13',
                  'effect': 'control',
                  'admin': False,
                  'idempotency_required': True,
                  'expected_revision_required': False,
                  'switch': None,
                  'params_schema': {'type': 'object',
                                    'properties': {'root': {'type': 'string',
                                                            'minLength': 1,
                                                            'maxLength': 4096,
                                                            'x-path-rule': 'workspace-relative POSIX path; '
                                                                           "reject absolute, '..' segments, NUL, "
                                                                           "leading '-', and any resolved target "
                                                                           'outside the confined root (symlinks '
                                                                           'resolved before the check)'},
                                                   'recursive': {'type': 'boolean'},
                                                   'max_depth': {'type': 'integer', 'minimum': 0, 'maximum': 8},
                                                   'include_globs': {'type': 'array',
                                                                     'items': {'type': 'string',
                                                                               'minLength': 1,
                                                                               'maxLength': 256},
                                                                     'maxItems': 16},
                                                   'exclude_globs': {'type': 'array',
                                                                     'items': {'type': 'string',
                                                                               'minLength': 1,
                                                                               'maxLength': 256},
                                                                     'maxItems': 16}},
                                    'required': ['root'],
                                    'additionalProperties': False},
                  'result_schema': {'type': 'object',
                                    'properties': {'subscription_id': {'type': 'string',
                                                                       'minLength': 1,
                                                                       'maxLength': 128,
                                                                       'pattern': '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'},
                                                   'epoch': {'type': 'integer', 'minimum': 0},
                                                   'seq': {'type': 'integer', 'minimum': 0},
                                                   'snapshot': {'$ref': '#/$defs/FsSnapshot'}},
                                    'required': ['subscription_id', 'epoch', 'seq', 'snapshot'],
                                    'additionalProperties': False},
                  'errors': [{'code': 'INVALID_SCHEMA',
                              'http_status': 400,
                              'retry_safe': False,
                              'meaning': 'fix the request'},
                             {'code': 'BUSINESS_ACCESS_DENIED',
                              'http_status': 403,
                              'retry_safe': False,
                              'meaning': 'caller lacks access'},
                             {'code': 'HOST_NOT_READY',
                              'http_status': 503,
                              'retry_safe': True,
                              'meaning': 'feature switch off or dependency unavailable; retry after enabling'},
                             {'code': 'IDEMPOTENCY_CONFLICT',
                              'http_status': 409,
                              'retry_safe': False,
                              'meaning': 'same idempotency_key with different params'},
                             {'code': 'OUTCOME_UNKNOWN',
                              'http_status': 503,
                              'retry_safe': False,
                              'meaning': 'query state before any retry; never blind-retry'},
                             {'code': 'PATH_OUTSIDE_WORKSPACE',
                              'http_status': 403,
                              'retry_safe': False,
                              'meaning': 'path rejected by confinement (traversal, symlink escape, internal '
                                         'dir)'},
                             {'code': 'LIMIT_EXCEEDED',
                              'http_status': 400,
                              'retry_safe': False,
                              'meaning': 'a bound from gap158_limits.py or config was hit; details.limit'}],
                  'cas': 'none',
                  'owner_isolation': 'owner from authenticated context only; foreign ids behave as absent; '
                                     'storage/registries keyed by owner_id',
                  'notes': 'Pull-based: bounded os.scandir walk (<= FS_WATCH_MAX_ENTRIES_PER_SCAN entries, depth '
                           '<= max_depth) of the subscribed root only; no OS watcher threads. Subscriptions are '
                           'process-local, keyed by owner, bound to the owner epoch, capped by '
                           'FS_WATCH_MAX_SUBSCRIPTIONS_PER_OWNER.'},
                 {'name': 'fs_watch.events',
                  'workstream': 'W7',
                  'item': 'G2',
                  'surface': 'client_business_v13',
                  'effect': 'read',
                  'admin': False,
                  'idempotency_required': False,
                  'expected_revision_required': False,
                  'switch': None,
                  'params_schema': {'type': 'object',
                                    'properties': {'subscription_id': {'type': 'string',
                                                                       'minLength': 1,
                                                                       'maxLength': 128,
                                                                       'pattern': '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'},
                                                   'after_seq': {'type': 'integer', 'minimum': 0},
                                                   'limit': {'type': 'integer',
                                                             'minimum': 1,
                                                             'maximum': 100,
                                                             'default': 50}},
                                    'required': ['subscription_id', 'after_seq'],
                                    'additionalProperties': False},
                  'result_schema': {'type': 'object',
                                    'properties': {'subscription_id': {'type': 'string',
                                                                       'minLength': 1,
                                                                       'maxLength': 128,
                                                                       'pattern': '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'},
                                                   'epoch': {'type': 'integer', 'minimum': 0},
                                                   'events': {'type': 'array',
                                                              'items': {'$ref': '#/$defs/FsEvent'},
                                                              'maxItems': 100},
                                                   'next_seq': {'type': 'integer', 'minimum': 0},
                                                   'overflow': {'type': 'boolean'}},
                                    'required': ['subscription_id', 'epoch', 'events', 'next_seq', 'overflow'],
                                    'additionalProperties': False},
                  'errors': [{'code': 'INVALID_SCHEMA',
                              'http_status': 400,
                              'retry_safe': False,
                              'meaning': 'fix the request'},
                             {'code': 'BUSINESS_ACCESS_DENIED',
                              'http_status': 403,
                              'retry_safe': False,
                              'meaning': 'caller lacks access'},
                             {'code': 'HOST_NOT_READY',
                              'http_status': 503,
                              'retry_safe': True,
                              'meaning': 'feature switch off or dependency unavailable; retry after enabling'},
                             {'code': 'RESOURCE_NOT_FOUND',
                              'http_status': 404,
                              'retry_safe': False,
                              'meaning': 'absent or owned by another owner (indistinguishable)'}],
                  'cas': 'none',
                  'owner_isolation': 'owner from authenticated context only; foreign ids behave as absent; '
                                     'storage/registries keyed by owner_id',
                  'notes': 'Rescans at most once per FS_WATCH_MIN_SCAN_INTERVAL_SECONDS and diffs (inode, size, '
                           'mtime_ns); ring of FS_WATCH_EVENT_RING events; after_seq older than the ring -> one '
                           'overflow event (client must remount). CAS for edits uses workspace.files.read sha256 '
                           '+ workspace.files.write expected_sha256.'},
                 {'name': 'fs_watch.remount',
                  'workstream': 'W7',
                  'item': 'G2',
                  'surface': 'client_business_v13',
                  'effect': 'control',
                  'admin': False,
                  'idempotency_required': True,
                  'expected_revision_required': False,
                  'switch': None,
                  'params_schema': {'type': 'object',
                                    'properties': {'subscription_id': {'type': 'string',
                                                                       'minLength': 1,
                                                                       'maxLength': 128,
                                                                       'pattern': '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'}},
                                    'required': ['subscription_id'],
                                    'additionalProperties': False},
                  'result_schema': {'type': 'object',
                                    'properties': {'subscription_id': {'type': 'string',
                                                                       'minLength': 1,
                                                                       'maxLength': 128,
                                                                       'pattern': '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'},
                                                   'epoch': {'type': 'integer', 'minimum': 0},
                                                   'seq': {'type': 'integer', 'minimum': 0},
                                                   'snapshot': {'$ref': '#/$defs/FsSnapshot'}},
                                    'required': ['subscription_id', 'epoch', 'seq', 'snapshot'],
                                    'additionalProperties': False},
                  'errors': [{'code': 'INVALID_SCHEMA',
                              'http_status': 400,
                              'retry_safe': False,
                              'meaning': 'fix the request'},
                             {'code': 'BUSINESS_ACCESS_DENIED',
                              'http_status': 403,
                              'retry_safe': False,
                              'meaning': 'caller lacks access'},
                             {'code': 'HOST_NOT_READY',
                              'http_status': 503,
                              'retry_safe': True,
                              'meaning': 'feature switch off or dependency unavailable; retry after enabling'},
                             {'code': 'IDEMPOTENCY_CONFLICT',
                              'http_status': 409,
                              'retry_safe': False,
                              'meaning': 'same idempotency_key with different params'},
                             {'code': 'OUTCOME_UNKNOWN',
                              'http_status': 503,
                              'retry_safe': False,
                              'meaning': 'query state before any retry; never blind-retry'},
                             {'code': 'RESOURCE_NOT_FOUND',
                              'http_status': 404,
                              'retry_safe': False,
                              'meaning': 'absent or owned by another owner (indistinguishable)'}],
                  'cas': 'none',
                  'owner_isolation': 'owner from authenticated context only; foreign ids behave as absent; '
                                     'storage/registries keyed by owner_id',
                  'notes': 'New epoch, fresh snapshot event; seq keeps increasing.'},
                 {'name': 'fs_watch.unsubscribe',
                  'workstream': 'W7',
                  'item': 'G2',
                  'surface': 'client_business_v13',
                  'effect': 'control',
                  'admin': False,
                  'idempotency_required': True,
                  'expected_revision_required': False,
                  'switch': None,
                  'params_schema': {'type': 'object',
                                    'properties': {'subscription_id': {'type': 'string',
                                                                       'minLength': 1,
                                                                       'maxLength': 128,
                                                                       'pattern': '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'}},
                                    'required': ['subscription_id'],
                                    'additionalProperties': False},
                  'result_schema': {'type': 'object',
                                    'properties': {'removed': {'type': 'boolean'}},
                                    'required': ['removed'],
                                    'additionalProperties': False},
                  'errors': [{'code': 'INVALID_SCHEMA',
                              'http_status': 400,
                              'retry_safe': False,
                              'meaning': 'fix the request'},
                             {'code': 'BUSINESS_ACCESS_DENIED',
                              'http_status': 403,
                              'retry_safe': False,
                              'meaning': 'caller lacks access'},
                             {'code': 'HOST_NOT_READY',
                              'http_status': 503,
                              'retry_safe': True,
                              'meaning': 'feature switch off or dependency unavailable; retry after enabling'},
                             {'code': 'IDEMPOTENCY_CONFLICT',
                              'http_status': 409,
                              'retry_safe': False,
                              'meaning': 'same idempotency_key with different params'},
                             {'code': 'OUTCOME_UNKNOWN',
                              'http_status': 503,
                              'retry_safe': False,
                              'meaning': 'query state before any retry; never blind-retry'}],
                  'cas': 'none',
                  'owner_isolation': 'owner from authenticated context only; foreign ids behave as absent; '
                                     'storage/registries keyed by owner_id',
                  'notes': 'Idempotent.'},
                 {'name': 'fs_watch.search',
                  'workstream': 'W7',
                  'item': 'G2',
                  'surface': 'client_business_v13',
                  'effect': 'execute',
                  'admin': False,
                  'idempotency_required': True,
                  'expected_revision_required': False,
                  'switch': None,
                  'params_schema': {'type': 'object',
                                    'properties': {'subscription_id': {'type': 'string',
                                                                       'minLength': 1,
                                                                       'maxLength': 128,
                                                                       'pattern': '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'},
                                                   'query': {'type': 'string', 'minLength': 1, 'maxLength': 256},
                                                   'glob': {'type': 'string', 'minLength': 0, 'maxLength': 256},
                                                   'max_results': {'type': 'integer',
                                                                   'minimum': 1,
                                                                   'maximum': 500}},
                                    'required': ['subscription_id', 'query'],
                                    'additionalProperties': False},
                  'result_schema': {'type': 'object',
                                    'properties': {'search_id': {'type': 'string',
                                                                 'minLength': 1,
                                                                 'maxLength': 128,
                                                                 'pattern': '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'},
                                                   'state': {'type': 'string',
                                                             'enum': ['running', 'completed', 'cancelled']}},
                                    'required': ['search_id', 'state'],
                                    'additionalProperties': False},
                  'errors': [{'code': 'INVALID_SCHEMA',
                              'http_status': 400,
                              'retry_safe': False,
                              'meaning': 'fix the request'},
                             {'code': 'BUSINESS_ACCESS_DENIED',
                              'http_status': 403,
                              'retry_safe': False,
                              'meaning': 'caller lacks access'},
                             {'code': 'HOST_NOT_READY',
                              'http_status': 503,
                              'retry_safe': True,
                              'meaning': 'feature switch off or dependency unavailable; retry after enabling'},
                             {'code': 'IDEMPOTENCY_CONFLICT',
                              'http_status': 409,
                              'retry_safe': False,
                              'meaning': 'same idempotency_key with different params'},
                             {'code': 'OUTCOME_UNKNOWN',
                              'http_status': 503,
                              'retry_safe': False,
                              'meaning': 'query state before any retry; never blind-retry'},
                             {'code': 'RESOURCE_NOT_FOUND',
                              'http_status': 404,
                              'retry_safe': False,
                              'meaning': 'absent or owned by another owner (indistinguishable)'},
                             {'code': 'LIMIT_EXCEEDED',
                              'http_status': 400,
                              'retry_safe': False,
                              'meaning': 'a bound from gap158_limits.py or config was hit; details.limit'}],
                  'cas': 'none',
                  'owner_isolation': 'owner from authenticated context only; foreign ids behave as absent; '
                                     'storage/registries keyed by owner_id',
                  'notes': 'Literal substring search (no regular expressions) on a bounded worker; files > '
                           'FS_SEARCH_MAX_FILE_BYTES skipped; matches arrive as search_match events on the '
                           'subscription stream; at most FS_SEARCH_MAX_CONCURRENT per owner.'},
                 {'name': 'fs_watch.search_cancel',
                  'workstream': 'W7',
                  'item': 'G2',
                  'surface': 'client_business_v13',
                  'effect': 'control',
                  'admin': False,
                  'idempotency_required': True,
                  'expected_revision_required': False,
                  'switch': None,
                  'params_schema': {'type': 'object',
                                    'properties': {'search_id': {'type': 'string',
                                                                 'minLength': 1,
                                                                 'maxLength': 128,
                                                                 'pattern': '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'}},
                                    'required': ['search_id'],
                                    'additionalProperties': False},
                  'result_schema': {'type': 'object',
                                    'properties': {'search_id': {'type': 'string',
                                                                 'minLength': 1,
                                                                 'maxLength': 128,
                                                                 'pattern': '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'},
                                                   'state': {'type': 'string',
                                                             'enum': ['running', 'completed', 'cancelled']}},
                                    'required': ['search_id', 'state'],
                                    'additionalProperties': False},
                  'errors': [{'code': 'INVALID_SCHEMA',
                              'http_status': 400,
                              'retry_safe': False,
                              'meaning': 'fix the request'},
                             {'code': 'BUSINESS_ACCESS_DENIED',
                              'http_status': 403,
                              'retry_safe': False,
                              'meaning': 'caller lacks access'},
                             {'code': 'HOST_NOT_READY',
                              'http_status': 503,
                              'retry_safe': True,
                              'meaning': 'feature switch off or dependency unavailable; retry after enabling'},
                             {'code': 'IDEMPOTENCY_CONFLICT',
                              'http_status': 409,
                              'retry_safe': False,
                              'meaning': 'same idempotency_key with different params'},
                             {'code': 'OUTCOME_UNKNOWN',
                              'http_status': 503,
                              'retry_safe': False,
                              'meaning': 'query state before any retry; never blind-retry'},
                             {'code': 'RESOURCE_NOT_FOUND',
                              'http_status': 404,
                              'retry_safe': False,
                              'meaning': 'absent or owned by another owner (indistinguishable)'}],
                  'cas': 'none',
                  'owner_isolation': 'owner from authenticated context only; foreign ids behave as absent; '
                                     'storage/registries keyed by owner_id',
                  'notes': 'Idempotent.'}],
     '$defs': {'FsEntry': {'type': 'object',
                           'properties': {'path': {'type': 'string',
                                                   'minLength': 1,
                                                   'maxLength': 4096,
                                                   'x-path-rule': 'workspace-relative POSIX path; reject '
                                                                  "absolute, '..' segments, NUL, leading '-', "
                                                                  'and any resolved target outside the confined '
                                                                  'root (symlinks resolved before the check)'},
                                          'kind': {'type': 'string', 'enum': ['file', 'dir', 'symlink', 'other']},
                                          'size': {'type': 'integer', 'minimum': 0},
                                          'mtime_ns': {'type': 'integer', 'minimum': 0}},
                           'required': ['path', 'kind', 'size', 'mtime_ns'],
                           'additionalProperties': False},
               'FsSnapshot': {'type': 'object',
                              'properties': {'entries': {'type': 'array',
                                                         'items': {'$ref': '#/$defs/FsEntry'},
                                                         'maxItems': 5000},
                                             'truncated': {'type': 'boolean'}},
                              'required': ['entries', 'truncated'],
                              'additionalProperties': False},
               'FsChange': {'type': 'object',
                            'properties': {'op': {'type': 'string', 'enum': ['created', 'modified', 'deleted']},
                                           'path': {'type': 'string',
                                                    'minLength': 1,
                                                    'maxLength': 4096,
                                                    'x-path-rule': 'workspace-relative POSIX path; reject '
                                                                   "absolute, '..' segments, NUL, leading '-', "
                                                                   'and any resolved target outside the confined '
                                                                   'root (symlinks resolved before the check)'},
                                           'kind': {'type': 'string',
                                                    'enum': ['file', 'dir', 'symlink', 'other']},
                                           'size': {'type': 'integer', 'minimum': 0},
                                           'mtime_ns': {'type': 'integer', 'minimum': 0}},
                            'required': ['op', 'path', 'kind'],
                            'additionalProperties': False},
               'FsSearchMatch': {'type': 'object',
                                 'properties': {'path': {'type': 'string',
                                                         'minLength': 1,
                                                         'maxLength': 4096,
                                                         'x-path-rule': 'workspace-relative POSIX path; reject '
                                                                        "absolute, '..' segments, NUL, leading "
                                                                        "'-', and any resolved target outside "
                                                                        'the confined root (symlinks resolved '
                                                                        'before the check)'},
                                                'line': {'type': 'integer', 'minimum': 0},
                                                'preview': {'type': 'string', 'minLength': 0, 'maxLength': 512}},
                                 'required': ['path', 'line', 'preview'],
                                 'additionalProperties': False},
               'FsEvent': {'type': 'object',
                           'properties': {'seq': {'type': 'integer', 'minimum': 0},
                                          'kind': {'type': 'string',
                                                   'enum': ['snapshot', 'delta', 'search_match', 'overflow']},
                                          'epoch': {'type': 'integer', 'minimum': 0},
                                          'changes': {'type': 'array',
                                                      'items': {'$ref': '#/$defs/FsChange'},
                                                      'maxItems': 500},
                                          'match': {'$ref': '#/$defs/FsSearchMatch'},
                                          'search_id': {'type': 'string', 'minLength': 0, 'maxLength': 128},
                                          'reason': {'type': 'string', 'minLength': 0, 'maxLength': 64}},
                           'required': ['seq', 'kind', 'epoch'],
                           'additionalProperties': False}},
     'limits': {'module': 'agent_py_agent/agent/business_spaces/gap158_limits.py',
                'constants': [{'name': 'LLM_GATEWAY_TEST_MAX_OUTPUT_TOKENS', 'value': 16, 'used_by': 'W1'},
                              {'name': 'LLM_RECORDS_METADATA_MAX_BYTES', 'value': 4096, 'used_by': 'W1'},
                              {'name': 'LLM_RECORDS_AGGREGATE_DEFAULT_WINDOW_SECONDS',
                               'value': 604800,
                               'used_by': 'W1'},
                              {'name': 'LLM_RECORDS_AGGREGATE_MAX_WINDOW_SECONDS',
                               'value': 2678400,
                               'used_by': 'W1'},
                              {'name': 'LLM_RECORDS_AGGREGATE_MAX_MODELS', 'value': 200, 'used_by': 'W1'},
                              {'name': 'LLM_RECORDS_PRUNE_BATCH_ROWS', 'value': 500, 'used_by': 'W1'},
                              {'name': 'OPENAI_COMPAT_MAX_MESSAGES', 'value': 512, 'used_by': 'W1'},
                              {'name': 'OPENAI_COMPAT_STREAM_CHUNK_CHARS', 'value': 2048, 'used_by': 'W1'},
                              {'name': 'POLICY_RULES_MAX_PER_OWNER', 'value': 500, 'used_by': 'W2'},
                              {'name': 'POLICY_PENDING_MAX_PER_OWNER', 'value': 200, 'used_by': 'W2'},
                              {'name': 'POLICY_HISTORY_SEGMENT_BYTES', 'value': 4194304, 'used_by': 'W2'},
                              {'name': 'POLICY_HISTORY_MAX_SEGMENTS', 'value': 8, 'used_by': 'W2'},
                              {'name': 'POLICY_HISTORY_SCAN_MAX_RECORDS', 'value': 5000, 'used_by': 'W2'},
                              {'name': 'POLICY_TASK_SUMMARY_SCAN_RECORDS', 'value': 2000, 'used_by': 'W2'},
                              {'name': 'SCOPE_RULES_MAX_PER_OWNER', 'value': 500, 'used_by': 'W2'},
                              {'name': 'SCOPE_TASK_RULES_MAX_PER_TASK', 'value': 100, 'used_by': 'W2'},
                              {'name': 'POLICY_JUDGE_WORKERS', 'value': 2, 'used_by': 'W2'},
                              {'name': 'SIDE_QUESTIONS_MAX_PER_CONVERSATION', 'value': 50, 'used_by': 'W3'},
                              {'name': 'SIDE_QUESTIONS_MAX_CONCURRENT_PER_OWNER', 'value': 2, 'used_by': 'W3'},
                              {'name': 'SIDE_QUESTION_WORKERS', 'value': 4, 'used_by': 'W3'},
                              {'name': 'SIDE_QUESTION_EVENT_RING', 'value': 256, 'used_by': 'W3'},
                              {'name': 'SIDE_QUESTION_CONTEXT_MAX_MESSAGES', 'value': 40, 'used_by': 'W3'},
                              {'name': 'SIDE_QUESTION_CONTEXT_MAX_CHARS', 'value': 120000, 'used_by': 'W3'},
                              {'name': 'AGENT_PROMPT_HISTORY_MAX_PER_ROLE', 'value': 20, 'used_by': 'W4'},
                              {'name': 'PROMPT_TRIGGERS_MAX_PER_ROLE', 'value': 32, 'used_by': 'W4'},
                              {'name': 'SKILL_ZIP_MAX_COMPRESSION_RATIO', 'value': 100, 'used_by': 'W5'},
                              {'name': 'SKILL_PACKAGE_FILE_MAX_BYTES', 'value': 1048576, 'used_by': 'W5'},
                              {'name': 'CUSTOM_TOOLS_MAX_PER_OWNER', 'value': 50, 'used_by': 'W5'},
                              {'name': 'CUSTOM_TOOLS_PROBE_TIMEOUT_SECONDS', 'value': 5, 'used_by': 'W5'},
                              {'name': 'NOTIFY_CHANNELS_MAX_PER_OWNER', 'value': 50, 'used_by': 'W6'},
                              {'name': 'NOTIFY_DRAIN_BATCH', 'value': 20, 'used_by': 'W6'},
                              {'name': 'NOTIFY_WORKERS', 'value': 2, 'used_by': 'W6'},
                              {'name': 'OPS_UPDATE_LOG_RING', 'value': 2000, 'used_by': 'W6'},
                              {'name': 'FS_WATCH_MAX_SUBSCRIPTIONS_PER_OWNER', 'value': 8, 'used_by': 'W7'},
                              {'name': 'FS_WATCH_MAX_ENTRIES_PER_SCAN', 'value': 5000, 'used_by': 'W7'},
                              {'name': 'FS_WATCH_MIN_SCAN_INTERVAL_SECONDS', 'value': 1.0, 'used_by': 'W7'},
                              {'name': 'FS_WATCH_EVENT_RING', 'value': 1024, 'used_by': 'W7'},
                              {'name': 'FS_WATCH_IDLE_EXPIRY_SECONDS', 'value': 900, 'used_by': 'W7'},
                              {'name': 'FS_SEARCH_MAX_FILE_BYTES', 'value': 1048576, 'used_by': 'W7'},
                              {'name': 'FS_SEARCH_MAX_CONCURRENT', 'value': 2, 'used_by': 'W7'},
                              {'name': 'SCM_MAX_SUBSCRIPTIONS_PER_OWNER', 'value': 8, 'used_by': 'W7'},
                              {'name': 'SCM_MAX_STATUS_ENTRIES', 'value': 2000, 'used_by': 'W7'},
                              {'name': 'SCM_MAX_LOG_ENTRIES', 'value': 50, 'used_by': 'W7'},
                              {'name': 'SCM_MAX_PATHS_PER_ACTION', 'value': 200, 'used_by': 'W7'},
                              {'name': 'SCM_GIT_TIMEOUT_SECONDS', 'value': 15, 'used_by': 'W7'},
                              {'name': 'SCM_MIN_REFRESH_INTERVAL_SECONDS', 'value': 1.0, 'used_by': 'W7'},
                              {'name': 'ACP_MAX_SESSIONS_PER_OWNER', 'value': 4, 'used_by': 'W8'},
                              {'name': 'ACP_EVENT_RING', 'value': 2048, 'used_by': 'W8'},
                              {'name': 'ACP_PROBE_TIMEOUT_SECONDS', 'value': 15, 'used_by': 'W8'},
                              {'name': 'ACP_TERMINATE_GRACE_SECONDS', 'value': 3.0, 'used_by': 'W8'},
                              {'name': 'WEB_TERMINAL_MAX_SESSIONS_PER_OWNER', 'value': 4, 'used_by': 'W9'},
                              {'name': 'WEB_TERMINAL_GRANT_TTL_SECONDS', 'value': 120, 'used_by': 'W9'},
                              {'name': 'WEB_TERMINAL_READ_MAX_BYTES', 'value': 65536, 'used_by': 'W9'},
                              {'name': 'PRESENCE_TTL_SECONDS', 'value': 30, 'used_by': 'W9'},
                              {'name': 'PRESENCE_MAX_MEMBERS_PER_DOC', 'value': 50, 'used_by': 'W9'},
                              {'name': 'DOC_STATE_MAX_BYTES', 'value': 262144, 'used_by': 'W9'}]}}


@pytest.fixture(scope='module')
# LLM: 直接导入生产模块，不能用内存限额掩盖 W0 缺失时的导入故障。
# 函数用途: 为全部回归取得真实 FS 实现。
def fs(contracts):
    return importlib.import_module('agent_py_agent.agent.business_spaces.gap158_w7_fs_watch')


class _EpochRepo:
    """只替代数据库持有者，不替代原 read_owner_epoch/activate_owner_epoch SQL。"""
    def __init__(self):
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(':memory:', check_same_thread=False)
        self.conn.execute('CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT)')
        self.conn.commit()

    @contextmanager
    def transaction(self):
        with self.lock, self.conn:
            yield self.conn


class _Clock:
    def __init__(self):
        self.now = 10000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class _Harness:
    def __init__(self, fs, base, contracts, owner='local/test'):
        self.fs, self.contracts, self.owner = fs, contracts, owner
        self.home = base / owner.replace('/', '-')
        self.root = self.home / 'workspace'
        self.root.mkdir(parents=True)
        self.repo, self.clock = _EpochRepo(), _Clock()
        self.allowed = True
        self.host = SimpleNamespace(
            home_paths=SimpleNamespace(owner_id=owner, owner_home_dir=self.home,
                                       owner_workspace_dir=self.root),
            config=SimpleNamespace(workspace_root='', business_spaces_enabled=True),
            subagents=SimpleNamespace(runtime_db=self.repo))
        self.context = bind_business_context(self.host, authorize=lambda *_: self.allowed,
                                             subject_id='test-subject')
        self.counter = itertools.count()
        self.actions = {a['name']: a for a in contracts['actions'] if a['name'].startswith('fs_watch.')}

    def call(self, action, params, key=None):
        return V13Call('fs_watch.' + action, params, 'req', self.owner, 'test-subject',
            self.context, SimpleNamespace(owner_id=self.owner), self.home,
            key or f'test-key-{next(self.counter):016d}', clock=self.clock)

    def invoke(self, action, params, key=None):
        call = self.call(action, params, key)
        spec = self.actions[call.action]
        validate_object(params, {**spec['params_schema'], '$defs': self.contracts['$defs']})
        result = getattr(self.fs, action)(call)
        validate_object(result, {**spec['result_schema'], '$defs': self.contracts['$defs']})
        return result

    def subscribe(self, **params):
        return self.invoke('subscribe', {'root': '.', **params})

    def poll(self, snapshot, after=None, **params):
        return self.invoke('events', {'subscription_id': snapshot['subscription_id'],
            'after_seq': snapshot['seq'] if after is None else after, **params})

    @property
    # LLM: 测试从稳定 owner/home 键观察状态，不要求生产把状态附在请求级宿主上。
    # 函数用途: 返回本测试拥有的进程内状态。
    def state(self):
        return self.fs._owner_states[(self.owner, str(self.home.resolve()))]

    # LLM: 搜索句柄来自同一个 owner 状态，不靠请求宿主寿命查找。
    # 函数用途: 取出搜索任务以等待确定性退出。
    def job(self, result):
        return self.state.searches[result['search_id']]

    def finish(self, job):
        assert job.done.wait(3), '搜索未在限定时间退出'
        job.thread.join(3)
        assert not job.thread.is_alive()

    # LLM: fixture 只清理自己创建的桶与工作线程，不改变别的 owner 状态。
    # 函数用途: 回收本测试资源，避免共享模块缓存污染后续回归。
    def close(self):
        state = self.fs._owner_states.get((self.owner, str(self.home.resolve())))
        if state is not None:
            for job in state.searches.values():
                job.cancel.set()
            for job in state.searches.values():
                self.finish(job)
            self.fs._owner_states.pop(state.owner, None)
        self.repo.conn.close()


@pytest.fixture
def harness(fs, contracts, tmp_path):
    # 测试目录由 pytest 隔离；不依赖任务副本的机器路径或真实 HOME。
    h = _Harness(fs, tmp_path, contracts)
    try:
        yield h
    finally:
        h.close()


def _error(code):
    return pytest.raises(HostAPIError, match=code)


def test_snapshot_delta_signature_and_throttle(harness):
    h = harness
    (h.root / 'a.txt').write_text('old')
    snap = h.subscribe()
    assert [e['path'] for e in snap['snapshot']['entries']] == ['a.txt']
    assert snap['snapshot']['truncated'] is False
    initial = h.poll(snap, after=0)
    assert initial['events'] == [{'seq': snap['seq'], 'kind': 'snapshot', 'epoch': snap['epoch']}]
    (h.root / 'b.txt').write_text('new')
    assert h.poll(snap)['events'] == []
    h.clock.advance(h.fs.FS_WATCH_MIN_SCAN_INTERVAL_SECONDS)
    delta = h.poll(snap)
    assert delta['events'][0]['changes'][0]['op'] == 'created'
    assert delta['events'][0]['changes'][0]['path'] == 'b.txt'
    # 大小与 mtime 保持相同，仅替换 inode 也必须识别 modified。
    stamp = (h.root / 'a.txt').stat()
    replacement = h.root / 'replacement'
    replacement.write_text('OLD')
    os.utime(replacement, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    replacement.replace(h.root / 'a.txt')
    (h.root / 'b.txt').unlink()
    h.clock.advance(h.fs.FS_WATCH_MIN_SCAN_INTERVAL_SECONDS)
    result = h.poll(snap, after=delta['next_seq'])
    changes = result['events'][0]['changes']
    assert [(c['op'], c['path']) for c in changes] == [('modified', 'a.txt'), ('deleted', 'b.txt')]


@pytest.mark.parametrize('path', ['..', '../x', '/tmp', 'a/../../x', '\x00', '-x',
                                 'a/-x', '.git', 'a/.GiT/config', 'C:/x', '\\server\\x'])
def test_lexical_paths_rejected_before_scan(harness, monkeypatch, path):
    def forbidden(_):
        pytest.fail('词法拒绝前不能扫描')
    monkeypatch.setattr(harness.fs, '_scan', forbidden)
    with _error('PATH_OUTSIDE_WORKSPACE'):
        harness.invoke('subscribe', {'root': path})


def test_symlink_root_parent_and_hardlinks_are_not_followed(harness, monkeypatch):
    h = harness
    outside = h.home / 'outside'
    outside.mkdir()
    (outside / 'secret').write_text('needle')
    (h.root / 'escape').symlink_to(outside, target_is_directory=True)
    with _error('PATH_OUTSIDE_WORKSPACE'):
        h.subscribe(root='escape')
    with _error('PATH_OUTSIDE_WORKSPACE'):
        h.subscribe(root='escape/nested')
    # 当前执行环境禁止创建硬链接；以真实 stat 的 nlink=2 投影单独验证过滤门。
    (h.root / 'hardlink').write_text('needle')
    original = h.fs.os.scandir
    @contextmanager
    def multi_link(fd):
        with original(fd) as entries:
            def rows():
                for entry in entries:
                    if entry.name != 'hardlink':
                        yield entry
                        continue
                    real = entry.stat(follow_symlinks=False)
                    values = list(real)
                    values[3] = 2
                    yield SimpleNamespace(name=entry.name, stat=lambda **_: os.stat_result(values))
            yield rows()
    monkeypatch.setattr(h.fs.os, 'scandir', multi_link)
    snap = h.subscribe(recursive=True)
    assert snap['snapshot']['entries'] == []


def test_recursive_depth_globs_and_internal_directories(harness):
    h = harness
    (h.root / 'a').mkdir()
    (h.root / 'a' / 'b').mkdir()
    (h.root / 'a' / 'one.txt').write_text('one')
    (h.root / 'a' / 'b' / 'two.txt').write_text('two')
    (h.root / '.GiT').mkdir()
    (h.root / '.GiT' / 'config').write_text('hidden')
    snap = h.subscribe(recursive=True, max_depth=1, include_globs=['*.txt'], exclude_globs=['*two*'])
    assert [e['path'] for e in snap['snapshot']['entries']] == ['a/one.txt']
    h.host.config.workspace_root = str(h.home)
    (h.home / 'data').mkdir()
    (h.home / 'config').mkdir()
    (h.home / 'data' / 'secret').write_text('hidden')
    home_snap = h.subscribe(recursive=True)
    paths = [e['path'] for e in home_snap['snapshot']['entries']]
    assert not any(p.startswith(('data', 'config')) for p in paths)
    with _error('PATH_OUTSIDE_WORKSPACE'):
        h.subscribe(root='data')


def test_filtered_items_still_spend_scan_budget(harness, monkeypatch):
    h = harness
    monkeypatch.setattr(h.fs, 'FS_WATCH_MAX_ENTRIES_PER_SCAN', 3)
    for index in range(12):
        (h.root / f'f{index}').write_text('x')
    count = []
    original = h.fs.os.scandir

    @contextmanager
    def counted(fd):
        with original(fd) as listing:
            def items():
                for item in listing:
                    count.append(item.name)
                    yield item
            yield items()
    monkeypatch.setattr(h.fs.os, 'scandir', counted)
    snap = h.subscribe(include_globs=['does-not-match'])
    assert snap['snapshot'] == {'entries': [], 'truncated': True}
    assert len(count) == 3
    h.clock.advance(h.fs.FS_WATCH_MIN_SCAN_INTERVAL_SECONDS)
    assert h.poll(snap)['events'][0]['reason'] == 'scan_truncated'


def test_ring_overflow_remount_epoch_and_sequence(harness, monkeypatch):
    h = harness
    monkeypatch.setattr(h.fs, 'FS_WATCH_EVENT_RING', 3)
    snap = h.subscribe()
    for index in range(5):
        (h.root / f'f{index}').write_text(str(index))
        h.clock.advance(h.fs.FS_WATCH_MIN_SCAN_INTERVAL_SECONDS)
        latest = h.poll(snap, after=0)
    assert latest['overflow'] is True
    assert len(latest['events']) == 1
    assert latest['events'][0]['kind'] == 'overflow'
    params = {'subscription_id': snap['subscription_id']}
    mounted = h.invoke('remount', params, key='same-remount-key-001')
    assert mounted['epoch'] > snap['epoch']
    assert mounted['seq'] > latest['next_seq']
    assert len(mounted['snapshot']['entries']) == 5
    assert h.invoke('remount', params, key='same-remount-key-001') == mounted
    assert h.poll(mounted, after=latest['next_seq'])['events'][0]['kind'] == 'snapshot'


def test_subscription_cap_owner_isolation_and_idempotency(harness, contracts, monkeypatch):
    h = harness
    monkeypatch.setattr(h.fs, 'FS_WATCH_MAX_SUBSCRIPTIONS_PER_OWNER', 1)
    snap = h.invoke('subscribe', {'root': '.'}, key='same-subscribe-001')
    assert h.invoke('subscribe', {'root': '.'}, key='same-subscribe-001') == snap
    with _error('IDEMPOTENCY_CONFLICT'):
        h.invoke('subscribe', {'root': '.', 'recursive': True}, key='same-subscribe-001')
    with _error('LIMIT_EXCEEDED'):
        h.subscribe()
    other = _Harness(h.fs, h.home.parent, contracts, owner='local/other')
    try:
        with _error('RESOURCE_NOT_FOUND'):
            other.poll(snap)
        assert other.invoke('unsubscribe', {'subscription_id': snap['subscription_id']}) == {'removed': False}
    finally:
        other.close()
    params = {'subscription_id': snap['subscription_id']}
    assert h.invoke('unsubscribe', params, key='same-unsubscribe-01') == {'removed': True}
    assert h.invoke('unsubscribe', params, key='same-unsubscribe-01') == {'removed': True}
    assert h.invoke('unsubscribe', params) == {'removed': False}
    with _error('RESOURCE_NOT_FOUND'):
        h.poll(snap)


# LLM: 断言保持原惰性回收与真实 epoch 读取，仅改为观察稳定 owner 状态。
# 函数用途: 验证本人下一次请求清理过期订阅与换代回执。
def test_lazy_idle_expiry_and_real_owner_epoch(harness):
    h = harness
    snap = h.subscribe()
    state = h.state
    h.clock.advance(h.fs.FS_WATCH_IDLE_EXPIRY_SECONDS)
    assert snap['subscription_id'] in state.subscriptions
    with _error('RESOURCE_NOT_FOUND'):
        h.poll(snap)
    assert state.subscriptions == {}
    assert state.receipts == {}
    fresh = h.subscribe()
    assert h.fs.current_epoch(h.call('events', {})) == 0
    assert activate_owner_epoch(h.repo) == 1
    assert h.fs.current_epoch(h.call('events', {})) == 1
    with _error('RESOURCE_NOT_FOUND'):
        h.poll(fresh)
    assert h.subscribe()['epoch'] == 1


def test_trusted_workspace_and_epoch_dependencies(harness):
    h = harness
    call = h.call('subscribe', {'root': '/untrusted'})
    assert h.fs.workspace_root(call) == h.root
    assert h.fs.confined_parts('./a/b') == ('a', 'b')
    h.host.config.workspace_root = ['workspace', '/ignored']
    assert h.fs.workspace_root(call) == h.root
    h.host.conversation_store = SimpleNamespace(storage=SimpleNamespace(owner_epoch_repository=h.repo))
    h.host.subagents = None
    assert h.fs.current_epoch(call) == 0
    h.host.conversation_store = None
    with _error('HOST_NOT_READY'):
        h.fs.current_epoch(call)


# LLM: 保持原搜索匹配、截断与重放断言，状态观察不再依赖请求级宿主。
# 函数用途: 检验字面搜索和幂等结果不会重开任务。
def test_literal_search_matches_file_bound_limit_and_replay(harness, monkeypatch):
    h = harness
    monkeypatch.setattr(h.fs, 'FS_SEARCH_MAX_FILE_BYTES', 1024)
    (h.root / 'text.txt').write_text('a.b\naxb\n' + 'x' * 600 + 'a.b\na.b\n')
    (h.root / 'binary.txt').write_bytes(b'a.b\x00')
    (h.root / 'large.txt').write_text('a.b' * 1024)
    snap = h.subscribe()
    params = {'subscription_id': snap['subscription_id'], 'query': 'a.b',
              'glob': '*.txt', 'max_results': 2}
    result = h.invoke('search', params, key='search-replay-key-001')
    job = h.job(result)
    h.finish(job)
    assert job.state == 'completed'
    assert h.invoke('search', params, key='search-replay-key-001') == result
    assert len(h.state.searches) == 1
    batch = h.poll(snap)
    matches = [e['match'] for e in batch['events'] if e['kind'] == 'search_match']
    assert [(m['path'], m['line']) for m in matches] == [('text.txt', 1), ('text.txt', 3)]
    assert len(matches[1]['preview']) == 512
    assert h.invoke('search_cancel', {'search_id': result['search_id']})['state'] == 'completed'
    with _error('IDEMPOTENCY_CONFLICT'):
        h.invoke('search', {**params, 'query': 'different'}, key='search-replay-key-001')


def test_search_cancel_event_keeps_slot_until_worker_exit(harness, monkeypatch):
    h = harness
    (h.root / 'text').write_text('needle')
    snap = h.subscribe()
    entered, release = threading.Event(), threading.Event()
    original = h.fs._read_search_file

    def blocked(sub, path):
        entered.set()
        assert release.wait(3)
        return original(sub, path)
    monkeypatch.setattr(h.fs, '_read_search_file', blocked)
    monkeypatch.setattr(h.fs, 'FS_SEARCH_MAX_CONCURRENT', 1)
    params = {'subscription_id': snap['subscription_id'], 'query': 'needle'}
    result = h.invoke('search', params)
    job = h.job(result)
    try:
        assert entered.wait(3)
        cancelled = h.invoke('search_cancel', {'search_id': result['search_id']}, key='cancel-key-0001')
        assert cancelled['state'] == 'cancelled'
        assert job.cancel.is_set()
        assert h.invoke('search_cancel', {'search_id': result['search_id']}, key='cancel-key-0001') == cancelled
        with _error('LIMIT_EXCEEDED'):
            h.invoke('search', params)
    finally:
        release.set()
    h.finish(job)
    assert not any(e['kind'] == 'search_match' for e in h.poll(snap)['events'])
    another = h.invoke('search', params)
    h.finish(h.job(another))
    assert h.job(another).state == 'completed'


@pytest.mark.parametrize('interrupt', ['epoch', 'remount', 'unsubscribe', 'authorization', 'idle'])
# LLM: 保持原 Event 交错与撤销断言，仅从稳定 owner 桶取订阅。
# 函数用途: 验证不同撤销来源都不能发布过期搜索结果。
def test_inflight_results_discarded_after_revocation(harness, monkeypatch, interrupt):
    h = harness
    (h.root / 'text').write_text('needle')
    snap = h.subscribe()
    state = h.state
    sub = state.subscriptions[snap['subscription_id']]
    entered, release = threading.Event(), threading.Event()
    original = h.fs._read_search_file

    def blocked(subscription, path):
        content = original(subscription, path)
        entered.set()
        assert release.wait(3)
        return content
    monkeypatch.setattr(h.fs, '_read_search_file', blocked)
    result = h.invoke('search', {'subscription_id': sub.sid, 'query': 'needle'})
    job = h.job(result)
    try:
        assert entered.wait(3)
        if interrupt == 'epoch':
            activate_owner_epoch(h.repo)
        elif interrupt == 'remount':
            h.invoke('remount', {'subscription_id': sub.sid})
        elif interrupt == 'unsubscribe':
            h.invoke('unsubscribe', {'subscription_id': sub.sid})
        elif interrupt == 'idle':
            h.clock.advance(h.fs.FS_WATCH_IDLE_EXPIRY_SECONDS)
            with _error('RESOURCE_NOT_FOUND'):
                h.poll(snap)
        else:
            h.allowed = False
    finally:
        release.set()
    h.finish(job)
    assert job.state == 'cancelled'
    assert not any(event['kind'] == 'search_match' for event in sub.ring)


def test_root_replacement_cannot_follow_new_symlink(harness):
    h = harness
    folder = h.root / 'folder'
    folder.mkdir()
    (folder / 'text').write_text('original')
    outside = h.home / 'outside'
    outside.mkdir()
    (outside / 'text').write_text('secret')
    snap = h.subscribe(root='folder')
    folder.rename(h.root / 'old-folder')
    folder.symlink_to(outside, target_is_directory=True)
    h.clock.advance(h.fs.FS_WATCH_MIN_SCAN_INTERVAL_SECONDS)
    assert h.poll(snap)['events'][0]['reason'] == 'root_unavailable'
    result = h.invoke('search', {'subscription_id': snap['subscription_id'], 'query': 'secret'})
    h.finish(h.job(result))
    assert not any(e['kind'] == 'search_match' for e in h.poll(snap)['events'])
    with _error('RESOURCE_NOT_FOUND'):
        h.invoke('remount', {'subscription_id': snap['subscription_id']})


# LLM: 保留原容量、回执和空闲清理严格断言，只更换状态的观察位置。
# 函数用途: 验证有界缓存不能静默丢回执或重开旧搜索。
def test_registries_receipts_are_bounded_without_restarting_old_search(harness, monkeypatch):
    h = harness
    monkeypatch.setattr(h.fs, 'FS_WATCH_EVENT_RING', 6)
    snap = h.subscribe()
    params = {'subscription_id': snap['subscription_id'], 'query': 'missing'}
    first = h.invoke('search', params, key='old-search-key-01')
    h.finish(h.job(first))
    for _ in range(4):
        result = h.invoke('search', params)
        h.finish(h.job(result))
    state = h.state
    assert len(state.receipts) == 6
    assert len(state.searches) == 5
    with _error('LIMIT_EXCEEDED'):
        h.invoke('search', params)
    assert h.invoke('search', params, key='old-search-key-01') == first
    assert len(state.searches) == 5
    h.clock.advance(h.fs.FS_WATCH_IDLE_EXPIRY_SECONDS)
    with _error('RESOURCE_NOT_FOUND'):
        h.poll(snap)
    assert state.subscriptions == {}
    assert state.searches == {}
    assert state.receipts == {}


# LLM: 在独立模块名下执行真实源码，既不改生产缓存，也不创建替代限额源码。
# 函数用途: 装载受控 W0 缺失或已安装分支供导入回归。
def _load_fs_variant(monkeypatch, limits):
    package = 'agent_py_agent.agent.business_spaces'
    monkeypatch.setitem(sys.modules, package + '.gap158_limits', limits)
    source = Path(__file__).parents[1] / 'agent/business_spaces/gap158_w7_fs_watch.py'
    name = package + '._fs_import_probe'
    spec = importlib.util.spec_from_file_location(name, source)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


# LLM: 缺失分支必须独立导入成功，并逐项匹配冻结合同，不借测试补发常量。
# 函数用途: 检验未合入 W0 时生产 fallback 真正可用。
def test_real_fallback_import_matches_frozen_limits(monkeypatch, contracts):
    module = _load_fs_variant(monkeypatch, None)
    for row in contracts['limits']['constants']:
        if row['name'].startswith(('FS_WATCH_', 'FS_SEARCH_')):
            assert getattr(module, row['name']) == row['value']


# LLM: 替换分支只在此测试注入 W0，并用不同于 fallback 的值证明导入优先级。
# 函数用途: 检验 W0 合入后的常量会覆盖兼容默认值。
def test_w0_limits_replace_fallback(monkeypatch, contracts):
    limits = types.ModuleType('agent_py_agent.agent.business_spaces.gap158_limits')
    values = {}
    for row in contracts['limits']['constants']:
        if row['name'].startswith(('FS_WATCH_', 'FS_SEARCH_')):
            values[row['name']] = row['value'] + 1
            setattr(limits, row['name'], values[row['name']])
    module = _load_fs_variant(monkeypatch, limits)
    assert {name: getattr(module, name) for name in values} == values


# LLM: 模拟原生入口每次新建宿主，原 owner、home、epoch 不变时不能丢订阅或幂等回执。
# 函数用途: 连续跨宿主订阅、拉事件、搜索和取消，验证真实状态身份锚。
def test_new_host_preserves_owner_state_and_control_replays(harness, monkeypatch):
    h = harness
    (h.root / 'text').write_text('needle')
    snap = h.invoke('subscribe', {'root': '.'}, key='fresh-subscribe-001')
    old_host = h.host
    h.host = SimpleNamespace(home_paths=old_host.home_paths, config=old_host.config,
                             subagents=old_host.subagents)
    h.context = bind_business_context(h.host, authorize=lambda *_: h.allowed,
                                     subject_id='test-subject')
    assert h.host is not old_host
    assert h.invoke('subscribe', {'root': '.'}, key='fresh-subscribe-001') == snap
    assert h.poll(snap, after=0)['events'][0]['kind'] == 'snapshot'
    entered, release = threading.Event(), threading.Event()
    original = h.fs._read_search_file

    def blocked(sub, path):
        entered.set()
        assert release.wait(3)
        return original(sub, path)
    monkeypatch.setattr(h.fs, '_read_search_file', blocked)
    params = {'subscription_id': snap['subscription_id'], 'query': 'needle'}
    result = h.invoke('search', params, key='fresh-search-key-001')
    job = h.job(result)
    try:
        assert entered.wait(3)
        h.host = SimpleNamespace(home_paths=old_host.home_paths, config=old_host.config,
                                 subagents=old_host.subagents)
        h.context = bind_business_context(h.host, authorize=lambda *_: h.allowed,
                                         subject_id='test-subject')
        assert h.invoke('search', params, key='fresh-search-key-001') == result
        cancelled = h.invoke('search_cancel', {'search_id': result['search_id']},
                             key='fresh-cancel-key-001')
        assert cancelled['state'] == 'cancelled'
        assert h.invoke('search_cancel', {'search_id': result['search_id']},
                        key='fresh-cancel-key-001') == cancelled
    finally:
        release.set()
    h.finish(job)
    assert not any(e['kind'] == 'search_match' for e in h.poll(snap)['events'])


# LLM: 配置不是跨 owner 授权；即使 full 模式也必须先过真实 PathAccessPolicy。
# 函数用途: 断言另一 owner 的配置根在扫描前被拒绝。
@pytest.mark.parametrize('mode', ['normal', 'full'])
def test_foreign_owner_configured_root_denied_before_scan(harness, monkeypatch, mode):
    h = harness
    h.host.config.path_access_mode = mode
    h.host.config.workspace_root = str(h.home.parent / 'other-owner' / 'workspace')

    def forbidden(_):
        pytest.fail('外 owner 配置根拒绝之前不得扫描')
    monkeypatch.setattr(h.fs, '_scan', forbidden)
    with _error('PATH_OUTSIDE_WORKSPACE'):
        h.subscribe()


# LLM: 缓存仅按 owner/home 分桶；同 owner 在不同 home 的数据不能借标识相互取到。
# 函数用途: 直接验证稳定状态的身份与实时授权，不替换文件扫描实现。
def test_state_home_isolation_and_live_access(harness, contracts, tmp_path):
    h = harness
    first = h.fs._state(h.call('events', {}))
    other = _Harness(h.fs, tmp_path / 'separate', contracts, owner=h.owner)
    try:
        second = h.fs._state(other.call('events', {}))
        assert first is not second
        assert first.owner != second.owner
        h.allowed = False
        with _error('CAPABILITY_DENIED'):
            h.fs._state(h.call('events', {}))
    finally:
        other.close()


# LLM: 以已有环限额约束桶数，只检查最旧桶；忙桶和未过期桶不得为新 owner 被静默驱逐。
# 函数用途: 验证注册表容量拒绝、单桶惰性回收以及不扫描其它 owner。
def test_owner_registry_capacity_checks_only_oldest(harness, contracts, tmp_path, monkeypatch):
    h = harness
    state = h.fs._state(h.call('events', {}))
    monkeypatch.setattr(h.fs, 'FS_WATCH_EVENT_RING', 1)
    other = _Harness(h.fs, tmp_path / 'separate', contracts, owner='local/another')
    try:
        with _error('HOST_NOT_READY'):
            h.fs._state(other.call('events', {}))
        assert list(h.fs._owner_states) == [state.owner]
        state.touched -= h.fs.FS_WATCH_IDLE_EXPIRY_SECONDS
        pending = SimpleNamespace(done=threading.Event())
        state.searches['busy'] = pending
        with _error('HOST_NOT_READY'):
            h.fs._state(other.call('events', {}))
        pending.done.set()
        second = h.fs._state(other.call('events', {}))
        assert list(h.fs._owner_states) == [second.owner]
        assert state.owner not in h.fs._owner_states
    finally:
        state.searches.clear()
        other.close()


# LLM: 不把配置变化前的缓存当成授权，根、枚举和文件读取均使用真实 PathAccessPolicy。
# 函数用途: 检验宿主专有来源的枚举过滤、搜索拒绝和节流期的策略撤销。
def test_actual_policy_filters_scan_and_rechecks_search(harness):
    h = harness
    (h.root / '.env').write_text('needle')
    (h.root / 'public.txt').write_text('public')
    # owner 自用 .env 按合同允许；显式宿主来源才是策略拒绝样本，不另造文件名黑名单。
    call = h.call('subscribe', {'root': '.'})
    assert h.fs._path_policy(call).check(h.root / '.env').allowed
    h.host.config.host_exclusive_path_roots = [str(h.root / '.env')]
    assert h.fs._path_policy(call).check(h.root / '.env').code == 'PATH_HOST_CREDENTIAL_BLOCKED'
    snap = h.subscribe()
    assert [e['path'] for e in snap['snapshot']['entries']] == ['public.txt']
    sub = h.state.subscriptions[snap['subscription_id']]
    with _error('PATH_OUTSIDE_WORKSPACE'):
        h.fs._read_search_file(sub, '.env')
    h.host.config.path_access_mode = 'full'
    assert h.fs._path_policy(call).check(h.root / '.env').code == 'PATH_HOST_CREDENTIAL_BLOCKED'
    # Full 不能越过宿主来源保护；撤掉来源声明后才恢复 owner 自用文件的可读视图。
    h.host.config.host_exclusive_path_roots = []
    full = h.subscribe()
    assert '.env' in [e['path'] for e in full['snapshot']['entries']]
    h.host.config.path_access_mode = 'normal'
    with _error('RESOURCE_NOT_FOUND'):
        h.poll(full)


# LLM: 即使最后一个文件没有匹配，读后撤权或换代也必须取消；运行完不在状态中保留原授权对象。
# 函数用途: 用 Event 精确控制无匹配读后的撤销，并核对授权上下文回收。
@pytest.mark.parametrize('interrupt', ['authorization', 'epoch'])
def test_no_match_read_rechecks_revocation_and_releases_context(harness, monkeypatch, interrupt):
    h = harness
    (h.root / 'text').write_text('no match')
    snap = h.subscribe()
    entered, release = threading.Event(), threading.Event()
    original = h.fs._read_search_file

    def blocked(sub, path):
        content = original(sub, path)
        entered.set()
        assert release.wait(3)
        return content
    monkeypatch.setattr(h.fs, '_read_search_file', blocked)
    context_ref = weakref.ref(h.context)
    result = h.invoke('search', {'subscription_id': snap['subscription_id'], 'query': 'needle'})
    job = h.job(result)
    try:
        assert entered.wait(3)
        if interrupt == 'epoch':
            activate_owner_epoch(h.repo)
        else:
            h.allowed = False
        h.context = bind_business_context(h.host, authorize=lambda *_: h.allowed,
                                         subject_id='test-subject')
    finally:
        release.set()
    h.finish(job)
    assert job.state == 'cancelled'
    assert not any(e['kind'] == 'search_match' for e in job.subscription.ring)
    gc.collect()
    assert context_ref() is None
    if interrupt == 'epoch':
        with _error('RESOURCE_NOT_FOUND'):
            h.invoke('search_cancel', {'search_id': result['search_id']})
