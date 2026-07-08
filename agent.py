import os

import requests
from dotenv import load_dotenv
from strands import Agent, tool
from strands.models.litellm import LiteLLMModel

load_dotenv()

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
# These return mock data. get_trigger_events below is wired to a real API
# (NewsAPI.org); the other three stay stubbed until real data sources are connected.

@tool
def get_contact_profile(contact_id: str) -> dict:
    """Get role and profile info for a prospect.

    Args:
        contact_id: Identifier for the prospect (e.g. name or LinkedIn handle).

    Returns:
        A dict with job title, tenure, and recent posts.
    """
    return {
        "name": "Jordan Reyes",
        "title": "VP of Sales",
        "tenure_months": 8,
        "recent_posts": ["Excited to be scaling our outbound team this quarter."],
    }


@tool
def get_company_info(company_name: str) -> dict:
    """Get firmographic data for a prospect's company.

    Args:
        company_name: Name of the company to look up.

    Returns:
        A dict with company size, industry, funding stage, and funding history.
    """
    return {
        "name": company_name,
        "industry": "B2B SaaS",
        "size": "150-200 employees",
        "funding_stage": "Series B",
        "funding_history": [{"round": "Series B", "amount": "$28M", "date": "2026-06-15"}],
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
def get_crm_history(contact_id: str) -> dict:
    """Check for prior contact or CRM notes for a prospect.

    Args:
        contact_id: Identifier for the prospect.

    Returns:
        A dict with past touches, notes, and current deal stage.
    """
    return {
        "past_touches": [],
        "notes": "",
        "deal_stage": "not in pipeline",
    }


def run():
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
        "New prospect: Jordan Reyes, VP of Sales at Acme Corp. "
        "Draft a cold outreach opener."
    )
    print(result)


if __name__ == "__main__":
    run()
