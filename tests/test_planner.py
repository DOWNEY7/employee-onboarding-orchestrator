"""
tests/test_planner.py
─────────────────────────────────────────────────────────────────────────────
pytest: PlannerAgent tests — verifies checklist generation from the matrix.

These tests use the matrix loading and filtering logic directly (no LLM call)
to keep the test suite fast and deterministic.
"""

from __future__ import annotations

from pathlib import Path

from agents.planner import (
    _filter_matrix_for_role,
    _load_matrix,
    _matrix_to_checklist,
)
from models.state import ChecklistItem, Priority

REPO_ROOT = Path(__file__).parent.parent
ONBOARDING_MATRIX_PATH = REPO_ROOT / "checklists" / "onboarding_matrix.json"
LEAVER_MATRIX_PATH = REPO_ROOT / "checklists" / "leaver_matrix.json"


# ── Matrix loading ─────────────────────────────────────────────────────────────


class TestMatrixLoading:
    def test_onboarding_matrix_loads(self):
        steps = _load_matrix(ONBOARDING_MATRIX_PATH)
        assert isinstance(steps, list)
        assert len(steps) > 0

    def test_onboarding_matrix_has_25_steps(self):
        steps = _load_matrix(ONBOARDING_MATRIX_PATH)
        assert len(steps) == 25, f"Expected 25 steps, got {len(steps)}"

    def test_leaver_matrix_loads(self):
        steps = _load_matrix(LEAVER_MATRIX_PATH)
        assert isinstance(steps, list)
        assert len(steps) > 0

    def test_leaver_matrix_has_17_steps(self):
        steps = _load_matrix(LEAVER_MATRIX_PATH)
        assert len(steps) == 17, f"Expected 17 steps, got {len(steps)}"

    def test_matrix_steps_have_required_fields(self):
        steps = _load_matrix(ONBOARDING_MATRIX_PATH)
        required_fields = {"step_number", "title", "description", "system", "owner", "priority"}
        for step in steps:
            missing = required_fields - set(step.keys())
            assert not missing, f"Step {step.get('step_number')} missing fields: {missing}"

    def test_all_priorities_valid(self):
        valid_priorities = {"Critical", "High", "Medium", "Low"}
        steps = _load_matrix(ONBOARDING_MATRIX_PATH)
        for step in steps:
            assert step["priority"] in valid_priorities, (
                f"Invalid priority '{step['priority']}' in step {step['step_number']}"
            )

    def test_leaver_matrix_has_critical_first_steps(self):
        """First 3 leaver steps must all be Critical."""
        steps = _load_matrix(LEAVER_MATRIX_PATH)
        sorted_steps = sorted(
            steps, key=lambda s: s.get("execution_order", s.get("step_number", 99))
        )
        for i, step in enumerate(sorted_steps[:3]):
            assert step["priority"] == "Critical", (
                f"Step {i + 1} '{step['title']}' must be Critical, got {step['priority']}"
            )


# ── Role/department filtering ──────────────────────────────────────────────────


class TestMatrixFiltering:
    def test_wildcard_returns_all_steps(self):
        steps = _load_matrix(ONBOARDING_MATRIX_PATH)
        filtered = _filter_matrix_for_role(
            steps, role="Software Engineer", department="Engineering"
        )
        # All steps have required_for=['*'], so all should be returned
        assert len(filtered) == len(steps)

    def test_empty_role_still_returns_wildcard_steps(self):
        steps = _load_matrix(ONBOARDING_MATRIX_PATH)
        filtered = _filter_matrix_for_role(steps, role="", department="")
        assert len(filtered) == len(steps)


# ── Checklist item conversion ──────────────────────────────────────────────────


class TestChecklistConversion:
    def test_converts_to_checklist_items(self):
        steps = _load_matrix(ONBOARDING_MATRIX_PATH)
        checklist = _matrix_to_checklist(steps)
        assert len(checklist) == len(steps)
        assert all(isinstance(item, ChecklistItem) for item in checklist)

    def test_checklist_has_minimum_20_items_for_it_dept(self):
        """IT department must have at least 20 checklist items (per spec)."""
        steps = _load_matrix(ONBOARDING_MATRIX_PATH)
        filtered = _filter_matrix_for_role(steps, role="Software Engineer", department="IT")
        checklist = _matrix_to_checklist(filtered)
        assert len(checklist) >= 20, f"Expected ≥20 items, got {len(checklist)}"

    def test_checklist_priorities_are_valid_enum_values(self):
        steps = _load_matrix(ONBOARDING_MATRIX_PATH)
        checklist = _matrix_to_checklist(steps)
        valid_priorities = {p.value for p in Priority}
        for item in checklist:
            assert item.priority in valid_priorities, (
                f"Item '{item.title}' has invalid priority '{item.priority}'"
            )

    def test_checklist_items_have_ids(self):
        steps = _load_matrix(ONBOARDING_MATRIX_PATH)
        checklist = _matrix_to_checklist(steps)
        ids = [item.id for item in checklist]
        assert all(ids), "All items should have non-empty IDs"
        # IDs should be unique
        assert len(ids) == len(set(ids)), "Checklist item IDs should be unique"

    def test_step_numbers_are_sequential(self):
        steps = _load_matrix(ONBOARDING_MATRIX_PATH)
        checklist = _matrix_to_checklist(steps)
        step_numbers = [item.step_number for item in checklist]
        assert step_numbers == sorted(step_numbers), "Steps should be in ascending order"

    def test_critical_steps_present_in_checklist(self):
        """Verify that known Critical steps appear in the converted checklist."""
        critical_titles = {
            "Create M365 user account",
            "Assign appropriate licence (E3 / E5 / F3)",
            "Enable MFA and set authentication methods",
            "Acceptable Use Policy sent and acknowledged",
        }
        steps = _load_matrix(ONBOARDING_MATRIX_PATH)
        checklist = _matrix_to_checklist(steps)
        titles = {item.title for item in checklist}
        for expected_title in critical_titles:
            assert expected_title in titles, f"Missing critical step: '{expected_title}'"

    def test_leaver_checklist_has_security_steps(self):
        """Leaver checklist must include the top security-ordered steps."""
        security_titles = {
            "Revoke all active sessions and refresh tokens",
            "Disable Azure AD / Entra ID account",
            "Remove from all security groups",
        }
        steps = _load_matrix(LEAVER_MATRIX_PATH)
        sorted_steps = sorted(
            steps, key=lambda s: s.get("execution_order", s.get("step_number", 99))
        )
        checklist = _matrix_to_checklist(sorted_steps)
        titles = {item.title for item in checklist}
        for expected_title in security_titles:
            assert expected_title in titles, f"Missing leaver security step: '{expected_title}'"


# ── Schema validation ──────────────────────────────────────────────────────────


class TestChecklistItemSchema:
    def test_model_dump_roundtrip(self):
        steps = _load_matrix(ONBOARDING_MATRIX_PATH)
        checklist = _matrix_to_checklist(steps[:3])
        for item in checklist:
            dumped = item.model_dump()
            restored = ChecklistItem(**dumped)
            assert restored.title == item.title
            assert restored.priority == item.priority

    def test_completed_defaults_to_false(self):
        steps = _load_matrix(ONBOARDING_MATRIX_PATH)
        checklist = _matrix_to_checklist(steps)
        assert all(item.completed is False for item in checklist)
