"""Online structured model interface; no preset taxonomies, responses or case repairs."""

from __future__ import annotations

import http.client
import json
import os
import time
import urllib.error
import urllib.request

from confdr.shared.io import digest
from confdr.shared.progress import progress
from confdr.shared.schema import ModelOutputValidator


def object_schema(properties: dict) -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": properties,
    }


def diagnosis_schema(*, auxiliary: bool = False) -> dict:
    evidence = {
        "type": "array",
        "items": object_schema({"path": {"type": "string"}, "value_json": {"type": "string"}}),
    }
    return object_schema(
        {
            "status": {
                "type": "string",
                "enum": ["fault", "unknown"] if auxiliary else ["fault", "healthy", "unknown"],
            },
            "summary": {"type": "string"},
            "findings": {
                "type": "array",
                "items": object_schema(
                    {
                        "category_ids": {"type": "array", "items": {"type": "string"}},
                        "verification_scope": {
                            "type": "string",
                            "enum": ["control_plane", "runtime", "unknown"],
                        },
                        "summary": {"type": "string"},
                        "evidence": evidence,
                    }
                ),
            },
            "healthy_evidence": {**evidence, "maxItems": 0} if auxiliary else evidence,
            "confidence": {"type": "number"},
        }
    )


def repair_schema() -> dict:
    return object_schema(
        {
            "summary": {"type": "string"},
            "confidence": {"type": "number"},
            "fixed_yaml": {"type": "string"},
            "environment_change": object_schema(
                {
                    "needed": {"type": "boolean"},
                    "reason": {"type": "string"},
                    "impact": {"type": "string"},
                    "evidence": {
                        "type": "array",
                        "items": object_schema(
                            {
                                "path": {"type": "string"},
                                "value_json": {"type": "string"},
                            }
                        ),
                    },
                }
            ),
        }
    )


def advice_schema() -> dict:
    return object_schema(
        {
            "summary": {"type": "string"},
            "remaining_errors": {"type": "array", "items": {"type": "string"}},
            "failure_analysis": {"type": "string"},
            "alternatives": {
                "type": "array",
                "items": object_schema(
                    {
                        "action": {"type": "string"},
                        "rationale": {"type": "string"},
                        "prerequisites": {"type": "array", "items": {"type": "string"}},
                        "tradeoffs": {"type": "string"},
                        "validation": {"type": "string"},
                        "certainty": {"type": "string", "enum": ["supported", "needs_confirmation"]},
                    }
                ),
            },
            "missing_information": {"type": "array", "items": {"type": "string"}},
        }
    )


def health_evidence(case: dict, observations: dict) -> list[str]:
    """Positive health evidence must cover all target Pods; empty logs or Running alone do not imply health."""
    pods = case.get("WaitingDeployPods") or {}
    statuses = observations.get("pod_status") or {}
    if not pods:
        return []
    simulated = "kwok" in str(observations.get("collection", {}).get("scope", ""))
    evidence = []
    for name, pod in pods.items():
        status = statuses.get(name)
        if not isinstance(status, dict):
            return []
        if status.get("namespace", "default") != (pod.get("namespace") or "default"):
            return []
        if status.get("phase") == "Succeeded":
            evidence.append(f"pod_status/{name}: Succeeded")
            continue
        ready = any(
            c.get("type") == "Ready" and str(c.get("status")).lower() == "true"
            for c in status.get("conditions") or []
        )
        if simulated:
            if status.get("phase") != "Running" or not ready or not status.get("nodeName"):
                return []
            evidence.append(f"pod_status/{name}: simulated Running, Ready=True, scheduled")
            continue
        containers = status.get("containerStatuses") or []
        if status.get("phase") != "Running" or not ready or not containers:
            return []
        if not all(c.get("ready") is True and "running" in (c.get("state") or {}) for c in containers):
            return []
        evidence.append(f"pod_status/{name}: Running, Ready=True, containers ready")
    return evidence


def schema_unsupported(detail: str) -> bool:
    """Recognise an endpoint that refuses schema-guided decoding, as opposed to one that failed.

    Some OpenAI-compatible endpoints (and some relay models) reject ``response_format``
    ``json_schema`` on Chat Completions while accepting ``json_object``.  That is a capability
    answer, not a transient error, so the caller degrades instead of retrying it.
    """
    low = detail.lower()
    return "json_schema" in low and any(
        word in low for word in ("unavailable", "unsupported", "not supported", "not available")
    )


class LLM:
    def __init__(self, config: dict | None = None, *, backend: str | None = None, role: str = "main"):
        self.role = role
        cfg = config or {}
        self.backend = backend or os.getenv("CONFDR_LLM_BACKEND") or cfg.get("backend", "online")
        if self.backend != "online":
            raise ValueError("Only online mode is supported; configure a real model API")
        main_env_model = os.getenv("CONFDR_LLM_MODEL")
        self.model = main_env_model or cfg.get("model") or ""
        self.model_source = "CONFDR_LLM_MODEL" if main_env_model else "config.llm.model"
        self.base_url = (os.getenv("CONFDR_LLM_BASE_URL") or cfg.get("base_url") or "").rstrip("/")
        self.timeout = float(cfg.get("timeout_seconds", 120))
        self.max_tokens = int(cfg.get("max_completion_tokens", 16000))
        if role == "verifier":
            verifier_env_model = os.getenv("CONFDR_VERIFIER_MODEL")
            verifier_config_model = cfg.get("verifier_model")
            self.model = verifier_env_model or verifier_config_model or self.model
            if verifier_env_model:
                self.model_source = "CONFDR_VERIFIER_MODEL"
            elif verifier_config_model:
                self.model_source = "config.llm.verifier_model"
            self.base_url = (
                os.getenv("CONFDR_VERIFIER_BASE_URL") or cfg.get("verifier_base_url") or self.base_url
            ).rstrip("/")
        self.task_models = cfg.get("task_models", {})
        self.retries = int(cfg.get("http_retries", 2))
        # Cleared once an endpoint reports that it cannot decode against a JSON Schema.
        #
        # Also forced off up front for a relay that *silently ignores* `response_format:
        # json_schema`: some relays answer 200 but decode freely, returning its own shape
        # (the required property is simply absent), so `schema_unsupported()` -- which only
        # looks at HTTP error bodies -- can never fire and the fallback below is unreachable.
        # With structured_outputs off the schema is stated in the system prompt instead
        # (see `_chat_body`), which is the branch `schema_unsupported()` was meant to select.
        self.structured_outputs = os.getenv("CONFDR_LLM_STRUCTURED_OUTPUTS", "").strip().lower() not in (
            "0",
            "false",
            "no",
            "off",
        )

        self.calls: list[dict] = []

    def _chat_body(self, model: str, system: str, payload: dict, schema: dict, task: str) -> dict:
        """Build the request body for the current structured-output mode.

        When the endpoint cannot take ``json_schema``, the same schema is stated in the system
        prompt so the model still knows the required shape; the response is validated against
        that schema locally either way, so the guarantee does not depend on the API.
        """
        user = {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}
        if self.structured_outputs:
            return {
                "model": model,
                "messages": [{"role": "system", "content": system}, user],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": task,
                        "strict": True,
                        "schema": schema,
                    },
                },
                "max_completion_tokens": self.max_tokens,
                # "max_tokens": self.max_tokens,
            }
        return {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": system
                    + "\n\nReturn exactly one JSON object and no other text. It must validate "
                    "against this JSON Schema:\n"
                    + json.dumps(schema, ensure_ascii=False),
                },
                user,
            ],
            "response_format": {"type": "json_object"},
            "max_completion_tokens": self.max_tokens,
        }

    def _chat_request(self, body: dict, key: str):
        return urllib.request.Request(
            self.base_url + "/chat/completions",
            json.dumps(body).encode(),
            {"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
            method="POST",
        )

    def preflight(self) -> None:
        key = (os.getenv("CONFDR_VERIFIER_API_KEY") if self.role == "verifier" else None) or os.getenv(
            "CONFDR_LLM_API_KEY"
        )
        if not self.model or not self.base_url or not key:
            raise ValueError(
                "Set CONFDR_LLM_MODEL, CONFDR_LLM_BASE_URL and CONFDR_LLM_API_KEY (.env is not loaded automatically; source it first)"
            )
        if not self.base_url.startswith(("http://", "https://")):
            raise ValueError("Model BASE_URL must be an http(s) URL, usually ending in /v1")

    def complete(self, task: str, system: str, payload: dict, schema: dict) -> dict:
        started = time.monotonic()
        model = self.task_models.get(task) or self.model
        record = {
            "task": task,
            "backend": self.backend,
            "model": model,
            "role": self.role,
            "status": "error",
            "prompt_hash": digest({"system": system, "payload": payload}),
        }
        show_progress = task in {
            "plan_vocabulary",
            "independent_spec",
            "generate_rules",
            "backtranslate",
            "semantic_review",
        }
        label = f"{self.role}/{task}"
        if show_progress:
            progress(f"{label}: started")
        try:
            key = (os.getenv("CONFDR_VERIFIER_API_KEY") if self.role == "verifier" else None) or os.getenv(
                "CONFDR_LLM_API_KEY", ""
            )
            if not model or not self.base_url or not key:
                raise RuntimeError("Online mode requires CONFDR_LLM_MODEL, BASE_URL and API_KEY")
            body = self._chat_body(model, system, payload, schema, task)
            record["response_format"] = body["response_format"]["type"]
            request = self._chat_request(body, key)
            for retry in range(self.retries + 1):
                record["http_attempts"] = retry + 1
                if show_progress:
                    progress(
                        f"{label}: HTTP attempt {retry + 1}/{self.retries + 1}; socket timeout={self.timeout:g}s"
                    )
                try:
                    with urllib.request.urlopen(request, timeout=self.timeout) as response:
                        envelope = json.load(response)
                    break
                except urllib.error.HTTPError as exc:
                    detail = ""
                    try:
                        detail = exc.read().decode("utf-8", "replace")
                    except (OSError, ValueError):
                        detail = ""
                    if self.structured_outputs and schema_unsupported(detail):
                        # Not a transient failure: this endpoint will not take the schema. Switch
                        # to the prompt-stated schema and re-issue; later calls stay on that route.
                        self.structured_outputs = False
                        body = self._chat_body(model, system, payload, schema, task)
                        record["response_format"] = body["response_format"]["type"]
                        request = self._chat_request(body, key)
                        continue
                    if retry == self.retries or exc.code not in {429, 500, 502, 503, 504}:
                        raise RuntimeError(f"LLM HTTP {exc.code}") from None
                except (
                    urllib.error.URLError,
                    TimeoutError,
                    OSError,
                    # A response body that never arrives or arrives short is a transport failure,
                    # not a bad answer: the endpoint announced a Content-Length and then closed the
                    # connection (`http.client.IncompleteRead`), or the bytes stopped mid-JSON
                    # (`json.JSONDecodeError`).  Neither is an OSError, so before this they escaped
                    # the retry loop *and* the caller's per-case handler in runtime/experiment.py
                    # and killed the whole batch -- observed at case 183/191 on 2026-10-08.
                    http.client.HTTPException,
                    json.JSONDecodeError,
                ):
                    if retry == self.retries:
                        raise RuntimeError("LLM request failed or timed out") from None
                time.sleep(min(2**retry, 4))
            if show_progress:
                progress(f"{label}: response received; validating JSON Schema")
            record["http_attempts"] = retry + 1
            record["usage"] = envelope.get("usage", {})
            choice = envelope["choices"][0]
            record["finish_reason"] = choice.get("finish_reason")
            if choice["message"].get("refusal"):
                raise RuntimeError("LLM refused the request")
            if choice.get("finish_reason") != "stop":
                raise RuntimeError(
                    f"LLM returned incomplete output (finish_reason={choice.get('finish_reason')!r})"
                )
            result = json.loads(choice["message"]["content"])
            if not isinstance(result, dict):
                raise ValueError("LLM result must be a JSON object")
            errors = sorted(ModelOutputValidator(schema).iter_errors(result), key=lambda e: str(e.path))
            if errors:
                raise ValueError(
                    "LLM output failed JSON Schema validation at "
                    + str(list(errors[0].path))
                    + ": "
                    + errors[0].message[:400]
                )
            record["response_hash"] = digest(result)
            record["status"] = "ok"
            return result
        except RuntimeError as exc:
            record["error"] = str(exc)
            raise
        except (ValueError, TypeError, KeyError, IndexError, RecursionError) as exc:
            record["error"] = f"{type(exc).__name__}: {str(exc)[:600]}"
            raise RuntimeError(
                f"Invalid LLM response format: {type(exc).__name__}: {str(exc)[:600]}"
            ) from None
        finally:
            record["seconds"] = round(time.monotonic() - started, 4)
            self.calls.append(record)
            if show_progress:
                progress(f"{label}: {record['status']} ({record['seconds']:.1f}s)")


def evidence_ref(parts: list[str], value) -> dict:
    return {
        "path": "/" + "/".join(x.replace("~", "~0").replace("/", "~1") for x in parts),
        "value_json": json.dumps(value, ensure_ascii=False),
    }


def grounded_evidence(evidence: list, payload: dict) -> bool:
    """Citations must resolve to the current input and match each value exactly; this verifies provenance, not the root-cause judgment."""
    import re

    for item in evidence:
        if not isinstance(item, dict) or set(item) != {"path", "value_json"}:
            return False
        path = item["path"]
        if not isinstance(path, str) or not path.startswith("/") or re.search(r"~(?![01])", path):
            return False
        parts = [p.replace("~1", "/").replace("~0", "~") for p in path[1:].split("/")]
        if parts[0] not in {"case", "documents", "observations", "pod_info", "original_yaml"}:
            return False
        try:
            value = payload
            for part in parts:
                if isinstance(value, list):
                    if not re.fullmatch(r"0|[1-9][0-9]*", part):
                        return False
                    value = value[int(part)]
                else:
                    value = value[part]
            # A known empty inventory is observable evidence, unlike missing/null data.
            if value is None:
                return False
            if digest(value) != digest(json.loads(item["value_json"])):
                return False
        except (KeyError, TypeError, IndexError, ValueError):
            return False
    return True
