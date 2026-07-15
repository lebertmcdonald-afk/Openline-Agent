"""OpenLine — web UI for the Saturday presentation.

Wraps the exact same agent logic in agent.py (tools, invoke_with_fallback,
self_check, the tiered checkpoint) in a Streamlit interface instead of the
terminal. This is a display layer only — no agent logic is duplicated here.

Gated by a passcode: this will be a public URL hitting real, shared API
keys (Anthropic/OpenRouter/NewsAPI/HubSpot), so nothing that spends quota
runs until the passcode is entered. Without this, a stranger who finds the
URL before Saturday could exhaust the shared quota before the actual
presentation.
"""

import os

import streamlit as st
from dotenv import load_dotenv

load_dotenv()

from agent import (  # noqa: E402
    PLACEHOLDER_PATTERN,
    SYSTEM_PROMPT,
    get_company_info,
    get_contact_profile,
    get_crm_history,
    get_trigger_events,
    invoke_with_fallback,
    log_run,
    self_check,
    tool_data_log,
)

st.set_page_config(page_title="OpenLine", page_icon="✉️", layout="centered")


def get_secret(key: str) -> str | None:
    """Read a secret from Streamlit's secrets manager first (how this works
    once deployed), falling back to .env for local dev. st.secrets raises
    if no secrets.toml exists at all, so this has to be defensive.
    """
    try:
        if key in st.secrets:
            return st.secrets[key]
    except Exception:
        pass
    return os.environ.get(key)


def check_passcode() -> bool:
    correct = get_secret("DEMO_PASSCODE")
    if not correct:
        st.error("No DEMO_PASSCODE configured — set one in Streamlit secrets or .env before deploying.")
        st.stop()

    if st.session_state.get("authenticated"):
        return True

    st.title("✉️ OpenLine")
    st.caption("Enter the demo passcode to continue.")
    entered = st.text_input("Passcode", type="password")
    if st.button("Enter"):
        if entered == correct:
            st.session_state["authenticated"] = True
            st.rerun()
        else:
            st.error("Incorrect passcode.")
    return False


def render_checkpoint_and_result() -> None:
    """Render whatever the last run produced, reading from session_state so
    a Streamlit rerun (e.g. clicking a confirm button) doesn't trigger a new
    agent call — the agent only runs once per "Draft Outreach" click.
    """
    state = st.session_state["last_run"]
    result = state["result"]
    provider_used = state["provider_used"]
    prospect_input = state["prospect_input"]
    stub_tools = state["stub_tools"]
    failed_tools = state["failed_tools"]
    incomplete_tools = state["incomplete_tools"]
    placeholders_found = state["placeholders_found"]
    self_check_result = state["self_check_result"]
    check_failed = self_check_result["status"] in ("FLAGGED", "UNVERIFIED")
    is_skip = str(result).strip().startswith("SKIP")

    st.caption(f"Model provider used: {provider_used}")

    if result is None:
        st.error("OpenLine ran out of budget before completing a draft for this prospect.")
        st.caption("Recommend manual research on this prospect before outreach.")
        return

    if is_skip:
        st.warning(str(result))
        return

    # TIER 3 — never acceptable: an unfilled placeholder isn't a judgment
    # call, it's broken output. No override, ever.
    if placeholders_found:
        st.error(
            f"**WITHHELD — broken output**\n\n"
            f"Unfilled template placeholder(s): {', '.join(placeholders_found)}\n\n"
            f"This is broken if sent as-is — withheld automatically, no override available."
        )
        if not state.get("logged"):
            log_run(prospect_input, str(result), incomplete_tools, shown=False,
                     self_check_result=self_check_result, provider_used=provider_used)
            state["logged"] = True
        return

    # TIER 2 — real risk: a tool failed, or self-check flagged a claim.
    # Requires typing the full phrase, not a single click, so the reflexive
    # "yes" habit from Tier 1 can't blow through it on autopilot.
    if failed_tools or check_failed:
        title = "REAL RISK — tool failure" if failed_tools else "REAL RISK — self-check flagged this draft"
        lines = [f"**{title}**", ""]
        if failed_tools:
            lines.append(f"{len(failed_tools)} of {len(tool_data_log)} data sources FAILED to respond: {', '.join(failed_tools)}")
            if "get_crm_history" in failed_tools:
                lines.append("get_crm_history failed — this prospect's prior-contact status is UNCONFIRMED, not clean.")
        if check_failed:
            lines.append(f"Self-check status: {self_check_result['status']}")
            for claim in self_check_result["unsupported_claims"]:
                lines.append(f"- {claim}")
        if stub_tools:
            lines.append(f"Also stubbed (not real): {', '.join(stub_tools)}")
        st.error("\n\n".join(lines))

        confirm = st.text_input("Type 'send anyway' to override, or leave blank to withhold:", key="tier2_confirm")
        col1, col2 = st.columns(2)
        if col1.button("Submit"):
            if confirm.strip().lower() == "send anyway":
                render_draft(result)
                if not state.get("logged"):
                    log_run(prospect_input, str(result), incomplete_tools, shown=True,
                             self_check_result=self_check_result, provider_used=provider_used)
                    state["logged"] = True
            else:
                st.error("Draft withheld. Recommend manual research on this prospect before outreach.")
                if not state.get("logged"):
                    log_run(prospect_input, str(result), incomplete_tools, shown=False,
                             self_check_result=self_check_result, provider_used=provider_used)
                    state["logged"] = True
        return

    # TIER 1 — routine: only stubbed data, nothing else wrong. Expected on
    # nearly every run today (2 of 4 tools are permanently stubbed pending
    # real LinkedIn/Crunchbase access), so this stays lightweight.
    if stub_tools:
        st.warning(
            f"{len(stub_tools)} of {len(tool_data_log)} data sources are stubbed, not real: "
            f"{', '.join(stub_tools)}\n\nThis draft was written without real data from those sources."
        )
        if st.button("Show draft anyway"):
            render_draft(result)
            if not state.get("logged"):
                log_run(prospect_input, str(result), incomplete_tools, shown=True,
                         self_check_result=self_check_result, provider_used=provider_used)
                state["logged"] = True
        return

    # Fully clean, fully verified run — nothing to gate.
    render_draft(result)
    if not state.get("logged"):
        log_run(prospect_input, str(result), incomplete_tools, shown=True,
                 self_check_result=self_check_result, provider_used=provider_used)
        state["logged"] = True


def render_draft(result: str) -> None:
    import re
    text = str(result).strip()
    angle_match = re.search(r"ANGLE:\s*(.+)", text, re.IGNORECASE)
    reason_match = re.search(r"REASON:\s*(.+?)(?:\n\s*\n|\nDRAFT:|\Z)", text, re.IGNORECASE | re.DOTALL)
    draft_match = re.search(r"DRAFT:\s*(.+)", text, re.IGNORECASE | re.DOTALL)

    if not (angle_match and reason_match and draft_match):
        st.info(text)
        return

    angle = angle_match.group(1).strip().strip("*").strip()
    reason = reason_match.group(1).strip().strip("*").strip()
    draft = draft_match.group(1).strip().strip("*").strip()

    st.markdown(f"**ANGLE:** {angle}")
    st.caption(reason)
    st.success(draft)


def main() -> None:
    if not check_passcode():
        st.stop()

    st.title("✉️ OpenLine")
    st.caption("AI-drafted, honest cold outreach openers — reviewed by you before anything is sent.")

    with st.form("prospect_form"):
        name = st.text_input("Prospect name")
        company = st.text_input("Company")
        email = st.text_input("Email")
        submitted = st.form_submit_button("Draft Outreach")

    if submitted:
        if not (name and company and email):
            st.error("Name, company, and email are all required.")
            return

        prospect_input = f"New prospect: {name} at {company}, email {email}. Draft a cold outreach opener."
        tools = [get_contact_profile, get_company_info, get_trigger_events, get_crm_history]

        tool_data_log.clear()
        with st.spinner("Researching prospect and drafting..."):
            result, provider_used, model = invoke_with_fallback(prospect_input, tools, SYSTEM_PROMPT)

            stub_tools = [n for n, status, _ in tool_data_log if status == "stub"]
            failed_tools = [n for n, status, _ in tool_data_log if status == "failed"]
            incomplete_tools = stub_tools + failed_tools
            is_skip = str(result).strip().startswith("SKIP") if result else False
            placeholders_found = PLACEHOLDER_PATTERN.findall(str(result)) if (result and not is_skip) else []

            self_check_result = {"status": "SKIPPED", "unsupported_claims": []}
            if result and not is_skip:
                tool_outputs = {n: v for n, _, v in tool_data_log}
                self_check_result = self_check(model, str(result), tool_outputs)

        st.session_state["last_run"] = {
            "result": result,
            "provider_used": provider_used,
            "prospect_input": prospect_input,
            "stub_tools": stub_tools,
            "failed_tools": failed_tools,
            "incomplete_tools": incomplete_tools,
            "placeholders_found": placeholders_found,
            "self_check_result": self_check_result,
            "logged": False,
        }

    if "last_run" in st.session_state:
        render_checkpoint_and_result()


if __name__ == "__main__":
    main()
