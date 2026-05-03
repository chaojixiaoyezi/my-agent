#!/usr/bin/env python3
"""Manual mutation testing framework for my-agent.

This script performs mutation testing by:
1. Taking source code and creating mutations (changed logic)
2. Running existing tests
3. Reporting which mutations were NOT caught (survived)
4. Adding tests to capture survived mutations
"""

import os
import sys
import subprocess
import tempfile
import shutil
import re
import json
from pathlib import Path
from typing import Callable, Optional


class MutationTestResult:
    def __init__(self, module: str, mutation_id: int, description: str,
                 original_code: str, mutated_code: str, killed: bool,
                 test_output: str = ""):
        self.module = module
        self.mutation_id = mutation_id
        self.description = description
        self.original_code = original_code
        self.mutated_code = mutated_code
        self.killed = killed
        self.test_output = test_output


class MutationTestFramework:
    def __init__(self, project_root: str):
        self.project_root = Path(project_root)
        self.results: list[MutationTestResult] = []

    def run_tests_for_module(self, module_path: str) -> bool:
        """Run tests for a specific module. Returns True if tests pass."""
        try:
            result = subprocess.run(
                ["python", "-m", "pytest", module_path, "-v", "--tb=short", "-x"],
                capture_output=True,
                text=True,
                timeout=120,
                cwd=str(self.project_root)
            )
            return result.returncode == 0
        except Exception as e:
            print(f"Error running tests: {e}")
            return False

    def apply_mutation(self, source_path: str, mutations: list[tuple[str, str]]) -> list[str]:
        """Apply mutations and return list of mutated file contents.

        Each mutation is (old_code, new_code) tuple.
        """
        with open(source_path, 'r') as f:
            original = f.read()

        mutated_files = []
        for old, new in mutations:
            if old in original:
                mutated = original.replace(old, new, 1)
                mutated_files.append(mutated)
            else:
                mutated_files.append(None)
        return mutated_files

    def test_mutation(self, source_path: str, mutated_code: str,
                      test_module_pattern: str, mutation_desc: str) -> MutationTestResult:
        """Test a single mutation and return result."""
        mutation_id = len(self.results)

        # Create temp file with mutated code
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py',
                                         dir=self.project_root, delete=False) as f:
            f.write(mutated_code)
            temp_path = f.name

        try:
            # Replace original with mutated temporarily
            backup_path = source_path + '.backup'
            shutil.copy(source_path, backup_path)
            shutil.copy(temp_path, source_path)

            try:
                # Run tests
                result = subprocess.run(
                    ["python", "-m", "pytest", test_module_pattern,
                     "-v", "--tb=short", "-x"],
                    capture_output=True,
                    text=True,
                    timeout=120,
                    cwd=str(self.project_root)
                )
                killed = result.returncode != 0
            finally:
                # Restore original
                shutil.move(backup_path, source_path)

            return MutationTestResult(
                module=source_path,
                mutation_id=mutation_id,
                description=mutation_desc,
                original_code=open(source_path).read(),
                mutated_code=mutated_code,
                killed=killed,
                test_output=result.stdout + result.stderr
            )
        finally:
            os.unlink(temp_path)


def get_models_mutations() -> list[tuple[str, str, str]]:
    """Generate mutations for models.py"""
    return [
        # Mutation 1: Change > to >= in TaskStatus comparison
        (
            'TaskStatus.ABANDONED.value,\n    TaskStatus.COMPLETED.value,',
            'TaskStatus.ABANDONED.value,\n    TaskStatus.COMPLETED',  # Missing .value
            "DISPATCH_INELIGIBLE_STATUSES - missing .value on COMPLETED"
        ),
        # Mutation 2: Change default risk_level
        (
            'risk_level: str = "low"',
            'risk_level: str = "medium"',
            "QualityContract default risk_level changed"
        ),
        # Mutation 3: Change cannot_self_accept default
        (
            'cannot_self_accept: bool = True',
            'cannot_self_accept: bool = False',
            "QualityContract cannot_self_accept default flipped"
        ),
        # Mutation 4: Change parent_final_gate default
        (
            'parent_final_gate: bool = True',
            'parent_final_gate: bool = False',
            "QualityContract parent_final_gate default flipped"
        ),
        # Mutation 5: Change max_depth default
        (
            'max_depth: int = 0',
            'max_depth: int = 1',
            "SubAgentCard max_depth default changed"
        ),
    ]


def get_failure_analyzer_mutations() -> list[tuple[str, str, str]]:
    """Generate mutations for failure_analyzer.py"""
    return [
        # Mutation 1: Change max_retry_attempts comparison
        (
            'if task.runner_attempts >= self.max_retry_attempts:',
            'if task.runner_attempts > self.max_retry_attempts:',
            "Timeout retry threshold changed from >= to >"
        ),
        # Mutation 2: Change timeout multiplier
        (
            'new_timeout = min(current_timeout * 1.5, self.max_timeout)',
            'new_timeout = min(current_timeout * 2.0, self.max_timeout)',
            "Timeout multiplier increased from 1.5 to 2.0"
        ),
        # Mutation 3: Change max_timeout comparison
        (
            'if current_timeout >= self.max_timeout:',
            'if current_timeout > self.max_timeout:',
            "Max timeout comparison changed from >= to >"
        ),
        # Mutation 4: Change parse error retry threshold
        (
            'if task.runner_attempts < self.max_retry_attempts:',
            'if task.runner_attempts <= self.max_retry_attempts:',
            "Parse error retry threshold changed"
        ),
        # Mutation 5: Change generic failure retry check
        (
            'if task.runner_attempts < self.max_retry_attempts:',
            'if task.runner_attempts <= self.max_retry_attempts:',
            "Generic failure retry threshold changed"
        ),
    ]


def get_memory_push_mutations() -> list[tuple[str, str, str]]:
    """Generate mutations for memory_push.py"""
    return [
        # Mutation 1: Change limit check
        (
            'if len(memories_text) >= limit:',
            'if len(memories_text) > limit:',
            "Memory limit check changed from >= to >"
        ),
        # Mutation 2: Change content length check
        (
            'if not record.content or len(record.content) < 10:',
            'if not record.content or len(record.content) < 5:',
            "Memory content min length reduced from 10 to 5"
        ),
        # Mutation 3: Change memory text truncation
        (
            'if len(mem) > 200:\n                mem = mem[:200] + "..."',
            'if len(mem) > 100:\n                mem = mem[:100] + "..."',
            "Memory text truncation changed from 200 to 100 chars"
        ),
        # Mutation 4: Change trigger type filter
        (
            'if entry.type in {MemoryType.LESSON_GENERAL, MemoryType.LESSON_TASK}:',
            'if entry.type in {MemoryType.LESSON_GENERAL}:',
            "Memory filter removed LESSON_TASK type"
        ),
        # Mutation 5: Change goal keyword extraction
        (
            'if len(goal) > 50:\n                query_parts.append(goal[:50])',
            'if len(goal) > 30:\n                query_parts.append(goal[:30])',
            "Goal keyword extraction length reduced from 50 to 30"
        ),
    ]


def get_correlation_mutations() -> list[tuple[str, str, str]]:
    """Generate mutations for correlation.py"""
    return [
        # Mutation 1: Change confidence sorting
        (
            'sorted(candidates, key=lambda item: float(item.get("confidence", 0.0)), reverse=True)',
            'sorted(candidates, key=lambda item: float(item.get("confidence", 0.0)))',
            "Entry candidates sorting reversed removed (no descending)"
        ),
        # Mutation 2: Change entity merge logic
        (
            'merged[str(key)].extend(str(value) for value in values)',
            'merged[str(key)].extend(str(value) for value in values[:1])',
            "Entity merge limited to first value only"
        ),
        # Mutation 3: Change gap checking
        (
            'if not entry_candidates:',
            'if len(entry_candidates) < 1:',
            "Entry candidates check changed"
        ),
        # Mutation 4: Change finding filter
        (
            'return [finding for finding in findings if finding.finding_id in refs]',
            'return list(findings)',
            "Finding filter bypassed - returns all findings"
        ),
        # Mutation 5: Change inference confidence
        (
            'finding.confidence if finding.confidence is not None else finding.risk_score',
            'finding.risk_score',
            "Inference confidence always uses risk_score"
        ),
    ]


def get_router_mutations() -> list[tuple[str, str, str]]:
    """Generate mutations for router.py"""
    return [
        # Mutation 1: Change score_card name weight
        (
            'token_score += 6.0',
            'token_score += 3.0',
            "Name match score weight reduced"
        ),
        # Mutation 2: Change score_card capabilities weight
        (
            'token_score += 5.0',
            'token_score += 2.0',
            "Capabilities match score weight reduced"
        ),
        # Mutation 3: Change score_card keyword weight
        (
            'token_score += 4.0',
            'token_score += 2.0',
            "Keyword match score weight reduced"
        ),
        # Mutation 4: Change capability_candidate_limit default
        (
            'capability_candidate_limit: int = 5',
            'capability_candidate_limit: int = 10',
            "Default candidate limit increased"
        ),
        # Mutation 5: Change tokenize to lowercase only
        (
            'lowered = text.lower()\n    tokens = re.findall(r"[a-z0-9_]+|[\\u4e00-\\u9fff]+", lowered)',
            'lowered = text.lower()\n    tokens = []',
            "Tokenize function broken - no tokens extracted"
        ),
    ]


def run_mutation_test_for_module(module_name: str, module_path: str,
                                  mutations: list[tuple[str, str, str]],
                                  test_pattern: str) -> list[MutationTestResult]:
    """Run mutation tests for a single module."""
    print(f"\n{'='*60}")
    print(f"Mutation testing: {module_name}")
    print(f"{'='*60}")

    project_root = Path(__file__).parent
    framework = MutationTestFramework(str(project_root))

    results = []
    for i, (old_code, new_code, description) in enumerate(mutations):
        print(f"\n[{i+1}/{len(mutations)}] {description}")

        # Read original file
        with open(module_path, 'r') as f:
            original = f.read()

        # Check if mutation applies
        if old_code not in original:
            print(f"  SKIP: Could not find target code in module")
            continue

        # Create mutated version
        mutated = original.replace(old_code, new_code, 1)

        # Test the mutation
        result = framework.test_mutation(
            module_path, mutated, test_pattern, description
        )
        result.module = module_name
        results.append(result)

        status = "KILLED" if result.killed else "SURVIVED"
        print(f"  Result: {status}")

    return results


def main():
    project_root = Path(__file__).parent

    # Define modules and their test patterns
    modules = [
        {
            "name": "models.py",
            "path": "agent_py_agent/agent/subagents/models.py",
            "test_pattern": "agent_py_agent/tests/test_subagent_models.py",
            "mutations_fn": get_models_mutations,
        },
        {
            "name": "failure_analyzer.py",
            "path": "agent_py_agent/agent/agent_core/failure_analyzer.py",
            "test_pattern": "agent_py_agent/tests/test_failure_analyzer.py",
            "mutations_fn": get_failure_analyzer_mutations,
        },
        {
            "name": "memory_push.py",
            "path": "agent_py_agent/agent/memory_push.py",
            "test_pattern": "agent_py_agent/tests/test_memory_push.py",
            "mutations_fn": get_memory_push_mutations,
        },
        {
            "name": "correlation.py",
            "path": "agent_py_agent/agent/log_analysis/security/correlation.py",
            "test_pattern": "agent_py_agent/tests/test_log_correlation.py",
            "mutations_fn": get_correlation_mutations,
        },
        {
            "name": "router.py",
            "path": "agent_py_agent/agent/capability/router.py",
            "test_pattern": "agent_py_agent/tests/test_capabilities.py",
            "mutations_fn": get_router_mutations,
        },
    ]

    all_results = []

    for mod in modules:
        mutations = mod["mutations_fn"]()
        results = run_mutation_test_for_module(
            mod["name"],
            str(project_root / mod["path"]),
            mutations,
            mod["test_pattern"]
        )
        all_results.extend(results)

    # Print summary
    print(f"\n{'='*60}")
    print("MUTATION TEST SUMMARY")
    print(f"{'='*60}")

    killed_count = sum(1 for r in all_results if r.killed)
    survived_count = sum(1 for r in all_results if not r.killed)
    total = len(all_results)
    score = (killed_count / total * 100) if total > 0 else 0

    print(f"Total mutations: {total}")
    print(f"Killed: {killed_count}")
    print(f"Survived: {survived_count}")
    print(f"Mutation Score: {score:.1f}%")

    if survived_count > 0:
        print(f"\n{'='*60}")
        print("SURVIVED MUTATIONS (need test coverage)")
        print(f"{'='*60}")
        for r in all_results:
            if not r.killed:
                print(f"\n[{r.module}] {r.description}")
                print(f"  Original: {r.original_code[:200]}...")
                print(f"  Mutated:  {r.mutated_code[:200]}...")

    # Save results
    report_path = project_root / "mutation_test_report.json"
    with open(report_path, 'w') as f:
        json.dump([{
            "module": r.module,
            "mutation_id": r.mutation_id,
            "description": r.description,
            "killed": r.killed,
            "test_output": r.test_output[:500]
        } for r in all_results], f, indent=2)
    print(f"\nReport saved to: {report_path}")

    return 0 if score >= 80 else 1


if __name__ == "__main__":
    sys.exit(main())