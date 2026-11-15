# Azure OpenAI setup — Group Coordinator

Status: **NOT VERIFIED (live)**. Nothing below has been tried on our
subscription yet. The probe script (`scripts/azure_probe.py`) is the proof;
its output goes in "Probe results" at the bottom.

## Why Azure and what it costs

- Azure for Students: 100 USD credit per member, no credit card, renewable
  every year while a student. **Azure OpenAI is not free**: every call
  spends credit from the first one.
- We develop on **one** member's subscription. The others stay in reserve.
- The tutor asked to be told if the credits run out. If a model is missing
  or the credit is about to end: stop and tell Matteo. No other providers.

## Region

**Recommended: East US 2 (`eastus2`).** In the Microsoft table of Global
Standard deployments it is the only region listed with every model the code
uses: `gpt-realtime-2.1-mini`, `gpt-4o`, `gpt-5`, `gpt-4o-mini`,
`text-embedding-3-small` (read 15 Nov 2026, page "Foundry Models sold
directly by Azure — region availability", Global Standard, Americas tab).

- European regions (Sweden Central, West Europe, Italy North): the Europe
  tab of that page could not be read. **NOT VERIFIED.** Proof needed: open
  the page, Europe tab, and check that row `gpt-realtime-2.1-mini` has a tick.
- "Global Standard" means Microsoft may process the request in any region.
  The data notice in the web app must say messages go to Azure OpenAI.

## Deployments to create (names = model names)

The code passes the model name as `model=...`. On Azure, `model` is the
**deployment name**. Giving each deployment exactly the model's name means
the code does not change (INTEGRATION_MAP.md, section 6).

| Deployment name | Model | Needed for | Priority |
|---|---|---|---|
| `gpt-realtime-2.1-mini` | gpt-realtime-2.1-mini (2026-07-07) | main + intervention agent | **required** |
| `gpt-4o` | gpt-4o (2024-11-20) | summaries, proposal writer | **required** |
| `text-embedding-3-small` | text-embedding-3-small | `remember`, decision search | **required** |
| `gpt-5` | gpt-5 (2025-08-07) | daily summary (falls back to gpt-4o) | optional |
| `gpt-4o-mini` | gpt-4o-mini (2024-07-18) | web search (not used by the coordinator) | optional |

`gpt-4o-transcribe` is voice only: skip it.

## Steps in the portal

Menu names in the Azure portal change often. If a label differs, look for
the closest one and tell Claude, who updates this file.

1. Activate Azure for Students at azure.microsoft.com/free/students with the
   university account. Check that the subscription "Azure for Students"
   appears in the portal.
2. Portal → **Create a resource** → search **Azure OpenAI** → Create.
   - Subscription: Azure for Students
   - Resource group: new, `rg-group17-coordinator`
   - Region: **East US 2**
   - Name: e.g. `g17-coordinator` (becomes `https://g17-coordinator.openai.azure.com`)
   - Pricing tier: Standard S0
   - Network: all networks (we call it from our PCs)
3. Open the resource → **Go to Foundry portal** (or ai.azure.com) →
   **Deployments** → **Deploy model** → **Deploy base model**.
4. For each row of the table above: pick the model, deployment type
   **Global Standard**, deployment name **exactly as in the table**, keep the
   default rate limit (lower it if the portal says the quota is exceeded).
   If a model does not appear or the quota is 0: stop and write it in
   "Probe results".
5. Resource → **Keys and Endpoint**: copy `KEY 1` and the endpoint.
6. **Budget with alert**: portal → **Cost Management** → **Budgets** → Add.
   Scope: the Azure for Students subscription. Amount: 40 USD per month.
   Alerts at 50 % and 80 % (actual cost), email to the account owner.
7. Write the two values in `muca-server/.env` (never committed; `.env` is
   in `.gitignore`):

```
AZURE_OPENAI_ENDPOINT=https://<resource-name>.openai.azure.com
AZURE_OPENAI_API_KEY=<KEY 1>
```

Do not paste the key in chat.

## Environment variables (names only)

| Variable | Read by | Meaning |
|---|---|---|
| `AZURE_OPENAI_ENDPOINT` | probe now, `run_server.py` from M1 | resource URL, no path |
| `AZURE_OPENAI_API_KEY` | probe now, `run_server.py` from M1 | resource key |
| `OPENAI_API_KEY` | `run_server.py:131` today | unchanged default: if the Azure pair is not set, the server behaves exactly as today |

## How the code will reach Azure (M1, not built yet)

Verified facts from Microsoft docs (project instructions, section 5):
the v1 API works with the standard OpenAI client with
`base_url=https://<resource>.openai.azure.com/openai/v1/`; Realtime is at
`wss://<resource>.openai.azure.com/openai/v1/realtime?model=<deployment>`
and the Python SDK connects with `AsyncOpenAI(websocket_base_url=...)` and
`client.realtime.connect(model=<deployment>)`.

The change is limited to building the client in `run_server.py:131`: if
both Azure variables are set, build `AsyncOpenAI(api_key=..., base_url=...,
websocket_base_url=...)`; otherwise keep today's line. The probe script's
`build_client()` is exactly that code, tried first.

## Running the probe

```bash
cd muca-server
.venv/Scripts/python scripts/azure_probe.py          # Windows
python scripts/azure_probe.py --only realtime        # one probe
```

Expected cost: under 0.02 USD for the whole run.

## Probe results

_Not run yet._ Fill in: date, region, each line printed (outcome, latency,
tokens), cost seen in Cost Management the next day.
