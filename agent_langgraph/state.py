from __future__ import annotations

from typing import Any, TypedDict


class GraphState(TypedDict, total=False):
    pdf: str
    user_prompt: str
    workspace: str
    max_attempts: int
    max_minutes: float
    started_at: float
    attempt: int
    status: str
    error_class: str
    error: str
    staged_pdf: str
    spec_path: str
    adapter_path: str
    request_path: str
    attempt_dir: str
    strategy_index: int
    strategy: dict[str, Any]
    gates: dict[str, Any]
    history: list[dict[str, Any]]
    pipeline_hash: str
    final_path: str
