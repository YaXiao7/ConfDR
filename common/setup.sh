#!/usr/bin/env bash
# One-time setup for a Linux server.  Run from anywhere; it works on itself.
#
#   bash setup.sh
#
# Creates the venv, installs confdr into it, and installs the KWOK binary runtime.
# It touches nothing outside this directory and writes no credentials: the relay
# credentials come from tmp/baseline-keys.env, which you copy in yourself (see
# README.md, Setup).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

VENV="${CONFDR_VENV:-$HERE/.venv}"
PY="$VENV/bin/python"
# Matches the versions the Windows reference run was scored under: kwok v0.8.0,
# Kubernetes v1.30.2, etcd v3.6.10.  Scheduling semantics changed between kwok releases,
# so a different version here would not be comparable with the published numbers.
K8S_VERSION="${K8S_VERSION:-v1.30.2}"
ETCD_VERSION="${ETCD_VERSION:-v3.6.10}"
KWOK_VERSION="${KWOK_VERSION:-v0.8.0}"
KWOK_DIR="${CONFDR_KWOK_BIN:-$HOME/.kwok/local-bin}"
# Offline bundle: `tmp/kwok-linux-amd64/` ships the seven binaries for hosts that cannot
# reach github.com or dl.k8s.io (e.g. behind a firewall).  Set CONFDR_KWOK_BUNDLE to
# point somewhere else, or CONFDR_KWOK_SKIP_BUNDLE=1 to force downloading.
KWOK_BUNDLE="${CONFDR_KWOK_BUNDLE:-$HERE/tmp/kwok-linux-amd64}"

say() { printf '\n=== %s\n' "$*"; }

# `python3 -m venv` needs ensurepip, which minimal Ubuntu/Debian images ship as a
# separate package (python3-venv / python3.10-venv).  Prefer that package, but do not
# require root: a pip-less venv plus get-pip.py works anywhere with network access.
#
# Everything here announces itself.  The first version suppressed both the venv output
# and pip's, so a slow or stuck step looked exactly like a hang.
create_venv() {
  rm -rf "$VENV"
  echo "creating a virtualenv at $VENV ..."
  if python3 -m venv "$VENV" 2>/tmp/confdr-venv.err; then
    return 0
  fi
  minor="$(python3 -c 'import sys; print(sys.version_info[1])')"
  echo "  standard venv failed: $(tail -n 1 /tmp/confdr-venv.err 2>/dev/null)"
  echo "  preferred fix:   sudo apt install -y python3-venv    # or python3.${minor}-venv"
  echo "  falling back to: --without-pip + get-pip.py"
  rm -f /tmp/confdr-venv.err
  python3 -m venv --without-pip "$VENV" || return 1
  echo "  fetching pip from bootstrap.pypa.io ..."
  # --max-time so a blocked network fails instead of hanging forever with no output.
  curl -fsSL --max-time 120 --retry 2 --retry-delay 2 \
    https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py || {
      echo "  could not download get-pip.py -- no route to bootstrap.pypa.io?" >&2
      return 1
    }
  "$VENV/bin/python" /tmp/get-pip.py || return 1
  rm -f /tmp/get-pip.py
}

say "1/4  Python"
if [ -n "${CONFDR_PY:-}" ]; then
  PY="$CONFDR_PY"
  echo "using CONFDR_PY=$PY (no venv created)"
else
  command -v python3 >/dev/null || { echo "python3 not found" >&2; exit 1; }
  ver="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
  python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
    || { echo "Python >= 3.10 required, found $ver" >&2; exit 1; }
  echo "python3 $ver"
  # A venv left behind by an earlier failed attempt has bin/python but no pip, so
  # test for pip rather than for the interpreter.
  if [ -x "$PY" ] && "$PY" -m pip --version >/dev/null 2>&1; then
    echo "reusing the existing venv at $VENV"
  else
    create_venv
  fi
  echo "upgrading pip ..."
  "$PY" -m pip install --upgrade pip
fi
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || { echo "$PY is older than 3.10" >&2; exit 1; }

say "2/4  confdr (editable install)"
# Editable, and it has to be re-run whenever this directory moves: confdr resolves
# config/settings.yaml from the package's own location, so a stale install elsewhere
# would load a different settings.yaml without any error.
# Not --quiet: this pulls z3-solver, which is a large wheel and can take minutes.  Silent
# for minutes is indistinguishable from hung.
echo "installing confdr and its dependencies (PyYAML, z3-solver, jsonschema) ..."
"$PY" -m pip install -e .
"$PY" -c 'import confdr, pathlib; print("confdr ->", pathlib.Path(confdr.__file__).parent)'

say "3/4  KWOK binary runtime"
# The clusters here run with --runtime=binary: kwokctl starts etcd, kube-apiserver,
# kube-controller-manager, kube-scheduler and kwok-controller as local processes instead
# of pulling container images.  No Docker needed, and no registry access.
#
# Note: `kwokctl get binaries` (the way older releases fetched these) no longer exists in
# v0.8.0 -- it is `kwokctl get artifacts` now, and that only lists names, it does not
# download.  So the five runtime binaries come from the bundle or from dl.k8s.io, and are
# placed where `configure-kwok.py` points config/settings.yaml.
mkdir -p "$KWOK_DIR" "$HOME/.local/bin"

# Which file in the bundle provides `$1`, or nothing if it is not there.  Only
# kwok-controller differs: it IS the kwok binary (same sha256), so the bundle ships one
# 91 MB file for both names instead of two.  Always exits 0 -- callers test the output,
# and a non-zero status here would trip `set -e` inside a command substitution.
#
# Tests -f, not -x: an unzip or a tar that does not restore the exec bit would otherwise
# make the bundle look absent and send the script off to github.com.  The install below
# sets the mode explicitly, so the archive's permission bits do not matter.
bundle_file() {
  want="$1"
  if [ "$want" = kwok-controller ]; then
    want=kwok
  fi
  if [ -f "$KWOK_BUNDLE/$want" ]; then printf '%s' "$want"; fi
  return 0
}

# True when the bundle can supply `$1`.  Written as a function rather than a bare test so
# the skip-bundle switch is honoured in one place.
bundle_has() { [ -n "$(bundle_file "$1")" ]; }

# One of {etcd,kube-apiserver,kube-controller-manager,kube-scheduler,kwok-controller}
# -> where to fetch it and what it is called upstream.
runtime_url() {
  case "$1" in
    etcd) echo "https://github.com/etcd-io/etcd/releases/download/${ETCD_VERSION}/etcd-${ETCD_VERSION#v}-linux-amd64.tar.gz" ;;
    kube-apiserver|kube-controller-manager|kube-scheduler)
          echo "https://dl.k8s.io/release/${K8S_VERSION}/bin/linux/amd64/$1" ;;
    # kwok-controller IS the kwok binary; the reference host keeps a copy under both
    # names.  kwok v0.8.0 is 91 MB and sha256-identical to its kwok-controller.
    kwok-controller) echo "https://github.com/kubernetes-sigs/kwok/releases/download/${KWOK_VERSION}/kwok-linux-amd64" ;;
  esac
}

# kwokctl and kwok (the CLI) belong on PATH; the other five are runtime components.
for name in kwokctl kwok; do
  if command -v "$name" >/dev/null; then
    echo "$name already on PATH: $(command -v "$name")"
    continue
  fi
  if [ "${CONFDR_KWOK_SKIP_BUNDLE:-0}" != 1 ] && [ -n "$(bundle_file "$name")" ]; then
    echo "installing $name from the offline bundle"
    install -m 0755 "$KWOK_BUNDLE/$(bundle_file "$name")" "$HOME/.local/bin/$name"
  else
    url="https://github.com/kubernetes-sigs/kwok/releases/download/${KWOK_VERSION}/${name}-linux-amd64"
    echo "downloading $name ${KWOK_VERSION}"
    curl -fsSL --max-time 600 --retry 3 --retry-delay 2 "$url" -o "/tmp/$name" || {
      echo "could not download $url" >&2
      echo "github.com is blocked here -- ship tmp/kwok-linux-amd64/ alongside this tree," >&2
      echo "or set CONFDR_KWOK_BUNDLE to a directory holding kwokctl and kwok." >&2
      exit 1
    }
    install -m 0755 "/tmp/$name" "$HOME/.local/bin/$name"
    rm -f "/tmp/$name"
  fi
done

# kubectl: on PATH, needed by confdr's deployment evaluation and by the
# LLMSecConfig baseline's own harness.
if command -v kubectl >/dev/null; then
  echo "kubectl already on PATH: $(command -v kubectl)"
elif [ "${CONFDR_KWOK_SKIP_BUNDLE:-0}" != 1 ] && bundle_has kubectl; then
  echo "installing kubectl from the offline bundle"
  install -m 0755 "$KWOK_BUNDLE/kubectl" "$HOME/.local/bin/kubectl"
else
  echo "downloading kubectl ${K8S_VERSION}"
  curl -fsSL --max-time 600 --retry 3 --retry-delay 2 \
    "https://dl.k8s.io/release/${K8S_VERSION}/bin/linux/amd64/kubectl" -o /tmp/kubectl || {
      echo "could not download kubectl from dl.k8s.io" >&2; exit 1; }
  install -m 0755 /tmp/kubectl "$HOME/.local/bin/kubectl"
  rm -f /tmp/kubectl
fi

for name in etcd kube-apiserver kube-controller-manager kube-scheduler kwok-controller; do
  if [ -x "$KWOK_DIR/$name" ]; then
    echo "runtime binary present: $KWOK_DIR/$name"
    continue
  fi
  if [ "${CONFDR_KWOK_SKIP_BUNDLE:-0}" != 1 ] && [ -n "$(bundle_file "$name")" ]; then
    echo "installing $name from the offline bundle"
    install -m 0755 "$KWOK_BUNDLE/$(bundle_file "$name")" "$KWOK_DIR/$name"
    continue
  fi
  url="$(runtime_url "$name")"
  echo "downloading $name"
  if [ "$name" = etcd ]; then
    curl -fsSL --max-time 600 --retry 3 --retry-delay 2 "$url" -o /tmp/etcd.tgz || {
      echo "could not download $url" >&2; exit 1; }
    tar -xzf /tmp/etcd.tgz --strip-components=1 -C "$KWOK_DIR" "etcd-${ETCD_VERSION#v}-linux-amd64/etcd"
    rm -f /tmp/etcd.tgz
  else
    curl -fsSL --max-time 600 --retry 3 --retry-delay 2 "$url" -o "$KWOK_DIR/$name" || {
      echo "could not download $url" >&2; exit 1; }
  fi
  chmod +x "$KWOK_DIR/$name"
done
ls -1 "$KWOK_DIR"

say "4/4  doctor"
export PATH="$HOME/.local/bin:$PATH"
# config/settings.yaml ships with the author's binary paths; point them here so `confdr`
# works even when invoked without sourcing env.sh.
"$PY" tmp/baseline-adapt/configure-kwok.py --bin-dir "$KWOK_DIR" || true
"$PY" -m confdr doctor || true

cat <<EOF

=== next

  The KWOK binaries are in $KWOK_DIR.
  If that is not where you want them, move the directory and export
  CONFDR_KWOK_BIN=<new path> before every run.

  1. copy your relay credentials to tmp/baseline-keys.env  (template: .env.example)
  2. export CONFDR_KWOK_BIN=$KWOK_DIR
     export CONFDR_HTTP_PROXY=      # only if the server reaches the relay directly
  3. bash run-all.sh --stage smoke

  Walkthrough:           README.md
EOF
