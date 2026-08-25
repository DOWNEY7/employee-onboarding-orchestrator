"""
agents/planner.py
─────────────────────────────────────────────────────────────────────────────
PlannerAgent — builds a typed OnboardingChecklist from the role/department
intake form by consulting the matrix JSON and then enriching it with an LLM.

Graph node signature:
    planner_node(state: OnboardingState) -> dict  (partial state update)
    leaver_planner_node(state: LeaverState) -> dict
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from core.llm import get_llm
from models.state import ChecklistItem, LeaverState, OnboardingState, Priority

logger = logging.getLogger(__name__)

# ── Paths ──────────────────────────────────────────────────────────────────────
_REPO_ROOT = Path(__file__).parent.parent
ONBOARDING_MATRIX_PATH = _REPO_ROOT / "checklists" / "onboarding_matrix.json"
LEAVER_MATRIX_PATH = _REPO_ROOT / "checklists" / "leaver_matrix.json"


# ── Helpers ────────────────────────────────────────────────────────────────────


def _load_matrix(path: Path) -> list[dict[str, Any]]:
    """Load and parse a checklist matrix JSON file."""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    return data.get("steps", [])


def _filter_matrix_for_role(steps: list[dict], role: str, department: str) -> list[dict]:
    """
    Filter matrix steps for the given role/department.
    Currently all steps have required_for=['*'], so this returns all steps.
    Future: implement role-specific filtering here.
    """
    filtered = []
    for step in steps:
        required_for = step.get("required_for", ["*"])
        roles = [r.lower() for r in required_for]
        depts = [d.lower() for d in required_for]
        if "*" in required_for or role.lower() in roles or department.lower() in depts:
            filtered.append(step)
    return filtered


def _matrix_to_checklist(steps: list[dict]) -> list[ChecklistItem]:
    """Convert raw matrix step dicts to ChecklistItem Pydantic models."""
    items = []
    for step in steps:
        item = ChecklistItem(
            step_number=step["step_number"],
            title=step["title"],
            description=step["description"],
            system=step["system"],
            owner=step["owner"],
            priority=Priority(step["priority"]),
            tags=step.get("tags", []),
        )
        items.append(item)
    return items


# ── LLM Enrichment ────────────────────────────────────────────────────────────

_SYSTEM_PROMPT_ONBOARDING = """You are an expert IT onboarding coordinator with deep knowledge
of Microsoft 365, Azure AD, Intune, and corporate IT processes.

Your task is to review a base checklist of onboarding steps and enrich each item with:
1. A more specific, actionable description tailored to the new starter's role and department.
2. Any additional role-specific steps that are NOT in the base list but are clearly required.

Return your response as a valid JSON array of checklist items matching this schema:
{{
  "checklist": [
    {{
      "step_number": <int>,
      "title": "<string>",
      "description": "<string — specific, actionable>",
      "system": "<string>",
      "owner": "<string>",
      "priority": "<Critical|High|Medium|Low>",
      "tags": ["<string>", ...],
      "notes": "<string — any reviewer notes>"
    }},
    ...
  ]
}}

Rules:
- Keep all original steps. Do not remove or reorder them.
- Add new steps at the end with step_number > {max_step}.
- Be concise but specific. Tailor descriptions to the role.
- Do not invent systems that don't exist in a typical M365 environment.
"""

_SYSTEM_PROMPT_LEAVER = """You are an expert IT security engineer specialising in controlled
employee offboarding.

Your task is to review a base leaver checklist and enrich each item with specific, actionable
guidance tailored to the departing employee's context.

Return your response as a valid JSON array matching the schema:
{{
  "checklist": [
    {{
      "step_number": <int>,
      "title": "<string>",
      "description": "<string — specific, security-focused>",
      "system": "<string>",
      "owner": "<string>",
      "priority": "<Critical|High|Medium|Low>",
      "tags": ["<string>", ...],
      "notes": "<string>"
    }},
    ...
  ]
}}

CRITICAL: Maintain the security-ordered sequence. Steps 1-3 are ALWAYS critical and immediate.
"""


def _enrich_with_llm(
    base_checklist: list[ChecklistItem],
    form: dict[str, Any],
    mode: str = "onboarding",
) -> list[ChecklistItem]:
    """Use the LLM to enrich and potentially extend the base checklist."""
    llm = get_llm()
    base_json = json.dumps(
        [item.model_dump() for item in base_checklist],
        indent=2,
        default=str,
    )
    max_step = max(item.step_number for item in base_checklist) if base_checklist else 0

    if mode == "onboarding":
        system_content = _SYSTEM_PROMPT_ONBOARDING.format(max_step=max_step)
        user_content = (
            f"New starter details:\n"
            f"- Name: {form.get('first_name', '')} {form.get('last_name', '')}\n"
            f"- Role: {form.get('role', 'Unknown')}\n"
            f"- Department: {form.get('department', 'Unknown')}\n"
            f"- Location: {form.get('location', 'UK')}\n"
            f"- Start Date: {form.get('start_date', 'Unknown')}\n"
            f"- M365 Licence: {form.get('licence_sku', 'E3')}\n"
            f"- Additional Notes: {form.get('additional_notes', 'None')}\n\n"
            f"Base checklist (JSON):\n{base_json}\n\n"
            f"Enrich and extend this checklist for the role above."
        )
    else:
        system_content = _SYSTEM_PROMPT_LEAVER
        user_content = (
            f"Leaver details:\n"
            f"- Name: {form.get('first_name', '')} {form.get('last_name', '')}\n"
            f"- Email: {form.get('email', 'Unknown')}\n"
            f"- Last Working Day: {form.get('last_working_day', 'Unknown')}\n"
            f"- Reason: {form.get('reason', 'Unknown')}\n"
            f"- Notes: {form.get('additional_notes', 'None')}\n\n"
            f"Base checklist (JSON):\n{base_json}\n\n"
            f"Enrich this de-provisioning checklist for the leaver above."
        )

    messages = [
        SystemMessage(content=system_content),
        HumanMessage(content=user_content),
    ]

    logger.info(
        "PlannerAgent: calling LLM to enrich checklist (%d base items)", len(base_checklist)
    )
    response = llm.invoke(messages)
    raw = response.content.strip()

    # Strip markdown fences if present
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(lines[1:-1]) if lines[-1] == "```" else "\n".join(lines[1:])

    parsed = json.loads(raw)
    enriched_dicts = parsed.get("checklist", [])

    enriched = []
    for item_dict in enriched_dicts:
        try:
            enriched.append(ChecklistItem(**item_dict))
        except Exception as exc:
            logger.warning(
                "PlannerAgent: skipping malformed checklist item: %s — %s", item_dict, exc
            )

    return enriched or base_checklist  # fall back to base on parse failure


# ── Graph Node Functions ───────────────────────────────────────────────────────


def planner_node(state: OnboardingState) -> dict:
    """
    LangGraph node: PlannerAgent (onboarding).

    Reads the StarterForm from state, loads the onboarding matrix, filters
    for role/dept, calls the LLM to enrich, and returns an updated state
    with the populated checklist.
    """
    logger.info("PlannerAgent: starting onboarding planning")
    form = state.get("form", {})
    reviewer_notes = state.get("reviewer_notes", "")
    iteration = state.get("iteration", 0)

    role = form.get("role", "")
    department = form.get("department", "")

    # Load + filter matrix
    all_steps = _load_matrix(ONBOARDING_MATRIX_PATH)
    filtered_steps = _filter_matrix_for_role(all_steps, role, department)
    base_checklist = _matrix_to_checklist(filtered_steps)

    logger.info(
        "PlannerAgent: %d base steps loaded for %s / %s", len(base_checklist), role, department
    )

    # LLM enrichment — include reviewer notes on re-runs
    if reviewer_notes and iteration > 0:
        # Prepend reviewer feedback to additional_notes
        notes = f"[REVIEWER NOTES]: {reviewer_notes}\n{form.get('additional_notes', '')}"
        form = {**form, "additional_notes": notes}

    warnings = list(state.get("warnings", []))
    try:
        enriched = _enrich_with_llm(base_checklist, form, mode="onboarding")
    except Exception as exc:
        logger.error("PlannerAgent: LLM enrichment failed: %s — using base checklist", exc)
        enriched = base_checklist
        warnings.append(
            "PlannerAgent: LLM enrichment failed — checklist generated from the base "
            "role/department matrix only, without LLM-tailored additions."
        )

    logger.info("PlannerAgent: finalised checklist with %d items", len(enriched))

    return {
        "checklist": [item.model_dump() for item in enriched],
        "role_matrix": filtered_steps,
        "iteration": iteration + 1,
        "error": "",
        "warnings": warnings,
    }


def leaver_planner_node(state: LeaverState) -> dict:
    """
    LangGraph node: PlannerAgent (leaver/offboarding).

    Loads the leaver matrix and enriches it with LLM context from the LeaverForm.
    """
    logger.info("PlannerAgent: starting leaver planning")
    form = state.get("form", {})

    all_steps = _load_matrix(LEAVER_MATRIX_PATH)
    # Leaver matrix is always used in full, sorted by execution_order
    sorted_steps = sorted(
        all_steps, key=lambda s: s.get("execution_order", s.get("step_number", 99))
    )
    base_checklist = _matrix_to_checklist(sorted_steps)

    logger.info("PlannerAgent: %d leaver steps loaded", len(base_checklist))

    warnings = list(state.get("warnings", []))
    try:
        enriched = _enrich_with_llm(base_checklist, form, mode="leaver")
    except Exception as exc:
        logger.error("PlannerAgent: LLM enrichment failed: %s — using base checklist", exc)
        enriched = base_checklist
        warnings.append(
            "PlannerAgent: LLM enrichment failed — checklist generated from the base "
            "leaver matrix only, without LLM-tailored additions."
        )

    logger.info("PlannerAgent: leaver checklist finalised with %d items", len(enriched))

    return {
        "checklist": [item.model_dump() for item in enriched],
        "role_matrix": sorted_steps,
        "iteration": 1,
        "error": "",
        "warnings": warnings,
    }


# ── Convenience wrapper (used in tests) ───────────────────────────────────────
class PlannerAgent:
    """Thin wrapper for direct invocation outside the graph (e.g., testing)."""

    def run_onboarding(self, state: OnboardingState) -> dict:
        return planner_node(state)

    def run_leaver(self, state: LeaverState) -> dict:
        return leaver_planner_node(state)
