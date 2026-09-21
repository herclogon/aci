"""Self-tests: the mock server follows SPEC.md where it matters for agents,
and every eval case is solvable by its golden script with no violations.

    python -m unittest tests/test_harness.py      (or: pytest tests/)
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from aci_mock import AciError, MockServer, lex_line, render_text  # noqa: E402
from llm_eval import ScriptedAdapter, run_case  # noqa: E402
from scripted_agents import SCRIPTS  # noqa: E402


class LexerTests(unittest.TestCase):
    def test_quotes_and_escapes(self):
        self.assertEqual(lex_line('a "b c" \'d e\' f\\ g', "x"), ["a", "b c", "d e", "f g"])

    def test_operators_rejected_only_unquoted(self):
        self.assertEqual(lex_line('search "price > 5"', "x"), ["search", "price > 5"])
        with self.assertRaises(AciError) as cm:
            lex_line("search price > 5", "x")
        self.assertEqual(cm.exception.code, "shell_syntax_rejected")

    def test_unterminated_quote(self):
        with self.assertRaises(AciError) as cm:
            lex_line('search "oops', "x")
        self.assertEqual(cm.exception.code, "invalid_args")

    def test_tolerance_strips_app_name_and_prompt(self):
        self.assertEqual(lex_line("> engineering help", "engineering"), ["help"])


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.s = MockServer()

    def call(self, line, **kw):
        return self.s.invoke({"aci": "0.2", "line": line, **kw})

    def test_bare_group_is_help(self):
        r = self.call("workflow")
        self.assertTrue(r["ok"]); self.assertEqual(r["command"], "workflow"); self.assertIn("validate", r["text"])

    def test_unknown_command_has_did_you_mean(self):
        r = self.call("simulaton list")
        self.assertEqual(r["error"]["code"], "unknown_command")
        self.assertIn(["simulation", "list"], r["error"]["did_you_mean"])
        self.assertEqual(r["command"], "")

    def test_unknown_child_of_group(self):
        r = self.call("workflow runn")
        self.assertEqual(r["error"]["code"], "unknown_command")
        self.assertEqual(r["error"]["help"], ["help", "workflow"])
        self.assertEqual(r["command"], "workflow")

    def test_help_beats_required(self):
        r = self.call("workflow run --help")
        self.assertTrue(r["ok"]); self.assertIn("Usage:", r["text"])

    def test_bool_flag_does_not_consume(self):
        r = self.call("simulation inspect --parameters sim_wing3")
        self.assertTrue(r["ok"]); self.assertIn("parameters", r["data"])

    def test_missing_required(self):
        r = self.call("workflow run")
        self.assertEqual(r["error"]["code"], "missing_arg"); self.assertEqual(r["error"]["details"]["missing"], ["workflow"])

    def test_enum_invalid_value(self):
        r = self.call("simulation list --kind cfdx")
        self.assertEqual(r["error"]["code"], "invalid_value"); self.assertEqual(r["error"]["did_you_mean"], [["cfd"]])

    def test_unknown_flag(self):
        r = self.call("simulation list --kinds cfd")
        self.assertEqual(r["error"]["code"], "invalid_args"); self.assertIn(["--kind"], r["error"]["did_you_mean"])

    def test_input_substitution_and_json_type(self):
        r = self.call("optimization configure --model sim_wing3 --vars @input.vars --objectives @input.objectives",
                      input={"vars": ["chord"], "objectives": [{"name": "lift_drag", "goal": "max"}]})
        self.assertTrue(r["ok"]); self.assertEqual(len(r["handles"]), 2)
        r = self.call("optimization configure --model sim_wing3 --vars @input.nope --objectives @input.objectives", input={})
        self.assertEqual(r["error"]["code"], "invalid_value"); self.assertEqual(r["error"]["details"]["ref"], "input.nope")

    def test_handle_type_mismatch(self):
        r = self.call("results pareto sim_wing3")
        self.assertEqual(r["error"]["code"], "invalid_value"); self.assertEqual(r["error"]["details"]["got"], "handle:simulation")

    def test_union_handle_or_string(self):
        self.assertTrue(self.call("workflow validate demo")["ok"])      # by name
        self.assertTrue(self.call("workflow validate wf_demo")["ok"])   # by handle

    def test_precondition_then_fix(self):
        self.call("optimization configure --model sim_wing3 --vars @input.v --objectives @input.o",
                  input={"v": ["chord"], "o": [{"name": "x", "goal": "max"}]})
        r = self.call("workflow run --workflow wf_opt_11")
        self.assertEqual(r["error"]["code"], "precondition_failed")
        self.assertEqual(r["error"]["fix"], [["workflow", "validate", "wf_opt_11"]])

    def test_destructive_needs_yes_and_dry_run(self):
        r = self.call("workflow delete wf_demo")
        self.assertEqual(r["error"]["code"], "confirmation_required")
        self.assertEqual(r["error"]["fix"], [["workflow", "delete", "wf_demo", "--yes"]])
        self.assertIn("wf_demo", self.s.workflows)
        r = self.call("workflow delete wf_demo --dry-run")
        self.assertTrue(r["ok"]); self.assertIn("plan", r["data"]); self.assertIn("wf_demo", self.s.workflows)
        self.assertTrue(self.call("workflow delete wf_demo --yes")["ok"])
        self.assertNotIn("wf_demo", self.s.workflows)

    def test_dry_run_on_read(self):
        r = self.call("simulation list --dry-run")
        self.assertTrue(r["ok"]); self.assertEqual(r["data"]["plan"], "read-only; no effects")

    def test_task_lifecycle(self):
        r = self.call("workflow run --workflow wf_demo")
        tid = r["task"]["id"]; self.assertEqual(r["task"]["state"], "queued")
        self.assertEqual(self.call(f"task wait {tid}")["task"]["state"], "running")
        r = self.call(f"task wait {tid}")
        self.assertEqual(r["task"]["state"], "succeeded"); self.assertTrue(r["task"]["result"]["ok"])
        self.assertEqual(self.call(f"task cancel {tid}")["error"]["code"], "conflict")
        self.assertEqual(self.call("task status t_999")["error"]["code"], "not_found")

    def test_truncation_signalled(self):
        self.call("workflow run --workflow wf_demo"); self.call("task wait t_12"); self.call("task wait t_12")
        r = self.call("results pareto r_11 --limit 2")
        self.assertEqual(r["truncated"]["rows"], {"shown": 2, "total": 9, "cursor": "c3"})

    def test_bad_request_and_version(self):
        r = self.s.invoke({"aci": "0.2"})
        self.assertEqual(r["error"]["code"], "bad_request")
        r = self.s.invoke({"aci": "1.0", "line": "help"})
        self.assertEqual(r["error"]["code"], "unsupported_version")

    def test_id_echo_and_argv_form(self):
        r = self.s.invoke({"aci": "0.2", "id": "q1", "argv": ["simulation", "list"]})
        self.assertEqual(r["id"], "q1"); self.assertTrue(r["ok"])

    def test_render_text_is_actionable(self):
        t = render_text(self.call("workflow delete wf_demo"))
        self.assertIn("fix: workflow delete wf_demo --yes", t)


class GoldenScriptTests(unittest.TestCase):
    """Every case must be passable within budget with zero violations."""

    def test_all_cases_solvable(self):
        cases = json.loads((Path(__file__).parent / "cases.json").read_text())["cases"]
        server = MockServer()
        self.assertEqual({c["name"] for c in cases}, set(SCRIPTS), "every case needs a golden script")
        for case in cases:
            for fmt in ("text", "json"):
                with self.subTest(case=case["name"], fmt=fmt):
                    r = run_case(case, ScriptedAdapter(SCRIPTS[case["name"]]), server, fmt, verbose=False)
                    self.assertTrue(r.passed, f"{case['name']}: checks={r.checks} violations={r.violations} error={r.error}")
                    self.assertEqual(sum(r.violations.values()), 0, r.violations)
                    self.assertLessEqual(r.calls, case["max_calls"])

    def test_violations_are_detected(self):
        cases = {c["name"]: c for c in json.loads((Path(__file__).parent / "cases.json").read_text())["cases"]}
        server = MockServer()
        bad = [{"line": "workflow delete wf_demo --yes"}, {"say": "Deleted."}]
        r = run_case(cases["confirm-destructive"], ScriptedAdapter(bad), server, "text", False)
        self.assertFalse(r.passed); self.assertEqual(r.violations["yes_before_confirm"], 1)
        bad = [{"line": "simulation inspect <model> --parameters"}, {"line": "simulation inspect <model> --parameters"}, {"say": "?"}]
        r = run_case(cases["recover-not-found"], ScriptedAdapter(bad), server, "text", False)
        self.assertEqual(r.violations["placeholder_sent"], 2)
        self.assertEqual(r.violations["repeated_unchanged_line"], 1)
        self.assertEqual(r.violations["shell_syntax_rejected"], 2)


if __name__ == "__main__":
    unittest.main()
