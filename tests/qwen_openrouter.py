"""Run the AAI evaluation across a descending set of Qwen model sizes.

The script checks OpenRouter's live catalog before starting. Models that are no
longer offered are reported as unavailable instead of being silently replaced.
Each available model writes its normal report to tests/results/.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path


CATALOG_URL = "https://openrouter.ai/api/v1/models"
DEFAULT_MODELS = [
    "qwen/qwen3-32b",
    "qwen/qwen3.8-27b",
    "qwen/qwen3-14b",
    "qwen/qwen3.5-9b",
    "qwen/qwen3-8b",
    "qwen/qwen-2.5-7b-instruct",
    "qwen/qwen3-4b",
]


def available_models() -> set[str]:
    with urllib.request.urlopen(CATALOG_URL, timeout=30) as response:
        return {model["id"] for model in json.load(response)["data"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", action="append", dest="models",
                        help="Model slug to test; repeat to override the default size sweep")
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--case", action="append", help="Run only these case names")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    if not os.environ.get("OPENROUTER_API_KEY"):
        parser.error("OPENROUTER_API_KEY is required")

    requested = args.models or DEFAULT_MODELS
    try:
        available = available_models()
    except Exception as exc:
        parser.error(f"could not read the OpenRouter model catalog: {exc}")

    runnable = [model for model in requested if model in available]
    unavailable = [model for model in requested if model not in available]
    for model in unavailable:
        print(f"SKIP unavailable: {model}")
    if not runnable:
        print("No requested models are currently available.", file=sys.stderr)
        return 2

    runner = Path(__file__).with_name("llm_eval.py")
    failures: list[str] = []
    for model in runnable:
        print(f"\n=== {model} ===", flush=True)
        command = [sys.executable, str(runner), "--provider", "openrouter", "--model", model,
                   "--format", args.format, "--repeat", str(args.repeat)]
        for case in args.case or []:
            command.extend(["--case", case])
        if args.verbose:
            command.append("--verbose")
        if subprocess.run(command, check=False).returncode:
            failures.append(model)

    print(f"\nQwen sweep: {len(runnable) - len(failures)}/{len(runnable)} models passed; "
          f"{len(unavailable)} unavailable.")
    if failures:
        print("Failed: " + ", ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
