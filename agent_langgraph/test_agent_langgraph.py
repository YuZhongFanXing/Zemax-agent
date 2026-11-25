from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from . import graph as graph_module
from .graph import build_graph
from .model import StubPlanner


class LangGraphAgentTests(unittest.TestCase):
    def test_declarative_failure_tries_next_candidate_within_budget(self):
        class Planner:
            declarative = True

            def invoke(self, prompt, workspace, name, pdf_path):
                if not name.startswith("llm_prepare"):
                    raise AssertionError("An existing candidate should be tried before replanning")
                spec = {"validation": {}, "representation_candidates": [
                    {"id": "first"}, {"id": "second"},
                ]}
                (workspace / "geometry_spec.json").write_text(json.dumps(spec), encoding="utf-8")

            def validate(self, spec_path, filename):
                pass

            def build(self, workspace, filename, attempt):
                pass

            def verify(self, attempt, pdf, spec_path):
                return {"geometry_gate": attempt.name == "attempt_02"}

        for budget, expected in ((1, "FAIL"), (2, "PASS")):
            with self.subTest(budget=budget), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                pdf = root / "model.pdf"
                pdf.write_bytes(b"pdf")
                result = build_graph(Planner()).invoke(
                    {"pdf": str(pdf), "workspace": str(root / "run"), "max_attempts": budget},
                    config={"configurable": {"thread_id": "candidate_fallback"}},
                )
                self.assertEqual(result["status"], expected)
                self.assertEqual(result["attempt"], budget)
                self.assertEqual(result["strategy"]["candidate"]["id"],
                                 "second" if budget == 2 else "first")

    def test_graph_finalizes_after_pass(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            pdf = root / "model.pdf"
            pdf.write_bytes(b"pdf")

            def planner(prompt, workspace, name, pdf_path, images):
                if name == "llm_prepare":
                    (workspace / "geometry_spec.json").write_text(json.dumps({
                        "source_pdf": "input/model.pdf", "units": "mm",
                        "model_family": "custom", "source_trace": ["page 1"],
                        "features": [{"id": "body", "type": "native", "source_trace": ["page 1"], "geometry_mapping": {"object": "native"}}],
                        "representation_candidates": [{"id": "native", "kind": "native_nce"}],
                        "validation": {"metrics": [], "tolerances": {}}
                    }), encoding="utf-8")
                    (workspace / "model_adapter.py").write_text("def build(*a): return {}\ndef readback(*a): return {}\ndef validate_export(*a): return {'pass': True}\n", encoding="utf-8")
                elif name.startswith("llm_visual"):
                    attempt = workspace / "attempts" / "attempt_01"
                    names = ("front.png", "back.png", "side.png", "end_section.png", "axonometric.png")
                    hashes = {n: hashlib.sha256((attempt / "export_views" / n).read_bytes()).hexdigest() for n in names}
                    (attempt / "visual_review.json").write_text(json.dumps({"pass": True, "image_hashes": hashes, "views": {n: "PASS" for n in names}}), encoding="utf-8")
                return "ok"

            def fake_run(script, request, log):
                attempt = Path(json.loads(request.read_text(encoding="utf-8"))["output_dir"])
                if script.name == "zos_runner.py":
                    (attempt / "model.zos").write_bytes(b"zos")
                    (attempt / "model.stl").write_bytes(b"stl")
                    (attempt / "native_result.json").write_text("{}", encoding="utf-8")
                    (attempt / "native_readback.json").write_text("{}", encoding="utf-8")
                elif script.name == "check_exports.py":
                    (attempt / "geometry_check.json").write_text(json.dumps({
                        "numeric_gate": True, "topology_gate": True,
                        "custom_validation": {"pass": True}
                    }), encoding="utf-8")
                elif script.name == "make_views.py":
                    views = attempt / "export_views"
                    views.mkdir(parents=True, exist_ok=True)
                    for name in ("front.png", "back.png", "side.png", "end_section.png", "axonometric.png"):
                        (views / name).write_bytes(b"png")
                    (attempt / "view_manifest.json").write_text("{}", encoding="utf-8")
                elif script.name == "write_report.py":
                    (attempt / "attempt_status.json").write_text(json.dumps({
                        "final_status": "PASS",
                        "gates": {"openability_gate": True, "numeric_gate": True,
                                  "topology_gate": True, "custom_geometry_gate": True,
                                  "image_gate": True}
                    }), encoding="utf-8")

            graph = build_graph(StubPlanner(planner))
            with patch.object(graph_module, "run_script", side_effect=fake_run):
                result = graph.invoke({"pdf": str(pdf), "workspace": str(root / "run"),
                                       "max_attempts": 2, "max_minutes": 1,
                                       "attempt": 0, "strategy_index": 0, "history": []},
                                      config={"configurable": {"thread_id": "test"}})
            self.assertEqual(result["status"], "PASS")
            self.assertTrue(Path(result["final_path"]).is_dir())


if __name__ == "__main__":
    unittest.main()
