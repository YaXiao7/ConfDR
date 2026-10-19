"""CLI entry point; load modules per command and keep generation separate from execution."""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import sys
from pathlib import Path

from confdr.shared.io import ROOT, read, settings


def _search_seconds(value):
    seconds = float(value)
    if not math.isfinite(seconds) or seconds < 0:
        raise argparse.ArgumentTypeError("Search timeout must be finite and nonnegative (0 = unlimited)")
    return seconds


def _solver_milliseconds(value):
    milliseconds = int(value)
    if milliseconds <= 0:
        raise argparse.ArgumentTypeError("Solver timeout must be positive")
    return milliseconds


def parser():
    root = argparse.ArgumentParser(
        prog="confdr",
        description="Taxonomy -> FOL generation -> ConfDR method -> Independent deployment evaluation",
    )
    root.add_argument("--config", type=Path, help="Configuration file (must precede the subcommand)")
    commands = root.add_subparsers(dest="command", required=True)
    gen = commands.add_parser("generate-fol", help="Generate a new rule catalog from your taxonomy")
    gen.add_argument("--taxonomy", type=Path, default=ROOT / "config/taxonomy.yaml")
    gen.add_argument("--output", type=Path, required=True)
    gen.add_argument(
        "--categories", nargs="+", help="Generate only selected category IDs for a focused check"
    )
    gen.add_argument(
        "--knowledge", type=Path, help="Optional YAML/JSON knowledge list with id/text/categories per entry"
    )
    merge = commands.add_parser("merge-rules", help="Merge category rules/evidence and validate offline")
    merge.add_argument("--base", type=Path, required=True, help="Base rules.yaml")
    merge.add_argument("--patch", type=Path, required=True, help="Replacement rules.yaml")
    merge.add_argument("--categories", nargs="+", required=True)
    merge.add_argument(
        "--allow-new-categories",
        action="store_true",
        help="Append categories absent from the base and merge compatible added vocabulary and evidence",
    )
    merge.add_argument("--output", type=Path, required=True, help="New complete bundle directory")
    val = commands.add_parser("validate-rules", help="Revalidate existing rules without rewriting rules.yaml")
    val.add_argument("--rules", type=Path, required=True)
    val.add_argument(
        "--refresh-reference",
        action="store_true",
        help="Rebuild independent references from the current rule file taxonomy after semantic changes",
    )
    val.add_argument(
        "--offline",
        action="store_true",
        help="Run machine checks only; fail if changes require model back-translation",
    )
    collect = commands.add_parser("collect", help="Collect one case or sequentially collect a dataset")
    inputs = collect.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--case", type=Path, help="Single case directory")
    inputs.add_argument("--dataset", type=Path, help="Recursively collect all cases in a directory")
    collect.add_argument("--include-fixed", action="store_true", help="Include historical *_fixed cases")
    collect.add_argument(
        "--output", type=Path, required=True, help="New output directory; do not overwrite the input"
    )
    origin = collect.add_mutually_exclusive_group(required=True)
    origin.add_argument("--kubeconfig", type=Path, help="Collect from an existing cluster in read-only mode")
    origin.add_argument(
        "--kwok", action="store_true", help="Deploy to a temporary KWOK cluster and collect observations"
    )
    collect.add_argument(
        "--no-logs", action="store_true", help="Skip container logs when collecting from a real cluster"
    )
    collect.add_argument(
        "--require-running", action="store_true",
        help="For KWOK healthy controls, wait for Running Pods and Bound PVCs through the full timeout",
    )
    for command, help_text in (
        ("run", "Run the ConfDR method without KWOK"),
        ("experiment", "Complete all method outputs before independent deployment evaluation"),
    ):
        experiment = commands.add_parser(command, help=help_text)
        experiment.add_argument("--dataset", type=Path, required=True)
        experiment.add_argument("--rules", type=Path, required=True)
        experiment.add_argument("--output", type=Path, required=True)
        experiment.add_argument(
            "--max-rounds",
            type=int,
            choices=range(1, 11),
            help="Maximum repair attempts per case (1–10; read from configuration by default, initially 5)",
        )
        experiment.add_argument(
            "--conflict-method",
            choices=("label_skipping", "mus"),
            help="Detection method for initial detection and repair rechecks (default from runtime config)",
        )
        experiment.add_argument(
            "--search-timeout-s",
            type=_search_seconds,
            metavar="SECONDS",
            help="Total Z3 search budget per detection; 0 = unlimited (default from runtime config)",
        )
        experiment.add_argument(
            "--solver-timeout-ms",
            type=_solver_milliseconds,
            metavar="MS",
            help="Time limit per Z3 check (default from runtime config)",
        )
        experiment.add_argument(
            "--no-lg",
            action="store_true",
            help=(
                "Ablation 'w/o LG': the repair model receives no formal rules, conflicting "
                "constraint instances, or configuration facts"
            ),
        )
        experiment.add_argument(
            "--include-fixed", action="store_true", help="Include historical *_fixed cases"
        )
        experiment.add_argument(
            "--label-mapping",
            type=Path,
            help="User-provided mapping from legacy labels to lists of current category IDs",
        )
    deployment = commands.add_parser(
        "evaluate-deployment", help="Evaluate completed method outputs in KWOK without rerunning repairs"
    )
    deployment.add_argument(
        "--run", type=Path, required=True, help="Completed run directory containing experiment.json"
    )
    deployment.add_argument(
        "--prefixes-only", action="store_true",
        help="Add 1/3/5-round KWOK evidence to an already evaluated run without redeploying its baseline and final output",
    )
    metrics = commands.add_parser(
        "evaluate-metrics", help="Summarize paper metrics offline without rerunning repairs"
    )
    metrics.add_argument("--run", type=Path, nargs="+", required=True, help="Completed run directories")
    metrics.add_argument(
        "--output", type=Path,
        help="Report directory for the combined report (required for multiple runs unless --per-run)",
    )
    metrics.add_argument(
        "--per-run", action="store_true",
        help="Write each run's own metrics into that run's directory instead of one combined report",
    )
    commands.add_parser(
        "doctor", help="Check local dependencies, taxonomy and API configuration without network requests"
    )
    return root


def doctor(cfg):
    import importlib.metadata

    dependencies = {}
    for name in ("PyYAML", "z3-solver", "jsonschema"):
        try:
            dependencies[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            dependencies[name] = "missing"
    llm = cfg.get("llm", {})
    report = {
        "python": sys.version.split()[0],
        "dependencies": dependencies,
        "commands": {
            key: shutil.which(str(cfg.get("kwok", {}).get(key, key))) for key in ("kwokctl", "kubectl")
        },
        "model_configured": bool(os.getenv("CONFDR_LLM_MODEL") or llm.get("model")),
        "base_url_configured": bool(os.getenv("CONFDR_LLM_BASE_URL") or llm.get("base_url")),
        "api_key_configured": bool(os.getenv("CONFDR_LLM_API_KEY")),
        "default_taxonomy": str(ROOT / "config/taxonomy.yaml"),
        "network_tested": False,
        "kwok_required_for_method": False,
    }
    categories = (read(ROOT / "config/taxonomy.yaml") or {}).get("categories", [])
    report["default_category_count"] = len(categories)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if "missing" not in dependencies.values() else 2


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        cfg = settings(args.config)
        if args.command == "evaluate-metrics":
            from confdr.runtime.paper_metrics import evaluate_metrics, format_report

            if args.per_run and args.output:
                raise ValueError("--per-run and --output are mutually exclusive")
            if args.per_run and len(args.run) == 1:
                raise ValueError("--per-run needs more than one run; a single run already writes its own report")
            report = evaluate_metrics(args.run, args.output, per_run=args.per_run)
            # Every run has its own file now, so echo one section per run rather than the
            # case-weighted mixture, which is neither a detection nor a repair metric.
            if args.per_run:
                report = {"datasets": report["datasets"], "overall": {"case_count": 0}}
            print(format_report(report), end="")
            return 0
        if args.command == "doctor":
            return doctor(cfg)
        if args.command == "collect":
            from confdr.collection.service import collect_case, collect_dataset

            if args.require_running and not args.kwok:
                raise ValueError("--require-running requires --kwok")
            options = {
                "kwok": args.kwok,
                "kubeconfig": args.kubeconfig,
                "logs": not args.no_logs,
                "require_running": args.require_running,
            }
            if args.dataset:
                report = collect_dataset(
                    args.dataset, args.output, cfg, include_fixed=args.include_fixed, **options
                )
                print(json.dumps(report["counts"], ensure_ascii=False))
                print(f"Batch report: {args.output / 'collection-batch.json'}")
                return 0 if report["counts"]["error"] == 0 and (
                    not args.require_running or report["counts"]["deployment_failed"] == 0
                ) else 2
            result = collect_case(args.case, args.output, cfg, **options)
            print(f"Collection complete: {args.output}; no new ground-truth labels included")
            return 0 if result.get("status") != "error" and (
                not args.require_running or result.get("status") == "passed"
            ) else 2
        from confdr.shared.llm import LLM
        from confdr.shared.verification import ACCEPTED

        if args.command == "generate-fol":
            from confdr.formalization.generation import validate_taxonomy
            from confdr.formalization.service import generate

            validate_taxonomy(read(args.taxonomy))
            if args.output.exists():
                raise ValueError("Rule output directory already exists; choose a new directory")
            llm, verifier = LLM(cfg.get("llm")), LLM(cfg.get("llm"), role="verifier")
            llm.preflight()
            verifier.preflight()
            print(
                "Starting rule generation and validation; model requests may take several minutes.",
                flush=True,
            )
            report = generate(
                args.taxonomy,
                args.output,
                llm,
                verifier,
                knowledge_path=args.knowledge,
                category_ids=args.categories,
                **cfg.get("formalization", {}),
            )
        elif args.command == "merge-rules":
            from confdr.formalization.merge import merge_bundles

            report = merge_bundles(
                args.base,
                args.patch,
                args.output,
                category_ids=args.categories,
                allow_new_categories=args.allow_new_categories,
                timeout_ms=cfg.get("formalization", {}).get("timeout_ms", 3000),
            )
            if report["status"] == "unverified":
                print(
                    "Merged bundle saved. Some review contexts changed; run validate-rules "
                    f"--rules {args.output / 'rules.yaml'} to refresh stale reviews.",
                    file=sys.stderr,
                )
        elif args.command == "validate-rules":
            from confdr.formalization.service import revalidate

            if args.offline and args.refresh_reference:
                raise ValueError("--offline and --refresh-reference cannot be used together")
            verifier = None if args.offline else LLM(cfg.get("llm"), role="verifier")
            if verifier:
                verifier.preflight()
            report = revalidate(
                args.rules,
                verifier=verifier,
                refresh_reference=args.refresh_reference,
                max_rounds=cfg.get("formalization", {}).get("max_rounds", 3),
                timeout_ms=cfg.get("formalization", {}).get("timeout_ms", 3000),
            )
        elif args.command == "evaluate-deployment":
            from confdr.validation.deployment import evaluate_deployment
            from confdr.validation.kwok import KwokValidator

            advisor = None
            if not args.prefixes_only:
                advisor = LLM(cfg.get("llm"))
                try:
                    advisor.preflight()
                except ValueError:
                    advisor = None
            summary = evaluate_deployment(
                args.run,
                KwokValidator(cfg.get("kwok")),
                llm=advisor,
                progress=lambda text: print(text, flush=True),
                prefixes_only=args.prefixes_only,
            )
            print(
                f"Independent deployment evaluation complete: {args.run.resolve()}; {summary['statuses']}; method results unchanged"
            )
            return 0
        elif args.command in {"run", "experiment"}:
            from confdr.runtime.experiment import run_experiment, run_method
            from confdr.shared.catalog import read_bundle

            catalog = read_bundle(args.rules, timeout_ms=cfg.get("formalization", {}).get("timeout_ms", 3000))
            llm = LLM(cfg.get("llm"))
            llm.preflight()
            print(
                f"Effective main model: {getattr(llm, 'model', 'unknown')} "
                f"({getattr(llm, 'model_source', 'unknown source')})",
                flush=True,
            )
            options = dict(cfg.get("runtime", {}))
            if args.max_rounds is not None:
                options["max_rounds"] = args.max_rounds
            for argument, key in (
                (args.conflict_method, "conflict_method"),
                (args.search_timeout_s, "search_timeout_s"),
                (args.solver_timeout_ms, "timeout_ms"),
            ):
                if argument is not None:
                    options[key] = argument
            if getattr(args, "no_lg", False):
                options["no_lg"] = True
            print(f"Detection method: {options.get('conflict_method', 'label_skipping')}", flush=True)
            if options.get("no_lg"):
                print("Ablation: w/o LG (no formal rules, conflicting constraint instances, or configuration facts)", flush=True)
            kwargs = {
                "options": options,
                "include_fixed": args.include_fixed,
                "label_mapping": read(args.label_mapping) if args.label_mapping else None,
                "progress": lambda text: print(text, flush=True),
            }
            if args.command == "run":
                summary = run_method(args.dataset, args.output, catalog, llm, **kwargs)
            else:
                from confdr.validation.kwok import KwokValidator

                summary = run_experiment(
                    args.dataset, args.output, catalog, llm, KwokValidator(cfg.get("kwok")), **kwargs
                )
            print(f"Method complete: {args.output.resolve()}; method statuses: {summary['statuses']}")
            if "deployment_statuses" in summary:
                print(f"Independent deployment evaluation: {summary['deployment_statuses']}")
            return 0
        else:
            raise ValueError("Unknown command")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["status"] in ACCEPTED else 2
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
