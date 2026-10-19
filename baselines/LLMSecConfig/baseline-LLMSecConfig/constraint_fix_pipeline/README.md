# Constraint-Guided YAML Fix Pipeline (All Mode)

This module is a standalone, LLM-guided fix pipeline for Kubernetes YAML configurations. It integrates a constraint solver and historical fix examples to produce corrected YAML. The pipeline is simplified to a single "all" mode.

## Folder Structure

- `constraint_fix_pipeline/`
  - `runner.py`: Orchestrates data → constraints → prompt → LLM → outputs
  - `utils/io.py`: Load datasets and historical fixes; format for prompts
  - `fix/constraints.py`: Multi-file constraint analysis, conflicts, MUS, repair suggestions, RAG
  - `prompts/templates.py`: Prompt assembly (YAML + examples + constraints + instructions)
  - `llm/chain.py`: Chain builder with YAML-only output parser
  - `data/`: Input YAML dataset (multi-file and multi-document supported)
  - `result/`: Historical fix examples (JSON or paired YAML)
  - `outputs/`: Generated outputs (`fixed.yaml`, `run.json`)

## Installation

```bash
pip install langchain-core langchain-openai fire pyyaml
```

## Usage (All Mode Only)

Provide input YAMLs under `data/` and historical examples under `result/`, then run:

```bash
# python -m constraint_fix_pipeline.runner --data_dir constraint_fix_pipeline\dataset --result_dir constraint_fix_pipeline\result --output_dir constraint_fix_pipeline\outputs
python constraint_fix_pipeline/runner.py \
  --data_dir constraint_fix_pipeline/data \
  --result_dir constraint_fix_pipeline/result \
  --output_dir constraint_fix_pipeline/outputs \
  --api_base http://0.0.0.0:4000 \
  --api_key sk-1234 \
  --model_endpoint gpt-4o-mini
```

Outputs:
- `outputs/fixed.yaml`: Final corrected YAML (single code block)
- `outputs/run.json`: Summary (objects/conflicts/cores counts, etc.)

## What It Does

1. Load YAML dataset and historical fix examples.
2. Analyze constraints (multi-file relationships, conflicts, minimal unsat cores, suggestions);
   optionally attach relevant examples via RAG.
3. Assemble a prompt with input YAML + few-shot examples + constraints + instructions.
4. Invoke the LLM and parse only the YAML code block.
5. Save the corrected YAML and a run log.

## Historical Fix Examples Format

- JSON file(s):
  ```json
  {"before": "...", "after": "...", "desc": "optional"}
  ```
- Paired YAML files:
  - `example1_before.yaml`
  - `example1_after.yaml`

## Extending Constraints

Add or refine detectors in `fix/constraints.py` (e.g., HPA, PDB, NetworkPolicy). The pipeline will include their conflicts and suggestions automatically in the prompt.

## Notes

- The module is fully independent and does not modify the original project files.
- The pipeline runs only in all mode; raw mode has been removed.