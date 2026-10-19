# ConfDR Replication Package

This package accompanies the paper *ConfDR: Misconfiguration Detection and Repair for Pod Pending in Kubernetes Application Deployment*. It contains the data of the empirical study, PendBench, the implementation of ConfDR, and the baselines.

## Contents

| Folder | Content |
|---|---|
| `empirical-study/` | Candidate records, screening prompts, ChatGPT and manual screening results, and the open coding of the 159 cases (RQ1, RQ2) |
| `datasets/PendBench/` | PendBench (D1): 159 Pending cases and 32 manually repaired cases |
| `datasets/DeployFix/` | The DeployFix dataset (D2) in the same format: 24 Pending cases and 16 healthy cases |
| `ConfDR/` | Implementation (`src/confdr`), taxonomy and settings (`config/`), rule bundles (`rules/`; the experiments use `rules-v1.2`), tests, and experiment drivers (`drivers/`) |
| `baselines/` | SlsDetector, LLMSecConfig, and DeployFix: adapted code or upstream patch, and drivers |
| `common/` | Environment setup (`env.sh`, `setup.sh`) and the adapters that convert the datasets and score the baselines (`tmp/baseline-adapt/`) |
| `tools/` | `collect-metrics.py` (metrics of all methods) and `llm-usage.sh` (time and token usage) |

### Empirical study

The files follow the steps of the data collection and analysis:

- `T0-stackoverflow-tags.xlsx`: `kubernetes` and the tags that co-occur with it on Stack Overflow, with $n_0(t)$, $n_1(t)$, significance $\alpha(t)$, and relevance $\beta(t)$; `Selected` marks the tags used to retrieve $\mathcal{Q}_2$.
- `Q3-stackoverflow-candidates.xlsx`: the 407 candidate Stack Overflow questions ($\mathcal{Q}_3$), which have an accepted answer and at least 50 views.
- `I3-github-candidates.xlsx`: the 1,869 candidate GitHub issues ($\mathcal{I}_3$), which are closed and labeled as a bug or as scheduling, with the search query that returned each issue.
- `chatgpt-prompt-stackoverflow.md`, `chatgpt-prompt-github.md`: the prompts used for the ChatGPT screening of each source.
- `Q3-stackoverflow-chatgpt-screening.xlsx`, `I3-github-chatgpt-screening.xlsx`: the candidates with the label that ChatGPT assigned to each (`ChatGPTLabel`: `INCLUDE`, `UNCERTAIN`, or `EXCLUDE`).
- `manual-screening.xlsx`: the 413 records labeled `INCLUDE` or `UNCERTAIN` by ChatGPT, which two researchers reviewed manually; `Retained` marks the 159 records kept as cases (79 Stack Overflow questions and 80 GitHub issues).
- `open-coding.xlsx`: the open coding of the 159 cases. `Category` lists the PodPendingTaxonomy categories of each case. A case with two fault instances spans two rows.

No table redistributes post bodies; they are available at the links. Titles originally written in Chinese are translated and marked `[translated]`.

### Case format

Each case is a directory named after its source (`SO-<id>` or `GH-<owner>-<repo>-<number>`; repaired cases end with `--healthy`):

| Entry | Content |
|---|---|
| `apps/` | Workload manifests |
| `envs/` | Cluster objects (nodes, storage classes, persistent volumes, ...) |
| `case.json`, `collection.json`, `pod_info.json` | Cluster state, events, and Pod status collected in KWOK |
| `truth.json` | `is_pending` and `pending_labels` (PodPendingTaxonomy categories) |
| `deployfix/node-env.txt` | Node labels in the input format of DeployFix (D2 only; generated for D1 by `deployfix-gen-env.py`) |

`datasets/PendBench/dataset-manifest.json` records the SHA-256 of every case; ConfDR refuses to run on a modified copy of D1.

## Requirements

- Linux x86_64, bash, Python 3.10
- KWOK v0.8.0 with the Kubernetes v1.30.2 and etcd v3.6.10 binaries (installed by `setup.sh`)
- An OpenAI-compatible chat-completions endpoint
- A Rust toolchain (DeployFix only)

## Setup

The scripts expect one project root that holds the method, the adapters, and the frozen datasets. From the root of this package:

```bash
export CONFDR_ROOT=$HOME/confdr-run
mkdir -p "$CONFDR_ROOT/outputs" "$CONFDR_ROOT/sldstst-ConfDR-18d2307"
cp -a common/. ConfDR/. ConfDR/drivers/. "$CONFDR_ROOT"/
cp -a baselines/SlsDetector/drivers/. baselines/LLMSecConfig/drivers/. "$CONFDR_ROOT"/
cp -a datasets/PendBench "$CONFDR_ROOT/outputs/D1-collect-26-10-v3"
cp -a datasets/DeployFix "$CONFDR_ROOT/outputs/D2-collect-server-01"
cp -a baselines/SlsDetector/baseline-SlsDetector baselines/SlsDetector/config.py "$CONFDR_ROOT/sldstst-ConfDR-18d2307"/
cp -a baselines/LLMSecConfig/baseline-LLMSecConfig baselines/LLMSecConfig/repair "$CONFDR_ROOT/sldstst-ConfDR-18d2307"/
cp .env.example "$CONFDR_ROOT/tmp/baseline-keys.env"    # then fill in the endpoint, key, and model
cd "$CONFDR_ROOT" && bash setup.sh
export CONFDR_KWOK_BIN=$HOME/.kwok/local-bin
```

`setup.sh` creates `.venv`, installs ConfDR, and installs the KWOK binaries. Set `CONFDR_HTTP_PROXY` only if the endpoint must be reached through a proxy. `python -m confdr doctor` checks the installation without network requests, and `LIMIT=3 bash D1-method-GPT.sh` runs a smoke test on three cases.

DeployFix is built from its upstream repository:

```bash
git clone https://github.com/solecnugit/deployfix.git && cd deployfix
git checkout 145eaf9c1feb8cfb685fc5a88de9a30640f6dee2
git apply <package>/baselines/DeployFix/deployfix-local.patch
cargo build --release    # target/release/deployfix-cli
```

The patch makes the results deterministic: it disables parallel Z3 solving, iterates labels in a fixed order, keeps assertions inside their push/pop scope, and avoids a panic when a conflicting key has no entity.

## Reproducing the experiments

Run all commands from `$CONFDR_ROOT`. KWOK evaluations must run one at a time because they share one cluster.

| RQ | Experiment | Command |
|---|---|---|
| RQ1, RQ2 | Taxonomy and characteristics | `empirical-study/open-coding.xlsx` |
| RQ3 | ConfDR detection on D1 and D2 | `bash D1-method-GPT.sh`, `bash D2-method-GPT.sh` |
| RQ3 | SlsDetector | `bash run-all.sh --stage adapt`, then `bash run-d1-detect.sh --label gpt56terra` and `bash D2-detection-GPT.sh` |
| RQ3, RQ4 | DeployFix | `deployfix-gen-env.py`, `deployfix-d1-run.py`, and `deployfix-repair-run.py` in `baselines/DeployFix/drivers/` (usage in each file) |
| RQ4 | KWOK evaluation of the ConfDR repairs | `bash D1-verify-KWOK-serial.sh`, `bash D2-verify-KWOK.sh` |
| RQ4 | LLMSecConfig | `bash repair-v2.sh --col D1`, `bash repair-v2.sh --col D2` |
| RQ5 | w/o LG | `bash ablation-woLG-GPT.sh` (`ablation-woLG-all.sh` adds DeepSeek-V4.1-Flash) |
| RQ5 | w/o LS, w/o LLM-D | Detection views in `paper-metrics.json`: `single_round` is w/o LS, `all_conflicts` is w/o LLM-D, and `combined_detection` is ConfDR |
| RQ5, RQ6 | w/o IR and one, three, or five attempts | Prefixes of the five-attempt run, reported under `retry_budgets` in `paper-metrics.json`; `python -m confdr evaluate-deployment --run <run> --prefixes-only` deploys the prefix outputs |
| RQ6 | DeepSeek-V4.1-Flash | Set `CHEAP_MODEL` and `CONFDR_LLM_STRUCTURED_OUTPUTS=0`, then `bash run-d1-method.sh --label ds41flash` and `bash D2-all-stages-DeepSeek.sh` |

Each run writes `paper-metrics.json` and `paper-metrics.txt` into its output folder. `bash run-d1-metrics.sh --label <label>` rescores the D1 runs without model calls, `python tools/collect-metrics.py` summarizes all methods, and `STAGE=reports bash tools/llm-usage.sh` reports time and token usage.

## Run outputs

Each ConfDR run folder (`outputs/<run>/`) contains:

| File | Content |
|---|---|
| `experiment.json` | Configuration and progress of the run |
| `rules-used.json` | Rule bundle used by the run |
| `paper-metrics.json`, `paper-metrics.txt` | Detection, repair, and efficiency metrics |
| `deployment-summary.json` | Summary of the KWOK evaluation |
| `dataset/<case>/` | Inputs and outputs of each case: `result.json` (detection and repair), `deployment.json` (KWOK evaluation), and `res.txt` (solver output) |

A DeployFix repair counts as successful when its own recheck reports no conflict, not when the KWOK deployment passes.
