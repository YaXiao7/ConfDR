"""Host-specific settings for the baseline adapters, read from the environment.

The adapters were written on Windows against one machine's layout; everything that
differed between that machine and a Linux server lives here instead of being baked
into each script:

  CONFDR_KWOK_BIN     directory holding the KWOK binary runtime
                      (default: ~/.kwok/local-bin, i.e. `kwokctl get binaries` output)
  CONFDR_HTTP_PROXY   proxy for the LLM relay, e.g. http://127.0.0.1:8080.
                      Empty/unset means direct -- a server with plain internet access
                      needs no proxy at all, and setting one that is not listening is
                      worse than setting none.
  CONFDR_SLDSTST_DIR  the legacy `sldstst-ConfDR-18d2307` checkout that carries the
                      SlsDetector baseline and the v1 Z3 solver
                      (default: <tree>/sldstst-ConfDR-18d2307)
  CONFDR_KEYS_FILE    dotenv file holding OPENAI_API_KEY / OPENAI_API_BASE
                      (default: <tree>/tmp/baseline-keys.env)
  CONFDR_BASELINE_KEYS  same, spelled the way the shell scripts read it

`NO_PROXY` always excludes loopback: kubectl and kwokctl talk to 127.0.0.1, and a
proxy that swallows that traffic makes cluster setup fail before any LLM call.
"""

from __future__ import annotations

import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
TREE = HERE.parents[1]

KWOK_BIN = Path(os.getenv("CONFDR_KWOK_BIN") or (Path.home() / ".kwok" / "local-bin"))
PROXY = os.getenv("CONFDR_HTTP_PROXY", "").strip()
SLDSTST = Path(os.getenv("CONFDR_SLDSTST_DIR") or (TREE / "sldstst-ConfDR-18d2307"))
KEYS = Path(
    os.getenv("CONFDR_KEYS_FILE")
    or os.getenv("CONFDR_BASELINE_KEYS")
    or (TREE / "tmp" / "baseline-keys.env")
)
NO_PROXY = "127.0.0.1,localhost,::1"


def binary(name: str) -> str:
    """Absolute path of one KWOK runtime binary, with the platform's suffix."""
    suffix = "" if os.name == "posix" else ".exe"
    return str(KWOK_BIN / f"{name}{suffix}")


def kwok_create_args() -> str:
    """The `kwokctl create cluster` flags that select the local binary runtime.

    Rendered as the single quoted shell command the LLMSecConfig runner stores in its
    own source, so the patch that replaces the shipped docker-image invocation is
    built from this machine's paths rather than the author's.
    """
    return (
        '"(kwokctl delete cluster || true) && kwokctl create cluster '
        "--runtime=binary "
        f"--etcd-binary={binary('etcd')} "
        f"--kube-apiserver-binary={binary('kube-apiserver')} "
        f"--kube-controller-manager-binary={binary('kube-controller-manager')} "
        f"--kube-scheduler-binary={binary('kube-scheduler')} "
        f'--kwok-controller-binary={binary("kwok-controller")}"'
    )


def apply_proxy(env: dict) -> dict:
    """Point `env` at the relay proxy when one is configured, and never at loopback."""
    if PROXY:
        env["HTTPS_PROXY"] = PROXY
        env["HTTP_PROXY"] = PROXY
        env["https_proxy"] = PROXY
        env["http_proxy"] = PROXY
    env["NO_PROXY"] = NO_PROXY
    env["no_proxy"] = NO_PROXY
    return env


def describe() -> str:
    return (
        f"tree={TREE}\n"
        f"kwok-bin={KWOK_BIN}\n"
        f"sldstst={SLDSTST}\n"
        f"keys={KEYS}\n"
        f"proxy={PROXY or '(none)'}"
    )
