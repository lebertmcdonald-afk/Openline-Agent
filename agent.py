import os
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv
from strands import Agent, tool
from strands.models.litellm import LiteLLMModel

load_dotenv()

# Checkpoint support: each tool appends (tool_name, "real" | "stub") here as it
# runs, so run() can tell after the fact whether the draft it's about to show
# was built on any placeholder data. Cleared at the start of each run().
tool_data_log: list[tuple[str, str]] = []

SYSTEM_PROMPT = """You are OpenLine, an outbound research and drafting assistant for an SDR's cold outreach.

Tool sequence: When a rep adds a new prospect, call tools in this exact order: (1) get_contact_profile, (2) get_company_info, (3) get_trigger_events, (4) get_crm_history. Do not skip a tool or change the order.

Angle selection: After all four tools have returned, choose the single most relevant angle: prioritize a trigger event dated within the last 60 days if one exists; otherwise fall back to a role-specific pain point. Never combine more than one angle.

Output format: Return your response in exactly this structure:
ANGLE: [Trigger Event / Role-Based]
REASON: [one sentence on why this angle was chosen]
DRAFT: [3-4 sentence cold email opener]

Constraints: Never send, schedule, or post anything — only return the draft for the rep to review. Never invent a detail that a tool did not return. If a tool fails or returns no data, state that plainly in the REASON line and draft using only confirmed information.

Termination condition: Do not produce a DRAFT until all four tools have been called and returned a result (or an explicit failure). If get_crm_history shows this prospect was already contacted in the last 14 days, stop and return SKIP: Recently contacted instead of a draft."""


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
    tool_data_log.append(("get_contact_profile", "stub"))
    return {
        "name": contact_id,
        "title": "unavailable (stubbed tool, no real profile source connected)",
        "tenure_months": None,
        "recent_posts": [],
    }


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
    tool_data_log.append(("get_company_info", "stub"))
    return {
        "name": company_name,
        "industry": "unavailable (stubbed tool, no real firmographic source connected)",
        "size": "unavailable",
        "funding_stage": "unavailable",
        "funding_history": [],
    }


@tool
def get_trigger_events(company_name: str) -> list[dict]:
    """Check for recent newsworthy events tied to a company.

    Calls the NewsAPI.org /v2/everything endpoint for real, current news
    about the given company (funding, leadership hires, product launches).

    Args:
        company_name: Name of the company to search news for.

    Returns:
        A list of dicts with event description, source, and publish date.
        Empty list if no articles are found or the API call fails.
    """
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

    tool_data_log.append(("get_trigger_events", "real"))

    if not response.ok:
        return []

    articles = response.json().get("articles", [])

    return [
        {
            "description": article["title"],
            "source": article["source"]["name"],
            "date": article["publishedAt"][:10],
        }
        for article in articles
    ]


@tool
def get_crm_history(contact_email: str) -> dict:
    """Check for prior contact or CRM notes for a prospect in HubSpot.

    Calls the HubSpot Contacts Search API to look up a prospect by email.
    Prospect must be identified by email, since that's what HubSpot search
    keys on — do not invent an email if one wasn't provided for this prospect.

    Args:
        contact_email: Prospect's email address to look up.

    Returns:
        A dict with prior_contact (bool), last_touch_date, days_since_last_touch
        (int, or None if never contacted), deal_stage, and notes. Returns
        prior_contact=False if the contact isn't found in HubSpot.
    """
    headers = {
        "Authorization": f"Bearer {os.environ['HUBSPOT_API_KEY']}",
        "Content-Type": "application/json",
    }

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

    tool_data_log.append(("get_crm_history", "real"))

    if not response.ok or not response.json().get("results"):
        return {
            "prior_contact": False,
            "last_touch_date": None,
            "days_since_last_touch": None,
            "deal_stage": "not in pipeline",
            "notes": "No CRM record found.",
        }

    props = response.json()["results"][0]["properties"]
    last_touch_raw = props.get("notes_last_updated")

    days_since_last_touch = None
    if last_touch_raw:
        last_touch_date = datetime.fromisoformat(last_touch_raw.replace("Z", "+00:00"))
        days_since_last_touch = (datetime.now(timezone.utc) - last_touch_date).days

    return {
        "prior_contact": bool(last_touch_raw),
        "last_touch_date": last_touch_raw[:10] if last_touch_raw else None,
        "days_since_last_touch": days_since_last_touch,
        "deal_stage": props.get("hs_lead_status") or "unknown",
        "notes": f"{props.get('firstname', '')} {props.get('lastname', '')}".strip(),
    }


def run():
    tool_data_log.clear()

    model = LiteLLMModel(
        client_args={
            "api_key": os.environ["OPENROUTER_API_KEY"],
            "base_url": "https://openrouter.ai/api/v1",
        },
        model_id="openrouter/openrouter/free",
        params={"max_tokens": 2048},
    )

    agent = Agent(
        model=model,
        tools=[get_contact_profile, get_company_info, get_trigger_events, get_crm_history],
        system_prompt=SYSTEM_PROMPT,
        # Strands' default PrintingCallbackHandler streams every chunk to stdout,
        # which duplicates the explicit print(result) below. Disable it so the
        # final ANGLE/REASON/DRAFT block prints exactly once.
        callback_handler=None,
    )

    result = agent(
        "New prospect: Roberto Nachmann at AMF International Cargo Inc, "
        "email rnachmann@gmail.com. Draft a cold outreach opener."
    )

    # CHECKPOINT: the agent can't send anything itself, so the highest-stakes
    # moment is a human about to trust this draft. If any tool ran on stubbed
    # data, that's the one moment worth interrupting for — a fully-real run
    # should never be blocked, or the checkpoint becomes noise nobody reads.
    # A SKIP result has no draft to trust in the first place, so it should
    # never trigger the checkpoint either.
    stub_tools = [name for name, status in tool_data_log if status == "stub"]
    is_skip = str(result).strip().startswith("SKIP")

    if stub_tools and not is_skip:
        print("CHECKPOINT — before showing this draft:")
        print(f"  {len(stub_tools)} of {len(tool_data_log)} data sources are stubbed, not real: {', '.join(stub_tools)}")
        print("  This draft was written without real data from those sources.")
        answer = input("Show the draft anyway? [y/n]: ").strip().lower()
        if answer != "y":
            print("Draft withheld. Recommend manual research on this prospect before outreach.")
            return

    print(result)


if __name__ == "__main__":
    run()
