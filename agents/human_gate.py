"""
agents/human_gate.py
─────────────────────────────────────────────────────────────────────────────
HumanApprovalGate — LangGraph interrupt()-based node that pauses the graph
and waits for a human to approve or reject the generated pack.

On approval → graph continues to AuditorAgent.
On rejection → graph routes back to PlannerAgent with reviewer notes.

The gate also exposes a helper to build the approval summary presented to the
reviewer so they have all the information they need to make a decision.
"""

from __future__ import annotations

import logging
from typing import Any

from langgraph.types import interrupt

from models.state import ApprovalStatus, LeaverState, OnboardingState

logger = logging.getLogger(__name__)


# ── Summary builder ────────────────────────────────────────────────────────────

def _build_approval_summary(state: dict[str, Any]) -> str:
    """
    Build a human-readable summary of the pack for the reviewer.
    Returned as a plain-text / Markdown string.
    """
    form = state.get("form", {})
    checklist = state.get("checklist", [])
    scripts = state.get("scripts", {})
    comms = state.get("comms", {})
    iteration = state.get("iteration", 1)

    name = f"{form.get('first_name', '')} {form.get('last_name', '')}".strip() or "Unknown"
    role = form.get("role", "Unknown")
    dept = form.get("department", "Unknown")
    start = form.get("start_date") or form.get("last_working_day", "Unknown")

    critical_count = sum(1 for c in checklist if c.get("priority") == "Critical")
    high_count = sum(1 for c in checklist if c.get("priority") == "High")

    lines = [
        "# Onboarding Pack — Human Approval Required",
        "",
        f"**Iteration:** {iteration}",
        "",
        "## New Starter / Leaver",
        f"- **Name:** {name}",
        f"- **Role:** {role}",
        f"- **Department:** {dept}",
        f"- **Date:** {start}",
        "",
        "## Pack Summary",
        f"- **Checklist items:** {len(checklist)} total  "
        f"({critical_count} Critical · {high_count} High)",
        f"- **Scripts generated:** {len(scripts)}",
        f"- **Scripts:** {', '.join(scripts.keys()) or 'None'}",
        f"- **Welcome email:** "
        f"{'✅ Drafted' if comms.get('welcome_email_subject') else '❌ Missing'}",
        f"- **Day-1 guide:** "
        f"{'✅ Drafted' if comms.get('day1_guide_markdown') else '❌ Missing'}",
        "",
        "## Checklist Preview (first 5 items)",
    ]

    for item in checklist[:5]:
        lines.append(f"- [{item.get('priority', '?')}] {item.get('title', 'Unknown')}")

    if len(checklist) > 5:
        lines.append(f"  _(and {len(checklist) - 5} more...)_")

    lines += [
        "",
        "---",
        "**To approve:** POST `/approve/{run_id}` with `{\"approved\": true, \"notes\": \"\"}`",
        "**To reject:** POST `/approve/{run_id}` with "
        "`{\"approved\": false, \"notes\": \"reason here\"}`",
    ]

    return "\n".join(lines)


# ── Graph Node Functions ───────────────────────────────────────────────────────

def human_gate_node(state: OnboardingState) -> dict:
    """
    LangGraph node: HumanApprovalGate (onboarding).

    Calls `interrupt()` which pauses graph execution and surfaces the approval
    summary to the caller. When resumed, the caller must supply:
        {"approved": bool, "notes": str}
    """
    logger.info(
        "HumanApprovalGate: pausing for human review (iteration %d)", state.get("iteration", 1)
    )

    summary = _build_approval_summary(dict(state))

    # interrupt() pauses the graph and returns `summary` to the caller.
    # The graph resumes when Command(resume=...) is sent by the API layer.
    decision = interrupt({
        "summary": summary,
        "message": "Please review the onboarding pack above and approve or reject.",
        "run_id": state.get("run_id", ""),
    })

    # decision is the value passed to Command(resume=...) — expect {"approved": bool, "notes": str}
    if isinstance(decision, dict):
        approved = bool(decision.get("approved", False))
        notes = str(decision.get("notes", ""))
    else:
        # Fallback: treat truthy resume value as approval
        approved = bool(decision)
        notes = ""

    logger.info("HumanApprovalGate: decision=%s notes=%r", approved, notes)

    status = ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED

    return {
        "approval_status": status.value,
        "approval_notes": notes,
        "reviewer_notes": notes if not approved else "",
    }


def leaver_human_gate_node(state: LeaverState) -> dict:
    """
    LangGraph node: HumanApprovalGate (leaver).
    Identical logic, adapted for the leaver flow.
    """
    logger.info("HumanApprovalGate (leaver): pausing for human review")

    summary = _build_approval_summary(dict(state))

    decision = interrupt({
        "summary": summary,
        "message": "Please review the de-provisioning pack above and approve or reject.",
        "run_id": state.get("run_id", ""),
    })

    if isinstance(decision, dict):
        approved = bool(decision.get("approved", False))
        notes = str(decision.get("notes", ""))
    else:
        approved = bool(decision)
        notes = ""

    status = ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED

    return {
        "approval_status": status.value,
        "approval_notes": notes,
        "reviewer_notes": notes if not approved else "",
    }


# ── Routing functions (used by StateGraph conditional edges) ───────────────────

def route_after_approval(state: OnboardingState | LeaverState) -> str:
    """
    Return the next node name based on approval_status.
    Used as the path_map function in add_conditional_edges().
    """
    status = state.get("approval_status", "")
    iteration = state.get("iteration", 1)

    if status == ApprovalStatus.APPROVED.value:
        logger.info("HumanApprovalGate: approved → auditor")
        return "auditor"
    elif iteration >= 3:
        # Safety valve: after 3 rejections, escalate rather than loop forever
        logger.warning("HumanApprovalGate: max iterations reached — routing to error")
        return "max_rejections"
    else:
        logger.info("HumanApprovalGate: rejected (iteration %d) → planner", iteration)
        return "planner"


class HumanApprovalGate:
    """Thin wrapper for direct invocation outside the graph (e.g., testing)."""

    def build_summary(self, state: dict) -> str:
        return _build_approval_summary(state)
