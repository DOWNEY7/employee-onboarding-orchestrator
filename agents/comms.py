"""
agents/comms.py
─────────────────────────────────────────────────────────────────────────────
CommsAgent — uses the LLM to draft:
  - Welcome email (HTML + plain text)
  - Day-1 guide (Markdown)

Graph nodes:
    comms_node(state: OnboardingState) -> dict
    leaver_comms_node(state: LeaverState) -> dict
"""

from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from core.llm import get_llm
from models.state import CommsBundle, LeaverState, OnboardingState

logger = logging.getLogger(__name__)


# ── Prompts ────────────────────────────────────────────────────────────────────

_WELCOME_EMAIL_SYSTEM = """You are a warm, professional IT communications specialist writing on
behalf of the IT team and HR at a UK technology company.

Write a welcome email for a new employee starting on day 1. The email should be:
- Friendly, professional, and encouraging
- Concise (no more than 400 words of body text)
- Include: login credentials note, IT support contact, what to expect on day 1, and a warm welcome

Return your response as JSON with this exact structure:
{
  "subject": "<email subject line>",
  "html": "<full HTML email body — use proper HTML tags, clean formatting>",
  "plain": "<plain text version of the same email>"
}

Do NOT wrap in markdown fences. Return raw JSON only.
"""

_DAY1_GUIDE_SYSTEM = """You are an experienced IT onboarding specialist writing a comprehensive
Day-1 guide for a new employee.

The guide must cover:
1. **Where to go** — building/location, reception, parking, desk assignment
2. **First login** — how to sign into their new laptop, Microsoft 365, and set up MFA
3. **Key contacts** — IT support, HR, their manager, and team contacts
4. **Systems to access** — list the key systems they need to log into on day 1
5. **Day-1 agenda** — suggested schedule for the first day
6. **First week** — what to expect in the first 5 days
7. **Getting help** — where to go when stuck (IT helpdesk, Slack/Teams, etc.)
8. **Useful tips** — 3-5 practical tips for settling in

Write in clear, friendly Markdown. Use headers, bullet points, and numbered lists.
Be specific to the role and department provided.
Target length: 600-900 words.

Return ONLY the Markdown content, no JSON wrapper.
"""


# ── Helper functions ───────────────────────────────────────────────────────────


def _draft_welcome_email(form: dict[str, Any]) -> dict[str, str]:
    """Call the LLM to draft the welcome email."""
    llm = get_llm()
    display_name = f"{form.get('first_name', '')} {form.get('last_name', '')}".strip()
    upn = f"{form.get('email_prefix', 'newuser')}@{form.get('domain', 'contoso.com')}"

    user_prompt = (
        f"New employee details:\n"
        f"- Name: {display_name}\n"
        f"- Role: {form.get('role', 'Unknown')}\n"
        f"- Department: {form.get('department', 'Unknown')}\n"
        f"- Start Date: {form.get('start_date', 'TBC')}\n"
        f"- Work email (UPN): {upn}\n"
        f"- Manager: {form.get('manager_email', 'Unknown')}\n"
        f"- Location: {form.get('location', 'UK')}\n\n"
        f"Please draft the welcome email for {display_name}."
    )

    messages = [
        SystemMessage(content=_WELCOME_EMAIL_SYSTEM),
        HumanMessage(content=user_prompt),
    ]

    logger.info("CommsAgent: drafting welcome email for %s", display_name)
    response = llm.invoke(messages)
    raw = response.content.strip()

    # Strip markdown fences if present
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(lines[1:-1]) if lines[-1] == "```" else "\n".join(lines[1:])

    parsed = json.loads(raw)
    return {
        "subject": parsed.get("subject", f"Welcome to the team, {display_name}!"),
        "html": parsed.get("html", ""),
        "plain": parsed.get("plain", ""),
    }


def _draft_day1_guide(form: dict[str, Any], checklist: list[dict[str, Any]]) -> str:
    """Call the LLM to draft the Day-1 guide in Markdown."""
    llm = get_llm()
    display_name = f"{form.get('first_name', '')} {form.get('last_name', '')}".strip()

    # Include high-priority checklist items as context
    critical_steps = [
        item["title"] for item in checklist if item.get("priority") in ("Critical", "High")
    ][:10]

    user_prompt = (
        f"Write the Day-1 guide for:\n"
        f"- Name: {display_name}\n"
        f"- Role: {form.get('role', 'Unknown')}\n"
        f"- Department: {form.get('department', 'Unknown')}\n"
        f"- Location: {form.get('location', 'UK')}\n"
        f"- Manager: {form.get('manager_email', 'Unknown')}\n"
        f"- Start Date: {form.get('start_date', 'TBC')}\n\n"
        f"Key IT steps that will be completed before they arrive:\n"
        + "\n".join(f"  - {step}" for step in critical_steps)
        + "\n\nWrite the guide now."
    )

    messages = [
        SystemMessage(content=_DAY1_GUIDE_SYSTEM),
        HumanMessage(content=user_prompt),
    ]

    logger.info("CommsAgent: drafting Day-1 guide for %s", display_name)
    response = llm.invoke(messages)
    return response.content.strip()


def _draft_leaver_handover_note(form: dict[str, Any]) -> dict[str, str]:
    """Draft a brief handover / farewell note for the leaver's manager."""
    llm = get_llm()
    display_name = f"{form.get('first_name', '')} {form.get('last_name', '')}".strip()

    system_prompt = """You are a professional HR communications specialist.

Draft a brief, sensitive, and professional handover communication to be shared with the
departing employee's team.
Include: acknowledgement of their last working day, data handover steps, who to contact
for ongoing queries.

Return JSON: {"subject": "...", "html": "...", "plain": "...", "handover_notes_md": "..."}
Do NOT wrap in markdown fences. Return raw JSON only.
"""

    user_prompt = (
        f"Leaver: {display_name}\n"
        f"Email: {form.get('email', 'Unknown')}\n"
        f"Last Working Day: {form.get('last_working_day', 'TBC')}\n"
        f"Reason: {form.get('reason', 'Unknown')}\n"
        f"Manager: {form.get('manager_email', 'Unknown')}\n"
        f"Data Transfer Recipient: "
        f"{form.get('data_transfer_recipient') or form.get('manager_email', 'manager')}\n"
    )

    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_prompt),
    ]

    logger.info("CommsAgent: drafting leaver handover note for %s", display_name)
    response = llm.invoke(messages)
    raw = response.content.strip()

    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(lines[1:-1]) if lines[-1] == "```" else "\n".join(lines[1:])

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = {
            "subject": f"Farewell — {display_name}",
            "html": raw,
            "plain": raw,
            "handover_notes_md": raw,
        }

    return parsed


# ── Graph Node Functions ───────────────────────────────────────────────────────


def comms_node(state: OnboardingState) -> dict:
    """
    LangGraph node: CommsAgent (onboarding).
    Drafts welcome email and Day-1 guide.
    """
    logger.info("CommsAgent: starting onboarding communications drafting")
    form = state.get("form", {})
    checklist = state.get("checklist", [])

    warnings = list(state.get("warnings", []))

    try:
        email = _draft_welcome_email(form)
    except Exception as exc:
        logger.error("CommsAgent: welcome email drafting failed: %s", exc)
        email = {
            "subject": f"Welcome to the team, {form.get('first_name', 'New Starter')}!",
            "html": "<p>Welcome! Your IT team will be in touch shortly.</p>",
            "plain": "Welcome! Your IT team will be in touch shortly.",
        }
        warnings.append(
            "CommsAgent: welcome email drafting failed — a generic placeholder email was used."
        )

    try:
        day1_guide = _draft_day1_guide(form, checklist)
    except Exception as exc:
        logger.error("CommsAgent: Day-1 guide drafting failed: %s", exc)
        day1_guide = (
            "# Day 1 Guide\n\nYour IT team will provide a detailed guide before your start date."
        )
        warnings.append(
            "CommsAgent: Day-1 guide drafting failed — a generic placeholder guide was used."
        )

    bundle = CommsBundle(
        welcome_email_subject=email["subject"],
        welcome_email_html=email["html"],
        welcome_email_plain=email["plain"],
        day1_guide_markdown=day1_guide,
    )

    logger.info("CommsAgent: communications bundle complete")
    return {"comms": bundle.model_dump(), "error": "", "warnings": warnings}


def leaver_comms_node(state: LeaverState) -> dict:
    """
    LangGraph node: CommsAgent (leaver).
    Drafts handover note and departure communication.
    """
    logger.info("CommsAgent: starting leaver communications drafting")
    form = state.get("form", {})

    warnings = list(state.get("warnings", []))

    try:
        handover = _draft_leaver_handover_note(form)
    except Exception as exc:
        logger.error("CommsAgent: leaver handover drafting failed: %s", exc)
        handover = {
            "subject": "Leaver Handover",
            "html": "<p>Handover note to be completed manually.</p>",
            "plain": "Handover note to be completed manually.",
            "handover_notes_md": "## Handover\n\nTo be completed.",
        }
        warnings.append(
            "CommsAgent: leaver handover drafting failed — a placeholder handover note was used "
            "and should be completed manually."
        )

    # For leaver, we repurpose CommsBundle fields
    bundle = CommsBundle(
        welcome_email_subject=handover.get("subject", ""),
        welcome_email_html=handover.get("html", ""),
        welcome_email_plain=handover.get("plain", ""),
        day1_guide_markdown=handover.get("handover_notes_md", ""),
    )

    logger.info("CommsAgent: leaver communications bundle complete")
    return {"comms": bundle.model_dump(), "error": "", "warnings": warnings}


class CommsAgent:
    """Thin wrapper for direct invocation outside the graph (e.g., testing)."""

    def run_onboarding(self, state: OnboardingState) -> dict:
        return comms_node(state)

    def run_leaver(self, state: LeaverState) -> dict:
        return leaver_comms_node(state)
