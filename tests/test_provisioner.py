"""
tests/test_provisioner.py
─────────────────────────────────────────────────────────────────────────────
pytest: ProvisionerAgent tests — verifies PowerShell script rendering and
validation logic.

These tests use the Jinja2 rendering engine directly and do NOT call the LLM
or execute any PowerShell.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agents.provisioner import (
    LEAVER_SCRIPTS,
    ONBOARDING_SCRIPTS,
    ProvisionerAgent,
    _build_leaver_context,
    _build_onboarding_context,
    _regex_validate,
    _render_template,
)

REPO_ROOT = Path(__file__).parent.parent


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def sample_starter_form() -> dict:
    return {
        "first_name": "Jane",
        "last_name": "Smith",
        "role": "Software Engineer",
        "department": "Engineering",
        "start_date": "2025-09-01",
        "manager_email": "john.doe@contoso.com",
        "email_prefix": "jane.smith",
        "domain": "contoso.com",
        "licence_sku": "E3",
        "location": "UK",
        "additional_notes": "Joining the backend platform team.",
    }


@pytest.fixture
def sample_leaver_form() -> dict:
    return {
        "first_name": "John",
        "last_name": "Doe",
        "email": "john.doe@contoso.com",
        "last_working_day": "2025-09-30",
        "manager_email": "sarah.jones@contoso.com",
        "reason": "Resignation",
        "data_transfer_recipient": "sarah.jones@contoso.com",
        "hardware_return_required": True,
        "additional_notes": "",
    }


@pytest.fixture
def starter_context(sample_starter_form) -> dict:
    return _build_onboarding_context(sample_starter_form)


@pytest.fixture
def leaver_context(sample_leaver_form) -> dict:
    return _build_leaver_context(sample_leaver_form)


# ── Context building ───────────────────────────────────────────────────────────

class TestContextBuilding:
    def test_upn_constructed_correctly(self, starter_context):
        assert starter_context["upn"] == "jane.smith@contoso.com"

    def test_display_name_constructed(self, starter_context):
        assert starter_context["display_name"] == "Jane Smith"

    def test_licence_sku_present(self, starter_context):
        assert starter_context["licence_sku"] == "E3"

    def test_groups_list_not_empty(self, starter_context):
        assert len(starter_context["groups"]) > 0
        for group in starter_context["groups"]:
            assert "name" in group
            assert "id" in group

    def test_leaver_email_present(self, leaver_context):
        assert leaver_context["email"] == "john.doe@contoso.com"

    def test_leaver_display_name(self, leaver_context):
        assert leaver_context["display_name"] == "John Doe"

    def test_leaver_data_transfer_recipient(self, leaver_context):
        assert leaver_context["data_transfer_recipient"] == "sarah.jones@contoso.com"

    def test_leaver_defaults_to_manager_when_no_transfer_recipient(self, sample_leaver_form):
        form_without_recipient = {**sample_leaver_form, "data_transfer_recipient": None}
        ctx = _build_leaver_context(form_without_recipient)
        assert ctx["data_transfer_recipient"] == form_without_recipient["manager_email"]


# ── Template rendering ─────────────────────────────────────────────────────────

class TestTemplateRendering:
    """Test that all templates render without errors and contain expected content."""

    def test_create_user_template_renders(self, starter_context):
        rendered = _render_template("create_user.ps1.j2", starter_context)
        assert len(rendered) > 100
        assert "jane.smith@contoso.com" in rendered
        assert "Jane Smith" in rendered

    def test_create_user_contains_correct_upn(self, starter_context):
        rendered = _render_template("create_user.ps1.j2", starter_context)
        assert "jane.smith@contoso.com" in rendered

    def test_create_user_contains_department(self, starter_context):
        rendered = _render_template("create_user.ps1.j2", starter_context)
        assert "Engineering" in rendered

    def test_assign_licence_template_renders(self, starter_context):
        rendered = _render_template("assign_licence.ps1.j2", starter_context)
        assert "jane.smith@contoso.com" in rendered
        assert "E3" in rendered

    def test_add_groups_template_renders(self, starter_context):
        rendered = _render_template("add_groups.ps1.j2", starter_context)
        assert "jane.smith@contoso.com" in rendered
        # Should contain at least one group name
        assert "Engineering" in rendered

    def test_configure_mfa_template_renders(self, starter_context):
        rendered = _render_template("configure_mfa.ps1.j2", starter_context)
        assert "jane.smith@contoso.com" in rendered
        assert "MFA" in rendered

    def test_deprovision_template_renders(self, leaver_context):
        rendered = _render_template("deprovision_user.ps1.j2", leaver_context)
        assert "john.doe@contoso.com" in rendered
        assert "CRITICAL" in rendered.upper()

    def test_all_onboarding_templates_render(self, starter_context):
        """Smoke test: all onboarding templates should render without error."""
        for _script_name, template_name in ONBOARDING_SCRIPTS:
            rendered = _render_template(template_name, starter_context)
            assert rendered, f"Template {template_name} produced empty output"

    def test_all_leaver_templates_render(self, leaver_context):
        """Smoke test: all leaver templates should render without error."""
        for _script_name, template_name in LEAVER_SCRIPTS:
            rendered = _render_template(template_name, leaver_context)
            assert rendered, f"Template {template_name} produced empty output"

    def test_scripts_start_with_requires(self, starter_context):
        """All rendered scripts should have a #Requires header."""
        for _, template_name in ONBOARDING_SCRIPTS:
            rendered = _render_template(template_name, starter_context)
            assert rendered.strip().startswith("#Requires"), (
                f"{template_name} should start with #Requires"
            )

    def test_scripts_have_cmdletbinding(self, starter_context):
        """All rendered scripts should use [CmdletBinding()]."""
        for _, template_name in ONBOARDING_SCRIPTS:
            rendered = _render_template(template_name, starter_context)
            assert "[CmdletBinding" in rendered, (
                f"{template_name} should contain [CmdletBinding]"
            )


# ── Regex validation ───────────────────────────────────────────────────────────

class TestRegexValidation:
    def test_clean_script_passes(self, starter_context):
        rendered = _render_template("create_user.ps1.j2", starter_context)
        result = _regex_validate(rendered, "create_user.ps1")
        assert result["passed"] is True

    def test_injection_pattern_flagged(self):
        malicious_script = "#Requires -Version 7.0\n[CmdletBinding()]\nInvoke-Expression $userInput"
        result = _regex_validate(malicious_script, "test.ps1")
        assert result["passed"] is True  # regex validation passes but warns
        warning_messages = [w["message"] for w in result.get("warnings", [])]
        assert any("Invoke-Expression" in msg for msg in warning_messages)

    def test_validation_result_has_required_fields(self, starter_context):
        rendered = _render_template("create_user.ps1.j2", starter_context)
        result = _regex_validate(rendered, "create_user.ps1")
        assert "passed" in result
        assert "method" in result
        assert result["method"] == "regex"
        assert "warnings" in result
        assert "script_name" in result


# ── ProvisionerAgent integration ──────────────────────────────────────────────

class TestProvisionerAgentWrapper:
    def test_render_script_method(self, starter_context):
        agent = ProvisionerAgent()
        rendered = agent.render_script("create_user.ps1.j2", starter_context)
        assert "jane.smith@contoso.com" in rendered

    def test_validate_script_method(self, starter_context):
        agent = ProvisionerAgent()
        rendered = agent.render_script("create_user.ps1.j2", starter_context)
        result = agent.validate_script(rendered, "create_user.ps1")
        assert "passed" in result

    def test_provisioner_node_produces_correct_scripts(self, sample_starter_form):
        """
        Test provisioner_node against an OnboardingState dict.
        (No LLM call — only template rendering)
        """
        from agents.provisioner import provisioner_node
        state = {
            "form": sample_starter_form,
            "run_id": "test-run-001",
        }
        result = provisioner_node(state)
        assert "scripts" in result
        assert len(result["scripts"]) == len(ONBOARDING_SCRIPTS)
        for script_name, _ in ONBOARDING_SCRIPTS:
            assert script_name in result["scripts"]
            assert "jane.smith@contoso.com" in result["scripts"][script_name]

    def test_leaver_provisioner_node(self, sample_leaver_form):
        from agents.provisioner import leaver_provisioner_node
        state = {
            "form": sample_leaver_form,
            "run_id": "test-leaver-001",
        }
        result = leaver_provisioner_node(state)
        assert "scripts" in result
        assert len(result["scripts"]) == len(LEAVER_SCRIPTS)
        for script_name, _ in LEAVER_SCRIPTS:
            assert script_name in result["scripts"]
            assert "john.doe@contoso.com" in result["scripts"][script_name]
