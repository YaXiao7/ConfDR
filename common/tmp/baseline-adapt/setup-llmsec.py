"""Stage a runnable, patched copy of the LLMSecConfig baseline for the NEW dataset.

Generalizes tmp/setup-llmsec-smoke.py: same four patches, but it stages the whole
D1-26-10 repair cohort instead of three hand-picked cases, and it keeps the
inference caps at the baseline's published values by default (the smoke run
pinned them to 1x2 purely to bound its own cost).

Patches, all on the COPY under tmp/baseline-adapt/llmsec/ (baseline tree untouched):

  1. scripts/ -- MISSING from the shipped baseline, yet runner.py:38-40 imports
     `constraint_fix_pipeline.scripts.*`, so it cannot be imported as shipped.
     Restored from repair/scripts/ (kubectl data plumbing, no prompts, no LLM).
  2. kwok create cluster -- docker image flags -> the local binary runtime, same
     binaries ConfDR2.0 uses. No image pull, no Linux paths.
  3. api_key / api_base defaults blanked. They ship non-empty, which makes the
     runner's `if not api_key: api_key = env[...]` fallback dead code and silently
     pins the run to a stale key. Blanking is what lets the relay credentials in.
  4. Namespace synthesis before apply. The shipped harness applies the candidate
     YAML to a bare cluster. That is fine for the original affinity_cases corpus,
     whose pods carry no `namespace:` at all, but D1-26-10 is scraped from real
     GitHub issues and its pods sit in real namespaces (`awx`, ...). With no
     Namespace object present `kubectl apply` fails with `namespaces "awx" not
     found`, and runner.py:196 cannot tell that apart from a YAML syntax error --
     so the case burns every syntax retry and is scored as a failure for an
     environment reason, not a repair reason. ConfDR2.0 solves the same problem by
     synthesizing the environment documents it needs (validation/kwok.py:71
     _namespaces + :103 default ServiceAccount) before applying; this mirrors it,
     so both sides are verified against the same cluster contents.
  5. `_run_cmd` decodes its output as UTF-8 with errors="replace". On this
     Chinese-locale Windows kubectl prints GBK, which raises UnicodeDecodeError
     inside subprocess's reader thread; the caller sees a *successful* command
     reported as failed, which is another route into a false syntax retry.
  6. inference caps, set from the CLI (default: leave the baseline's own 5 x 10).

Staging: dataset-repair/<case>/ (built by adapt_dataset.py, then given res.txt by
run-legacy-detect.py) is copied to llmsec/data/<group>/<case>/ where group is GH
or SO -- iter_group_case_dirs (utils/io.py:134) reads exactly two levels, so the
group level is structurally required, and using the source family keeps the two
halves of the dataset separable in the outputs.

  apps/, envs/    the case's manifests
  res.txt         the UNSAT gate (runner.py:423-429); absent = skipped, no repair
  case.json       solver cluster state; solve_time.json  solver timing
  truth.json      carried along for the evaluator, never read by the runner
                  (utils/io.py:18 only globs *.y*ml)

Usage:
  python tmp/baseline-adapt/setup-llmsec.py [--rounds 5] [--syntax 10]
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
from hostenv import SLDSTST, kwok_create_args  # noqa: E402

BASE = SLDSTST
SRC_PKG = BASE / "baseline-LLMSecConfig" / "constraint_fix_pipeline"
SCRIPTS_FROM = BASE / "repair" / "scripts"

SMOKE = HERE / "llmsec"
PKG = SMOKE / "constraint_fix_pipeline"
DATA = SMOKE / "data"
REPAIR = HERE / "dataset-repair"

OLD_KWOK = (
    '"(kwokctl delete cluster || true) && kwokctl create cluster '
    "--kube-apiserver-image=registry.aliyuncs.com/google_containers/kube-apiserver:v1.29.6 "
    "--kube-controller-manager-image=registry.aliyuncs.com/google_containers/kube-controller-manager:v1.29.6 "
    "--kube-scheduler-image=registry.aliyuncs.com/google_containers/kube-scheduler:v1.29.6 "
    "--etcd-image=registry.aliyuncs.com/google_containers/etcd:3.5.11-0 "
    '--kwok-controller-binary=/usr/bin/kwok"'
)
# Built from this host's CONFDR_KWOK_BIN at run time, so the same script works on the
# Windows development machine and on a Linux server with a different install prefix.
NEW_KWOK = kwok_create_args()

# --- patch 4: synthesize the namespaces the candidate manifests imply ---------
# Mirrors ConfDR2.0's validation/kwok.py:71 _namespaces + :103 default
# ServiceAccount so both sides apply the same environment documents.
ENV_HELPER = '''    def _ensure_namespaces(self, fixed_yaml_path: Path) -> bool:
        """Create the namespaces the candidate manifests reference but do not declare.

        The shipped harness applies the candidate YAML to a freshly created cluster
        as-is. That holds for the original affinity_cases corpus, whose pods carry no
        `namespace:` at all, but D1-26-10 is scraped from real GitHub issues and its
        pods sit in real namespaces (`awx`, ...). With no Namespace object present
        `kubectl apply` fails with `namespaces "awx" not found`, and the caller cannot
        tell that apart from a YAML syntax error -- so the case would burn every
        syntax retry and be scored as a failure for an environment reason, not a
        repair reason.

        ConfDR2.0 gives its own candidate the same environment, synthesizing the
        Namespace objects and default ServiceAccount it needs before it applies
        anything (validation/kwok.py:71 _namespaces, :103). This mirrors that, so
        both sides are verified against the same cluster contents.

        The environment is created as a separate, idempotent pre-step and the
        candidate file is then applied unchanged, so what gets dry-run and applied is
        still exactly the model's own YAML.
        """
        try:
            documents = [d for d in yaml.safe_load_all(
                fixed_yaml_path.read_text(encoding="utf-8")) if isinstance(d, dict)]
        except Exception:
            return True  # not an environment problem; let the apply surface the error

        cluster_scoped = {"Namespace", "Node", "PersistentVolume", "StorageClass", "CSINode"}
        required = {
            (doc.get("metadata") or {}).get("namespace") or "default"
            for doc in documents
            if doc.get("kind") not in cluster_scoped
        }
        declared = {
            (doc.get("metadata") or {}).get("name")
            for doc in documents
            if doc.get("kind") == "Namespace"
        }
        missing = sorted(required - declared
                         - {"default", "kube-system", "kube-public", "kube-node-lease"})
        for name in missing:
            print(f"[INFO] Synthesizing namespace {name!r} for the apply")
            if not self._run_cmd(
                f"kubectl create namespace {name} --dry-run=client -o yaml | kubectl apply -f -",
                f"Create Namespace {name}",
            ):
                return False
            # Pod admission needs the namespace's default ServiceAccount. The
            # controller creates it asynchronously, so create it explicitly to close
            # the race. `create || get` tolerates AlreadyExists -- the controller
            # winning that race -- while still failing if the SA is truly absent.
            # No shell redirection: shell=True runs cmd.exe here, where `> /dev/null`
            # redirects to a literal path that does not exist.
            self._run_cmd(
                f"kubectl create serviceaccount default -n {name} || "
                f"kubectl get serviceaccount default -n {name}",
                f"Create default ServiceAccount in {name}",
            )
        return True

'''

ENV_CALL = """        # 2. Apply YAML
        # Give the candidate the namespaces it references but does not declare, the
        # way ConfDR2.0's validator does (validation/kwok.py:71). Without this the
        # apply fails with `namespaces "x" not found` and is misread as a syntax
        # error, burning every syntax retry for an environment reason.
        if not self._ensure_namespaces(fixed_yaml_path):
            return False
"""

# --- patch 7: error evidence from ConfDR's input (the baseline detector is removed) ---
# The baseline no longer runs its own Z3 detector.  Every case enters repair, and the
# error context the LLM sees -- and the RAG code lookup -- is derived from ConfDR's
# frozen input instead: the FailedScheduling reason KWOK observed at collection time
# (collection.json), mapped onto the baseline's own S/R/N/V code vocabulary.
EVIDENCE_HELPERS = '''# --- error evidence from ConfDR's input (the baseline detector is removed) ---
_REASON_TO_CODES = [
    (re.compile(r"didn't match pod affinity rules", re.I), "S6"),
    (re.compile(r"didn't match pod anti-affinity rules", re.I), "S7"),
    (re.compile(r"satisfy existing pods anti-affinity rules", re.I), "S7"),
    (re.compile(r"didn't match Pod's node affinity/selector", re.I), "S5"),
    (re.compile(r"node\\(s\\) had untolerated taint", re.I), "S4"),
    (re.compile(r"Insufficient cpu", re.I), "R1"),
    (re.compile(r"Insufficient memory", re.I), "R2"),
    (re.compile(r"Insufficient nvidia\\.com/gpu", re.I), "R3"),
    (re.compile(r"didn't have free ports", re.I), "N1"),
    (re.compile(r"unbound immediate PersistentVolumeClaims", re.I), "V1"),
    (re.compile(r"persistentvolumeclaim .* not found", re.I), "V1"),
]

def codes_from_evidence_text(text: str) -> List[str]:
    codes: Set[str] = set()
    for rx, code in _REASON_TO_CODES:
        if rx.search(text or ""):
            codes.add(code)
    return sorted(codes)

def evidence_from_case(case_dir) -> Tuple[str, List[str]]:
    """(evidence_text, codes) from ConfDR's input (collection.json), not a detector."""
    text = ""
    cj = Path(case_dir) / "collection.json"
    if cj.is_file():
        try:
            d = json.loads(cj.read_text(encoding="utf-8", errors="ignore"))
            text = str(d.get("reason") or "")
        except Exception:
            text = ""
    if not text:
        text = "(no collection evidence)"
    return text, codes_from_evidence_text(text)

def pending_evidence_from_pod_info(case_out) -> str:
    """FailedScheduling messages KWOK recorded for the candidate, as plain text."""
    p = Path(case_out) / "pod_info.json"
    if not p.is_file():
        return "(no pod evidence)"
    try:
        info = json.loads(p.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return "(pod_info.json unreadable)"
    msgs: List[str] = []
    for _pod, events in (info.get("pod_events") or {}).items():
        for ev in events or []:
            m = (ev.get("message") or "").strip()
            if m and m not in msgs:
                msgs.append(m)
    if not msgs:
        for _pod, st in (info.get("pod_status") or {}).items():
            for c in (st.get("conditions") or []):
                m = (c.get("message") or "").strip()
                if m and m not in msgs:
                    msgs.append(m)
    return "\\n".join(msgs) if msgs else "(no scheduling failure message recorded)"

'''

# --- patch 8: the res.txt entry gate -> evidence from ConfDR's input ----------------
GATE_OLD_START = '            res_txt_content = ""'
GATE_OLD_END = 'pbar.write(f"[{case_name}] Warning: res.txt not found in {case_dir}")'
GATE_NEW = '''            # --- error evidence comes from ConfDR's frozen input, not a detector ---
            # The baseline's own Z3 detector is gone: every case enters repair, and the
            # error context the LLM sees is the FailedScheduling reason KWOK recorded for
            # the case at collection time (collection.json), mapped onto the baseline's
            # own S/R/N/V vocabulary so its prompt and RAG lookup stay in their shipped form.
            evidence_text, labels_found = evidence_from_case(case_dir)
            if pbar:
                pbar.write(f"[{case_name}] ConfDR input evidence: {evidence_text[:160]}")
'''

# --- patch 9: success from KWOK observation; Z3 recheck removed ---------------------
# Replaces the WHOLE `if not is_pending: ... else: ...` branch, through the end of the
# Pending branch, up to (not including) the `# End of loops` marker that follows it.
BRANCH_OLD_START = '                if not is_pending:'
BRANCH_OLD_END = '\n            # End of loops'
BRANCH_NEW = '''                if not is_pending:
                    # Pods are Running. KWOK observation is now the sole success criterion;
                    # the baseline's own Z3 re-check no longer participates.
                    final_status = "success"
                    if loop_history:
                        loop_history[-1]["semantic_success"] = True
                    if pbar:
                        pbar.write(f"[{case_name}] Pods are Running. Success (KWOK-observed).")
                    else:
                        print(f"[{case_name}] Pods are Running. Success (KWOK-observed).", flush=True)
                    break
                else:
                    # Pods are Pending: feed the KWOK-observed scheduling failures back to
                    # the LLM, the way the detector's UNSAT core used to be fed back.
                    pending_evidence = pending_evidence_from_pod_info(case_out)
                    if pbar:
                        pbar.write(f"[{case_name}] Pods still Pending. KWOK evidence: {pending_evidence[:160]}")
                    else:
                        print(f"[{case_name}] Pods still Pending. KWOK evidence: {pending_evidence[:160]}", flush=True)

                    new_labels_found = codes_from_evidence_text(pending_evidence)
                    new_error_pattern_lines: List[str] = []
                    for code in new_labels_found:
                        meta = code_map.get(code)
                        if meta:
                            new_error_pattern_lines.append(
                                f"[{code}] {meta.get('secondary','')}\\n"
                                f"    Category: {meta.get('primary','')}\\n"
                                f"    Labels: {meta.get('label','')}\\n"
                                f"    Description: {meta.get('description','')}"
                            )
                    new_error_pattern_section = "\\n\\n".join(new_error_pattern_lines) if new_error_pattern_lines else ""

                    new_repair_examples_parts, _ = self._load_repair_examples_for_codes(repair_root, new_labels_found)
                    new_repair_examples_section = ""
                    if new_repair_examples_parts:
                        new_repair_examples_section = "\\n\\n---\\n\\n".join(new_repair_examples_parts)
                    if rag_examples_text:
                        if new_repair_examples_section:
                            new_repair_examples_section += "\\n\\n--- Additional RAG Examples ---\\n\\n" + rag_examples_text
                        else:
                            new_repair_examples_section = rag_examples_text

                    current_variables["error_patterns"] = new_error_pattern_section
                    current_variables["repair_examples"] = new_repair_examples_section
                    current_variables["error_codes"] = (
                        "Previous repair left Pods Pending. KWOK-observed scheduling failures:\\n"
                        + (pending_evidence if pending_evidence else "(none)")
                    )
                    if 'fixed_yaml_str' in locals():
                        current_variables["input_yaml"] = fixed_yaml_str
'''


def fresh_copy(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def patch_text(path: Path, old: str, new: str, label: str, required: bool = True) -> None:
    text = path.read_text(encoding="utf-8")
    n = text.count(old)
    if n != 1:
        msg = f"[FAIL] {label}: expected exactly 1 occurrence in {path.name}, found {n}"
        if required:
            sys.exit(msg)
        print(f"  [note] {label}: {n} occurrences, skipped")
        return
    path.write_text(text.replace(old, new), encoding="utf-8")
    print(f"  patched  {label}")


def patch_block(path: Path, start: str, end: str, new: str, label: str) -> None:
    """Replace the code from `start` through `end` (both inclusive, first occurrence).

    Used for the two multi-line regions (the res.txt entry gate and the Z3-recheck
    branch) where reproducing the shipped block byte-for-byte in a literal would mean
    guessing at trailing whitespace.  Marker matching sidesteps that.
    """
    text = path.read_text(encoding="utf-8")
    i = text.find(start)
    if i < 0:
        sys.exit(f"[FAIL] {label}: start marker not found in {path.name}")
    j = text.find(end, i)
    if j < 0:
        sys.exit(f"[FAIL] {label}: end marker not found in {path.name}")
    j += len(end)
    path.write_text(text[:i] + new.rstrip("\n") + text[j:], encoding="utf-8")
    print(f"  patched  {label}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", default=None, help="max_semantic_rounds (default: leave 5)")
    ap.add_argument("--syntax", default=None, help="max_syntax_retries (default: leave 10)")
    ap.add_argument("--all", action="store_true",
                    help="stage every case, not just the UNSAT cohort (kept for the old gate)")
    ap.add_argument("--dataset", default=None,
                    help="staged repair tree to copy from (default: dataset-repair = D1; "
                         "pass dataset-repair-d2 for the D2 column)")
    args = ap.parse_args()

    repair_dir = Path(args.dataset).resolve() if args.dataset else REPAIR
    if not repair_dir.is_dir():
        sys.exit(f"missing {repair_dir}")

    print("=== 1. copy the baseline package into tmp ===")
    fresh_copy(SRC_PKG, PKG)
    print(f"  {SRC_PKG.name} -> {PKG.relative_to(ROOT)}")

    print("=== 2. restore the missing scripts/ package from repair/ ===")
    dst_scripts = PKG / "scripts"
    fresh_copy(SCRIPTS_FROM, dst_scripts)
    print("  restored:", ", ".join(sorted(p.name for p in dst_scripts.glob("*.py"))))
    # repair/scripts/ is bound to the `repair.config` package name; the two
    # config.py files are structurally identical, so this is a pure rename.
    for path in sorted(dst_scripts.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "repair.config" not in text:
            continue
        path.write_text(
            text.replace("from repair.config import", "from constraint_fix_pipeline.config import"),
            encoding="utf-8",
        )
        print(f"    rebound config import in {path.name}")

    print("=== 3. patch runner.py ===")
    runner = PKG / "runner.py"
    patch_text(runner, OLD_KWOK, NEW_KWOK, "kwok: docker images -> local binary runtime")
    # Non-empty defaults shadow the environment (runner.py:364-374), so the relay
    # credentials we export would otherwise be ignored.
    patch_text(runner, 'api_key: str = "sk-', 'api_key: str = ""  # was: sk-',
               "blank api_key default")
    patch_text(runner, 'api_base: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"',
               'api_base: str = ""', "blank api_base default")
    patch_text(runner, "import fire\nimport tiktoken", "import fire\nimport tiktoken\nimport yaml",
               "import yaml (needed by the namespace patch)")
    patch_text(runner, "    def _verify_syntax(self, fixed_yaml_path: Path, case_out: Path) -> bool:",
               ENV_HELPER + "    def _verify_syntax(self, fixed_yaml_path: Path, case_out: Path) -> bool:",
               "add _ensure_namespaces helper")
    patch_text(runner, "        # 2. Apply YAML\n", ENV_CALL,
               "synthesize referenced namespaces before apply")
    # kubectl emits GBK on this locale; decoding as UTF-8 raises inside subprocess's
    # reader thread and the command is reported as failed even when it succeeded.
    patch_text(runner, "                text=True,\n                timeout=120",
               '                text=True,\n                encoding="utf-8",\n'
               '                errors="replace",\n                timeout=120',
               "tolerate non-UTF-8 kubectl output")

    # --- patches 7-9: remove the baseline's detector, use ConfDR's input + KWOK ---
    # The `import re` line is already present in the shipped runner; this only adds the
    # evidence helpers (they need `re`, `json`, `Path`, `Set`, `List`, `Tuple` -- all
    # already imported at the top of runner.py).
    patch_text(runner, "\nclass ConstraintFixRunner:",
               "\n" + EVIDENCE_HELPERS + "\nclass ConstraintFixRunner:",
               "add ConfDR-input evidence helpers")
    patch_block(runner, GATE_OLD_START, GATE_OLD_END, GATE_NEW,
                "drop the res.txt entry gate (all cases enter repair)")
    # res_txt_content was the detector's raw res.txt, defined by the gate just removed.
    # Its three remaining reads are the error context sent to the LLM; that now comes
    # from ConfDR's input (evidence_text).  Replace-all is safe: the only write was the
    # (now deleted) assignment.
    _t = runner.read_text(encoding="utf-8")
    assert _t.count("res_txt_content") == 3, f"res_txt_content refs: {_t.count('res_txt_content')}"
    runner.write_text(_t.replace("res_txt_content", "evidence_text"), encoding="utf-8")
    print("  patched  error context source -> evidence_text (3 refs)")
    patch_block(runner, BRANCH_OLD_START, BRANCH_OLD_END, BRANCH_NEW,
                "success from KWOK observation; drop the Z3 recheck")

    if args.rounds:
        patch_text(runner, "max_semantic_rounds = 5", f"max_semantic_rounds = {args.rounds}",
                   f"max_semantic_rounds -> {args.rounds}")
    if args.syntax:
        patch_text(runner, "max_syntax_retries = 10", f"max_syntax_retries = {args.syntax}",
                   f"max_syntax_retries -> {args.syntax}")

    print("=== 4. stage cases ===")
    if DATA.exists():
        shutil.rmtree(DATA)
    staged = skipped_healthy = 0
    for case_dir in sorted(p for p in repair_dir.iterdir() if p.is_dir()):
        # The repair cohort is the truth-Pending cases: the baseline is scored on those.
        # D2's collection carries 16 controls (is_pending=false); they belong to the
        # detection set, not to repair, so they are not staged here.
        truth_file = case_dir / "truth.json"
        if truth_file.is_file():
            try:
                if not json.loads(truth_file.read_text(encoding="utf-8")).get("is_pending"):
                    skipped_healthy += 1
                    continue
            except Exception:
                pass
        # No res.txt gate any more: the baseline's own detector is gone, so every
        # pending case enters repair.  Grouping is only the two-level directory shape
        # the runner's iter_group_case_dirs requires; D2 gets its own group.
        if case_dir.name.startswith("GH-"):
            group = "GH"
        elif case_dir.name.startswith("SO"):
            group = "SO"
        else:
            group = "D2"
        dst = DATA / group / case_dir.name
        dst.parent.mkdir(parents=True, exist_ok=True)
        fresh_copy(case_dir, dst)
        staged += 1
    print(f"  staged {staged} pending cases (skipped {skipped_healthy} healthy; no res.txt gate)")
    for g in sorted(p.name for p in DATA.iterdir() if p.is_dir()):
        n = len(list((DATA / g).iterdir()))
        print(f"    {g}: {n}")

    print("\nremaining kwokctl references in runner.py (should be the binary runtime only):")
    for i, line in enumerate(runner.read_text(encoding="utf-8").splitlines(), 1):
        if "kwokctl" in line:
            print(f"  {i}: {line.strip()[:118]}...")


if __name__ == "__main__":
    main()
