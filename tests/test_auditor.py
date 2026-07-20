"""
tests/test_auditor.py
─────────────────────────────────────────────────────────────────────────────
pytest: AuditorAgent gap detection tests.

These tests do NOT require an LLM — they test the pure auditing logic
directly via the AuditorAgent wrapper.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agents.auditor import AuditorAgent, _find_in_checklist, _normalise_title
from models.state import AuditSeverity

# ── Fixtures ──────────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).parent.parent
ONBOARDING_MATRIX_PATH = REPO_ROOT / "checklists" / "onboarding_matrix.json"
LEAVER_MATRIX_PATH = REPO_ROOT / "checklists" / "leaver_matrix.json"


@pytest.fixture
def onboarding_matrix() -> list[dict]:
    with open(ONBOARDING_MATRIX_PATH) as f:
        return json.load(f)["steps"]


@pytest.fixture
def leaver_matrix() -> list[dict]:
    with open(LEAVER_MATRIX_PATH) as f:
        return json.load(f)["steps"]


@pytest.fixture
def full_onboarding_checklist(onboarding_matrix) -> list[dict]:
    """Simulate a complete checklist that exactly matches the matrix."""
    return [
        {
            "step_number": step["step_number"],
            "title": step["title"],
            "description": step["description"],
            "system": step["system"],
            "owner": step["owner"],
            "priority": step["priority"],
            "tags": step.get("tags", []),
            "completed": False,
            "notes": "",
            "id": f"step_{step['step_number']:02d}",
        }
        for step in onboarding_matrix
    ]


@pytest.fixture
def complete_comms() -> dict:
    return {
        "welcome_email_subject": "Welcome to the team, Jane!",
        "welcome_email_html": "<p>Welcome Jane!</p>",
        "welcome_email_plain": "Welcome Jane!",
        "day1_guide_markdown": "# Day 1 Guide\n\nWelcome!",
    }


@pytest.fixture
def complete_scripts() -> dict[str, str]:
    return {
        "01_create_user.ps1": "#Requires -Version 7.0\n# Script content",
        "02_assign_licence.ps1": "#Requires -Version 7.0\n# Script content",
        "03_add_groups.ps1": "#Requires -Version 7.0\n# Script content",
        "04_configure_mfa.ps1": "#Requires -Version 7.0\n# Script content",
    }


# ── Unit tests: normalisation & matching helpers ───────────────────────────────

class TestNormalisation:
    def test_lowercase(self):
        assert _normalise_title("Create M365 User Account") == "create m365 user account"

    def test_punctuation_stripped(self):
        assert _normalise_title("MFA / SSPR setup!") == "mfa  sspr setup"

    def test_empty_string(self):
        assert _normalise_title("") == ""


class TestFindInChecklist:
    def test_exact_match(self):
        checklist = [{"title": "Create M365 user account"}]
        assert _find_in_checklist("Create M365 user account", checklist) is True

    def test_case_insensitive(self):
        checklist = [{"title": "CREATE M365 USER ACCOUNT"}]
        assert _find_in_checklist("Create M365 user account", checklist) is True

    def test_word_overlap_match(self):
        # 4/5 words overlap → ≥60% → should match
        checklist = [{"title": "Create new M365 user account in Azure"}]
        assert _find_in_checklist("Create M365 user account", checklist) is True

    def test_no_match(self):
        checklist = [{"title": "Completely unrelated step"}]
        assert _find_in_checklist("Create M365 user account", checklist) is False

    def test_empty_checklist(self):
        assert _find_in_checklist("Anything", []) is False


# ── AuditorAgent gap detection tests ──────────────────────────────────────────

class TestAuditorGapDetection:
    """Tests that verify the auditor correctly flags missing steps."""

    def test_all_steps_present_passes(
        self,
        onboarding_matrix,
        full_onboarding_checklist,
        complete_scripts,
        complete_comms,
    ):
        """When all required steps are present, the audit should PASS."""
        agent = AuditorAgent()
        report = agent.run_audit(
            required_matrix=onboarding_matrix,
            checklist=full_onboarding_checklist,
            scripts=complete_scripts,
            comms=complete_comms,
        )
        assert report.passed is True
        gap_findings = [f for f in report.findings if "Required step" in f.reason]
        assert len(gap_findings) == 0

    def test_missing_critical_step_fails(
        self,
        onboarding_matrix,
        full_onboarding_checklist,
        complete_scripts,
        complete_comms,
    ):
        """Removing a Critical step should cause the audit to FAIL."""
        # Remove the first critical step (Create M365 user account)
        truncated = [
            item for item in full_onboarding_checklist
            if "Create M365 user account" not in item["title"]
        ]
        assert len(truncated) < len(full_onboarding_checklist), "Step was not removed"

        agent = AuditorAgent()
        report = agent.run_audit(
            required_matrix=onboarding_matrix,
            checklist=truncated,
            scripts=complete_scripts,
            comms=complete_comms,
        )
        assert report.passed is False

        critical_findings = [
            f for f in report.findings
            if f.severity == AuditSeverity.CRITICAL.value
        ]
        assert len(critical_findings) >= 1

    def test_missing_high_step_fails(
        self,
        onboarding_matrix,
        full_onboarding_checklist,
        complete_scripts,
        complete_comms,
    ):
        """Removing a High-priority step should cause the audit to FAIL."""
        truncated = [
            item for item in full_onboarding_checklist
            if "SharePoint" not in item["title"]
        ]
        agent = AuditorAgent()
        report = agent.run_audit(
            required_matrix=onboarding_matrix,
            checklist=truncated,
            scripts=complete_scripts,
            comms=complete_comms,
        )
        assert report.passed is False

    def test_missing_low_step_does_not_fail(
        self,
        onboarding_matrix,
        full_onboarding_checklist,
        complete_scripts,
        complete_comms,
    ):
        """Removing a Low-priority step should NOT fail the audit (info finding only)."""
        truncated = [
            item for item in full_onboarding_checklist
            if "Day-30" not in item["title"]
        ]
        agent = AuditorAgent()
        report = agent.run_audit(
            required_matrix=onboarding_matrix,
            checklist=truncated,
            scripts=complete_scripts,
            comms=complete_comms,
        )
        # Low-priority gaps → INFO severity → should not set audit_failed
        assert report.passed is True

    def test_missing_scripts_fails(
        self,
        onboarding_matrix,
        full_onboarding_checklist,
        complete_comms,
    ):
        """When no scripts are generated, the audit should FAIL."""
        agent = AuditorAgent()
        report = agent.run_audit(
            required_matrix=onboarding_matrix,
            checklist=full_onboarding_checklist,
            scripts={},
            comms=complete_comms,
        )
        assert report.passed is False
        script_findings = [f for f in report.findings if "scripts" in f.step_title.lower()]
        assert len(script_findings) >= 1

    def test_errored_script_fails(
        self,
        onboarding_matrix,
        full_onboarding_checklist,
        complete_comms,
    ):
        """A script with an ERROR marker should cause the audit to FAIL."""
        agent = AuditorAgent()
        report = agent.run_audit(
            required_matrix=onboarding_matrix,
            checklist=full_onboarding_checklist,
            scripts={"01_create_user.ps1": "# ERROR: Template failed\n"},
            comms=complete_comms,
        )
        assert report.passed is False

    def test_missing_comms_flagged(
        self,
        onboarding_matrix,
        full_onboarding_checklist,
        complete_scripts,
    ):
        """Missing comms should be flagged as a HIGH finding."""
        agent = AuditorAgent()
        report = agent.run_audit(
            required_matrix=onboarding_matrix,
            checklist=full_onboarding_checklist,
            scripts=complete_scripts,
            comms={},
        )
        assert report.passed is False
        comms_findings = [f for f in report.findings if "email" in f.step_title.lower()]
        assert len(comms_findings) >= 1


# ── AuditReport structure tests ────────────────────────────────────────────────

class TestAuditReportStructure:
    def test_report_fields(
        self,
        onboarding_matrix,
        full_onboarding_checklist,
        complete_scripts,
        complete_comms,
    ):
        agent = AuditorAgent()
        report = agent.run_audit(
            required_matrix=onboarding_matrix,
            checklist=full_onboarding_checklist,
            scripts=complete_scripts,
            comms=complete_comms,
        )
        assert report.total_required == len(onboarding_matrix)
        assert report.generated_at is not None
        assert isinstance(report.findings, list)
        assert isinstance(report.passed, bool)

    def test_findings_have_required_fields(
        self,
        onboarding_matrix,
        complete_scripts,
        complete_comms,
    ):
        agent = AuditorAgent()
        report = agent.run_audit(
            required_matrix=onboarding_matrix,
            checklist=[],  # Empty checklist → lots of findings
            scripts=complete_scripts,
            comms=complete_comms,
        )
        assert report.passed is False
        assert len(report.findings) > 0
        for finding in report.findings:
            assert finding.severity is not None
            assert finding.step_title != ""
            assert finding.reason != ""
