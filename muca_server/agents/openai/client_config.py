"""
Builds the one OpenAI client the whole server shares (run_server.py).

Default, unchanged: OpenAI with OPENAI_API_KEY, exactly the old line
`AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])`.

Azure OpenAI (Group Coordinator, Azure for Students credits): if BOTH
AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY are set, the same client is
pointed at the Azure v1 API instead:
  REST:     https://<resource>.openai.azure.com/openai/v1/
  Realtime: wss://<resource>.openai.azure.com/openai/v1/realtime?model=<deployment>
On Azure `model=` is the deployment name; deployments are named exactly like
the models the code uses (docs/coordinator/AZURE_SETUP.md), so nothing else
in the code changes.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from openai import AsyncOpenAI

AZURE_ENDPOINT_ENV_VAR = "AZURE_OPENAI_ENDPOINT"
AZURE_KEY_ENV_VAR = "AZURE_OPENAI_API_KEY"


def uses_azure(env: Mapping[str, str] = os.environ) -> bool:
    """True when both Azure variables are set (and not empty)."""
    return bool(env.get(AZURE_ENDPOINT_ENV_VAR, "").strip()) and bool(
        env.get(AZURE_KEY_ENV_VAR, "").strip()
    )


def azure_urls(endpoint: str) -> tuple[str, str]:
    """(REST base URL, Realtime WebSocket base URL) for an Azure resource endpoint."""
    root = endpoint.strip().rstrip("/")
    if not root.startswith("https://"):
        raise ValueError(f"{AZURE_ENDPOINT_ENV_VAR} must start with https://")
    return root + "/openai/v1/", "wss://" + root[len("https://"):] + "/openai/v1"


def build_openai_client(env: Mapping[str, str] = os.environ) -> AsyncOpenAI:
    """The shared client: Azure if configured, otherwise plain OpenAI as before."""
    if uses_azure(env):
        base_url, websocket_base_url = azure_urls(env[AZURE_ENDPOINT_ENV_VAR])
        return AsyncOpenAI(
            api_key=env[AZURE_KEY_ENV_VAR].strip(),
            base_url=base_url,
            websocket_base_url=websocket_base_url,
        )
    return AsyncOpenAI(api_key=env["OPENAI_API_KEY"])
