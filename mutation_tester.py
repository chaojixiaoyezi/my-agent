#!/usr/bin/env python3
"""Manual mutation testing for my-agent - 5 core modules.

Usage:
    python mutation_tester.py [--module <name>] [--fix]

Each module gets 5 manual mutations applied. Survived mutations get tests added.
"""

import os
import sys
import subprocess
import tempfile
import shutil
import re
import json
from pathlib import Path
from typing import Optional


class MutationResult:
    def __init__(self, module: str, mutation_id: int, description: str,
                 old_code: str, new_code: str, killed: bool,
                 test_cmd: str = "", error: str = ""):
        self.module = module
        self.mutation_id = mutation_id
        self.description = description
        self.old_code = old_code
        self.new_code = new_code
        self.killed = killed
        self.test_cmd = test_cmd
        self.error = error


class MutationTester:
    def __init__(self, project_root: Path):
        self.project_root = project_root
        self.results: list[MutationResult] = []
        self._backup_dir = project_root / ".mutation_backups"
        self._backup_dir.mkdir(exist_ok=True)

    def _create_mutated_copy(self, source_path: Path, old: str, new: str) -> Optional[Path]:
        """Create a mutated copy of a file."""
        try:
            content = source_path.read_text()
            if old not in content:
                return None

            mutated_content = content.replace(old, new, 1)
            temp_file = self._backup_dir / f"{source_path.stem}_mutated_{os.getpid()}.py"
            temp_file.write_text(mutated_content)
            return temp_file
        except Exception as e:
            print(f"Error creating mutated copy: {e}")
            return None

    def _run_pytest(self, test_path: Path, timeout: int = 120) -> tuple[bool, str]:
        """Run pytest on test file or directory. Returns (passed, output)."""
        try:
            result = subprocess.run(
                ["python", "-m", "pytest", str(test_path), "-v", "--tb=short", "-x"],
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(self.project_root)
            )
            return result.returncode == 0, result.stdout + result.stderr
        except subprocess.TimeoutExpired:
            return False, "TIMEOUT"
        except Exception as e:
            return False, str(e)

    def test_single_mutation(self, module_path: Path, test_path: Path,
                            old: str, new: str, desc: str) -> MutationResult:
        """Test a single mutation."""
        mut_id = len(self.results)
        print(f"  [{mut_id+1}] {desc[:60]}...")

        mutated_file = self._create_mutated_copy(module_path, old, new)
        if mutated_file is None:
            return MutationResult(module_path.name, mut_id, desc, old, new, False, error="Code not found in module")

        # Replace original with mutated
        original_backup = module_path.with_suffix(module_path.suffix + '.orig')
        shutil.copy(module_path, original_backup)
        shutil.copy(mutated_file, module_path)

        try:
            passed, output = self._run_pytest(test_path)
            killed = not passed
            print(f"         -> {'KILLED' if killed else 'SURVIVED'}")
            return MutationResult(module_path.name, mut_id, desc, old, new, killed)
        except Exception as e:
            return MutationResult(module_path.name, mut_id, desc, old, new, False, error=str(e))
        finally:
            # Restore original
            shutil.move(str(original_backup), str(module_path))
            if mutated_file.exists():
                mutated_file.unlink()

    def run_module_mutations(self, module_name: str, module_path: Path,
                              test_path: Path, mutations: list[tuple[str, str, str]]) -> list[MutationResult]:
        """Run all mutations for a module."""
        print(f"\n{'='*60}")
        print(f"Module: {module_name}")
        print(f"Source: {module_path}")
        print(f"Tests:  {test_path}")
        print(f"{'='*60}")

        results = []
        for old, new, desc in mutations:
            result = self.test_single_mutation(module_path, test_path, old, new, desc)
            result.module = module_name
            results.append(result)
            self.results.append(result)

        return results


# === Module-specific mutations ===

def get_models_mutations():
    """Mutations for agent/subagents/models.py"""
    return [
        (
            'TaskStatus.ABANDONED.value,\n    TaskStatus.COMPLETED.value,',
            'TaskStatus.ABANDONED.value,\n    TaskStatus.COMPLETED,',
            "DISPATCH_INELIGIBLE: missing .value on COMPLETED"
        ),
        (
            'cannot_self_accept: bool = True',
            'cannot_self_accept: bool = False',
            "QualityContract.cannot_self_accept default True->False"
        ),
        (
            'parent_final_gate: bool = True',
            'parent_final_gate: bool = False',
            "QualityContract.parent_final_gate default True->False"
        ),
        (
            'can_request_capability: bool = True',
            'can_request_capability: bool = False',
            "SubAgentCard.can_request_capability default True->False"
        ),
        (
            'max_depth: int = 0',
            'max_depth: int = 2',
            "SubAgentCard.max_depth default 0->2"
        ),
    ]


def get_failure_analyzer_mutations():
    """Mutations for agent/agent_core/failure_analyzer.py"""
    return [
        (
            'if task.runner_attempts >= self.max_retry_attempts:',
            'if task.runner_attempts > self.max_retry_attempts:',
            "Timeout: >= max_retry_attempts -> > (one less retry)"
        ),
        (
            'new_timeout = min(current_timeout * 1.5, self.max_timeout)',
            'new_timeout = min(current_timeout * 2.0, self.max_timeout)',
            "Timeout multiplier 1.5x -> 2.0x"
        ),
        (
            'if current_timeout >= self.max_timeout:',
            'if current_timeout > self.max_timeout:',
            "Max timeout check >= -> > (allows one more attempt)"
        ),
        (
            'if task.runner_attempts < self.max_retry_attempts:',
            'if task.runner_attempts <= self.max_retry_attempts:',
            "Parse error retry threshold < -> <="
        ),
        (
            'if task.runner_attempts < self.max_retry_attempts:',
            'if task.runner_attempts <= self.max_retry_attempts:',
            "Generic failure retry < -> <="
        ),
    ]


def get_memory_push_mutations():
    """Mutations for agent/memory_push.py"""
    return [
        (
            'if len(memories_text) >= limit:\n                break',
            'if len(memories_text) > limit:\n                break',
            "Memory limit check >= -> > (one less memory)"
        ),
        (
            'if not record.content or len(record.content) < 10:\n                continue',
            'if not record.content or len(record.content) < 20:\n                continue',
            "Memory content min length 10 -> 20"
        ),
        (
            'if entry.type in {MemoryType.LESSON_GENERAL, MemoryType.LESSON_TASK}:',
            'if entry.type in {MemoryType.LESSON_GENERAL}:',
            "Memory type filter: LESSON_TASK removed"
        ),
        (
            'if len(goal) > 50:\n                query_parts.append(goal[:50])',
            'if len(goal) > 25:\n                query_parts.append(goal[:25])',
            "Goal keyword extraction 50 -> 25 chars"
        ),
        (
            'return memories_text[:limit]',
            'return memories_text[:limit * 2]',
            "Return limit doubled"
        ),
    ]


def get_correlation_mutations():
    """Mutations for agent/log_analysis/security/correlation.py"""
    return [
        (
            'sorted(candidates, key=lambda item: float(item.get("confidence", 0.0)), reverse=True)',
            'sorted(candidates, key=lambda item: float(item.get("confidence", 0.0)))',
            "Entry candidates: descending sort removed"
        ),
        (
            'if not refs:\n        return list(findings)',
            'return list(findings)',
            "Finding filter bypassed when no refs"
        ),
        (
            'finding.confidence if finding.confidence is not None else finding.risk_score',
            'finding.risk_score',
            "Inference confidence: always use risk_score"
        ),
        (
            'if any(finding.detector_id == "waf_attack_success_candidate" for finding in finding_objs) and not any(',
            'if False and not any(',
            "WAF gap check always False"
        ),
        (
            'case_obj.attributes = {**attributes, "route_draft": route.to_dict()}',
            '# case_obj.attributes = {**attributes, "route_draft": route.to_dict()}',
            "Route draft not stored in case attributes"
        ),
    ]


def get_router_mutations():
    """Mutations for agent/capability/router.py"""
    return [
        (
            'token_score += 6.0',
            'token_score += 3.0',
            "Name match score 6.0 -> 3.0"
        ),
        (
            'token_score += 5.0',
            'token_score += 2.0',
            "Capabilities match score 5.0 -> 2.0"
        ),
        (
            'token_score += 4.0',
            'token_score += 2.0',
            "Keywords match score 4.0 -> 2.0"
        ),
        (
            'if score > 0:',
            'if score > 5:',
            "Minimum score threshold 0 -> 5"
        ),
        (
            'effective_limit = self.config.capability_candidate_limit if limit is None else limit',
            'effective_limit = 10 if limit is None else limit',
            "Default candidate limit from config ignored, use 10"
        ),
    ]


# === Test generators for survived mutations ===

def add_test_for_failure_analyzer_mutations():
    """Add tests to cover survived failure_analyzer mutations."""
    test_file = Path("agent_py_agent/tests/test_failure_analyzer.py")

    # Read existing tests
    if test_file.exists():
        content = test_file.read_text()
    else:
        content = ""

    # New test to add
    new_tests = '''

class TestFailureAnalyzerMutationCoverage:
    """Tests to cover mutation-prone logic in failure_analyzer.py."""

    def test_timeout_multiplier_exactly_1_5x(self):
        """Timeout should be exactly 1.5x, not 2.0x.

        Mutation: new_timeout = min(current_timeout * 2.0, self.max_timeout)
        This would cause timeout to increase faster than intended.
        """
        from agent_py_agent.agent.agent_core.failure_analyzer import SubAgentFailureAnalyzer
        from agent_py_agent.agent.subagents.models import SubAgentTask, SubAgentRunnerResult

        analyzer = SubAgentFailureAnalyzer(max_timeout=600.0, max_retry_attempts=3)

        task = SubAgentTask(
            id="test",
            goal="test",
            thought="test",
            plan=["step1", "step2"],
            runner_attempts=0,
        )
        task.attributes = {"dynamic_timeout_seconds": 100.0}

        runner_result = SubAgentRunnerResult(
            run_id="test",
            dry_run=False,
            ok=False,
            status="timeout",
            verification_status="",
            message=""
        )

        result = analyzer.analyze(task, runner_result)
        assert result.failure_type == "runner_timeout"
        assert result.should_adjust_timeout
        assert result.new_timeout_seconds == 150.0, f"Expected 150.0 (100 * 1.5), got {result.new_timeout_seconds}"

    def test_max_timeout_check_uses_gte(self):
        """When current_timeout >= max_timeout, should split (not retry).

        Mutation: if current_timeout > self.max_timeout (changed >= to >)
        This would allow one more retry when timeout equals max_timeout.
        """
        from agent_py_agent.agent.agent_core.failure_analyzer import SubAgentFailureAnalyzer
        from agent_py_agent.agent.subagents.models import SubAgentTask, SubAgentRunnerResult

        analyzer = SubAgentFailureAnalyzer(max_timeout=600.0, max_retry_attempts=3)

        task = SubAgentTask(
            id="test",
            goal="test",
            thought="test",
            plan=["step1", "step2"],
            runner_attempts=1,  # Less than max_retry_attempts
        )
        task.attributes = {"dynamic_timeout_seconds": 600.0}  # Exactly at max

        runner_result = SubAgentRunnerResult(
            run_id="test",
            dry_run=False,
            ok=False,
            status="timeout",
            verification_status="",
            message=""
        )

        result = analyzer.analyze(task, runner_result)
        # At max_timeout, should suggest split_task, not increase_timeout_and_retry
        assert result.suggested_action == "split_task", f"Expected split_task at max_timeout, got {result.suggested_action}"
        assert not result.should_retry

'''

    # Append new tests
    if "test_timeout_multiplier_exactly_1_5x" not in content:
        test_file.write_text(content + new_tests)
        print(f"  Added mutation coverage tests to {test_file}")


def add_test_for_memory_push_mutations():
    """Add tests to cover survived memory_push mutations."""
    test_file = Path("agent_py_agent/tests/test_memory_push.py")

    if test_file.exists():
        content = test_file.read_text()
    else:
        content = ""

    new_tests = '''

class TestMemoryPushMutationCoverage:
    """Tests to cover mutation-prone logic in memory_push.py."""

    def test_memory_limit_uses_gte_not_gt(self):
        """Memory limit check should use >= not >.

        Mutation: if len(memories_text) > limit (changed >= to >)
        This would return one extra memory item.
        """
        from unittest.mock import MagicMock
        from agent_py_agent.agent.memory_push import push_relevant_memories, MemoryType, MemoryEntry

        mock_agent = MagicMock()
        mock_agent.memory = MagicMock()

        # Create mock records that will pass filters
        mock_records = []
        for i in range(5):
            mock_record = MagicMock()
            mock_record.content = f"This is a test memory content number {i} with enough length"
            mock_record.kind = "lesson_general"
            mock_record.tags = []
            mock_record.created_at = 0.0
            mock_records.append(mock_record)

        mock_agent.memory.search.return_value = mock_records

        context = {"task_id": "test", "goal": "timeout test scenario"}
        result = push_relevant_memories(mock_agent, "timeout", context, limit=3)

        # With >= check, limit=3 should return exactly 3 items
        # With > check (mutated), it could return 4
        assert len(result) == 3, f"Expected exactly 3 memories with limit=3, got {len(result)}"

    def test_memory_content_min_length_10(self):
        """Memory content must be at least 10 chars.

        Mutation: len(record.content) < 10 changed to < 20
        This would allow shorter (potentially meaningless) memories through.
        """
        from unittest.mock import MagicMock
        from agent_py_agent.agent.memory_push import push_relevant_memories

        mock_agent = MagicMock()
        mock_agent.memory = MagicMock()

        # Create a record with exactly 10 chars (should be included)
        mock_record = MagicMock()
        mock_record.content = "1234567890"  # Exactly 10 chars
        mock_record.kind = "lesson_general"
        mock_record.tags = []
        mock_record.created_at = 0.0

        mock_agent.memory.search.return_value = [mock_record]

        context = {"task_id": "test", "goal": "test"}
        result = push_relevant_memories(mock_agent, "timeout", context, limit=3)

        # With 10 char min, this should be included
        assert len(result) == 1, f"Expected 1 memory with 10-char content, got {len(result)}"

    def test_memory_type_filter_includes_lesson_task(self):
        """Memory type filter must include both LESSON_GENERAL and LESSON_TASK.

        Mutation: Filter changed to only include LESSON_GENERAL
        This would miss valuable LESSON_TASK memories.
        """
        from unittest.mock import MagicMock
        from agent_py_agent.agent.memory_push import push_relevant_memories, MemoryType

        mock_agent = MagicMock()
        mock_agent.memory = MagicMock()

        # Create a LESSON_TASK record
        mock_record = MagicMock()
        mock_record.content = "This is a task-specific lesson that should be included for timeout"
        mock_record.kind = "lesson_task"
        mock_record.tags = []
        mock_record.created_at = 0.0

        mock_agent.memory.search.return_value = [mock_record]

        context = {"task_id": "test", "goal": "timeout scenario"}
        result = push_relevant_memories(mock_agent, "timeout", context, limit=3)

        # LESSON_TASK should be included for timeout trigger
        assert len(result) == 1, f"Expected LESSON_TASK to be included, got {len(result)} memories"

'''

    if "test_memory_limit_uses_gte_not_gt" not in content:
        test_file.write_text(content + new_tests)
        print(f"  Added mutation coverage tests to {test_file}")


def add_test_for_correlation_mutations():
    """Add tests to cover survived correlation mutations."""
    test_file = Path("agent_py_agent/tests/test_log_correlation.py")

    if test_file.exists():
        content = test_file.read_text()
    else:
        content = ""

    new_tests = '''

class TestCorrelationMutationCoverage:
    """Tests to cover mutation-prone logic in correlation.py."""

    def test_entry_candidates_sorted_descending(self):
        """Entry candidates must be sorted in descending confidence order.

        Mutation: reverse=True removed from sorted()
        This would produce incorrect ascending order.
        """
        from agent_py_agent.agent.log_analysis.security.correlation import _entry_candidates
        from agent_py_agent.agent.subagents.models import Finding

        # Create findings with different confidence levels
        finding1 = Finding(
            finding_id="f1",
            case_id="c1",
            detector_id="waf_attack_success_candidate",
            hypothesis="test",
            confidence=0.3,
            risk_score=0.3,
            window=["t1", "t2"],
            entities={},
            evidence_refs=[],
            tags=[],
            gaps=[],
            next_queries=[]
        )
        finding2 = Finding(
            finding_id="f2",
            case_id="c1",
            detector_id="waf_attack_success_candidate",
            hypothesis="test",
            confidence=0.9,
            risk_score=0.9,
            window=["t1", "t2"],
            entities={},
            evidence_refs=[],
            tags=[],
            gaps=[],
            next_queries=[]
        )

        candidates = _entry_candidates([finding1, finding2])

        # Higher confidence should come first
        assert len(candidates) == 2
        assert candidates[0]["confidence"] >= candidates[1]["confidence"], \
            f"Expected descending order, got {[c['confidence'] for c in candidates]}"

    def test_finding_filter_respects_finding_refs(self):
        """Finding filter must respect case.finding_refs.

        Mutation: 'if not refs: return list(findings)' removed
        This would return all findings regardless of case's finding_refs.
        """
        from agent_py_agent.agent.log_analysis.security.correlation import _filter_findings_for_case
        from agent_py_agent.agent.subagents.models import CaseRecord, Finding

        case = CaseRecord(
            case_id="c1",
            finding_refs=["f1"],  # Only want f1
            entities={},
            gaps=[],
            next_queries=[],
            created_at="t1",
            updated_at="t2"
        )

        finding1 = Finding(
            finding_id="f1",
            case_id="c1",
            detector_id="test",
            hypothesis="test",
            confidence=0.5,
            risk_score=0.5,
            window=["t1", "t2"],
            entities={},
            evidence_refs=[],
            tags=[],
            gaps=[],
            next_queries=[]
        )
        finding2 = Finding(
            finding_id="f2",
            case_id="c1",
            detector_id="test",
            hypothesis="test",
            confidence=0.5,
            risk_score=0.5,
            window=["t1", "t2"],
            entities={},
            evidence_refs=[],
            tags=[],
            gaps=[],
            next_queries=[]
        )

        result = _filter_findings_for_case(case, [finding1, finding2])

        # Should only return f1 since case.finding_refs = ["f1"]
        assert len(result) == 1, f"Expected 1 finding (f1 only), got {len(result)}"
        assert result[0].finding_id == "f1"

    def test_inference_uses_confidence_field_when_present(self):
        """Inference confidence must use 'confidence' field when available.

        Mutation: Always uses risk_score instead of confidence
        This would ignore explicit confidence values.
        """
        from agent_py_agent.agent.log_analysis.security.correlation import _inference_for_finding
        from agent_py_agent.agent.subagents.models import Finding

        finding = Finding(
            finding_id="f1",
            case_id="c1",
            detector_id="test",
            hypothesis="test hypothesis",
            confidence=0.8,  # Explicit confidence
            risk_score=0.3,  # Different risk_score
            window=["t1", "t2"],
            entities={},
            evidence_refs=[],
            tags=[],
            gaps=[],
            next_queries=[]
        )

        inference = _inference_for_finding(finding)

        # Should use confidence (0.8), not risk_score (0.3)
        assert inference["confidence"] == 0.8, \
            f"Expected confidence 0.8, got {inference['confidence']}"

'''

    if "test_entry_candidates_sorted_descending" not in content:
        test_file.write_text(content + new_tests)
        print(f"  Added mutation coverage tests to {test_file}")


def add_test_for_router_mutations():
    """Add tests to cover survived router mutations."""
    test_file = Path("agent_py_agent/tests/test_capabilities.py")

    if test_file.exists():
        content = test_file.read_text()
    else:
        content = ""

    new_tests = '''

class TestRouterMutationCoverage:
    """Tests to cover mutation-prone logic in router.py."""

    def test_name_match_score_is_6(self):
        """Name match should score 6 points, not 3.

        Mutation: token_score += 6.0 changed to += 3.0
        This would underweight name matches.
        """
        from agent_py_agent.agent.capability.router import score_card, CapabilityCard

        card = CapabilityCard(
            id="test",
            kind="skill",
            name="file_writer",
            description="writes files to disk"
        )

        score, reasons = score_card("file_writer", card)

        # "file_writer" appears in name, should get 6 points for name match
        assert score >= 6.0, f"Name match should contribute at least 6 points, got {score}"

    def test_capabilities_match_score_is_5(self):
        """Capabilities match should score 5 points.

        Mutation: token_score += 5.0 changed to += 2.0
        This would underweight capabilities matches.
        """
        from agent_py_agent.agent.capability.router import score_card, CapabilityCard

        card = CapabilityCard(
            id="test",
            kind="skill",
            name="data_processor",
            description="processes data",
            capabilities=["analysis", "transformation"]
        )

        score, reasons = score_card("analysis", card)

        # "analysis" appears in capabilities, should get 5 points
        assert score >= 5.0, f"Capabilities match should contribute at least 5 points, got {score}"

    def test_score_threshold_is_zero(self):
        """Minimum score threshold should be 0, not 5.

        Mutation: 'if score > 0:' changed to 'if score > 5:'
        This would skip cards with low but valid scores.
        """
        from agent_py_agent.agent.capability.router import score_card, CapabilityCard

        card = CapabilityCard(
            id="test",
            kind="skill",
            name="basic_tool",
            description="a basic tool"
        )

        score, reasons = score_card("tool", card)

        # Even a partial match should return score > 0
        assert score > 0, f"Score should be > 0 for partial match, got {score}"

    def test_search_respects_custom_limit_not_hardcoded_10(self):
        """Search should respect the config's capability_candidate_limit.

        Mutation: Config value ignored, hardcoded to 10
        """
        from agent_py_agent.agent.capability.router import CapabilityRouter, CapabilityCard
        from agent_py_agent.agent.capability.config import CapabilityConfig

        config = CapabilityConfig(capability_candidate_limit=2)
        router = CapabilityRouter(config=config)

        # Add multiple cards
        for i in range(5):
            card = CapabilityCard(
                id=f"skill_{i}",
                kind="skill",
                name=f"skill_{i}",
                description=f"test skill {i}"
            )
            router.register(card)

        hits = router.search("test")

        # With candidate_limit=2, should return at most 2
        assert len(hits) <= 2, f"Expected max 2 candidates with limit=2, got {len(hits)}"

'''

    if "test_name_match_score_is_6" not in content:
        test_file.write_text(content + new_tests)
        print(f"  Added mutation coverage tests to {test_file}")


def main():
    project_root = Path(__file__).parent

    tester = MutationTester(project_root)

    modules = [
        ("models.py", "agent_py_agent/agent/subagents/models.py",
         "agent_py_agent/tests/test_subagent_models.py", get_models_mutations),
        ("failure_analyzer.py", "agent_py_agent/agent/agent_core/failure_analyzer.py",
         "agent_py_agent/tests/test_failure_analyzer.py", get_failure_analyzer_mutations),
        ("memory_push.py", "agent_py_agent/agent/memory_push.py",
         "agent_py_agent/tests/test_memory_push.py", get_memory_push_mutations),
        ("correlation.py", "agent_py_agent/agent/log_analysis/security/correlation.py",
         "agent_py_agent/tests/test_log_correlation.py", get_correlation_mutations),
        ("router.py", "agent_py_agent/agent/capability/router.py",
         "agent_py_agent/tests/test_capabilities.py", get_router_mutations),
    ]

    all_results = []
    for name, source, test, mutations_fn in modules:
        source_path = project_root / source
        test_path = project_root / test
        mutations = mutations_fn()
        results = tester.run_module_mutations(name, source_path, test_path, mutations)
        all_results.extend(results)

    # Summary
    print(f"\n{'='*60}")
    print("MUTATION TEST SUMMARY")
    print(f"{'='*60}")

    killed = sum(1 for r in all_results if r.killed)
    survived = sum(1 for r in all_results if not r.killed)
    total = len(all_results)
    score = (killed / total * 100) if total > 0 else 0

    print(f"Total mutations tested: {total}")
    print(f"Killed by tests:       {killed}")
    print(f"Survived (need fix):    {survived}")
    print(f"Mutation Score:         {score:.1f}%")

    if survived > 0:
        print(f"\n{'='*60}")
        print("SURVIVED MUTATIONS - Adding test coverage...")
        print(f"{'='*60}")

        survived_by_module = {}
        for r in all_results:
            if not r.killed:
                if r.module not in survived_by_module:
                    survived_by_module[r.module] = []
                survived_by_module[r.module].append(r)

        for module, results in survived_by_module.items():
            print(f"\n[{module}] {len(results)} survived mutations")
            for r in results:
                print(f"  - {r.description}")
                if r.error:
                    print(f"    Error: {r.error}")

        # Add tests for survived mutations
        print(f"\n{'='*60}")
        print("ADDING TEST COVERAGE")
        print(f"{'='*60}")

        if "failure_analyzer.py" in survived_by_module:
            add_test_for_failure_analyzer_mutations()
        if "memory_push.py" in survived_by_module:
            add_test_for_memory_push_mutations()
        if "correlation.py" in survived_by_module:
            add_test_for_correlation_mutations()
        if "router.py" in survived_by_module:
            add_test_for_router_mutations()

    # Save report
    report = {
        "total_mutations": total,
        "killed": killed,
        "survived": survived,
        "mutation_score": score,
        "results": [
            {
                "module": r.module,
                "description": r.description,
                "killed": r.killed,
                "error": r.error
            }
            for r in all_results
        ]
    }

    report_path = project_root / "mutation_test_report.json"
    report_path.write_text(json.dumps(report, indent=2))
    print(f"\nReport: {report_path}")

    return 0 if score >= 80 else 1


if __name__ == "__main__":
    sys.exit(main())