# OpenLine

An AI agent for SDRs that drafts personalized cold outreach openers. Given a new prospect, it researches them across four data sources, picks the single strongest angle for a first message, and hands back a ready-to-edit draft — it never sends, schedules, or posts anything itself.

Built with [Strands Agents](https://github.com/strands-agents/sdk-python) + [LiteLLM](https://github.com/BerriAI/litellm), running on [OpenRouter](https://openrouter.ai)'s free model router.

## What it does

When a rep adds a new prospect, OpenLine calls four tools in a fixed order, then drafts a short, personalized opener grounded only in what those tools actually returned:

1. **`get_contact_profile`** — the prospect's role and profile context
2. **`get_company_info`** — firmographic context for their company
3. **`get_trigger_events`** — recent newsworthy company events (funding, leadership hires, product launches)
4. **`get_crm_history`** — prior contact history for this prospect

It then picks one angle — a recent trigger event (if dated within 60 days) or a role-based pain point — and returns:

```
ANGLE: [Trigger Event / Role-Based]
REASON: [one sentence on why this angle was chosen]
DRAFT: [3-4 sentence cold email opener]
```

If the prospect was already contacted in the last 14 days, it returns `SKIP: Recently contacted` instead of drafting anything.

## Current tool status

| Tool | Status | Source |
|---|---|---|
| `get_trigger_events` | **Real** | NewsAPI.org |
| `get_crm_history` | **Real** | HubSpot Contacts Search API |
| `get_contact_profile` | Stubbed | LinkedIn/people-data API is paywalled/partner-only |
| `get_company_info` | Stubbed | Crunchbase API requires a paid plan |

Stubbed tools return honest placeholder values (e.g. `"unavailable (stubbed tool, no real profile source connected)"`) rather than guessed data, so the model treats them as missing information instead of real signal.

## Human checkpoint

OpenLine has no send capability anywhere in the code — every tool call is read-only. Because of that, the highest-stakes moment isn't inside the agent, it's a human about to trust an incomplete draft. Before printing any draft, `run()` checks whether any tool used stubbed data. If so, it pauses:

```
CHECKPOINT — before showing this draft:
  2 of 4 data sources are stubbed, not real: get_contact_profile, get_company_info
  This draft was written without real data from those sources.
Show the draft anyway? [y/n]:
```

`y` reveals the draft. Anything else withholds it and recommends manual research instead. A fully-real run (once LinkedIn/Crunchbase are connected) skips this prompt entirely — it only fires when it's actually informative.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install 'strands-agents[litellm]' python-dotenv requests
```

Create a `.env` file:

```
OPENROUTER_API_KEY=sk-or-...
NEWSAPI_KEY=...
HUBSPOT_API_KEY=pat-na...
```

- **OpenRouter**: sign up at [openrouter.ai](https://openrouter.ai), create a key (no credit card required)
- **NewsAPI**: sign up at [newsapi.org](https://newsapi.org) for a free-tier key
- **HubSpot**: Settings → Integrations → Legacy Apps → Create legacy app → Private → add `crm.objects.contacts.read` scope → copy the access token from the Auth tab

## Run it

```bash
python agent.py
```

## Blast radius

- **Scope**: one prospect per run — no batch mode exists, so a bad draft can't fan out to multiple people.
- **Reversibility**: every tool call is read-only (search/GET requests only); nothing the agent does can modify or delete data in HubSpot or anywhere else. The HubSpot token is also scoped to read-only access.
- **Where the real risk sits**: entirely outside the agent — a human copying the draft into their own email client and hitting send. The checkpoint above is designed for exactly that handoff moment.
- **Known gap**: no persistent logging yet. Runs only print to stdout, so there's no audit trail of what was drafted, when, or for whom.
