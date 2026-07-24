"""
graph/leaver_graph.py
─────────────────────────────────────────────────────────────────────────────
LangGraph StateGraph for the employee leaver/offboarding workflow.

Graph topology:
    START → planner → provisioner → comms → human_gate ─┐
                                                         │ (rejected → planner)
                ┌────────────────────────────────────────┘
                │ (approved)
                ▼
            auditor → END

Identical structure to onboarding_graph but uses leaver-specific node
functions and the LeaverState TypedDict.
"""

from __future__ import annotations

import logging
import os
from typing import Literal

from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from agents.auditor import leaver_auditor_node
from agents.comms import leaver_comms_node
from agents.human_gate import leaver_human_gate_node, route_after_approval
from agents.planner import leaver_planner_node
from agents.provisioner import leaver_provisioner_node
from models.state import ApprovalStatus, LeaverState

logger = logging.getLogger(__name__)


# ── Max-rejections fallback ────────────────────────────────────────────────────


def leaver_max_rejections_node(state: LeaverState) -> dict:
    logger.error(
        "LeaverGraph: max rejection iterations reached for run %s",
        state.get("run_id", "unknown"),
    )
    return {
        "error": (
            "Maximum review iterations (3) reached without approval. "
            "The de-provisioning pack has been escalated for manual review."
        ),
        "approval_status": ApprovalStatus.REJECTED.value,
    }


# ── Error-sink node + routing helper ───────────────────────────────────────────────


def leaver_error_sink_node(state: LeaverState) -> dict:
    """
    Terminal node reached when any agent sets state["error"] to a non-empty string.
    Logs the failure so it surfaces in structured logs and the /status response.
    """
    error = state.get("error", "unknown error")
    run_id = state.get("run_id", "unknown")
    logger.error("LeaverGraph: run %s terminated early with error: %s", run_id, error)
    return {}


def _error_or(next_node: str):
    """Factory returning a routing function that short-circuits to error_sink on failure."""

    def _route(state: LeaverState) -> str:
        return "error_sink" if state.get("error") else next_node

    _route.__name__ = f"_error_or_{next_node}"
    return _route


def _route(state: LeaverState) -> Literal["auditor", "planner", "max_rejections"]:
    return route_after_approval(state)  # type: ignore[return-value]


# ── Graph builder ─────────────────────────────────────────────────────────────


def build_leaver_graph(
    checkpointer=None,
    db_path: str | None = None,
) -> tuple:
    """
    Build and compile the leaver StateGraph.

    Returns:
        (compiled_graph, checkpointer)
    """
    if checkpointer is None:
        if db_path is None:
            raw_url = os.getenv("DATABASE_URL", "sqlite:///./onboarding.db")
            db_path = raw_url.replace("sqlite:///", "")
        try:
            import sqlite3

            conn = sqlite3.connect(db_path, check_same_thread=False)
            checkpointer = SqliteSaver(conn)
            checkpointer.setup()
            logger.info("LeaverGraph: using SqliteSaver at %s", db_path)
        except Exception as exc:
            logger.warning("LeaverGraph: SqliteSaver unavailable (%s) — using MemorySaver", exc)
            checkpointer = MemorySaver()

    builder = StateGraph(LeaverState)

    builder.add_node("planner", leaver_planner_node)
    builder.add_node("provisioner", leaver_provisioner_node)
    builder.add_node("comms", leaver_comms_node)
    builder.add_node("human_gate", leaver_human_gate_node)
    builder.add_node("auditor", leaver_auditor_node)
    builder.add_node("max_rejections", leaver_max_rejections_node)
    builder.add_node("error_sink", leaver_error_sink_node)

    builder.add_edge(START, "planner")
    builder.add_conditional_edges(
        "planner",
        _error_or("provisioner"),
        {"provisioner": "provisioner", "error_sink": "error_sink"},
    )
    builder.add_conditional_edges(
        "provisioner",
        _error_or("comms"),
        {"comms": "comms", "error_sink": "error_sink"},
    )
    builder.add_conditional_edges(
        "comms",
        _error_or("human_gate"),
        {"human_gate": "human_gate", "error_sink": "error_sink"},
    )

    builder.add_conditional_edges(
        "human_gate",
        _route,
        {
            "auditor": "auditor",
            "planner": "planner",
            "max_rejections": "max_rejections",
        },
    )

    builder.add_edge("auditor", END)
    builder.add_edge("max_rejections", END)
    builder.add_edge("error_sink", END)

    graph = builder.compile(
        checkpointer=checkpointer,
        interrupt_before=["human_gate"],
    )

    logger.info("LeaverGraph: compiled successfully")
    return graph, checkpointer
