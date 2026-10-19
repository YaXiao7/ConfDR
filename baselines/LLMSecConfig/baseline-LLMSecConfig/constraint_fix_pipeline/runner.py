import json
import os
import csv
import re
import sys
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple, Set
import time
import fire
import tiktoken

# Optional progress bar
try:
    from tqdm import tqdm  # type: ignore
except Exception:
    tqdm = None

from constraint_fix_pipeline.utils.io import (
    dump_yaml_docs,
    format_examples_for_prompt,
    iter_case_dirs,
    iter_group_case_dirs,
    load_yaml_docs_from_case,
    load_external_conflicts_from_case,
    dedup_examples_by_source,
)
from constraint_fix_pipeline.utils.parser import YAMLTextOutputParser
from constraint_fix_pipeline.fix.constraints import analyze_constraints
from constraint_fix_pipeline.llm.chain import build_chain, create_model
from constraint_fix_pipeline.prompts.templates import (
    build_instructions,
    build_input_yaml_section,
    build_error_analysis_section,
)
from constraint_fix_pipeline.solver.z3_solver_while import run_solver_with_output
from constraint_fix_pipeline.scripts.collect_pod_info import collect_pod_status_and_events
from constraint_fix_pipeline.scripts.collect_cluster_state import collect_cluster_state
from constraint_fix_pipeline.scripts.data_preprocessing import process_case
from constraint_fix_pipeline.scripts.merge_case_input import merge_case_input


def load_prediction_from_res(res_path: Path) -> Tuple[bool, Set[str]]:
    """
    Parse res.txt:
      - has_unsat: True if UNSAT exists
      - constraint_labels: raw constraint labels from union block
    """
    text = res_path.read_text(encoding="utf-8", errors="ignore")
    
    # Explicit check for success message as requested
    if "SAT: All constraints satisfied in the first check." in text:
        return False, set()

    lines = text.splitlines()

    has_unsat = any("UNSAT" in line for line in lines)
    if not has_unsat:
        return False, set()

    constraint_labels: Set[str] = set()
    union_start = None

    for i, line in enumerate(lines):
        if "Union of all unsatisfiable constraint labels" in line:
            union_start = i + 1
            break

    if union_start is not None:
        for j in range(union_start, len(lines)):
            line = lines[j].strip()
            if not line:
                break
            if line.startswith(("---", "===", "Total number", "Union of")):
                continue
            constraint_labels.add(line)

    return True, constraint_labels


CONSTRAINT_PATTERN_RULES = [
    (re.compile(r"^NODE_NAME_"), "S3"),
    (re.compile(r"^NODE_SELECTOR_"), "S2"),
    (re.compile(r"^Taints_Tolerations_"), "S4"),
    (re.compile(r"^NodeAffinity_"), "S5"),
    (re.compile(r"^POD_AFFINITY_"), "S6"),
    (re.compile(r"^POD_ANTI_AFFINITY_"), "S7"),
    (re.compile(r"^CPU_LIMIT_"), "R1"),
    (re.compile(r"^MEM_LIMIT_"), "R2"),
    (re.compile(r"^POD_LIMIT_"), "R3"),
    (re.compile(r"^HOSTPORT_LIMIT_"), "N1"),
    (re.compile(r"^PVC_.*_NOTFOUND_FOR_"), "V1"),
    (re.compile(r"^PVC_.*_NEED_STATIC_OR_DYNAMIC_SATISFY_"), "V3"),
    # Updated to V7 to match CSV specific definition
    (re.compile(r"^PVC_.*_FIXEDPV_.*_NOTFOUND$"), "V7"),
    # Expanded V2 to cover NO_PROVISIONER
    (re.compile(r"^PVC_.*_SC_NOTFOUND$"), "V2"),
    (re.compile(r"^PVC_.*_SC_NO_PROVISIONER$"), "V2"),
    # Added V4, V5, V6 support
    (re.compile(r"^REASON_ACCESS_MATCH_"), "V4"),
    (re.compile(r"^REASON_VMODE_MATCH_"), "V5"),
    (re.compile(r"^REASON_CAP_MATCH_"), "V6"),
]

IGNORED_CONSTRAINT_PREFIXES = [
    "REQ_ASSIGN_"
]


def map_constraint_to_patterns(constraint_label: str) -> Set[str]:
    for pref in IGNORED_CONSTRAINT_PREFIXES:
        if constraint_label.startswith(pref):
            return set()

    patterns = set()
    for regex, code in CONSTRAINT_PATTERN_RULES:
        if regex.match(constraint_label):
            patterns.add(code)
    return patterns


def aggregate_pattern_labels(constraint_labels: Set[str]) -> List[str]:
    patterns: Set[str] = set()
    for label in constraint_labels:
        patterns |= map_constraint_to_patterns(label)
    return sorted(patterns)


class ConstraintFixRunner:
    def _run_cmd(self, cmd: str, desc: str) -> bool:
        """
        Execute command. Return True if success, False otherwise.
        """
        print(f"[INFO] {desc}")
        try:
            result = subprocess.run(
                cmd,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=120
            )
            if result.returncode != 0:
                print(f"[FATAL] {desc} Failed!")
                if result.stderr:
                    print("STDERR:", result.stderr)
                return False
            return True
        except Exception as e:
            print(f"[ERROR] Exception during {desc}: {e}")
            return False

    def _verify_syntax(self, fixed_yaml_path: Path, case_out: Path) -> bool:
        """
        Deploy to Kwok and check pod_info.json for syntax errors.
        Returns True if syntax is valid (even if pods are pending due to constraints),
        False if syntax is invalid (empty status/events).
        """
        # 1. Reset cluster
        if not self._run_cmd("(kwokctl delete cluster || true) && kwokctl create cluster --kube-apiserver-image=registry.aliyuncs.com/google_containers/kube-apiserver:v1.29.6 --kube-controller-manager-image=registry.aliyuncs.com/google_containers/kube-controller-manager:v1.29.6 --kube-scheduler-image=registry.aliyuncs.com/google_containers/kube-scheduler:v1.29.6 --etcd-image=registry.aliyuncs.com/google_containers/etcd:3.5.11-0 --kwok-controller-binary=/usr/bin/kwok", "Reset Cluster"):
            return False
        
        # Wait for cluster
        time.sleep(10)

        # 2. Apply YAML
        # Dry-run
        if not self._run_cmd(f"kubectl apply --dry-run=client -f {fixed_yaml_path}", "Dry-run Validation"):
            return False
        
        # Apply
        if not self._run_cmd(f"kubectl apply -f {fixed_yaml_path}", "Apply YAML"):
            return False
        
        time.sleep(30)

        # 3. Collect Pod Info
        pod_info_path = case_out / "pod_info.json"
        try:
            collect_pod_status_and_events(str(pod_info_path))
        except Exception as e:
            print(f"[ERROR] Failed to collect pod info: {e}")
            return False

        # 4. Check for syntax error condition
        # "is_pending" is False, and pod_status/pod_events/error_labels are empty
        try:
            info = json.loads(pod_info_path.read_text(encoding="utf-8"))
            is_pending = info.get("is_pending", False)
            pod_status = info.get("pod_status", {})
            pod_events = info.get("pod_events", {})
            error_labels = info.get("error_labels", [])

            if not is_pending and not pod_status and not pod_events and not error_labels:
                print(f"[FAIL] Syntax error detected in {fixed_yaml_path}")
                return False
            
            return True
        except Exception as e:
            print(f"[ERROR] Failed to parse pod_info.json: {e}")
            return False

    def _run_full_verification(self, case_dir: Path, case_out: Path, fixed_yaml_path: Path):
        """
        Run the full verification pipeline:
        collect_cluster_state -> data_preprocessing -> merge_case_input -> z3_solver
        """
        print("[INFO] Starting Full Verification Pipeline...")
        
        # 1. Collect Cluster State
        cluster_state_path = case_out / "cluster_state.json"
        try:
            collect_cluster_state(str(cluster_state_path))
        except Exception as e:
            print(f"[ERROR] Failed to collect cluster state: {e}")
            return

        # 2. Data Preprocessing (Convert apps yaml -> json)
        # Note: process_case reads from a directory. We want it to read fixed.yaml.
        # fixed.yaml is in case_out.
        # But process_case iterates *.yaml in dir.
        # It skips "input.yaml".
        # So pointing it to case_out is correct.
        apps_json_path = case_out  # process_case appends "apps.json" to output_file path (which is a dir?)
        # process_case signature: process_case(case_dir, output_file="apps.json")
        # Implementation: out_path = os.path.join(output_file, "apps.json")
        # So output_file should be the directory.
        try:
            process_case(str(case_out), str(case_out))
        except Exception as e:
            print(f"[ERROR] Failed during data preprocessing: {e}")
            return
        
        # 3. Merge Case Input
        apps_json_file = case_out / "apps.json"
        case_json_path = case_out / "case.json"
        try:
            merge_case_input(str(apps_json_file), str(cluster_state_path), str(case_json_path))
        except Exception as e:
            print(f"[ERROR] Failed to merge case input: {e}")
            return

        # 4. Run Solver
        try:
            print("[INFO] Running Z3 Solver...")
            # Run solver and output to res_redetect.txt in case_out
            run_solver_with_output(str(case_out), str(case_json_path))
            print(f"[OK] Solver finished. Result saved to {case_out / 'res_redetect.txt'}")
        except Exception as e:
            print(f"[ERROR] Failed to run Z3 solver: {e}")

    def _load_constraint_model_csv(self, csv_path: Path) -> Dict[str, Dict[str, str]]:
        code_map: Dict[str, Dict[str, str]] = {}
        if not csv_path.exists():
            return code_map

        try:
            with csv_path.open(encoding="utf-8") as fh:
                reader = csv.DictReader(fh)
                for row in reader:
                    # Match English headers from CSV
                    secondary = row.get("Secondary Category / Subclass", "") or ""

                    m = re.search(r'\b([SRNV]\d+)\b', secondary)
                    if m:
                        code = m.group(1).strip()

                        # Get the other fields
                        primary = row.get("Primary Category", "")
                        label = row.get("Specific Label", "")
                        desc = row.get("Description", "")

                        code_map[code] = {
                            "primary": primary,
                            "secondary": secondary,
                            "label": label,
                            "description": desc,
                        }
        except Exception as e:
            print(f"Warning: failed to load constraint model CSV: {e}", flush=True)

        return code_map

    def _load_repair_examples_for_codes(self, repair_root: Path, codes: List[str]) -> Tuple[List[str], Dict[str, List[str]]]:
        repair_examples_parts: List[str] = []
        repair_files_loaded: Dict[str, List[str]] = {}
        allowed_exts = {".yaml", ".yml", ".json", ".txt"}

        if not repair_root.exists():
            return repair_examples_parts, repair_files_loaded

        for code in codes:
            repair_files_loaded[code] = []
            dir_candidate = repair_root / code
            dirs = []
            if dir_candidate.exists() and dir_candidate.is_dir():
                dirs.append(dir_candidate)
            else:
                for d in repair_root.iterdir():
                    if d.is_dir() and d.name.lower() == code.lower():
                        dirs.append(d)
                        break
            for d in dirs:
                for p in sorted(d.rglob("*")):
                    if p.is_file() and p.suffix.lower() in allowed_exts and not p.name.startswith("."):
                        try:
                            content = p.read_text(encoding="utf-8")
                            repair_examples_parts.append(content)
                            repair_files_loaded[code].append(str(p.relative_to(repair_root)))
                        except Exception:
                            continue

            for ext in allowed_exts:
                f = repair_root / f"{code}{ext}"
                if f.exists() and f.is_file():
                    try:
                        content = f.read_text(encoding="utf-8")
                        repair_examples_parts.append(content)
                        repair_files_loaded[code].append(str(f.relative_to(repair_root)))
                    except Exception:
                        pass

            if not repair_files_loaded[code]:
                norm_code = re.sub(r'[^a-z0-9]', '', code.lower())
                for p in sorted(repair_root.rglob("*")):
                    try:
                        if p.is_file() and p.suffix.lower() in allowed_exts:
                            pname_norm = re.sub(r'[^a-z0-9]', '', p.name.lower())
                            if norm_code in pname_norm or code.lower() in p.name.lower():
                                content = p.read_text(encoding="utf-8")
                                repair_examples_parts.append(content)
                                repair_files_loaded[code].append(str(p.relative_to(repair_root)))
                    except Exception:
                        continue
                    if p.is_dir():
                        try:
                            dname_norm = re.sub(r'[^a-z0-9]', '', p.name.lower())
                            if norm_code in dname_norm or code.lower() in p.name.lower():
                                for f in sorted(p.rglob("*")):
                                    if f.is_file() and f.suffix.lower() in allowed_exts and not f.name.startswith("."):
                                        try:
                                            content = f.read_text(encoding="utf-8")
                                            repair_examples_parts.append(content)
                                            repair_files_loaded[code].append(str(f.relative_to(repair_root)))
                                        except Exception:
                                            continue
                        except Exception:
                            continue

        return repair_examples_parts, repair_files_loaded

    def run(
        self,
        data_dir: str = "constraint_fix_pipeline/data",
        result_dir: str = "constraint_fix_pipeline/result",
        output_dir: str = "constraint_fix_pipeline/outputs",
        model_endpoint: str = "qwen3-max",
        api_base: str = "https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key: str = "sk-REDACTED",
    ):
        Path(output_dir).mkdir(parents=True, exist_ok=True)

        # API key resolution
        env_api_key = os.getenv("OPENAI_API_KEY") or os.getenv("LLM_API_KEY")
        if not api_key:
            api_key = env_api_key or ""
        if not api_key:
            raise RuntimeError("API key is missing. Provide --api_key or set OPENAI_API_KEY environment variable.")

        env_base = os.getenv("OPENAI_API_BASE") or os.getenv("OPENAI_BASE_URL")
        if not api_base and env_base:
            api_base = env_base

        model = create_model(model_endpoint=model_endpoint, api_base=api_base, api_key=api_key, temperature=0.3)

        case_dirs = iter_group_case_dirs(data_dir)
        if not case_dirs:
            case_dirs = iter_case_dirs(data_dir)
        if not case_dirs:
            case_dirs = [Path(data_dir)]

        data_root = Path(data_dir).resolve()
        index: List[Dict[str, Any]] = []

        pbar = None
        if tqdm is not None:
            pbar = tqdm(total=len(case_dirs), desc="progress", unit="case", leave=True)

        # use constraint_model_retrieval folder for both CSV and repair_root
        retrieval_root = Path("constraint_fix_pipeline") / "constraint_model_retrieval"
        csv_path = retrieval_root / "constraint_model.csv"
        code_map = self._load_constraint_model_csv(csv_path)
        repair_root = retrieval_root / "constraint_model_repair"

        # SAT skip patterns
        # SAT_OK_PATTERNS removed in favor of load_prediction_from_res

        for case_dir in case_dirs:
            case_name = Path(case_dir).name
            case_path = Path(case_dir).resolve()
            try:
                rel = case_path.relative_to(data_root)
            except Exception:
                rel = Path(case_name)

            case_out = Path(output_dir) / rel

            case_start_ts = datetime.now().isoformat()
            case_log: Dict[str, Any] = {
                "case": case_name,
                "case_dir": str(case_dir),
                "status": "processing",
                "timestamp_start": case_start_ts,
            }

            res_txt_content = ""
            labels_found: List[str] = []
            res_file = Path(case_dir) / "res.txt"

            if res_file.exists():
                try:
                    res_txt_content = res_file.read_text(encoding="utf-8", errors="ignore")

                    has_unsat, raw_constraints = load_prediction_from_res(res_file)

                    if not has_unsat:
                        msg = f"[{case_name}] SAT (or no UNSAT) detected in res.txt, skip repair."
                        if pbar:
                            pbar.write(msg)
                            pbar.update(1)
                        else:
                            print(msg, flush=True)

                        case_end_ts = datetime.now().isoformat()
                        case_log.update({
                            "status": "skipped",
                            "reason": "sat_no_conflict",
                            "timestamp_end": case_end_ts,
                            "error_codes_found": [],
                        })
                        index.append(case_log)

                        # update global index only (no case folder)
                        try:
                            (Path(output_dir) / "index.json").write_text(
                                json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8"
                            )
                        except Exception:
                            pass

                        continue

                    labels_found = aggregate_pattern_labels(raw_constraints)
                    if pbar and labels_found:
                        pbar.write(f"[{case_name}] Matched error codes: {', '.join(labels_found)}")
                except Exception as e:
                    if pbar:
                        pbar.write(f"[{case_name}] Warning: failed to parse res.txt: {e}")
                    labels_found = []
            else:
                if pbar:
                    pbar.write(f"[{case_name}] Warning: res.txt not found in {case_dir}")

            docs = load_yaml_docs_from_case(case_dir)
            if not docs:
                msg = f"[Skip] No YAML documents found in case {case_name}"
                if pbar:
                    pbar.write(msg)
                    pbar.update(1)
                else:
                    print(msg, flush=True)
                case_log.update({"status": "skipped", "reason": "no_yaml"})
                index.append(case_log)
                try:
                    (Path(output_dir) / "index.json").write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")
                except Exception:
                    pass
                continue

            case_out.mkdir(parents=True, exist_ok=True)

            input_yaml = dump_yaml_docs(docs)
            external_conflicts = load_external_conflicts_from_case(case_dir)

            constraints_report: Dict[str, Any] = analyze_constraints(
                docs, external_conflicts=external_conflicts, examples_dir=result_dir
            )
            relationships = constraints_report.get("relationships", [])
            conflicts = constraints_report.get("conflicts", [])
            suggestions = constraints_report.get("suggestions", [])
            cores = constraints_report.get("unsat_cores", [])

            # RAG examples
            selected_examples: List[Dict[str, Any]] = []
            for core in cores:
                selected_examples.extend(core.get("examples", []) or [])
            selected_examples = dedup_examples_by_source(selected_examples)
            rag_examples_text, _ = format_examples_for_prompt(selected_examples)

            # build error pattern section from csv map
            error_pattern_lines: List[str] = []
            for code in labels_found:
                meta = code_map.get(code)
                if meta:
                    error_pattern_lines.append(
                        f"【{code}】 {meta.get('secondary','')}\n"
                        f"    Category: {meta.get('primary','')}\n"
                        f"    Labels: {meta.get('label','')}\n"
                        f"    Description: {meta.get('description','')}"
                    )
            error_pattern_section = "\n\n".join(error_pattern_lines) if error_pattern_lines else ""

            # Diagnostic: sample repair_root entries for visibility
            try:
                if pbar is not None:
                    if not repair_root.exists():
                        pbar.write(f"[{case_name}] Warning: repair_root not found at {repair_root.resolve()}")
                    else:
                        entries = []
                        for e in sorted(repair_root.iterdir()):
                            entries.append(e.name)
                            if len(entries) >= 20:
                                break
                        pbar.write(f"[{case_name}] repair_root entries (sample): {entries}")
            except Exception:
                pass

            repair_examples_parts, repair_files_loaded = self._load_repair_examples_for_codes(repair_root, labels_found)

            # If matched codes but no files found, append diagnostic sample into repair_examples_parts
            try:
                if labels_found and (not repair_files_loaded or all(len(v) == 0 for v in repair_files_loaded.values())):
                    diag_lines = []
                    diag_lines.append(f"No repair files found for codes: {labels_found}")
                    try:
                        if repair_root.exists():
                            found = [str(p.relative_to(repair_root)) for p in repair_root.rglob("*") if p.is_file()][:50]
                            diag_lines.append(f"Files under {repair_root} (sample up to 50): {found or '(none)'}")
                        else:
                            diag_lines.append(f"repair_root does not exist: {repair_root}")
                    except Exception as e:
                        diag_lines.append(f"Failed to enumerate repair_root: {e}")
                    if repair_examples_parts:
                        repair_examples_parts.append("\n\n---\n\n" + "\n".join(diag_lines))
                    else:
                        repair_examples_parts = ["\n".join(diag_lines)]
            except Exception:
                pass

            repair_examples_section = ""
            if repair_examples_parts:
                repair_examples_section = "\n\n---\n\n".join(repair_examples_parts)
            if rag_examples_text:
                if repair_examples_section:
                    repair_examples_section += "\n\n--- Additional RAG Examples ---\n\n" + rag_examples_text
                else:
                    repair_examples_section = rag_examples_text

            # persist artifacts
            try:
                (case_out / "constraints.json").write_text(json.dumps(constraints_report, indent=2, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass
            try:
                (case_out / "input.yaml").write_text(input_yaml, encoding="utf-8")
            except Exception:
                pass

            # prepare variables and build chain
            variables = {
                "input_yaml": input_yaml,
                "error_codes": res_txt_content,
                "error_patterns": error_pattern_section,
                "repair_examples": repair_examples_section,
                "relationships": relationships,
                "conflicts": conflicts,
                "suggestions": suggestions,
            }
            chain = build_chain(model=model, variables=variables)

            # save full prompt to log.txt
            prompt_token_count = None
            try:
                sys_role, sys_text = build_instructions()
                _, input_yaml_section = build_input_yaml_section(input_yaml)
                _, error_analysis_section = build_error_analysis_section(
                    error_codes=res_txt_content, error_patterns=error_pattern_section, repair_examples=repair_examples_section
                )

                rel_text = "\n".join(relationships) if relationships else "(none)"
                conf_text = "\n".join(f"- {c}" for c in conflicts) if conflicts else "(none)"
                sug_text = "\n".join(f"- {s}" for s in suggestions) if suggestions else "(none)"

                constraints_section = (
                    "#### Constraint Analysis (Multi-file Relationships & Conflicts)\n\n"
                    f"**Relationships:**\n{rel_text}\n\n"
                    f"**Conflicts:**\n{conf_text}\n\n"
                    f"**Suggestions:**\n{sug_text}\n"
                    "-----------------------------------------------------------------"
                )

                complete_prompt_lines: List[str] = []
                complete_prompt_lines.append("=" * 80)
                complete_prompt_lines.append("COMPLETE PROMPT SENT TO LLM FOR REPAIR")
                complete_prompt_lines.append("=" * 80)
                complete_prompt_lines.append(f"\nCASE: {case_name}")
                complete_prompt_lines.append(f"START: {case_start_ts}\n")
                complete_prompt_lines.append("=" * 80)
                complete_prompt_lines.append("SYSTEM MESSAGE (Role Definition)")
                complete_prompt_lines.append("=" * 80)
                complete_prompt_lines.append(sys_text)
                complete_prompt_lines.append("\n" + "=" * 80)
                complete_prompt_lines.append("USER MESSAGE (Context & Task)")
                complete_prompt_lines.append("\n" + input_yaml_section)
                complete_prompt_lines.append("\n" + error_analysis_section)
                complete_prompt_lines.append("\n" + constraints_section)
                complete_prompt_lines.append("\n" + "=" * 80)
                complete_prompt_lines.append("METADATA & TRACEABILITY")
                complete_prompt_lines.append("=" * 80)
                complete_prompt_lines.append(f"\nError Codes Found (fuzzy matched): {', '.join(labels_found) if labels_found else '(None)'}")
                if repair_files_loaded:
                    complete_prompt_lines.append(f"\nRepair Files Loaded:")
                    for code, files in repair_files_loaded.items():
                        if files:
                            complete_prompt_lines.append(f"  [{code}]: {', '.join(files)}")
                else:
                    complete_prompt_lines.append("\nRepair Files Loaded: (None)")
                complete_prompt_lines.append(f"\nConstraints Report:\n{json.dumps(constraints_report, indent=2, ensure_ascii=False)}")

                complete_prompt = "\n".join(complete_prompt_lines)
                (case_out / "log.txt").write_text(complete_prompt, encoding="utf-8")
                
                # Token count will be extracted from actual LLM response
                prompt_token_count = None
                completion_token_count = None
                total_token_count = None

                if pbar:
                    pbar.write(f"[{case_name}] prompt context saved to log.txt")
            except Exception as e:
                if pbar:
                    pbar.write(f"[{case_name}] Warning: failed to write log.txt: {str(e)}")

            # ------------------------------------------------------------------
            # Multi-Round Repair Logic
            # Outer Loop: Semantic Repair (Max 5 rounds) - Fix Pending Pods/Constraints
            # Inner Loop: Syntax Repair (Max 10 retries) - Fix YAML Syntax Errors
            # ------------------------------------------------------------------
            
            # Initial context variables
            current_variables = {
                "input_yaml": input_yaml,
                "error_codes": res_txt_content,
                "error_patterns": error_pattern_section,
                "repair_examples": repair_examples_section,
                "relationships": relationships,
                "conflicts": conflicts,
                "suggestions": suggestions,
            }

            max_semantic_rounds = 5
            max_syntax_retries = 10
            
            final_status = "failed"
            final_error = None
            
            # Tracking metrics across rounds
            total_llm_time = 0.0
            total_prompt_tokens = 0
            total_completion_tokens = 0
            
            # Tracking metrics for the final successful repair (last LLM call)
            last_repair_llm_time = 0.0
            last_repair_prompt_tokens = 0
            last_repair_completion_tokens = 0
            
            # Record loop counts
            loop_history = []
            
            for semantic_round in range(max_semantic_rounds):
                if pbar:
                    pbar.write(f"[{case_name}] === Semantic Repair Round {semantic_round+1}/{max_semantic_rounds} ===")
                else:
                    print(f"[{case_name}] === Semantic Repair Round {semantic_round+1}/{max_semantic_rounds} ===", flush=True)

                syntax_success = False
                fixed_yaml_path = case_out / "fixed.yaml"
                
                # To track syntax attempts in this semantic round
                syntax_attempts_count = 0
                
                # --- Inner Loop: Syntax Repair ---
                for syntax_attempt in range(max_syntax_retries):
                    syntax_attempts_count = syntax_attempt + 1
                    try:
                        msg = f"[{case_name}] [Round {semantic_round+1}] Invoking LLM (Syntax Attempt {syntax_attempt+1}/{max_syntax_retries})..."
                        if pbar:
                            pbar.write(msg)
                        else:
                            print(msg, flush=True)
                        
                        round_log_path = case_out / f"log_round_{semantic_round+1}_attempt_{syntax_attempt+1}.txt"
                        try:
                            pre_lines: List[str] = []
                            pre_lines.append(f"CASE: {case_name}")
                            pre_lines.append(f"ROUND: {semantic_round+1}")
                            pre_lines.append(f"ATTEMPT: {syntax_attempt+1}")
                            pre_lines.append("")
                            pre_lines.append("ERROR_CODES:")
                            pre_lines.append(str(current_variables.get("error_codes", "")))
                            pre_lines.append("")
                            pre_lines.append("ERROR_PATTERNS:")
                            pre_lines.append(str(current_variables.get("error_patterns", "")))
                            pre_lines.append("")
                            pre_lines.append("REPAIR_EXAMPLES:")
                            pre_lines.append(str(current_variables.get("repair_examples", "")))
                            pre_lines.append("")
                            pre_lines.append("INPUT_YAML:")
                            pre_lines.append(str(current_variables.get("input_yaml", "")))
                            round_log_path.write_text("\n".join(pre_lines), encoding="utf-8")
                        except Exception:
                            pass
                        
                        invoke_start = time.time()
                        
                        # invoke returns AIMessage now
                        response = chain.invoke(current_variables)
                        
                        invoke_end = time.time()
                        current_duration = invoke_end - invoke_start
                        total_llm_time += current_duration
                        last_repair_llm_time = current_duration

                        # Extract token usage
                        if hasattr(response, "response_metadata"):
                            usage = response.response_metadata.get("token_usage", {})
                            if usage:
                                p_tokens = usage.get("prompt_tokens", 0)
                                c_tokens = usage.get("completion_tokens", 0)
                                total_prompt_tokens += p_tokens
                                total_completion_tokens += c_tokens
                                
                                last_repair_prompt_tokens = p_tokens
                                last_repair_completion_tokens = c_tokens
                        
                        # Parse output
                        fixed_yaml_text = YAMLTextOutputParser().invoke(response)
                        fixed_yaml_str = fixed_yaml_text if isinstance(fixed_yaml_text, str) else str(fixed_yaml_text)
                        fixed_yaml_path.write_text(fixed_yaml_str, encoding="utf-8")
                        
                        try:
                            post_lines: List[str] = []
                            post_lines.append("")
                            post_lines.append("LLM_DURATION_SEC:")
                            post_lines.append(str(current_duration))
                            post_lines.append("")
                            post_lines.append("PROMPT_TOKENS:")
                            post_lines.append(str(last_repair_prompt_tokens))
                            post_lines.append("")
                            post_lines.append("COMPLETION_TOKENS:")
                            post_lines.append(str(last_repair_completion_tokens))
                            post_lines.append("")
                            post_lines.append("FIXED_YAML:")
                            post_lines.append(fixed_yaml_str)
                            with round_log_path.open("a", encoding="utf-8") as fh:
                                fh.write("\n".join(post_lines))
                        except Exception:
                            pass

                        # Verify syntax (Kwok check)
                        if self._verify_syntax(fixed_yaml_path, case_out):
                            syntax_success = True
                            break # Syntax OK, proceed to semantic check
                        else:
                            print(f"[WARN] Syntax verification failed for attempt {syntax_attempt+1}")
                            # Update context for next syntax retry:
                            # User Instruction: "If syntax error... directly put in LLM context that YAML has syntax error... instead of retrieved examples"
                            current_variables["error_codes"] = "SYNTAX ERROR: The previous YAML file contained syntax errors and could not be applied. Please fix the YAML syntax/formatting."
                            current_variables["error_patterns"] = ""
                            current_variables["repair_examples"] = ""
                            # Keep input_yaml and other constraints as base context? 
                            # User said "instead of putting in retrieved examples". 
                            # So we clear examples/patterns, focusing on syntax.

                    except Exception as e:
                        print(f"[WARN] Exception during LLM invocation/verification (Attempt {syntax_attempt+1}): {e}")
                        final_error = str(e)
                
                # Record this round's stats
                loop_history.append({
                    "semantic_round": semantic_round + 1,
                    "syntax_attempts": syntax_attempts_count,
                    "syntax_success": syntax_success,
                    "semantic_success": final_status == "success" if syntax_success else False
                })

                if not syntax_success:
                    print(f"[{case_name}] Failed to generate valid syntax after {max_syntax_retries} attempts.")
                    final_error = "Max syntax retries reached"
                    break # Break semantic loop if we can't even get valid syntax

                # --- Semantic Check (Full Verification) ---
                # Syntax is valid, now check if it solves the constraints (Pods Running?)
                # _verify_syntax already updated pod_info.json
                
                # Check is_pending status
                is_pending = False
                try:
                    pod_info_path = case_out / "pod_info.json"
                    info = json.loads(pod_info_path.read_text(encoding="utf-8"))
                    is_pending = info.get("is_pending", False)
                except Exception:
                    is_pending = True # Assume pending if read fails?

                if not is_pending:
                    # Pods are running. Perform full verification and confirm SAT before finalizing.
                    if pbar:
                        pbar.write(f"[{case_name}] ✓ Pods are running. Running full verification to confirm SAT...")
                    else:
                        print(f"[{case_name}] ✓ Pods are running. Running full verification to confirm SAT...", flush=True)
                    
                    # Run full verification pipeline to generate res_redetect.txt and other artifacts
                    self._run_full_verification(case_dir, case_out, fixed_yaml_path)
                    
                    # Check Z3 output for SAT/UNSAT to decide final success
                    res_redetect_path = case_out / "res_redetect.txt"
                    z3_conflict_codes: List[str] = []
                    z3_has_unsat = False
                    try:
                        if res_redetect_path.exists():
                            has_unsat, raw_constraints = load_prediction_from_res(res_redetect_path)
                            z3_has_unsat = has_unsat
                            if has_unsat and raw_constraints:
                                z3_conflict_codes = sorted(list(raw_constraints))
                    except Exception:
                        z3_has_unsat = False
                    
                    if not z3_has_unsat:
                        # Final success when Z3 reports SAT (or no UNSAT)
                        final_status = "success"
                        if loop_history:
                            loop_history[-1]["semantic_success"] = True
                        if pbar:
                            pbar.write(f"[{case_name}] ✓ Final Success! Z3 confirms SAT.")
                        else:
                            print(f"[{case_name}] ✓ Final Success! Z3 confirms SAT.", flush=True)
                        break
                    else:
                        # Z3 still reports UNSAT; feed new conflicts back to LLM and continue next semantic round
                        if pbar and z3_conflict_codes:
                            pbar.write(f"[{case_name}] Z3 still UNSAT. New conflicts: {', '.join(z3_conflict_codes)}")
                        
                        # Map raw constraints to pattern codes and rebuild context
                        new_labels_found = aggregate_pattern_labels(set(z3_conflict_codes))
                        if pbar and new_labels_found:
                            pbar.write(f"[{case_name}] [Round {semantic_round+1}] New matched error codes: {new_labels_found}")
                        
                        # Build error pattern section (from CSV)
                        new_error_pattern_lines: List[str] = []
                        for code in new_labels_found:
                            meta = code_map.get(code)
                            if meta:
                                new_error_pattern_lines.append(
                                    f"【{code}】 {meta.get('secondary','')}\n"
                                    f"    Category: {meta.get('primary','')}\n"
                                    f"    Labels: {meta.get('label','')}\n"
                                    f"    Description: {meta.get('description','')}"
                                )
                        new_error_pattern_section = "\n\n".join(new_error_pattern_lines) if new_error_pattern_lines else ""
                        
                        # Load repair examples (from repair_root)
                        new_repair_examples_parts, _ = self._load_repair_examples_for_codes(repair_root, new_labels_found)
                        new_repair_examples_section = ""
                        if new_repair_examples_parts:
                            new_repair_examples_section = "\n\n---\n\n".join(new_repair_examples_parts)
                        if rag_examples_text:
                            if new_repair_examples_section:
                                new_repair_examples_section += "\n\n--- Additional RAG Examples ---\n\n" + rag_examples_text
                            else:
                                new_repair_examples_section = rag_examples_text
                        
                        # Update variables for next Semantic Round
                        current_variables["error_patterns"] = new_error_pattern_section
                        current_variables["repair_examples"] = new_repair_examples_section
                        current_variables["error_codes"] = f"Previous repair produced running pods, but Z3 still UNSAT.\nNew Detected Conflicts:\n" + ("\n".join(z3_conflict_codes) if z3_conflict_codes else "(none)")
                        # Ensure we iterate on the latest fixed YAML
                        if 'fixed_yaml_str' in locals():
                            current_variables["input_yaml"] = fixed_yaml_str
                else:
                    # Pods are Pending. We need to run Z3 to find out why, and feed back to LLM.
                    if pbar:
                        pbar.write(f"[{case_name}] ✗ Pods are still Pending. Running Z3 for diagnosis...")
                    else:
                        print(f"[{case_name}] ✗ Pods are still Pending. Running Z3 for diagnosis...", flush=True)

                    # Run Z3 Pipeline
                    self._run_full_verification(case_dir, case_out, fixed_yaml_path)
                    
                    # Check if Z3 actually says SAT despite Pods being Pending
                    res_redetect_path = case_out / "res_redetect.txt"
                    if res_redetect_path.exists():
                        has_unsat, _ = load_prediction_from_res(res_redetect_path)
                        if not has_unsat:
                            # Z3 says SAT! Override Pending status.
                            final_status = "success"
                            if loop_history:
                                loop_history[-1]["semantic_success"] = True
                            msg = f"[{case_name}] ✓ Z3 confirms SAT (overriding Pending status)!"
                            if pbar:
                                pbar.write(msg)
                            else:
                                print(msg, flush=True)
                            break

                    # Read Z3 output (res_redetect.txt)
                    new_error_codes = ""
                    if res_redetect_path.exists():
                        # Parse UNSAT codes from res_redetect.txt
                        # Format: "Union of all unsatisfiable constraint labels:\n   CODE1\n   CODE2"
                        try:
                            content = res_redetect_path.read_text(encoding="utf-8")
                            lines = content.splitlines()
                            codes = []
                            capture = False
                            for line in lines:
                                if "Union of all unsatisfiable constraint labels" in line:
                                    capture = True
                                    continue
                                if capture and line.strip():
                                    codes.append(line.strip())
                            
                            if codes:
                                new_error_codes = "\n".join(codes)
                                if pbar:
                                    pbar.write(f"[{case_name}] Z3 detected new conflicts: {', '.join(codes)}")
                                
                                # --- RAG Retrieval for NEW codes ---
                                # 1. Map raw constraints to error codes (e.g. CPU_LIMIT -> R1)
                                new_labels_found = aggregate_pattern_labels(set(codes))
                                if pbar:
                                    pbar.write(f"[{case_name}] [Round {semantic_round+1}] New matched error codes: {new_labels_found}")
                                else:
                                    print(f"[{case_name}] [Round {semantic_round+1}] New matched error codes: {new_labels_found}", flush=True)
                                
                                # 2. Build error pattern section (from CSV)
                                new_error_pattern_lines: List[str] = []
                                for code in new_labels_found:
                                    meta = code_map.get(code)
                                    if meta:
                                        new_error_pattern_lines.append(
                                            f"【{code}】 {meta.get('secondary','')}\n"
                                            f"    Category: {meta.get('primary','')}\n"
                                            f"    Labels: {meta.get('label','')}\n"
                                            f"    Description: {meta.get('description','')}"
                                        )
                                new_error_pattern_section = "\n\n".join(new_error_pattern_lines) if new_error_pattern_lines else ""

                                # 3. Load repair examples (from repair_root)
                                new_repair_examples_parts, _ = self._load_repair_examples_for_codes(repair_root, new_labels_found)
                                new_repair_examples_section = ""
                                if new_repair_examples_parts:
                                    new_repair_examples_section = "\n\n---\n\n".join(new_repair_examples_parts)
                                
                                # (Optional) Retain or Append theoretical RAG examples?
                                # For now, we append the original theoretical examples if they exist
                                if rag_examples_text:
                                    if new_repair_examples_section:
                                        new_repair_examples_section += "\n\n--- Additional RAG Examples ---\n\n" + rag_examples_text
                                    else:
                                        new_repair_examples_section = rag_examples_text

                                # Update variables with NEW RAG context
                                current_variables["error_patterns"] = new_error_pattern_section
                                current_variables["repair_examples"] = new_repair_examples_section

                            else:
                                # Fallback if parsing failed or format different
                                new_error_codes = content
                        except Exception:
                             pass
                    
                    if not new_error_codes:
                         new_error_codes = "UNKNOWN_CONSTRAINT_FAILURE: Pods are pending but Z3 solver did not return specific error codes."

                    # Update variables for next Semantic Round
                    current_variables["error_codes"] = f"Previous repair failed. Pods are Pending.\nNew Detected Conflicts:\n{new_error_codes}"
                    # IMPORTANT: Update input_yaml to the latest fixed version for iterative refinement
                    if 'fixed_yaml_str' in locals():
                        current_variables["input_yaml"] = fixed_yaml_str

            # End of loops
            
            case_end_ts = datetime.now().isoformat()
            
            case_log.update({
                "status": final_status,
                "timestamp_end": case_end_ts,
                "llm_repair_time_sec": total_llm_time,
                "llm_prompt_tokens": total_prompt_tokens,
                "llm_completion_tokens": total_completion_tokens,
                "llm_total_tokens": total_prompt_tokens + total_completion_tokens,
                "final_repair_time_sec": last_repair_llm_time,
                "final_repair_prompt_tokens": last_repair_prompt_tokens,
                "final_repair_completion_tokens": last_repair_completion_tokens,
                "model_endpoint": model_endpoint,
                "conflicts_count": len(conflicts),
                "suggestions_count": len(suggestions),
                "unsat_core_count": len(cores),
                "external_conflicts_count": len(external_conflicts or []),
                "error_codes_found": labels_found,
                "repair_files_loaded_count": sum(len(v) for v in repair_files_loaded.values()),
                "loop_history": loop_history,
            })
            
            if final_error:
                case_log["error"] = final_error

            (case_out / "run.json").write_text(json.dumps(case_log, indent=2, ensure_ascii=False), encoding="utf-8")
            index.append(case_log)
            
            if final_status == "success":
                if pbar:
                    pbar.update(1)
            else:
                if pbar:
                    pbar.write(f"[{case_name}] ✗ Final Failure: {final_error or 'Pending after max rounds'}")
                    pbar.update(1)
                else:
                    print(f"[{case_name}] ✗ Final Failure: {final_error or 'Pending after max rounds'}", flush=True)

            # update global index
            try:
                (Path(output_dir) / "index.json").write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass

        if pbar:
            pbar.close()
        print("✓ Processing completed. Index at:", Path(output_dir) / "index.json")


if __name__ == "__main__":
    fire.Fire(ConstraintFixRunner().run)
