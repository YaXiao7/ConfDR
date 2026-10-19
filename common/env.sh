#!/usr/bin/env bash
# Common environment for every run script.  Source it, do not execute it.
#
#   . "$(dirname "$0")/env.sh"
#
# Credentials are read from tmp/baseline-keys.env and only ever live in this shell's
# environment.  Nothing is written back into the tree, and no value is echoed.
set -euo pipefail

# Git Bash reports /d/... which native Windows Python resolves against the current
# drive; pwd -W gives D:/... which both bash and Python accept.  On Linux they agree.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && (pwd -W 2>/dev/null || pwd))"
cd "$ROOT"

KEYS="${CONFDR_KEYS_FILE:-$ROOT/tmp/baseline-keys.env}"
[ -f "$KEYS" ] || {
  echo "missing $KEYS" >&2
  echo "copy your relay credentials there first -- see README.md (Setup)" >&2
  exit 2
}

if [ -n "${CONFDR_PY:-}" ]; then
  PY="$CONFDR_PY"
elif [ -x "$ROOT/.venv/bin/python" ]; then
  PY="$ROOT/.venv/bin/python"
else
  PY="$(command -v python3)"
fi
"$PY" -c 'import confdr' 2>/dev/null || {
  echo "$PY cannot import confdr -- run 'bash setup.sh' first" >&2
  exit 2
}

# shellcheck disable=SC1090
set -a; . "$KEYS"; set +a

# CHEAP_MODEL / OPENAI_API_BASE / OPENAI_API_KEY are the names the baseline adapters
# use; CONFDR_LLM_* is what the method reads.  Both come from the same file.
export CONFDR_LLM_MODEL="${CONFDR_LLM_MODEL:-${CHEAP_MODEL:-}}"
export CONFDR_LLM_BASE_URL="${CONFDR_LLM_BASE_URL:-${OPENAI_API_BASE:-}}"
export CONFDR_LLM_API_KEY="${CONFDR_LLM_API_KEY:-${OPENAI_API_KEY:-}}"

# KWOK: kwokctl and kubectl must be resolvable, and the binary runtime needs the five
# component binaries.  confdr reads those paths from config/settings.yaml, which ships
# with the author's Windows paths; configure-kwok.py rewrites just that key from
# CONFDR_KWOK_BIN, so no server-specific path is baked into the shipped file.
KWOK_BIN="${CONFDR_KWOK_BIN:-$HOME/.kwok/local-bin}"
export CONFDR_KWOK_BIN="$KWOK_BIN"
export PATH="$HOME/.local/bin:$PATH"
"$PY" "$ROOT/tmp/baseline-adapt/configure-kwok.py" --bin-dir "$KWOK_BIN"

# Proxy: set CONFDR_HTTP_PROXY only if the relay is reached through one (e.g. a local
# HTTP proxy).  A server with direct internet access needs none.  NO_PROXY
# always excludes loopback -- kubectl/kwok talk to 127.0.0.1 and must not be proxied.
if [ -n "${CONFDR_HTTP_PROXY:-}" ]; then
  export HTTPS_PROXY="$CONFDR_HTTP_PROXY" HTTP_PROXY="$CONFDR_HTTP_PROXY"
  export https_proxy="$CONFDR_HTTP_PROXY" http_proxy="$CONFDR_HTTP_PROXY"
fi
export NO_PROXY=127.0.0.1,localhost,::1 no_proxy=127.0.0.1,localhost,::1
export PYTHONIOENCODING=utf-8 PYTHONUTF8=1

# Fail early and loudly rather than halfway through a paid run.
[ -n "$CONFDR_LLM_MODEL" ] || { echo "CONFDR_LLM_MODEL/CHEAP_MODEL unset in $KEYS" >&2; exit 2; }
[ -n "$CONFDR_LLM_BASE_URL" ] || { echo "CONFDR_LLM_BASE_URL/OPENAI_API_BASE unset in $KEYS" >&2; exit 2; }
[ -n "$CONFDR_LLM_API_KEY" ] || { echo "CONFDR_LLM_API_KEY/OPENAI_API_KEY unset in $KEYS" >&2; exit 2; }
for exe in kwokctl kubectl; do
  command -v "$exe" >/dev/null || { echo "$exe not on PATH -- run 'bash setup.sh'" >&2; exit 2; }
done

# The five runtime binaries are the ones that actually get used: configure-kwok.py only
# warns about a missing one, and a wrong CONFDR_KWOK_BIN would otherwise surface much
# later as a kwokctl failure in the middle of a paid run.
for exe in etcd kube-apiserver kube-controller-manager kube-scheduler kwok-controller; do
  [ -x "$KWOK_BIN/$exe" ] || {
    echo "missing $KWOK_BIN/$exe" >&2
    echo "run 'bash setup.sh', or set CONFDR_KWOK_BIN to the directory holding the five KWOK binaries" >&2
    exit 2
  }
done

echo "model   : $CONFDR_LLM_MODEL"
echo "base    : $CONFDR_LLM_BASE_URL"
echo "api key : <from $KEYS, ${#CONFDR_LLM_API_KEY} chars>"
echo "kwok bin: $KWOK_BIN"
echo "proxy   : ${CONFDR_HTTP_PROXY:-(direct)}"
