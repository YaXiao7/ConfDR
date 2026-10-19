import shutil
from pathlib import Path


def collect_solution_map(solution_dir: Path) -> dict[str, Path]:
    mapping: dict[str, Path] = {}
    if not solution_dir.exists():
        return mapping
    for p in solution_dir.rglob("*"):
        if p.is_file() and p.suffix.lower() in {".yaml", ".yml"}:
            mapping[p.name] = p
    return mapping


def copy_envs(src_envs: Path, dst_envs: Path) -> None:
    if not src_envs.exists():
        return
    for p in src_envs.rglob("*"):
        if p.is_file():
            rel = p.relative_to(src_envs)
            out = dst_envs / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, out)


def merge_apps(src_apps: Path, solution_map: dict[str, Path], dst_apps: Path) -> None:
    if not src_apps.exists():
        return
    for p in src_apps.rglob("*"):
        if p.is_file() and p.suffix.lower() in {".yaml", ".yml"}:
            rel = p.relative_to(src_apps)
            out = dst_apps / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            sol = solution_map.get(p.name)
            if sol and sol.exists():
                shutil.copy2(sol, out)
            else:
                shutil.copy2(p, out)
        elif p.is_file():
            rel = p.relative_to(src_apps)
            out = dst_apps / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, out)


def process_cases(input_cases_root: Path, outputs_cases_root: Path, dataset_root: Path) -> None:
    if not outputs_cases_root.exists():
        return
    for out_case_dir in sorted([p for p in outputs_cases_root.iterdir() if p.is_dir()]):
        solution_dir = out_case_dir / "solution"
        if not solution_dir.exists():
            continue
        in_case_dir = input_cases_root / out_case_dir.name
        apps_dir = in_case_dir / "apps"
        envs_dir = in_case_dir / "envs"
        solution_map = collect_solution_map(solution_dir)
        dst_case = dataset_root / out_case_dir.name
        dst_apps = dst_case / "apps"
        dst_envs = dst_case / "envs"
        dst_case.mkdir(parents=True, exist_ok=True)
        merge_apps(apps_dir, solution_map, dst_apps)
        copy_envs(envs_dir, dst_envs)

def _collect_yaml_files(root: Path) -> list[Path]:
    files: list[Path] = []
    if not root.exists():
        return files
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in {".yaml", ".yml"}:
            files.append(p)
    return sorted(files, key=lambda x: str(x.relative_to(root)).lower())


def build_fixed_yaml_for_dataset(dataset_root: Path) -> None:
    if not dataset_root.exists():
        return
    for case_dir in sorted([p for p in dataset_root.iterdir() if p.is_dir()]):
        envs_dir = case_dir / "envs"
        apps_dir = case_dir / "apps"
        parts: list[str] = []
        for d in (apps_dir, envs_dir):
            for f in _collect_yaml_files(d):
                try:
                    txt = f.read_text(encoding="utf-8")
                    if txt.strip():
                        parts.append(txt.strip())
                except Exception:
                    continue
        if parts:
            combined: list[str] = []
            for i, t in enumerate(parts):
                if i > 0:
                    combined.append("\n---\n")
                combined.append(t)
            fixed_path = case_dir / "fixed.yaml"
            fixed_path.write_text("".join(combined) + "\n", encoding="utf-8")


def main():
    script_dir = Path(__file__).resolve().parent
    root = script_dir.parent
    input_cases_root = (script_dir / "data" / "affinity-cases").resolve()
    outputs_cases_root = (script_dir / "outputs" / "affinity-cases").resolve()
    dataset_root = (script_dir / "dataset").resolve()
    dataset_root.mkdir(parents=True, exist_ok=True)
    process_cases(input_cases_root, outputs_cases_root, dataset_root)
    build_fixed_yaml_for_dataset(dataset_root)


if __name__ == "__main__":
    main()
