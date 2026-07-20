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

    builder.add_node("planner",        leaver_planner_node)
    builder.add_node("provisioner",    leaver_provisioner_node)
    builder.add_node("comms",          leaver_comms_node)
    builder.add_node("human_gate",     leaver_human_gate_node)
    builder.add_node("auditor",        leaver_auditor_node)
    builder.add_node("max_rejections", leaver_max_rejections_node)

    builder.add_edge(START,         "planner")
    builder.add_edge("planner",     "provisioner")
    builder.add_edge("provisioner", "comms")
    builder.add_edge("comms",       "human_gate")

    builder.add_conditional_edges(
        "human_gate",
        _route,
        {
            "auditor":        "auditor",
            "planner":        "planner",
            "max_rejections": "max_rejections",
        },
    )

    builder.add_edge("auditor",        END)
    builder.add_edge("max_rejections", END)

    graph = builder.compile(
        checkpointer=checkpointer,
        interrupt_before=["human_gate"],
    )

    logger.info("LeaverGraph: compiled successfully")
    return graph, checkpointer
