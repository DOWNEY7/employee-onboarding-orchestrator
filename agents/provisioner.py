"""
agents/provisioner.py
─────────────────────────────────────────────────────────────────────────────
ProvisionerAgent — generates PowerShell scripts for each M365/Azure AD
checklist item by rendering Jinja2 templates.

Scripts are ARTIFACTS ONLY — they are never executed by this system.

Validation (optional):
  If `pwsh` is on PATH, rendered scripts are validated with PSScriptAnalyzer.
  If not available, a lightweight regex-based syntax check is performed instead.

Graph node:
    provisioner_node(state: OnboardingState) -> dict
    leaver_provisioner_node(state: LeaverState) -> dict
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from models.state import ChecklistItem, LeaverState, OnboardingState

logger = logging.getLogger(__name__)

# ── Paths ──────────────────────────────────────────────────────────────────────
_REPO_ROOT = Path(__file__).parent.parent
TEMPLATES_DIR = _REPO_ROOT / "scripts" / "templates"

# ── Jinja2 environment ─────────────────────────────────────────────────────────
_jinja_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    undefined=StrictUndefined,
    autoescape=False,
    keep_trailing_newline=True,
)

# ── Template mapping: checklist tag → template filename ───────────────────────
TEMPLATE_MAP: dict[str, str] = {
    "identity":         "create_user.ps1.j2",
    "licensing":        "assign_licence.ps1.j2",
    "groups":           "add_groups.ps1.j2",
    "mfa":              "configure_mfa.ps1.j2",
    "disable":          "deprovision_user.ps1.j2",  # leaver only
    "sessions":         "deprovision_user.ps1.j2",  # leaver only
}

# Output names mapped to template file
ONBOARDING_SCRIPTS: list[tuple[str, str]] = [
    ("01_create_user.ps1",     "create_user.ps1.j2"),
    ("02_assign_licence.ps1",  "assign_licence.ps1.j2"),
    ("03_add_groups.ps1",      "add_groups.ps1.j2"),
    ("04_configure_mfa.ps1",   "configure_mfa.ps1.j2"),
]

LEAVER_SCRIPTS: list[tuple[str, str]] = [
    ("01_deprovision_user.ps1", "deprovision_user.ps1.j2"),
]


# ── Regex-based syntax check (fallback when pwsh not available) ────────────────

_PS_SUSPICIOUS_PATTERNS = [
    (r"Invoke-Expression\s+\$", "Avoid Invoke-Expression with variables (injection risk)"),
    (r"iex\s+\$",               "Avoid iex alias with variables (injection risk)"),
    (r"ConvertTo-SecureString\s+[\"'][^\"']+[\"']\s+-AsPlainText\s+-Force(?!\s+\$)", "Plain-text password in script body"),
    (r"\bstart-process\b.*-credential",  "Start-Process with credential — review carefully"),
]


def _regex_validate(script_content: str, script_name: str) -> dict[str, Any]:
    """Lightweight regex-based security and syntax check."""
    warnings = []
    for pattern, message in _PS_SUSPICIOUS_PATTERNS:
        if re.search(pattern, script_content, re.IGNORECASE):
            warnings.append({"rule": "SecurityCheck", "message": message, "severity": "Warning"})

    # Check for required headers
    if "#Requires -Version" not in script_content:
        warnings.append({"rule": "BestPractice", "message": "Missing #Requires -Version", "severity": "Information"})
    if "[CmdletBinding" not in script_content:
        warnings.append({"rule": "BestPractice", "message": "Missing [CmdletBinding()]", "severity": "Information"})

    return {
        "passed": True,
        "method": "regex",
        "warnings": warnings,
        "script_name": script_name,
    }


def _psscriptanalyzer_validate(script_content: str, script_name: str) -> dict[str, Any]:
    """
    Run PSScriptAnalyzer via pwsh subprocess.
    Returns gracefully if pwsh is not on PATH.
    """
    pwsh = shutil.which("pwsh") or shutil.which("powershell")
    if not pwsh:
        logger.info("ProvisionerAgent: pwsh not found — using regex validation for %s", script_name)
        return _regex_validate(script_content, script_name)

    with tempfile.NamedTemporaryFile(suffix=".ps1", mode="w", encoding="utf-8", delete=False) as tmp:
        tmp.write(script_content)
        tmp_path = tmp.name

    try:
        result = subprocess.run(
            [
                pwsh, "-NoProfile", "-NonInteractive", "-Command",
                (
                    f"$results = Invoke-ScriptAnalyzer -Path '{tmp_path}' -Severity @('Error','Warning') "
                    f"-IncludeDefaultRules; "
                    f"$results | ConvertTo-Json -Depth 3"
                ),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode != 0:
            logger.warning("PSScriptAnalyzer stderr: %s", result.stderr[:500])
            return _regex_validate(script_content, script_name)

        import json
        output = result.stdout.strip()
        if not output or output == "null":
            findings = []
        else:
            raw = json.loads(output)
            findings = raw if isinstance(raw, list) else [raw]

        errors = [f for f in findings if f.get("Severity") == "Error"]
        warnings = [f for f in findings if f.get("Severity") == "Warning"]

        return {
            "passed": len(errors) == 0,
            "method": "PSScriptAnalyzer",
            "errors": errors,
            "warnings": warnings,
            "script_name": script_name,
        }

    except (subprocess.TimeoutExpired, FileNotFoundError, Exception) as exc:
        logger.warning("PSScriptAnalyzer failed for %s: %s — falling back to regex", script_name, exc)
        return _regex_validate(script_content, script_name)
    finally:
        Path(tmp_path).unlink(missing_ok=True)


# ── Template rendering ─────────────────────────────────────────────────────────

def _render_template(template_name: str, context: dict[str, Any]) -> str:
    """Render a Jinja2 template with the given context."""
    template = _jinja_env.get_template(template_name)
    return template.render(**context)


def _build_onboarding_context(form: dict[str, Any]) -> dict[str, Any]:
    """Build the Jinja2 context dict from a StarterForm dict."""
    return {
        "first_name":    form.get("first_name", ""),
        "last_name":     form.get("last_name", ""),
        "display_name":  f"{form.get('first_name', '')} {form.get('last_name', '')}".strip(),
        "role":          form.get("role", ""),
        "department":    form.get("department", ""),
        "start_date":    str(form.get("start_date", "")),
        "email_prefix":  form.get("email_prefix", ""),
        "domain":        form.get("domain", "contoso.com"),
        "upn":           f"{form.get('email_prefix', '')}@{form.get('domain', 'contoso.com')}",
        "licence_sku":   form.get("licence_sku", "E3"),
        "location":      form.get("location", "GB"),
        # Groups placeholder — provisioner generates stubs; real GUIDs set by admin
        "groups": [
            {"name": f"{form.get('department', 'Dept')}-All", "id": "00000000-0000-0000-0000-000000000001"},
            {"name": f"{form.get('department', 'Dept')}-Security", "id": "00000000-0000-0000-0000-000000000002"},
        ],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _build_leaver_context(form: dict[str, Any]) -> dict[str, Any]:
    """Build the Jinja2 context dict from a LeaverForm dict."""
    manager_email = form.get("manager_email", "")
    return {
        "first_name":              form.get("first_name", ""),
        "last_name":               form.get("last_name", ""),
        "display_name":            f"{form.get('first_name', '')} {form.get('last_name', '')}".strip(),
        "email":                   form.get("email", ""),
        "last_working_day":        str(form.get("last_working_day", "")),
        "reason":                  form.get("reason", ""),
        "data_transfer_recipient": form.get("data_transfer_recipient") or manager_email,
        "hardware_return_required": form.get("hardware_return_required", True),
        "generated_at":            datetime.now(timezone.utc).isoformat(),
    }


# ── Graph Node Functions ───────────────────────────────────────────────────────

def provisioner_node(state: OnboardingState) -> dict:
    """
    LangGraph node: ProvisionerAgent (onboarding).

    Renders PowerShell scripts from Jinja2 templates and validates them.
    """
    logger.info("ProvisionerAgent: rendering onboarding scripts")
    form = state.get("form", {})
    context = _build_onboarding_context(form)

    scripts: dict[str, str] = {}
    validations: dict[str, Any] = {}

    for script_name, template_name in ONBOARDING_SCRIPTS:
        try:
            rendered = _render_template(template_name, context)
            scripts[script_name] = rendered
            validations[script_name] = _psscriptanalyzer_validate(rendered, script_name)
            logger.info("ProvisionerAgent: rendered %s (%d chars)", script_name, len(rendered))
        except Exception as exc:
            logger.error("ProvisionerAgent: failed to render %s: %s", script_name, exc)
            scripts[script_name] = f"# ERROR: Failed to render {template_name}\n# {exc}\n"
            validations[script_name] = {"passed": False, "method": "error", "error": str(exc)}

    logger.info("ProvisionerAgent: %d scripts rendered", len(scripts))
    return {"scripts": scripts, "script_validation": validations, "error": ""}


def leaver_provisioner_node(state: LeaverState) -> dict:
    """
    LangGraph node: ProvisionerAgent (leaver).
    """
    logger.info("ProvisionerAgent: rendering leaver scripts")
    form = state.get("form", {})
    context = _build_leaver_context(form)

    scripts: dict[str, str] = {}
    validations: dict[str, Any] = {}

    for script_name, template_name in LEAVER_SCRIPTS:
        try:
            rendered = _render_template(template_name, context)
            scripts[script_name] = rendered
            validations[script_name] = _psscriptanalyzer_validate(rendered, script_name)
            logger.info("ProvisionerAgent: rendered %s (%d chars)", script_name, len(rendered))
        except Exception as exc:
            logger.error("ProvisionerAgent: failed to render %s: %s", script_name, exc)
            scripts[script_name] = f"# ERROR: Failed to render {template_name}\n# {exc}\n"
            validations[script_name] = {"passed": False, "method": "error", "error": str(exc)}

    return {"scripts": scripts, "script_validation": validations, "error": ""}


class ProvisionerAgent:
    """Thin wrapper for direct invocation outside the graph (e.g., testing)."""

    def run_onboarding(self, state: OnboardingState) -> dict:
        return provisioner_node(state)

    def run_leaver(self, state: LeaverState) -> dict:
        return leaver_provisioner_node(state)

    def render_script(self, template_name: str, context: dict[str, Any]) -> str:
        """Directly render a named template — useful in tests."""
        return _render_template(template_name, context)

    def validate_script(self, content: str, name: str = "test.ps1") -> dict[str, Any]:
        """Validate rendered script content — useful in tests."""
        return _psscriptanalyzer_validate(content, name)
