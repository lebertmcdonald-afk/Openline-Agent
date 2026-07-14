import os
import re
import sys
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from strands import Agent, tool
from strands.models.litellm import LiteLLMModel
from strands.types.exceptions import MaxTokensReachedException

console = Console()

load_dotenv()

# Checkpoint support: each tool appends (tool_name, "real" | "stub" | "failed",
# return_value) here as it runs. The status drives the checkpoint (below);
# the return_value is ground truth for the self-check pass to compare a
# draft's claims against, rather than trusting the model's own account of
# what a tool returned. Cleared at the start of each run().
tool_data_log: list[tuple[str, str, object]] = []

# Every run gets recorded here — prospect input, which sources were real vs
# stubbed, and whether the output was actually shown to the rep or withheld
# at the checkpoint. Without this, a run's only record was its terminal
# output, gone the moment the window closed.
LOG_PATH = "outreach_log.txt"

# Detects unfilled template placeholders like [First Name] or [Company Name]
# left in a draft. This is a deterministic check on purpose: the system
# prompt already tells the model not to do this and it still slips through
# on some runs, so this can't be a "trust the model to comply" fix — it has
# to catch the pattern in code regardless of what the model was told.
PLACEHOLDER_PATTERN = re.compile(r"\[[^\[\]]+\]")


def log_run(
    prospect_input: str,
    output: str,
    stub_tools: list[str],
    shown: bool,
    self_check_result: dict | None = None,
    provider_used: str | None = None,
) -> None:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    with open(LOG_PATH, "a") as f:
        f.write(f"=== {timestamp} ===\n")
        f.write(f"Prospect input: {prospect_input}\n")
        if provider_used:
            f.write(f"Model provider: {provider_used}\n")
        f.write(f"Stub data sources: {', '.join(stub_tools) if stub_tools else 'none'}\n")
        if self_check_result is None:
            # No draft was ever produced this run (e.g. ran out of budget),
            # so there was nothing for self_check to verify — distinct from
            # "SKIPPED" (a SKIP result exists) or a real CLEAN/FLAGGED/
            # UNVERIFIED outcome.
            f.write("Self-check: not run (no draft produced)\n")
        else:
            f.write(f"Self-check status: {self_check_result['status']}\n")
            if self_check_result["unsupported_claims"]:
                f.write(f"Self-check flagged claims: {'; '.join(self_check_result['unsupported_claims'])}\n")
        f.write(f"Shown to rep: {shown}\n")
        f.write(f"Output:\n{output}\n\n")

SYSTEM_PROMPT = """You are OpenLine, an outbound research and drafting assistant for an SDR's cold outreach.

Tool sequence: When a rep adds a new prospect, call tools in this exact order: (1) get_contact_profile, (2) get_company_info, (3) get_trigger_events, (4) get_crm_history. Do not skip a tool or change the order.

Angle selection: After all four tools have returned, choose the single most relevant angle: prioritize a trigger event dated within the last 60 days if one exists; otherwise fall back to a role-specific pain point. Never combine more than one angle.

Output format: Return your response in exactly this structure:
ANGLE: [Trigger Event / Role-Based]
REASON: [one sentence on why this angle was chosen]
DRAFT: [3-4 sentence cold email opener]

Constraints: Never send, schedule, or post anything — only return the draft for the rep to review. Never invent a detail that a tool did not return. If a tool fails or returns no data, state that plainly in the REASON line and draft using only confirmed information.

No placeholder syntax: The DRAFT must never contain bracketed template placeholders like [First Name], [Company Name], [Product/Service], or any other fill-in-the-blank text — that is not "drafting using only confirmed information," it's an unfilled template, and it's worse than a short draft because it looks finished when it isn't. If the contact's real name isn't returned by a tool, either use the identifier that was actually given in the prospect input (e.g. "Hi Test Pursuit team" or "Hi there") or write a shorter, more general opener — never leave a blank for the rep to fill in. Do not add meta-commentary addressed to the rep (e.g. "adjust this before sending") — the draft is the deliverable, not a template.

Trigger event framing: A trigger event's description is a fact, not a verdict. Restate only what get_trigger_events actually returned — do not characterize the event as good or bad news for the company, congratulate them, or assume what they did in response (e.g. do not say a company "pioneered" a solution, "handled" a situation well, or achieved an outcome that the event description does not state). If the event's meaning is ambiguous, sensitive, or could reasonably be read as negative for the company (e.g. security incidents, controversy, layoffs), do not spin it positively — either reference it neutrally as a fact you noticed, or fall back to a role-based angle instead.

This rule isn't limited to good/bad spin — it also covers invented context. Do not manufacture surrounding narrative the event description doesn't state: no invented effort, complexity, or personal impact (e.g. do not say "I imagine there's a lot happening behind the scenes" or "how you're supporting your team through this" — the event data says what happened, not what it's like internally to deal with it). This applies to role-based openers too, not just trigger events — do not invent what a role is "juggling" or "dealing with" beyond a generic, widely-true pain point. State the known fact and move directly to the ask. If a sentence isn't a restatement of the tool data, a widely-true generic pain point, or a question to the prospect, cut it.

This applies to questions too, not just statements — a question can smuggle in the same invented assumption its premise implies. A question is fine when it asks the prospect to supply information the agent doesn't have (e.g. "what's your team currently focused on?", "would you be open to a quick call?"). A question is not fine when its own premise assumes something specific and unconfirmed is already happening (e.g. "what your team is working through right now" assumes there is a specific thing being worked through — the same invented-context problem, just in question form). Test before writing a closing question: restate it as its most literal implied assertion. If that assertion would violate this rule, the question violates it too. This does not mean avoiding questions — every draft should still end with a genuine ask; it means the ask must not presuppose an unconfirmed fact.

Termination condition: Do not produce a DRAFT until all four tools have been called and returned a result (or an explicit failure). If get_crm_history shows this prospect was already contacted in the last 14 days, stop and return SKIP: Recently contacted instead of a draft.

Tool failure vs. empty result: these are different facts. get_trigger_events returning an empty list means it checked and found nothing — state that. get_trigger_events returning None, or get_crm_history returning prior_contact=None, means the check itself failed — a connection error, not a confirmed absence. If get_crm_history's check failed, you cannot confirm this prospect wasn't recently contacted — say so plainly in the REASON line and note that CRM status is unconfirmed rather than assuming it's safe to proceed as if prior_contact were False."""


# --- STUBBED TOOLS ---
# These return mock data. get_trigger_events (NewsAPI.org) and get_crm_history
# (HubSpot) below are wired to real APIs; these two stay stubbed until real
# LinkedIn/Crunchbase access is available.

@tool
def get_contact_profile(contact_id: str) -> dict:
    """Get role and profile info for a prospect.

    STUBBED: real LinkedIn/people-data API access is paywalled/partner-only,
    so this echoes back the queried contact_id with placeholder role data
    instead of real profile details. Do not treat title/tenure/posts below
    as real — flag them as unavailable rather than drafting around them.

    Args:
        contact_id: Identifier for the prospect (e.g. name or LinkedIn handle).

    Returns:
        A dict with job title, tenure, and recent posts.
    """
    value = {
        "name": contact_id,
        "title": "unavailable (stubbed tool, no real profile source connected)",
        "tenure_months": None,
        "recent_posts": [],
    }
    tool_data_log.append(("get_contact_profile", "stub", value))
    return value


@tool
def get_company_info(company_name: str) -> dict:
    """Get firmographic data for a prospect's company.

    STUBBED: real Crunchbase access requires a paid plan, so this returns
    placeholder "unavailable" values rather than guessed firmographics.
    Do not treat industry/size/funding below as real for any company.

    Args:
        company_name: Name of the company to look up.

    Returns:
        A dict with company size, industry, funding stage, and funding history.
    """
    value = {
        "name": company_name,
        "industry": "unavailable (stubbed tool, no real firmographic source connected)",
        "size": "unavailable",
        "funding_stage": "unavailable",
        "funding_history": [],
    }
    tool_data_log.append(("get_company_info", "stub", value))
    return value


@tool
def get_trigger_events(company_name: str) -> list[dict] | None:
    """Check for recent newsworthy events tied to a company.

    Calls the NewsAPI.org /v2/everything endpoint for real, current news
    about the given company (funding, leadership hires, product launches).

    Args:
        company_name: Name of the company to search news for.

    Returns:
        A list of dicts with event description, source, and publish date.
        Empty list if no articles are found (checked successfully, nothing
        there). None if the check itself could not be completed — a
        connection failure or timeout — which is a different fact from
        "no events exist" and must not be treated the same way.
    """
    try:
        response = requests.get(
            "https://newsapi.org/v2/everything",
            params={
                # qInTitle (not q) restricts matches to the article title, which
                # avoids false positives from the company name appearing as an
                # unrelated common word in article body text.
                "qInTitle": company_name,
                "sortBy": "publishedAt",
                "language": "en",
                "pageSize": 5,
                "apiKey": os.environ["NEWSAPI_KEY"],
            },
            timeout=10,
        )
    except requests.exceptions.RequestException:
        tool_data_log.append(("get_trigger_events", "failed", None))
        return None

    if not response.ok:
        tool_data_log.append(("get_trigger_events", "real", []))
        return []

    articles = response.json().get("articles", [])

    value = [
        {
            "description": article["title"],
            "source": article["source"]["name"],
            "date": article["publishedAt"][:10],
        }
        for article in articles
    ]
    tool_data_log.append(("get_trigger_events", "real", value))
    return value


@tool
def get_crm_history(contact_email: str) -> dict:
    """Check for prior contact or CRM notes for a prospect in HubSpot.

    Calls the HubSpot Contacts Search API to look up a prospect by email.
    Prospect must be identified by email, since that's what HubSpot search
    keys on — do not invent an email if one wasn't provided for this prospect.

    Args:
        contact_email: Prospect's email address to look up.

    Returns:
        A dict with prior_contact (bool or None), last_touch_date,
        days_since_last_touch (int, or None if never contacted), deal_stage,
        and notes. prior_contact=False means the contact was checked and
        genuinely not found in HubSpot. prior_contact=None means the check
        itself failed (connection error/timeout) — that is NOT the same as
        "not found," and must never be treated as confirmation it's safe to
        draft. Never assume no prior contact when this check couldn't run.
    """
    headers = {
        "Authorization": f"Bearer {os.environ['HUBSPOT_API_KEY']}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(
            "https://api.hubapi.com/crm/v3/objects/contacts/search",
            headers=headers,
            json={
                "filterGroups": [
                    {"filters": [{"propertyName": "email", "operator": "EQ", "value": contact_email}]}
                ],
                "properties": ["email", "firstname", "lastname", "notes_last_updated", "hs_lead_status"],
            },
            timeout=10,
        )
    except requests.exceptions.RequestException:
        value = {
            "prior_contact": None,
            "last_touch_date": None,
            "days_since_last_touch": None,
            "deal_stage": "unknown — CRM check failed",
            "notes": "CRM history could not be verified due to a connection failure. Do not assume no prior contact.",
        }
        tool_data_log.append(("get_crm_history", "failed", value))
        return value

    if not response.ok or not response.json().get("results"):
        value = {
            "prior_contact": False,
            "last_touch_date": None,
            "days_since_last_touch": None,
            "deal_stage": "not in pipeline",
            "notes": "No CRM record found.",
        }
        tool_data_log.append(("get_crm_history", "real", value))
        return value

    props = response.json()["results"][0]["properties"]
    last_touch_raw = props.get("notes_last_updated")

    days_since_last_touch = None
    if last_touch_raw:
        last_touch_date = datetime.fromisoformat(last_touch_raw.replace("Z", "+00:00"))
        days_since_last_touch = (datetime.now(timezone.utc) - last_touch_date).days

    value = {
        "prior_contact": bool(last_touch_raw),
        "last_touch_date": last_touch_raw[:10] if last_touch_raw else None,
        "days_since_last_touch": days_since_last_touch,
        "deal_stage": props.get("hs_lead_status") or "unknown",
        "notes": f"{props.get('firstname', '')} {props.get('lastname', '')}".strip(),
    }
    tool_data_log.append(("get_crm_history", "real", value))
    return value


def parse_self_check(response_text: str) -> dict:
    """Parse a self-check model response into a status and claim list.

    Expects:
        STATUS: [CLEAN / FLAGGED]
        UNSUPPORTED_CLAIMS:
        - claim one
        - claim two
        (or "none" on its own line)

    Fails closed: any response that doesn't clearly parse to CLEAN or
    FLAGGED comes back as UNVERIFIED, never CLEAN by default. A self-check
    we can't parse is not the same as a self-check that passed.
    """
    status_match = re.search(r"STATUS:\s*(CLEAN|FLAGGED)", response_text, re.IGNORECASE)

    if not status_match:
        return {
            "status": "UNVERIFIED",
            "unsupported_claims": ["self-check response did not contain a parseable STATUS line"],
        }

    status = status_match.group(1).upper()

    # Line-based, not one greedy regex to end-of-string: a real model
    # response often has conversational text before/after the structured
    # fields, and a greedy capture would swallow trailing prose as if it
    # were a claim. Only the UNSUPPORTED_CLAIMS line itself, plus
    # immediately-following bullet-style continuation lines, count.
    #
    # Deliberately NOT comma-splitting the first-line remainder: a real
    # claim's own explanation can legitimately contain commas (e.g. "no data
    # supports X, Y, or Z"), and splitting on every comma shreds one claim
    # into several meaningless fragments. If the model puts non-bracketed,
    # non-"none" text directly after the field label, that whole remainder
    # is treated as a single claim — multiple claims are only recognized
    # via separate bullet lines, which is unambiguous.
    claims: list[str] = []
    lines = response_text.splitlines()
    in_claims_block = False
    for line in lines:
        stripped = line.strip()
        field_match = re.match(r"UNSUPPORTED_CLAIMS:\s*(.*)", stripped, re.IGNORECASE)
        if field_match:
            in_claims_block = True
            remainder = field_match.group(1).strip().strip("[]").strip()
            if remainder and remainder.lower() not in ("none", "none.", "n/a"):
                claims.append(remainder)
            continue

        if in_claims_block:
            bullet_match = re.match(r"^(?:[-*]|\d+[.)])\s+(.*)", stripped)
            if bullet_match:
                claim = bullet_match.group(1).strip()
                if claim and claim.lower() not in ("none", "none.", "n/a"):
                    claims.append(claim)
            else:
                # A blank line or any non-bullet line ends the claims block —
                # whatever follows is not part of the list, even if it isn't
                # a recognized field of its own (e.g. trailing prose).
                in_claims_block = False

    if status == "FLAGGED" and not claims:
        # A FLAGGED status with no actual claims listed is itself a
        # malformed response — don't report "flagged" with nothing to show
        # the rep, since that's not actionable and looks like a bug.
        return {
            "status": "UNVERIFIED",
            "unsupported_claims": ["self-check reported FLAGGED but named no specific claim"],
        }

    return {"status": status, "unsupported_claims": claims}


def invoke_with_recovery(agent: Agent, prompt: str) -> str | None:
    """Call the agent, resuming once if it hits the max-tokens limit.

    MaxTokensReachedException leaves the partial message in the agent's own
    history, so a bare retry with no new prompt continues that same
    generation instead of restarting it. Bounded to one resume attempt —
    an agent still running out of tokens after that isn't going to fix
    itself on a third try. Returns None if a complete result still isn't
    available; callers must treat that as "no draft," never print a
    half-finished one as if it were done.
    """
    try:
        return str(agent(prompt))
    except MaxTokensReachedException:
        try:
            return str(agent())
        except MaxTokensReachedException:
            return None


SELF_CHECK_PROMPT = """You are verifying a drafted cold email against the raw data it was built from. Do not judge tone, quality, or persuasiveness — only factual grounding.

For each factual claim in the DRAFT, confirm it is directly supported by TOOL DATA below. A claim is unsupported if it states something more specific or more definite than what TOOL DATA actually contains — including invented outcomes, invented sentiment, or invented interpretation of an event, not just invented names or numbers.

Respond in exactly this format:
STATUS: [CLEAN / FLAGGED]
UNSUPPORTED_CLAIMS:
- [first unsupported claim, as one line]
- [second unsupported claim, if any]
(or a single line "none" if there are no unsupported claims)

Put exactly one claim per line, each starting with "- ". Never put multiple claims on the same line separated by commas — a claim's own explanation may itself contain commas, and that would make it unparseable."""


def self_check(model: LiteLLMModel, draft: str, tool_outputs: dict[str, object]) -> dict:
    """Ask the model to verify the draft's claims against real tool outputs.

    A second, separate call — a fresh agent with no tools attached, since
    this only needs to compare text against values already in hand, not
    fetch anything new. Fails closed: any error or unparseable response
    here returns UNVERIFIED, never CLEAN — a self-check we can't complete
    is not the same as a self-check that passed.
    """
    verifier = Agent(model=model, tools=[], system_prompt=SELF_CHECK_PROMPT, callback_handler=None)
    prompt = f"TOOL DATA:\n{tool_outputs}\n\nDRAFT:\n{draft}"

    try:
        response = invoke_with_recovery(verifier, prompt)
    except Exception:
        response = None

    if response is None:
        return {
            "status": "UNVERIFIED",
            "unsupported_claims": ["self-check call failed or ran out of budget"],
        }

    return parse_self_check(response)


def render_draft(result: str) -> None:
    """Print a finished ANGLE/REASON/DRAFT (or SKIP) result with color.

    Falls back to a plain panel if the text doesn't match the expected
    shape — display is cosmetic, so a parsing miss here should never hide
    or alter the actual output, just show it less prettily.

    Builds every piece as a rich Text object rather than an f-string passed
    to Panel/console.print. Draft content can legitimately contain literal
    square brackets (that's the exact placeholder bug fixed earlier this
    session — "[First Name]" is real output this function has to be able to
    display) and rich parses bracketed text in plain strings as markup. A
    Text object is never re-parsed as markup, so a bracket in the draft
    can't be misread as a style tag and crash the one thing meant to make
    the demo look better.
    """
    text = result.strip()

    if text.upper().startswith("SKIP"):
        console.print(Panel(Text(text), title="SKIPPED", border_style="yellow", title_align="left"))
        return

    angle_match = re.search(r"ANGLE:\s*(.+)", text, re.IGNORECASE)
    reason_match = re.search(r"REASON:\s*(.+?)(?:\n\s*\n|\nDRAFT:|\Z)", text, re.IGNORECASE | re.DOTALL)
    draft_match = re.search(r"DRAFT:\s*(.+)", text, re.IGNORECASE | re.DOTALL)

    if not (angle_match and reason_match and draft_match):
        console.print(Panel(Text(text), title="DRAFT", border_style="cyan"))
        return

    angle = angle_match.group(1).strip().strip("*").strip()
    reason = reason_match.group(1).strip().strip("*").strip()
    draft = draft_match.group(1).strip().strip("*").strip()

    angle_style = "green" if "trigger" in angle.lower() else "blue"

    header = Text()
    header.append("ANGLE  ", style=f"bold {angle_style}")
    header.append(angle, style=angle_style)
    console.print(header)
    console.print(Text(reason, style="dim italic"))
    console.print(Panel(Text(draft), title="DRAFT", border_style=angle_style, padding=(1, 2)))


def build_model(model_id: str, api_key_env: str, base_url: str | None = None) -> LiteLLMModel:
    client_args: dict[str, str] = {"api_key": os.environ[api_key_env]}
    if base_url:
        client_args["base_url"] = base_url
    return LiteLLMModel(
        client_args=client_args,
        model_id=model_id,
        # Bumped from 2048: a cheap mitigation that reduces how often
        # MaxTokensReachedException fires. It doesn't remove the failure
        # mode, so invoke_with_recovery still has to handle it.
        params={"max_tokens": 4096},
    )


def invoke_with_fallback(prompt: str, tools: list, system_prompt: str) -> tuple[str | None, str, LiteLLMModel]:
    """Try Claude Sonnet 5 first; fall back to OpenRouter's free model if the
    primary call fails outright (auth error, rate limit, connection error —
    anything invoke_with_recovery doesn't already handle on its own).

    Returns (result, provider_used, model) so callers can log which provider
    actually produced the draft and reuse the same model for self_check,
    rather than silently defaulting self_check to a provider that didn't
    generate the draft it's meant to be verifying.
    """
    try:
        model = build_model("anthropic/claude-sonnet-5", "ANTHROPIC_API_KEY")
        agent = Agent(model=model, tools=tools, system_prompt=system_prompt, callback_handler=None)
        result = invoke_with_recovery(agent, prompt)
        return result, "anthropic", model
    except Exception as e:
        console.print(
            f"[yellow]Primary model (Anthropic) call failed ({type(e).__name__}: {e}) — "
            f"falling back to OpenRouter free model.[/yellow]"
        )
        model = build_model(
            "openrouter/openrouter/free", "OPENROUTER_API_KEY", base_url="https://openrouter.ai/api/v1"
        )
        agent = Agent(model=model, tools=tools, system_prompt=system_prompt, callback_handler=None)
        result = invoke_with_recovery(agent, prompt)
        return result, "openrouter-fallback", model


def get_prospect_input() -> str:
    """Get the prospect to draft for, from a CLI argument or an interactive
    prompt — never hardcoded. A rep has to be able to run this on whatever
    lead they're actually working, not just the one demo prospect.

    Usage: python agent.py "Jane Doe at Acme Corp, jane@acme.com"
    """
    if len(sys.argv) > 1:
        return " ".join(sys.argv[1:])

    console.print("[dim]No prospect passed as an argument — enter one now.[/dim]")
    name = console.input("Prospect name: ").strip()
    company = console.input("Company: ").strip()
    email = console.input("Email: ").strip()
    return f"New prospect: {name} at {company}, email {email}. Draft a cold outreach opener."


def run():
    tool_data_log.clear()

    prospect_input = get_prospect_input()
    tools = [get_contact_profile, get_company_info, get_trigger_events, get_crm_history]
    result, provider_used, model = invoke_with_fallback(prospect_input, tools, SYSTEM_PROMPT)
    console.print(f"[dim]Model provider used: {provider_used}[/dim]")

    if result is None:
        console.print("[bold red]OpenLine ran out of budget before completing a draft for this prospect.[/bold red]")
        console.print("[dim]Recommend manual research on this prospect before outreach.[/dim]")
        log_run(prospect_input, "(no draft — ran out of budget)", [], shown=False, provider_used=provider_used)
        return

    # CHECKPOINT: the agent can't send anything itself, so the highest-stakes
    # moment is a human about to trust this draft. If any tool ran on stubbed
    # data OR failed outright (network error/timeout), that's worth
    # interrupting for — a failure is at least as checkpoint-worthy as a
    # stub, since it's a gap the rep can't see from the draft alone. A fully
    # real, fully successful run should never be blocked, or the checkpoint
    # becomes noise nobody reads. A SKIP result has no draft to trust in the
    # first place, so it should never trigger the checkpoint either.
    stub_tools = [name for name, status, _ in tool_data_log if status == "stub"]
    failed_tools = [name for name, status, _ in tool_data_log if status == "failed"]
    incomplete_tools = stub_tools + failed_tools
    is_skip = str(result).strip().startswith("SKIP")

    # Deterministic check, independent of the tool statuses above: the
    # system prompt tells the model never to leave placeholder brackets in
    # the DRAFT, but it doesn't comply every run, so this can't rely on the
    # model reporting its own mistake — it has to be caught in code even on
    # an otherwise fully-real run with no stubbed or failed tools at all.
    placeholders_found = PLACEHOLDER_PATTERN.findall(str(result)) if not is_skip else []

    # SELF-CHECK PASS: a second, independent model call comparing the draft
    # against the actual values each tool returned this run — not what the
    # model claims it used, the real logged values. Skipped on SKIP (no
    # draft exists to check). Fails closed via self_check()/parse_self_check().
    self_check_result = {"status": "SKIPPED", "unsupported_claims": []}
    if not is_skip:
        tool_outputs = {name: value for name, status, value in tool_data_log}
        self_check_result = self_check(model, str(result), tool_outputs)

    check_failed = self_check_result["status"] in ("FLAGGED", "UNVERIFIED")

    def withhold(reason: str) -> None:
        console.print(f"[red]{reason}[/red]")
        log_run(
            prospect_input, str(result), incomplete_tools, shown=False,
            self_check_result=self_check_result, provider_used=provider_used,
        )

    def show() -> None:
        render_draft(str(result))
        log_run(
            prospect_input, str(result), incomplete_tools, shown=True,
            self_check_result=self_check_result, provider_used=provider_used,
        )

    if is_skip:
        show()
        return

    # TIER 3 — never acceptable: an unfilled placeholder bracket isn't a
    # judgment call for the rep to make, it's simply broken output. No
    # prompt, no override — auto-withhold. Checked first, ahead of the
    # other tiers, since nothing else about the run changes that verdict.
    if placeholders_found:
        console.print(Panel(
            Text(
                f"Unfilled template placeholder(s): {', '.join(placeholders_found)}\n"
                "This is broken if sent as-is — withheld automatically, no override available.",
                style="bold red",
            ),
            title="WITHHELD — broken output", border_style="red", title_align="left",
        ))
        withhold("Recommend re-running this prospect; do not send this draft.")
        return

    # TIER 2 — real risk: a tool failed outright, or self-check flagged an
    # unsupported claim. This is the case a reflexive "y" habit from Tier 1
    # is actually dangerous for, so confirming requires typing a full
    # phrase rather than one keystroke a habituated hand can produce on
    # autopilot.
    if failed_tools or check_failed:
        body = Text()
        if failed_tools:
            body.append(
                f"{len(failed_tools)} of {len(tool_data_log)} data sources FAILED to respond: "
                f"{', '.join(failed_tools)}\n",
                style="bold red",
            )
            if "get_crm_history" in failed_tools:
                body.append(
                    "get_crm_history failed — this prospect's prior-contact status is UNCONFIRMED, not clean.\n",
                    style="bold red",
                )
        if check_failed:
            body.append(f"Self-check status: {self_check_result['status']}\n", style="bold magenta")
            for claim in self_check_result["unsupported_claims"]:
                body.append(f"  - {claim}\n", style="magenta")
        if stub_tools:
            body.append(
                f"Also stubbed (not real): {', '.join(stub_tools)}\n",
                style="yellow",
            )

        risk_title = "REAL RISK — tool failure" if failed_tools else "REAL RISK — self-check flagged this draft"
        console.print(Panel(body, title=risk_title, border_style="red", title_align="left"))
        answer = console.input(
            "[bold]Type 'send anyway' to override, or anything else to withhold: [/bold]"
        ).strip().lower()
        if answer != "send anyway":
            withhold("Draft withheld. Recommend manual research on this prospect before outreach.")
            return
        show()
        return

    # TIER 1 — routine: only stubbed data, nothing else wrong. This is the
    # expected, permanent state of today's demo (2 of 4 tools are always
    # stubbed pending real LinkedIn/Crunchbase access), so it stays
    # lightweight — a single y/n — but visually calm rather than alarmed,
    # so it stops looking identical to a Tier 2 risk.
    if stub_tools:
        body = Text()
        body.append(
            f"{len(stub_tools)} of {len(tool_data_log)} data sources are stubbed, not real: "
            f"{', '.join(stub_tools)}\n",
            style="yellow",
        )
        body.append("This draft was written without real data from those sources.\n", style="dim")
        console.print(Panel(body, title="stubbed data — routine", border_style="blue", title_align="left"))
        answer = console.input("Show the draft anyway? (y/n): ").strip().lower()
        if answer != "y":
            withhold("Draft withheld. Recommend manual research on this prospect before outreach.")
            return

    show()


if __name__ == "__main__":
    run()
