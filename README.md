# OpenLine

An AI agent for SDRs that drafts personalized cold outreach openers. Given a new prospect, it researches them across four data sources, picks the single strongest angle for a first message, verifies its own draft against that research, and hands back a ready-to-edit opener — it never sends, schedules, or posts anything itself.

Built with [Strands Agents](https://github.com/strands-agents/sdk-python) + [LiteLLM](https://github.com/BerriAI/litellm), running on Claude Sonnet 5 via the Anthropic API, with an automatic fallback to [OpenRouter](https://openrouter.ai)'s free model router if the primary call fails.

## The problem

SDRs targeting B2B SaaS companies work 30-50 new prospects a day. True personalization — a real fact about the prospect's role, company, or a recent trigger event — takes 15-30 minutes of manual research per contact across LinkedIn, Crunchbase, and news sites, just to write a 3-sentence opener. At that volume, reps either burn the day on research or fall back to generic templates that get ignored. OpenLine turns that research pass into a single grounded draft in seconds, without inventing anything the research didn't actually turn up.

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

If CRM history confirms the prospect was already contacted in the last 14 days, or if a required check fails and prior contact can't be ruled out, it returns `SKIP: ...` instead of drafting anything — an unconfirmed CRM status is treated the same as a confirmed recent contact, never as "safe to proceed."

## Self-check pass

After drafting, OpenLine runs a second, independent model call comparing the draft against the actual values each tool returned this run — not what the model claims it used, the real logged values — and flags any claim that isn't traceable to that data. This exists because the checkpoint used to only tell a rep *which sources were stubbed*, not whether the draft itself stayed honest about that gap; a draft with real data can still contain an invented detail, and a draft built on stubbed data can still be completely honest about its limits.

The self-check fails closed: if the verification call itself errors, times out, or returns something unparseable, the result is `UNVERIFIED`, never `CLEAN` by default.

## Current tool status

| Tool | Status | Source |
|---|---|---|
| `get_trigger_events` | **Real** | NewsAPI.org |
| `get_crm_history` | **Real** | HubSpot Contacts Search API |
| `get_contact_profile` | Stubbed | LinkedIn/people-data API is paywalled/partner-only |
| `get_company_info` | Stubbed | Crunchbase API requires a paid plan |

Stubbed tools return honest placeholder values (e.g. `"unavailable (stubbed tool, no real profile source connected)"`) rather than guessed data, so the model treats them as missing information instead of real signal. Both real tools are wrapped in error handling: a network failure is logged as `"failed"`, distinct from a tool that ran successfully and genuinely found nothing — those are different facts, and a failed CRM check is never treated as equivalent to "no prior contact found."

## Human checkpoint

OpenLine has no send capability anywhere in the code — every tool call is read-only. Because of that, the highest-stakes moment isn't inside the agent, it's a human about to trust an incomplete or unverified draft. Before printing any draft, `run()` checks four independent things, and pauses if any of them fire:

- **Stubbed data** — the draft was written using one or more placeholder tools.
- **Tool failure** — a real tool call (news search or CRM lookup) failed outright, rather than returning a clean empty result.
- **Unfilled placeholders** — a deterministic check for leftover template syntax like `[First Name]` that the model was told never to leave in, in case it does anyway.
- **Self-check flag** — the verification pass found a claim in the draft that isn't traceable to real tool data.

```
CHECKPOINT — before showing this draft:
  2 of 4 data sources are stubbed, not real: get_contact_profile, get_company_info
  This draft was written without real data from those sources.
Show the draft anyway? (y/n):
```

`y` reveals the draft. Anything else withholds it and recommends manual research instead. A fully-real, fully-verified run skips this prompt entirely — it only fires when it's actually informative.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install 'strands-agents[litellm]' python-dotenv requests rich
```

Create a `.env` file:

```
ANTHROPIC_API_KEY=sk-ant-...
OPENROUTER_API_KEY=sk-or-...
NEWSAPI_KEY=...
HUBSPOT_API_KEY=pat-na...
```

- **Anthropic** (primary model): create a key at [console.anthropic.com](https://console.anthropic.com)
- **OpenRouter** (fallback model, used only if the Anthropic call fails): sign up at [openrouter.ai](https://openrouter.ai), create a key (no credit card required)
- **NewsAPI**: sign up at [newsapi.org](https://newsapi.org) for a free-tier key
- **HubSpot**: Settings → Integrations → Legacy Apps → Create legacy app → Private → add `crm.objects.contacts.read` scope → copy the access token from the Auth tab

## Run it

```bash
python agent.py
```

Every run is appended to `outreach_log.txt` (timestamp, prospect input, which model provider actually generated the draft, which data sources were stubbed or failed, the self-check outcome, whether the draft was shown to the rep or withheld at the checkpoint, and the full output). This file isn't committed to git — it contains real prospect data.

## Blast radius

- **Scope**: one prospect per run — no batch mode exists, so a bad draft can't fan out to multiple people.
- **Reversibility**: every tool call is read-only (search/GET requests only); nothing the agent does can modify or delete data in HubSpot or anywhere else. The HubSpot token is also scoped to read-only access.
- **Where the real risk sits**: entirely outside the agent — a human copying the draft into their own email client and hitting send. The checkpoint above is designed for exactly that handoff moment.
- **Audit trail**: `outreach_log.txt` records every run, so there's a local record of what was drafted, for whom, which model provider produced it, whether self-check flagged anything, and whether it was shown or withheld.
