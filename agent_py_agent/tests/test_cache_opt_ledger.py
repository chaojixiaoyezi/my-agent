"""离线账本的计数、覆盖率和前提失败回归；使用stdlib unittest。"""
import json
import subprocess
import sys
import unittest
from pathlib import Path

from scripts.analyze_cache_ledger import analyze, parse_input


def row(i, prompt, cached, **extra):
    return {"call_id": f"call-{i}", "domain": "official-sol-responses", "segment": "cold-1",
            "purpose": "task", "source": "synthetic_fixture", "sequence": i,
            "prompt_tokens": prompt, "cached_tokens": cached, **extra}


def declaration(**flags):
    return {"domain": "official-sol-responses", "segment": "cold-1", "confirmations": {
        "append_only": True, "cold_start": True, "input_only_cache": True,
        "complete_sequence": True, **flags}}


class LedgerTest(unittest.TestCase):
    def test_cold_append_only_telescope(self):
        report = analyze([row(1, 15000, 0), row(2, 280000, 15000)], [declaration()])
        seg = report["segments"][0]
        self.assertEqual(seg["conditional_uncached_floor_tokens"], 280000)
        self.assertEqual(seg["observed_uncached_tokens"], 280000)
        self.assertEqual(seg["signed_residual_tokens"], 0)
        self.assertAlmostEqual(seg["conditional_ideal_hit_rate"], 15000 / 295000)

    def test_missing_cached_is_not_zero(self):
        report = analyze([row(1, 100, 0), row(2, 200, None)], [declaration()])
        self.assertEqual(report["coverage"]["counter_pair_request_coverage"], 0.5)
        self.assertEqual(report["coverage"]["paired_prompt_tokens"], 100)
        self.assertEqual(report["coverage"]["known_prompt_tokens_total"], 300)
        seg = report["segments"][0]
        self.assertEqual(seg["conditional_uncached_floor_tokens"], 200)
        self.assertIsNone(seg["signed_residual_tokens"])

    def test_all_unknown_and_true_zero_are_distinct(self):
        unknown = analyze([row(1, None, None)], [])
        zero = analyze([row(1, 0, 0)], [declaration()])
        self.assertEqual(unknown["coverage"]["complete_counter_pair_count"], 0)
        self.assertEqual(zero["coverage"]["complete_counter_pair_count"], 1)
        self.assertIsNone(zero["coverage"]["paired_hit_rate"])
        self.assertEqual(zero["segments"][0]["signed_residual_tokens"], 0)

    def test_invalid_counters_rejected(self):
        for prompt, cached in [(-1, 0), (1, -1), (True, 0), (1.0, 0), (1, False), (1, 2), ("1", 0)]:
            with self.subTest(prompt=prompt, cached=cached), self.assertRaises(ValueError):
                analyze([row(1, prompt, cached)], [])

    def test_duplicate_call_id_rejected_even_in_other_domain(self):
        with self.assertRaisesRegex(ValueError, "duplicate_call_id"):
            analyze([row(1, 1, 0), row(2, 2, 1, call_id="call-1", domain="other")], [])

    def test_duplicate_sequence_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate_segment_sequence"):
            analyze([row(1, 1, 0), row(2, 2, 1, sequence=1)], [])

    def test_signed_negative_residual_not_clamped(self):
        seg = analyze([row(1, 100, 0), row(2, 200, 150)], [declaration()])["segments"][0]
        self.assertEqual(seg["signed_residual_tokens"], -50)
        self.assertEqual(seg["status"], "confirmations_inconsistent")

    def test_unconfirmed_and_warm_segments_have_no_floor(self):
        for declarations in ([], [declaration(cold_start=False)], [declaration(append_only=None)]):
            with self.subTest(declarations=declarations):
                seg = analyze([row(1, 100, 0)], declarations)["segments"][0]
                self.assertIsNone(seg["conditional_uncached_floor_tokens"])

    def test_nonmonotonic_prompt_contradiction(self):
        seg = analyze([row(1, 200, 0), row(2, 100, 50)], [declaration()])["segments"][0]
        self.assertEqual(seg["status"], "confirmations_inconsistent")
        self.assertIsNone(seg["conditional_uncached_floor_tokens"])

    def test_warm_first_request_marks_confirmation_inconsistent(self):
        seg = analyze([row(1, 100, 20), row(2, 200, 100)], [declaration()])["segments"][0]
        self.assertEqual(seg["signed_residual_tokens"], -20)
        self.assertIn("first_request_cached_contradicts_cold_start", seg["reasons"])

    def test_missing_sequence_or_gap_disables_floor(self):
        for rows in ([row(1, 100, 0, sequence=None)], [row(2, 100, 0)]):
            with self.subTest(rows=rows):
                self.assertIsNone(analyze(rows, [declaration()])["segments"][0]["conditional_uncached_floor_tokens"])

    def test_source_and_purpose_not_silently_mixed(self):
        rows = [row(1, 100, 0), row(2, 200, 100, source="provider_usage", purpose="compact")]
        report = analyze(rows, [declaration()])
        self.assertEqual(len(report["groups"]), 2)
        self.assertIsNone(report["segments"][0]["conditional_uncached_floor_tokens"])

    def test_json_and_jsonl_agree_and_do_not_echo_content(self):
        rows = [row(1, 100, 0, content="PRIVATE-CONTENT-NOT-FOR-OUTPUT")]
        spec = declaration()
        doc = json.dumps({"requests": rows, "segments": [spec]})
        lines = json.dumps({"record_type": "segment", **spec}) + "\n" + json.dumps(rows[0])
        first = analyze(*parse_input(doc))
        self.assertEqual(first, analyze(*parse_input(lines)))
        self.assertNotIn("PRIVATE-CONTENT", json.dumps(first))

    def test_cli_stdout_only_for_valid_and_invalid_data(self):
        script = Path(__file__).resolve().parents[2] / "scripts" / "analyze_cache_ledger.py"
        for data, code in [(json.dumps([row(1, 100, None)]), 0), (json.dumps([row(1, -1, 0)]), 2)]:
            proc = subprocess.run([sys.executable, str(script), "-"], input=data, text=True, capture_output=True)
            self.assertEqual(proc.returncode, code)
            self.assertEqual(proc.stderr, "")
            self.assertEqual(json.loads(proc.stdout)["ok"], code == 0)


    def test_weighted_paired_ratio_and_group_coverage_are_not_mean_percentages(self):
        rows = [row(1, 100, 100), row(2, 900, 0, purpose="compact"), row(3, 500, None)]
        report = analyze(rows, [])
        self.assertEqual(report["coverage"]["paired_hit_rate"], 0.1)
        self.assertEqual(report["coverage"]["paired_uncached_tokens"], 900)
        self.assertEqual(report["coverage"]["paired_share_of_known_prompt_tokens"], 1000 / 1500)
        self.assertEqual(report["coverage"]["counter_pair_request_coverage"], 2 / 3)
        self.assertEqual({group["purpose"] for group in report["groups"]}, {"task", "compact"})

    def test_analysis_does_not_mutate_or_echo_extra_fields(self):
        rows = [{**row(1, 100, 0), "prompt": "PRIVATE-PROMPT", "credential": "PRIVATE-CREDENTIAL"}]
        specs = [declaration()]
        original = json.dumps([rows, specs], sort_keys=True)
        report = analyze(rows, specs)
        self.assertEqual(json.dumps([rows, specs], sort_keys=True), original)
        self.assertNotIn("PRIVATE-", json.dumps(report))
        self.assertEqual(report["basis"], "provided_counters_only_not_live_validation")

    def test_declared_empty_segments_and_empty_request_coverage(self):
        report = analyze([], [declaration()])
        self.assertEqual(report["coverage"]["request_count"], 0)
        self.assertIsNone(report["coverage"]["counter_pair_request_coverage"])
        self.assertFalse(report["coverage"]["all_requests_have_both_counters"])
        self.assertEqual(report["segments"], [])
        self.assertEqual(report["declared_segments_without_requests"], [
            {"domain": "official-sol-responses", "segment": "cold-1"}])

    def test_bad_shapes_duplicate_declarations_and_confirmation_values_rejected(self):
        for text in ("", "null", "[1]", '{"record_type":"unknown"}', '{"requests":{}}', "broken"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_input(text)
        for specs in ([declaration(), declaration()], [declaration(append_only=1)]):
            with self.subTest(specs=specs), self.assertRaises(ValueError):
                analyze([row(1, 1, 0)], specs)

    def test_cli_errors_use_stdout_json_and_help_keeps_argparse_contract(self):
        script = Path(__file__).resolve().parents[2] / "scripts" / "analyze_cache_ledger.py"
        for args in (["--unknown-option"], ["/nonexistent/cache-ledger.json"]):
            proc = subprocess.run([sys.executable, str(script), *args], text=True, capture_output=True)
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(proc.stderr, "")
            self.assertIs(json.loads(proc.stdout)["ok"], False)
        help_result = subprocess.run([sys.executable, str(script), "--help"], text=True, capture_output=True)
        self.assertEqual(help_result.returncode, 0)
        self.assertEqual(help_result.stderr, "")
        self.assertIn("usage:", help_result.stdout)


if __name__ == "__main__":
    unittest.main()
