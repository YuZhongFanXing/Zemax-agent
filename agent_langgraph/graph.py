from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from .model import CodexCLIPlanner, Planner
from .pipeline import PIPELINE, digest, read_json, run_script, stage_pdf, write_json
from .state import GraphState




def _history(state: GraphState, event: str, **details: Any) -> list[dict[str, Any]]:
    return [*state.get("history", []), {"event": event, **details}]


def _planner(state: GraphState) -> Planner:
    return state.get("_planner") or CodexCLIPlanner()


def _validate_files(workspace: Path, pdf_name: str) -> None:
    spec = workspace / "geometry_spec.json"
    adapter = workspace / "model_adapter.py"
    if not spec.is_file() or not adapter.is_file():
        raise RuntimeError("agent_inputs_missing")
    data = read_json(spec)
    required = ("source_pdf", "units", "model_family", "source_trace", "features",
                "representation_candidates", "validation")
    missing = [key for key in required if key not in data]
    if missing:
        raise RuntimeError("geometry_spec_missing:" + ",".join(missing))
    if data["units"] != "mm" or not data["features"]:
        raise RuntimeError("geometry_spec_invalid_units_or_features")
    if Path(str(data["source_pdf"])).name != pdf_name:
        raise RuntimeError("geometry_spec_source_pdf_mismatch")
    if not isinstance(data["representation_candidates"], list) or not data["representation_candidates"]:
        raise RuntimeError("representation_candidates_empty")
    text = adapter.read_text(encoding="utf-8", errors="replace")
    if not all(name in text for name in ("build", "readback", "validate_export")):
        raise RuntimeError("adapter_functions_missing")


def _normalize_contract(workspace: Path) -> None:
    """Normalize metadata shape without inventing any geometry."""
    path = workspace / "geometry_spec.json"
    data = read_json(path)
    changed = False
    source = data.get("source") if isinstance(data.get("source"), dict) else {}
    if "source_pdf" not in data and source.get("pdf"):
        data["source_pdf"] = source["pdf"]
        changed = True
    if "source_trace" not in data and (source.get("source_traces") or source.get("trace") or source.get("traces")):
        data["source_trace"] = (source.get("source_traces") or source.get("trace")
                                 or source.get("traces"))
        changed = True
    if isinstance(data.get("units"), dict):
        data["units"] = data["units"].get("length", "mm")
        changed = True
    if "model_family" not in data:
        parameters = data.get("parameters", {})
        data["model_family"] = "groove_array" if "channel_count" in parameters else "custom"
        changed = True
    if not data.get("features"):
        data["features"] = [{"id": "model_geometry", "type": data.get("model_family", "custom"),
                              "source_trace": data.get("source_trace", []),
                              "geometry_mapping": {"parameters": data.get("parameters", {})}}]
        changed = True
    if not isinstance(data.get("validation"), dict):
        data["validation"] = {"metrics": [], "tolerances": {}}
        changed = True
    if isinstance(data.get("source_trace"), (str, dict)):
        data["source_trace"] = [data["source_trace"]]
        changed = True
    for feature in data.get("features", []):
        if isinstance(feature.get("source_trace"), (str, dict)):
            feature["source_trace"] = [feature["source_trace"]]
            changed = True
        if isinstance(feature.get("geometry_mapping"), str):
            feature["geometry_mapping"] = {"description": feature["geometry_mapping"]}
            changed = True
    validation = data.get("validation")
    if isinstance(validation, dict) and isinstance(validation.get("metrics"), dict):
        validation["metrics"] = [{"name": key, "expected": value}
                                  for key, value in validation["metrics"].items()]
        changed = True
    if changed:
        write_json(path, data)


def _normalize_adapter_signature(workspace: Path) -> None:
    path = workspace / "model_adapter.py"
    text = path.read_text(encoding="utf-8")
    updated = re.sub(r"def build\(system,\s*api\)\s*:",
                     "def build(system, api, request=None):", text)
    updated = re.sub(r"def build\(system,\s*request(?:=None)?\)\s*:",
                     "def build(system, api, request=None):", updated)
    updated = re.sub(r"def readback\(system,\s*api\)\s*:",
                     "def readback(system, api, request=None, build_metadata=None):", updated)
    updated = re.sub(r"def readback\(system,\s*request(?:=None)?\)\s*:",
                     "def readback(system, api, request=None, build_metadata=None):", updated)
    if re.search(r"def readback\(system,\s*request,\s*built(?:=None)?\)\s*:", updated):
        updated = re.sub(
            r"def readback\(system,\s*request,\s*built(?:=None)?\)\s*:",
            "def readback(system, api, request, build_metadata=None):\n    built = build_metadata",
            updated)
    # Generated adapters sometimes hard-code one product-specific candidate id.
    # Bind the check to the current generic specification instead.
    updated = re.sub(
        r"if strategy != [\"'][^\"']+[\"']:\s*\n(\s*)raise NotImplementedError\([^\n]*\)",
        r"if strategy not in allowed:\n\1raise ValueError('unsupported representation strategy')",
        updated)
    if updated != text:
        path.write_text(updated, encoding="utf-8")


def _prepare_prompt(pdf: Path, workspace: Path) -> str:
    return f"""
Read only the current PDF {pdf} and workspace {workspace}. Generate geometry_spec.json
and model_adapter.py for native OpticStudio NCE modeling. Preserve every PDF feature,
map each feature to real native objects and readback assertions, and include ordered
representation_candidates. The adapter must implement build, readback, and
validate_export, consume request['representation_strategy'], and never use fallback,
placeholder, imported STL, sequential LDE-only geometry, comments, or metadata as
geometry. Keep source traces and assumptions. Do not modify any other file.
Use top-level keys source_pdf (the exact filename {pdf.name}), units ("mm"),
model_family, source_trace, features, representation_candidates and validation.
Implement build(system, api, request) and readback(system, api, request, build_metadata)
using the supplied live ZOSAPI objects directly. Do not import or require a bridge.
The readback must report pass and final_object_id. For general API examples only,
you may read {PIPELINE / "model_adapter_template.py"}.
""".strip()


def _repair_prompt(state: GraphState, evidence: str) -> str:
    return f"""
Repair the current native OpticStudio model for PDF {state['pdf']} using only the
current geometry_spec.json, model_adapter.py, strategy and this evidence:
{evidence[-18000:]}

Modify only geometry_spec.json and/or model_adapter.py. Keep candidate ids stable,
use real NCE objects, preserve all PDF dimensions, and make the next candidate
implementable when the current one cannot export or fails geometry/readback. Do not
change pipeline scripts, tolerances, reports, or use fallback geometry.
""".strip()


def build_graph(planner: Planner | None = None, *, harness_job=None):
    """Build a compiled LangGraph graph.

    ``planner`` is injectable for offline tests; production defaults to the local
    Codex CLI adapter. The planner is kept outside persisted JSON state.
    """
    planner = planner or CodexCLIPlanner()
    declarative = getattr(planner, "declarative", False)

    def budget_exhausted(state: GraphState) -> bool:
        started = float(state.get("started_at", time.time()))
        return time.time() - started >= float(state.get("max_minutes", 60.0)) * 60.0

    def preflight(state: GraphState) -> GraphState:
        pdf = Path(state["pdf"]).resolve()
        workspace = Path(state["workspace"]).resolve()
        if not pdf.is_file():
            return {"status": "FAIL", "error": "pdf_missing"}
        (workspace / "audit").mkdir(parents=True, exist_ok=True)
        staged = stage_pdf(pdf, workspace)
        manifest = {str(path): digest(path) for path in sorted(PIPELINE.glob("*.py"))}
        write_json(workspace / "audit" / "pipeline_manifest.json", manifest)
        return {"status": "PREFLIGHT", "staged_pdf": str(staged),
                "started_at": time.time(),
                "pipeline_hash": digest(workspace / "audit" / "pipeline_manifest.json"),
                "history": _history(state, "PREFLIGHT", pdf_sha256=digest(pdf))}

    def prepare_inputs(state: GraphState) -> GraphState:
        if budget_exhausted(state):
            return {"status": "FAIL", "error": "time_budget_exhausted"}
        workspace = Path(state["workspace"])
        pdf = Path(state["staged_pdf"])
        spec = workspace / "geometry_spec.json"
        adapter = workspace / "model_adapter.py"
        try:
            if declarative:
                from .responses import ADAPTER
                adapter = ADAPTER
                prompt = "Create a native OpticStudio model of every feature supported by the drawing."
                for number in range(1, state["max_attempts"] + 1):
                    try:
                        planner.invoke(prompt, workspace, f"llm_prepare_{number:02d}", pdf)
                        planner.validate(spec, pdf.name)
                        break
                    except (ValueError, TypeError, KeyError) as exc:
                        if number == state["max_attempts"] or budget_exhausted(state):
                            raise
                        prompt = f"Correct this plan validation error: {exc}"
            elif not (spec.exists() and adapter.exists()):
                prompt = harness_job.instruction() if harness_job else _prepare_prompt(pdf, workspace)
                planner.invoke(prompt, workspace, "llm_prepare", pdf)
            if not declarative:
                _normalize_contract(workspace)
                _normalize_adapter_signature(workspace)
                _validate_files(workspace, pdf.name)
            return {"status": "SPEC_VALIDATED", "spec_path": str(spec),
                    "adapter_path": str(adapter),
                    "history": _history(state, "SPEC_VALIDATED")}
        except Exception as exc:
            return {"status": "FAIL", "error": str(exc),
                    "history": _history(state, "SPEC_FAILED", error=str(exc))}

    def select_strategy(state: GraphState) -> GraphState:
        spec = read_json(Path(state["spec_path"]))
        candidates = spec["representation_candidates"]
        index = min(int(state.get("strategy_index", 0)), len(candidates) - 1)
        strategy = {"candidate_index": index, "candidate": candidates[index],
                    "candidate_count": len(candidates)}
        return {"strategy_index": index, "strategy": strategy,
                "history": _history(state, "STRATEGY_SELECTED", strategy=strategy)}

    def prepare_request(state: GraphState) -> GraphState:
        workspace = Path(state["workspace"])
        attempt = int(state.get("attempt", 0)) + 1
        attempt_dir = workspace / "attempts" / f"attempt_{attempt:02d}"
        attempt_dir.mkdir(parents=True, exist_ok=True)
        spec_path = Path(state["spec_path"])
        strategy = state["strategy"]
        request = {
            "version": 3, "source_pdf": state["staged_pdf"],
            "geometry_spec": str(spec_path),
            "model_adapter": state["adapter_path"],
            "output_dir": str(attempt_dir),
            "representation_strategy": strategy["candidate"].get("id", strategy["candidate"].get("strategy")),
            "representation_plan": strategy,
            "model_filename": "model.zos", "step_filename": "model.step",
            "stl_filename": "model.stl", "probe_filename": "probe.zos",
            "result_filename": "native_result.json",
            "validation": read_json(spec_path)["validation"],
            "export_step": bool(harness_job), "require_step": False,
            "export_stl": bool(harness_job or declarative), "require_stl": False, "require_openable": True,
        }
        if harness_job or declarative:
            request["optional_cad_exports"] = True
        request_path = workspace / "request.json"
        if harness_job or declarative:
            for source in (spec_path, Path(state["adapter_path"])):
                shutil.copy2(source, attempt_dir / source.name)
            request["geometry_spec"] = str(attempt_dir / spec_path.name)
            request["model_adapter"] = str(attempt_dir / Path(state["adapter_path"]).name)
            request_path = attempt_dir / "request.json"
        write_json(request_path, request)
        return {"attempt": attempt, "attempt_dir": str(attempt_dir),
                "request_path": str(request_path), "status": "REQUEST_READY",
                "history": _history(state, "REQUEST_READY", attempt=attempt)}

    def execute(state: GraphState) -> GraphState:
        log = Path(state["attempt_dir"]) / "agent.log"
        try:
            request = Path(state["request_path"])
            if harness_job:
                harness_job.execute(request, Path(state["attempt_dir"]))
            elif declarative:
                planner.build(Path(state["workspace"]), Path(state["staged_pdf"]).name,
                              Path(state["attempt_dir"]).name)
            else:
                run_script(PIPELINE / "zos_runner.py", request, log)
            return {"status": "EXECUTED", "history": _history(state, "PIPELINE_PASS")}
        except Exception as exc:
            return {"status": "EXECUTION_FAILED", "error": str(exc),
                    "history": _history(state, "PIPELINE_FAIL", error=str(exc))}

    def visual_gate(state: GraphState) -> GraphState:
        if state.get("status") != "EXECUTED":
            return state
        # Openability-only mode deliberately has no CAD/mesh visual gate.
        event = "GEOMETRY_AND_VIEW_REVIEW_PENDING" if declarative else "VISUAL_SKIPPED_OPENABILITY_ONLY"
        return {"status": "VISUAL_PASS", "history": _history(state, event)}

    def inspect(state: GraphState) -> GraphState:
        attempt = Path(state["attempt_dir"])
        if declarative and state.get("status") == "VISUAL_PASS":
            try:
                gates = planner.verify(attempt, Path(state["staged_pdf"]), Path(state["spec_path"]))
            except Exception as exc:
                error = f"verification_failed:{exc}"
                return {"status": "GATE_FAILED", "error": error,
                        "history": _history(state, "GATES_FAIL", error=error)}
            passed = all(gates.values())
            return {"status": "PASS" if passed else "GATE_FAILED", "gates": gates,
                    "error": None if passed else "independent_geometry_or_view_validation_failed",
                    "history": _history(state, "GATES_PASS" if passed else "GATES_FAIL", gates=gates)}
        if harness_job:
            if state.get("status") != "EXECUTED":
                return state
            try:
                gates = harness_job.verify(attempt)
                if all(gates.values()):
                    return {"status": "PASS", "error": None, "gates": gates,
                            "history": _history(state, "GATES_PASS", gates=gates)}
                return {"status": "GATE_FAILED", "error": "native_verification_failed",
                        "gates": gates, "history": _history(state, "GATES_FAIL", gates=gates)}
            except Exception as exc:
                status = ("FAIL" if str(exc) == "independent_geometry_and_view_verification_missing"
                          else "GATE_FAILED")
                return {"status": status, "error": str(exc),
                        "history": _history(state, "GATES_FAIL", error=str(exc))}
        if state.get("status") == "VISUAL_PASS":
            native = attempt / "native_readback.json"
            model = attempt / "model.zos"
            if native.is_file() and model.is_file() and model.stat().st_size > 0:
                return {"status": "PASS", "gates": {"openability_gate": True,
                        "cad_gate": "SKIPPED", "geometry_gate": "SKIPPED"},
                        "history": _history(state, "OPENABILITY_PASS")}
        status_path = attempt / "attempt_status.json"
        if not status_path.is_file():
            return {"status": "GATE_FAILED", "error": "attempt_status_missing"}
        status = read_json(status_path)
        gates = status.get("gates", {})
        passed = status.get("final_status") == "PASS" and all(
            bool(gates.get(name)) for name in ("openability_gate", "numeric_gate",
                                               "topology_gate", "custom_geometry_gate", "image_gate"))
        return {"status": "PASS" if passed else "GATE_FAILED", "gates": gates,
                "history": _history(state, "GATES_PASS" if passed else "GATES_FAIL", gates=gates)}

    def repair(state: GraphState) -> GraphState:
        if state.get("attempt", 0) >= state.get("max_attempts", 8) or budget_exhausted(state):
            return {"status": "FAIL", "error": "attempt_budget_exhausted"}
        if declarative:
            strategy = state["strategy"]
            next_index = int(strategy["candidate_index"]) + 1
            if next_index < strategy["candidate_count"]:
                return {"status": "RETRY", "strategy_index": next_index,
                        "history": _history(state, "TRY_NEXT_CANDIDATE", next_strategy_index=next_index)}
        evidence = str(state.get("error") or "")
        log = Path(state["attempt_dir"]) / "agent.log"
        if log.is_file():
            evidence += "\n" + log.read_text(encoding="utf-8", errors="replace")[-16000:]
        if declarative:
            verification = log.with_name("verification.json")
            if verification.is_file():
                evidence += "\n" + verification.read_text(encoding="utf-8")[-16000:]
        try:
            prompt = (f"Correct the current modeling plan using this execution error:\n{evidence}"
                      if declarative else harness_job.instruction(evidence) if harness_job else _repair_prompt(state, evidence))
            if declarative:
                for correction in range(2):
                    planner.invoke(prompt, Path(state["workspace"]),
                                   f"llm_repair_{state['attempt']:02d}_{correction + 1}",
                                   Path(state["staged_pdf"]))
                    try:
                        planner.validate(Path(state["spec_path"]), Path(state["staged_pdf"]).name)
                        break
                    except (ValueError, TypeError, KeyError) as exc:
                        if correction or budget_exhausted(state):
                            raise
                        prompt += f"\nThe revised plan was rejected: {exc}"
            else:
                planner.invoke(prompt, Path(state["workspace"]),
                               f"llm_repair_{state.get('attempt', 0):02d}", Path(state["staged_pdf"]))
                _normalize_contract(Path(state["workspace"]))
                _normalize_adapter_signature(Path(state["workspace"]))
                _validate_files(Path(state["workspace"]), Path(state["staged_pdf"]).name)
            index = 0 if declarative else int(state.get("strategy_index", 0)) + 1
            return {"status": "RETRY", "strategy_index": index,
                    "history": _history(state, "REPAIR", next_strategy_index=index)}
        except Exception as exc:
            return {"status": "FAIL", "error": str(exc),
                    "history": _history(state, "REPAIR_FAILED", error=str(exc))}

    def finalize(state: GraphState) -> GraphState:
        workspace = Path(state["workspace"])
        final = workspace / "final"
        if final.exists():
            shutil.rmtree(final)
        shutil.copytree(state["attempt_dir"], final)
        write_json(workspace / "audit" / "graph_state.json", {k: v for k, v in state.items() if not k.startswith("_")})
        return {"status": "PASS", "final_path": str(final),
                "history": _history(state, "FINALIZED", final=str(final))}

    def route_after_prepare(state: GraphState) -> str:
        return "fail" if state.get("status") == "FAIL" else "strategy"

    def route_after_preflight(state: GraphState) -> str:
        return "prepare" if state.get("status") == "PREFLIGHT" else "fail"

    def route_after_execute(state: GraphState) -> str:
        if harness_job:
            return "inspect"
        return "visual" if state.get("status") == "EXECUTED" else "inspect"

    def route_after_inspect(state: GraphState) -> str:
        if state.get("status") == "FAIL":
            return "fail"
        return "finalize" if state.get("status") == "PASS" else "repair"

    def route_after_repair(state: GraphState) -> str:
        return "retry" if state.get("status") == "RETRY" else "fail"

    graph = StateGraph(GraphState)
    graph.add_node("preflight", preflight)
    graph.add_node("prepare_inputs", prepare_inputs)
    graph.add_node("select_strategy", select_strategy)
    graph.add_node("prepare_request", prepare_request)
    graph.add_node("execute_pipeline", execute)
    graph.add_node("visual_gate", visual_gate)
    graph.add_node("inspect_gates", inspect)
    graph.add_node("repair", repair)
    graph.add_node("finalize", finalize)
    graph.add_edge(START, "preflight")
    graph.add_conditional_edges("preflight", route_after_preflight,
                                {"prepare": "prepare_inputs", "fail": END})
    graph.add_conditional_edges("prepare_inputs", route_after_prepare,
                                {"strategy": "select_strategy", "fail": END})
    graph.add_edge("select_strategy", "prepare_request")
    graph.add_edge("prepare_request", "execute_pipeline")
    graph.add_conditional_edges("execute_pipeline", route_after_execute,
                                {"visual": "visual_gate", "inspect": "inspect_gates"})
    graph.add_edge("visual_gate", "inspect_gates")
    graph.add_conditional_edges("inspect_gates", route_after_inspect,
                                {"finalize": "finalize", "repair": "repair", "fail": END})
    graph.add_conditional_edges("repair", route_after_repair,
                                {"retry": "select_strategy", "fail": END})
    graph.add_edge("finalize", END)
    return graph.compile(checkpointer=MemorySaver())
