"""
Azure OpenAI probe for the Group Coordinator (Phase 0).

Calls each model once, the same way MUCA-Server calls it, and prints the
outcome, the latency and the tokens used. It is NOT production code: it is
run by hand, once, to check that the Azure for Students subscription can
really serve every model before we build anything on top of it.

What it checks (one call each, ~0.01 USD in total):
  1. Realtime, text only       -> like the main and intervention agents
  2. Chat Completions + schema -> like the summaries and the coordinator's
                                  cards (the models set in .env)
  3. Embeddings                -> like the "remember" strategy
  4. Responses + web_search    -> like the web searcher (optional on Azure)

Usage (from the muca-server folder, with the venv active):
    python scripts/azure_probe.py
    python scripts/azure_probe.py --only realtime

It reads two variables from the environment or from .env:
    AZURE_OPENAI_ENDPOINT   e.g. https://my-resource.openai.azure.com
    AZURE_OPENAI_API_KEY    the resource key (never printed)

Deployment names must be identical to the model names the code uses
(see docs/coordinator/AZURE_SETUP.md), so the model string we pass below is
both the model name in the code and the deployment name on Azure.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import time

from dotenv import load_dotenv
from openai import AsyncOpenAI

# The same names the code uses (docs/coordinator/INTEGRATION_MAP.md, section 6).
REALTIME_DEPLOYMENT = "gpt-realtime-2.1-mini"


def text_deployments() -> list[str]:
    """
    The text models actually configured (the same variables run_server.py
    and the coordinator settings read), else the code's defaults.
    """
    return sorted(
        {
            os.environ.get("MUCA_SUMMARY_MODEL") or "gpt-4o",
            os.environ.get("MUCA_DAILY_SUMMARY_MODEL") or "gpt-5",
            os.environ.get("MUCA_COORDINATOR_MODEL") or "gpt-4o",
        }
    )


EMBEDDING_DEPLOYMENT = "text-embedding-3-small"
WEB_SEARCH_DEPLOYMENT = "gpt-4o-mini"

# Each single call gives up after this many seconds.
CALL_TIMEOUT_SECONDS = 60


def build_client() -> AsyncOpenAI:
    """One client for REST and Realtime, pointed at the Azure v1 API."""
    endpoint = os.environ["AZURE_OPENAI_ENDPOINT"].rstrip("/")
    key = os.environ["AZURE_OPENAI_API_KEY"]
    # REST calls go to https://<resource>.openai.azure.com/openai/v1/ and
    # Realtime to wss://<resource>.openai.azure.com/openai/v1/realtime.
    websocket_base = endpoint.replace("https://", "wss://", 1) + "/openai/v1"
    return AsyncOpenAI(
        api_key=key,
        base_url=endpoint + "/openai/v1/",
        websocket_base_url=websocket_base,
    )


def report(name: str, ok: bool, started: float, detail: str) -> None:
    """Print one result line: outcome, latency in ms, then the details."""
    latency_ms = (time.perf_counter() - started) * 1000
    outcome = "OK  " if ok else "FAIL"
    print(f"[{outcome}] {name:<38} {latency_ms:7.0f} ms  {detail}")


async def probe_realtime(client: AsyncOpenAI) -> None:
    """Text-only Realtime turn, with the same session settings the agents use."""
    started = time.perf_counter()
    try:
        async with client.realtime.connect(model=REALTIME_DEPLOYMENT) as conn:
            await conn.session.update(
                session={
                    "type": "realtime",
                    "output_modalities": ["text"],
                    "instructions": "Answer with one short word.",
                }
            )
            await conn.conversation.item.create(
                item={
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Say ok."}],
                }
            )
            await conn.response.create()
            text = ""
            async for event in conn:
                if event.type == "response.output_text.delta":
                    text += event.delta
                elif event.type == "error":
                    raise RuntimeError(event.error.message)
                elif event.type == "response.done":
                    usage = event.response.usage
                    tokens = (
                        f"in={usage.input_tokens} out={usage.output_tokens}"
                        if usage
                        else "tokens=?"
                    )
                    report(
                        f"realtime {REALTIME_DEPLOYMENT}",
                        True,
                        started,
                        f"{tokens} reply={text.strip()!r}",
                    )
                    return
    except Exception as exc:  # we want every failure printed, not raised
        report(f"realtime {REALTIME_DEPLOYMENT}", False, started, repr(exc)[:200])


async def probe_summary(client: AsyncOpenAI, deployment: str) -> None:
    """Chat Completions with a strict JSON schema, like day_summarizer._call."""
    started = time.perf_counter()
    schema = {
        "name": "probe",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        },
    }
    try:
        response = await client.chat.completions.create(
            model=deployment,
            messages=[
                {"role": "system", "content": "Reply in JSON."},
                {"role": "user", "content": "Set ok to true."},
            ],
            response_format={"type": "json_schema", "json_schema": schema},
        )
        usage = response.usage
        report(
            f"chat+schema {deployment}",
            True,
            started,
            f"in={usage.prompt_tokens} out={usage.completion_tokens} "
            f"json={response.choices[0].message.content!r}",
        )
    except Exception as exc:
        report(f"chat+schema {deployment}", False, started, repr(exc)[:200])


async def probe_embedding(client: AsyncOpenAI) -> None:
    """One embedding, like OpenAIEmbedder."""
    started = time.perf_counter()
    try:
        response = await client.embeddings.create(
            model=EMBEDDING_DEPLOYMENT, input="espresso machine"
        )
        report(
            f"embedding {EMBEDDING_DEPLOYMENT}",
            True,
            started,
            f"in={response.usage.prompt_tokens} dims={len(response.data[0].embedding)}",
        )
    except Exception as exc:
        report(f"embedding {EMBEDDING_DEPLOYMENT}", False, started, repr(exc)[:200])


async def probe_web_search(client: AsyncOpenAI) -> None:
    """Responses API with the hosted web_search tool, like OpenAIWebSearcher.

    The Group Coordinator does not need web search. A failure here is only
    a note for the report, not a blocker.
    """
    started = time.perf_counter()
    try:
        response = await client.responses.create(
            model=WEB_SEARCH_DEPLOYMENT,
            tools=[{"type": "web_search"}],
            input="What is the capital of Sweden? One word.",
        )
        usage = response.usage
        report(
            f"web_search {WEB_SEARCH_DEPLOYMENT} (optional)",
            True,
            started,
            f"in={usage.input_tokens} out={usage.output_tokens}",
        )
    except Exception as exc:
        report(
            f"web_search {WEB_SEARCH_DEPLOYMENT} (optional)",
            False,
            started,
            repr(exc)[:200],
        )


async def main(only: str | None) -> None:
    load_dotenv()
    client = build_client()
    probes = {
        "realtime": lambda: probe_realtime(client),
        "summary": lambda: asyncio.gather(
            *(probe_summary(client, d) for d in text_deployments())
        ),
        "embedding": lambda: probe_embedding(client),
        "web_search": lambda: probe_web_search(client),
    }
    for name, run in probes.items():
        if only and name != only:
            continue
        await asyncio.wait_for(run(), timeout=CALL_TIMEOUT_SECONDS * 2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument(
        "--only",
        choices=["realtime", "summary", "embedding", "web_search"],
        help="run a single probe",
    )
    asyncio.run(main(parser.parse_args().only))
