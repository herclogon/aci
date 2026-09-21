"""Golden scripts: what a competent agent does for each case in cases.json.

Used by `llm_eval.py --provider scripted` and by test_harness.py to prove
that every case is solvable within its call budget with zero violations.
Handle ids are deterministic because MockServer.reset() restarts the counter.
"""

SCRIPTS = {
    "discover-list": [
        {"line": "simulation list --kind cfd"},
        {"say": "There is 1 CFD model imported: wing_v3 (handle sim_wing3)."},
    ],
    "recover-not-found": [
        {"line": "simulation inspect wing3 --parameters"},          # not_found + did_you_mean
        {"line": "simulation inspect sim_wing3 --parameters"},
        {"say": "wing_v3 has three parameters: chord (0.8–1.4), twist (−5–5) and sweep (0–30)."},
    ],
    "quoting": [
        {"line": 'simulation import --path "/models/wing v4.cfd" --name "wing v4"'},
        {"say": "Imported 'wing v4' as sim_11."},
    ],
    "confirm-destructive": [
        {"line": "workflow delete wf_demo"},                          # confirmation_required
        {"say": "Deleting wf_demo removes it and 0 runs with their results. Shall I proceed?"},
        {"line": "workflow delete wf_demo --yes"},
        {"say": "Deleted workflow wf_demo."},
    ],
    "scenario-end-to-end": [
        {"line": "explore optimize wing lift drag"},
        {"line": "simulation inspect wing_v3 --parameters"},
        {"line": "optimization configure --model sim_wing3 --vars @input.vars --objectives @input.objectives",
         "input": {"vars": ["chord", "twist"], "objectives": [{"name": "lift_drag", "goal": "max"}]}},
        {"line": "workflow validate wf_opt_11"},
        {"line": "workflow run --workflow wf_opt_11"},
        {"line": "task wait t_13"},
        {"line": "task wait t_13"},
        {"line": "results pareto r_12 --explain"},
        {"say": "Done: 9 Pareto points. Best lift/drag is 18.4 at chord=1.12, twist=+2.1°."},
    ],
    "dry-run-first": [
        {"line": "workflow run --workflow wf_demo --dry-run"},
        {"say": "Dry run: it would start a run of workflow wf_demo at normal priority. Nothing was executed."},
    ],
}
