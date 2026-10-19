from typing import Any, Dict, Optional
import inspect
import os

from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable
from langchain_openai import ChatOpenAI

from repair.utils.parser import YAMLTextOutputParser

from ..prompts.templates import build_prompt_from_variables


def _normalize_base_url(api_base: Optional[str]) -> Optional[str]:
    """Ensure base URL includes '/v1' and has no trailing slash."""
    if not api_base:
        return None
    base = api_base.rstrip('/')
    if not base.endswith('/v1'):
        base = base + '/v1'
    return base


def create_model(
    model_endpoint: str,
    api_base: Optional[str] = None,
    api_key: Optional[str] = None,
    temperature: float = 0.3,
    request_timeout: Optional[int] = None,
    max_retries: Optional[int] = None,
    retry_delay: Optional[int] = None,
    timeout: Optional[int] = None,
):
    """Create an OpenAI-compatible chat model robust to library versions.

    - Normalizes base URL to include '/v1' for proxy compatibility.
    - Introspects ChatOpenAI signature to pick correct argument names
      across versions (api_key vs openai_api_key, base_url vs openai_api_base,
      timeout vs request_timeout).
    - Falls back to setting environment variables for legacy clients.
    - Accepts both `timeout` and `request_timeout` for cross-version compatibility.
    """
    kwargs: Dict[str, Any] = {"model": model_endpoint, "temperature": temperature}
    base = _normalize_base_url(api_base)

    # Ensure env vars for legacy code paths inside langchain/openai clients.
    if api_key and not os.getenv("OPENAI_API_KEY"):
        os.environ["OPENAI_API_KEY"] = api_key
    if base and not (os.getenv("OPENAI_API_BASE") or os.getenv("OPENAI_BASE_URL")):
        os.environ["OPENAI_API_BASE"] = base

    # Inspect ChatOpenAI signature to choose correct parameter names across versions.
    sig = inspect.signature(ChatOpenAI.__init__)
    params = sig.parameters

    if api_key:
        if "api_key" in params:
            kwargs["api_key"] = api_key
        elif "openai_api_key" in params:
            kwargs["openai_api_key"] = api_key

    if base:
        if "base_url" in params:
            kwargs["base_url"] = base
        elif "openai_api_base" in params:
            kwargs["openai_api_base"] = base

    # Determine effective timeout value from provided aliases or defaults.
    effective_timeout = (
        timeout if timeout is not None else request_timeout if request_timeout is not None else 60
    )

    # Apply timeout/max_retries based on available parameters in current version.
    if "timeout" in params:
        kwargs["timeout"] = effective_timeout
    elif "request_timeout" in params:
        kwargs["request_timeout"] = effective_timeout

    if max_retries is None:
        max_retries = 2
    if "max_retries" in params:
        kwargs["max_retries"] = max_retries

    # Note: retry_delay is accepted for CLI compatibility but not used by ChatOpenAI.

    return ChatOpenAI(**kwargs)


def build_chain(model: Runnable, variables: Dict[str, Any]) -> Runnable:
    prompt: ChatPromptTemplate = build_prompt_from_variables(variables)
    # Return prompt | model directly so caller can access AIMessage metadata (usage, etc.)
    chain = prompt | model
    return chain