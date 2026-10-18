"""LLM factory for all agents (pydantic-ai models), selected via LLM_PROVIDER.

  openai   OPENAI_API_KEY, OPENAI_MODEL
  litellm  LITELLM_API_BASE, LITELLM_KEY, LITELLM_DEFAULT_MODEL   (lecture proxy)
  azure    AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_KEY, AZURE_OPENAI_DEPLOYMENT, AZURE_OPENAI_API_VERSION
  ollama   OLLAMA_BASE_URL, OLLAMA_MODEL
  mock     scripted offline model (pydantic-ai FunctionModel) - only for testing the wiring
"""
from __future__ import annotations

import os
from typing import Callable

from pydantic_ai.settings import ModelSettings


def _req(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(f"Environment variable {name} is not set (see .env.example)")
    return val


def provider_name() -> str:
    return os.getenv("LLM_PROVIDER", "openai").strip().lower()


def build_model(mock_function: Callable | None = None):
    provider = provider_name()

    if provider == "mock":
        from pydantic_ai.models.function import FunctionModel
        if mock_function is None:
            raise RuntimeError("this agent has no mock implementation")
        return FunctionModel(mock_function, model_name="mock")

    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    if provider == "openai":
        return OpenAIChatModel(os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
                               provider=OpenAIProvider(api_key=_req("OPENAI_API_KEY")))
    if provider == "litellm":
        return OpenAIChatModel(os.getenv("LITELLM_DEFAULT_MODEL", "gpt-4o-mini"),
                               provider=OpenAIProvider(base_url=_req("LITELLM_API_BASE"), api_key=_req("LITELLM_KEY")))
    if provider == "azure":
        from pydantic_ai.providers.azure import AzureProvider
        return OpenAIChatModel(_req("AZURE_OPENAI_DEPLOYMENT"), provider=AzureProvider(
            azure_endpoint=_req("AZURE_OPENAI_ENDPOINT"),
            api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21"),
            api_key=_req("AZURE_OPENAI_KEY"),
        ))
    if provider == "ollama":
        return OpenAIChatModel(os.getenv("OLLAMA_MODEL", "qwen2.5:7b"), provider=OpenAIProvider(
            base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1"), api_key="ollama"))
    raise ValueError(f"Unknown LLM_PROVIDER '{provider}' (openai | litellm | azure | ollama | mock)")


def model_settings() -> ModelSettings | None:
    temp = os.getenv("LLM_TEMPERATURE", "").strip()
    return ModelSettings(temperature=float(temp)) if temp else None


def model_label() -> str:
    p = provider_name()
    return {
        "openai": os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        "litellm": os.getenv("LITELLM_DEFAULT_MODEL", "gpt-4o-mini"),
        "azure": os.getenv("AZURE_OPENAI_DEPLOYMENT", "?"),
        "ollama": os.getenv("OLLAMA_MODEL", "qwen2.5:7b"),
        "mock": "scripted-mock",
    }.get(p, "?") + f" via {p}"
