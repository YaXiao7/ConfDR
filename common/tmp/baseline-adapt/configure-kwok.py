"""Point config/settings.yaml's KWOK binary runtime at this host's binaries.

`config/settings.yaml` ships with the author's Windows paths baked into
`kwok.create_args`:

    create_args: ["--runtime", "binary",
      "--etcd-binary=C:/Users/<user>/.kwok/local-bin/etcd.exe", ...]

`KwokValidator` (validation/kwok.py:425) reads `create_args` straight from that file
and has no environment override, so on a server every `kwokctl create cluster` would
be handed five paths that do not exist.  This rewrites just that key, leaving every
other setting -- including the 30 s `command_timeout_seconds` that ConfDR's own row
was scored under -- byte-identical.

Idempotent: running it twice produces the same file.  `env.sh` calls it on every
source, so moving the tree or changing CONFDR_KWOK_BIN takes effect on the next run.

Usage:
    python tmp/baseline-adapt/configure-kwok.py            # from CONFDR_KWOK_BIN
    python tmp/baseline-adapt/configure-kwok.py --check    # report, change nothing
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SETTINGS = ROOT / "config" / "settings.yaml"
BINARIES = ("etcd", "kube-apiserver", "kube-controller-manager", "kube-scheduler", "kwok-controller")


def render(bin_dir: Path, suffix: str) -> str:
    # POSIX form on every platform: the shipped value uses forward slashes, and a
    # backslash path would also need escaping once it sits inside YAML.
    base = bin_dir.as_posix()
    flags = ['"--runtime", "binary",']
    for name in BINARIES:
        flags.append(f'    "--{name}-binary={base}/{name}{suffix}",')
    flags[-1] = flags[-1].rstrip(",")
    return "  create_args: [" + "\n".join(flags).lstrip() + "]"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="report the current value and exit")
    ap.add_argument("--bin-dir", default=os.getenv("CONFDR_KWOK_BIN") or str(Path.home() / ".kwok" / "local-bin"))
    args = ap.parse_args()

    suffix = "" if os.name == "posix" else ".exe"
    bin_dir = Path(args.bin_dir)
    text = SETTINGS.read_text(encoding="utf-8")
    # Preserve the file's own line endings; Path.write_text would normalise them.
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)

    start = next((i for i, ln in enumerate(lines) if ln.startswith("  create_args:")), None)
    if start is None:
        sys.exit(f"no kwok.create_args key in {SETTINGS}")
    end = start
    while end + 1 < len(lines) and not lines[end].rstrip().endswith("]"):
        end += 1
    current = "".join(lines[start : end + 1])
    wanted = render(bin_dir, suffix) + newline

    if args.check:
        print(f"{SETTINGS}:")
        print(current.rstrip())
        print(f"\nwould become:\n{wanted.rstrip()}")
        return

    missing = [n for n in BINARIES if not (bin_dir / f"{n}{suffix}").is_file()]
    if missing:
        print(
            f"[WARN] {bin_dir} is missing: {', '.join(missing)}\n"
            "       run 'bash setup.sh' first, or set CONFDR_KWOK_BIN to the right directory",
            file=sys.stderr,
        )

    if current == wanted:
        print(f"kwok binaries already -> {bin_dir}")
        return
    lines[start : end + 1] = [wanted]
    # newline="" keeps the string's own endings instead of translating them.
    with SETTINGS.open("w", encoding="utf-8", newline="") as stream:
        stream.write("".join(lines))
    print(f"kwok binaries -> {bin_dir}")


if __name__ == "__main__":
    main()
