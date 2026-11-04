"""Run the triage evaluation and write evals/results/latest.json and latest.md.

    python evals/run_eval.py                       # Claude, every ticket, needs ANTHROPIC_API_KEY
    python evals/run_eval.py --limit 10            # first ten tickets only
    python evals/run_eval.py --model claude-haiku-5-5
    python evals/run_eval.py --offline             # keyword fallback only, no API key, no network

The exit code is 0 when the release gate passes (P1 recall of 100%), 1 when it
fails and 2 when the run could not start. ``--no-gate`` always exits 0 after a run.
"""

import argparse
import hashlib
import json
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # Run as a script, so make the repository importable.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evals import harness, report, scoring  # noqa: E402
from triageops import triage  # noqa: E402
from triageops.config import settings  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = harness.EVALS_DIR / "results"


def prompt_digest() -> str:
    """Changes whenever the system prompt or the tool schema changes, so runs can be compared."""
    material = triage.SYSTEM_PROMPT + json.dumps(triage.RECORD_TRIAGE_TOOL, sort_keys=True)
    return hashlib.sha256(material.encode()).hexdigest()[:16]


def _display_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return path.name


def build_report(
    results: list[dict[str, Any]],
    *,
    mode: str,
    model: str,
    dataset_path: Path,
    dataset_size: int,
    concurrency: int,
    limit: int | None,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    scored = scoring.score(results, model=None if mode == "rules-fallback" else model)
    return {
        "schema_version": scoring.SCHEMA_VERSION,
        "run": {
            "generated_at": (generated_at or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "mode": mode,
            "model": triage.FALLBACK_MODEL if mode == "rules-fallback" else model,
            "concurrency": concurrency,
            "limit": limit,
            "prompt_sha256": prompt_digest(),
            "commit": os.environ.get("GITHUB_SHA") or None,
            "dataset": {
                "path": _display_path(dataset_path),
                "tickets": dataset_size,
                "sha256": harness.dataset_digest(dataset_path),
            },
        },
        **scored,
        "results": results,
    }


def write_outputs(data: dict[str, Any], out_dir: Path, name: str) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path, md_path = out_dir / f"{name}.json", out_dir / f"{name}.md"
    json_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    md_path.write_text(report.render(data), encoding="utf-8")
    return json_path, md_path


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Score the TriageOps triage pipeline against the labelled dataset.")
    parser.add_argument("--limit", type=int, default=None, help="only run the first N tickets")
    parser.add_argument("--model", default=None, help=f"triage model, default {settings.triage_model}")
    parser.add_argument("--concurrency", type=int, default=harness.DEFAULT_CONCURRENCY, help="parallel tickets, default 4")
    parser.add_argument("--dataset", type=Path, default=harness.DATASET_PATH, help="JSONL dataset path")
    parser.add_argument("--out-dir", type=Path, default=RESULTS_DIR, help="where to write the results")
    parser.add_argument("--name", default="latest", help="file name for the results, default latest")
    parser.add_argument("--offline", action="store_true", help="skip Claude and score the keyword fallback")
    parser.add_argument("--no-gate", action="store_true", help="exit 0 even when P1 recall misses the target")
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")
    if args.concurrency < 1:
        parser.error("--concurrency must be at least 1")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    model = args.model or settings.triage_model
    mode = "rules-fallback" if args.offline else "claude"
    if mode == "claude" and not settings.anthropic_api_key:
        print("ANTHROPIC_API_KEY is not set. Set it, or pass --offline to score the keyword fallback.", file=sys.stderr)
        return 2

    try:
        cases = harness.load_dataset(args.dataset)
    except (OSError, ValueError) as exc:
        print(f"Could not load the dataset: {exc}", file=sys.stderr)
        return 2
    selected = cases[: args.limit] if args.limit else cases

    def progress(result: dict[str, Any]) -> None:
        wrong = scoring.mismatches(result)
        status = "ok" if not wrong else "wrong on " + ", ".join(wrong)
        print(f"{result['id']}  {status}", file=sys.stderr)

    logger = logging.getLogger("triageops")
    saved_key, saved_level = settings.anthropic_api_key, logger.level
    if args.offline:
        settings.anthropic_api_key = ""  # Forces the keyword fallback for every ticket.
        logger.setLevel(logging.ERROR)  # Each fallback would otherwise log a warning.
    try:
        results = harness.run_cases(selected, model=model, concurrency=args.concurrency, progress=progress)
    finally:
        settings.anthropic_api_key = saved_key
        logger.setLevel(saved_level)

    data = build_report(
        results,
        mode=mode,
        model=model,
        dataset_path=args.dataset,
        dataset_size=len(cases),
        concurrency=args.concurrency,
        limit=args.limit,
    )
    json_path, md_path = write_outputs(data, args.out_dir, args.name)

    gate, metrics = data["gate"], data["metrics"]
    print(f"Wrote {_display_path(json_path)} and {_display_path(md_path)}")
    print(
        f"P1 recall {report.pct(gate['value'])}, priority {report.pct(metrics['priority']['accuracy'])}, "
        f"category {report.pct(metrics['category']['accuracy'])}, team {report.pct(metrics['team']['accuracy'])}, "
        f"injection {report.pct(metrics['injection']['detection_rate'])} "
        f"with {metrics['injection']['false_positives']} false positives"
    )
    if mode == "claude" and metrics["fallbacks"]["count"]:
        print(
            f"Warning: {metrics['fallbacks']['count']} tickets fell back to keyword rules: "
            + ", ".join(metrics["fallbacks"]["reasons"]),
            file=sys.stderr,
        )
    print(f"Release gate {'passed' if gate['passed'] else 'FAILED'}")
    return 0 if gate["passed"] or args.no_gate else 1


if __name__ == "__main__":
    sys.exit(main())
