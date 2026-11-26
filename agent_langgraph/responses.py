"""Responses API planner with scoped drawing tools."""
import base64
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from .modeling_mcp import MODEL_VIEWS, call
from .pipeline import PIPELINE, write_json

ADAPTER = PIPELINE / "declarative_adapter.py"


def validate_assumptions(assumptions):
    if not isinstance(assumptions, list):
        raise ValueError("assumptions must be a list")
    for index, item in enumerate(assumptions, 1):
        if (not isinstance(item, dict)
                or item.get("basis") not in ("derived", "estimated", "assumed")
                or any(not isinstance(item.get(key), str) or not item[key].strip()
                       for key in ("feature", "value", "reason", "source"))):
            raise ValueError(f"assumption {index} requires feature, value, basis, reason and source")


def validate_spec(spec_path, filename):
    from scripts.pipeline.declarative_adapter import operations

    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    for key in ("source_pdf", "units", "model_family", "source_trace",
                "features", "validation", "representation_candidates"):
        if key not in spec:
            raise ValueError(f"missing field: {key}")
    if spec["source_pdf"] != filename or spec["units"] != "mm":
        raise ValueError("source_pdf or units do not match the request")
    if not spec["features"] or not spec["source_trace"]:
        raise ValueError("drawing features and source trace are required")
    validate_assumptions(spec.get("assumptions", []))
    candidates = spec["representation_candidates"]
    if not candidates or len({item["id"] for item in candidates}) != len(candidates):
        raise ValueError("candidate IDs must be present and unique")
    for candidate in candidates:
        operations(spec, candidate["id"])


def skill(name):
    path = Path(__file__).parent / "skills" / name / "SKILL.md"
    return path.read_text(encoding="utf-8-sig").split("---", 2)[2].strip()


class ResponsesPlanner:
    declarative = True

    def __init__(self, url=None, key=None, model=None, clarify=None, protocol="responses",
                 text_only=False, feature_tools=True, reply_language=None):
        self.url = url
        self.key = key
        self.model = model
        self.clarify = clarify
        self.reply_language = reply_language
        self.clarification_context = None
        self.protocol = protocol
        self.text_only = text_only
        self.feature_tools = feature_tools

    def validate(self, spec_path, filename):
        call(spec_path.parent, filename, "validate_plan")

    def build(self, workspace, filename, attempt):
        return call(workspace, filename, "build_model", attempt=attempt)

    def verify(self, attempt, pdf, spec_path):
        workspace = attempt.parents[1]
        result = json.loads((attempt / "native_result.json").read_text(encoding="utf-8"))
        gates = {"openability_gate": result["model"].get("pass") is True,
                 "geometry_gate": False, "image_gate": False}
        evidence = call(workspace, pdf.name, "measure_model", attempt=attempt.name)
        views = call(workspace, pdf.name, "render_model_views", attempt=attempt.name)
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        assumptions = spec.get("assumptions", [])
        variant = spec.get("selected_variant")
        content = [{"type": "input_text", "text":
                    skill("verify-zemax-model") + "\n" +
                    "Independently compare the actual reconstructed model views and measured geometry "
                    "to the PDF. Register the drawing views to the model axes using a single "
                    "consistent rigid orientation. Report the view mapping and compare the "
                    "corresponding silhouettes and visible features. View filenames encode model "
                    "axes, so the drawing may label the same projection differently. "
                    "Use the measured section outlines in model coordinates to check "
                    "feature count, pitch, angles, termination and cross-section. "
                    "Check explicitly specified drawing features against the drawing. Check each "
                    "estimate or assumption against the measured geometry and flag conflicts with "
                    "the drawing. A missing drawing dimension alone is acceptable when the "
                    "documented approximation is consistent with the available drawing. "
                    "Fail any gate if the corresponding model evidence is missing. "
                    "Return JSON with boolean geometry_pass "
                    "and image_pass, checks, findings and additional_assumptions. In "
                    "additional_assumptions record every unlabelled model dimension inferred "
                    "from drawing proportions or missing drawing information that is absent "
                    "from the planner list. Each entry has feature, value, basis (derived, "
                    "estimated or assumed), reason and source. Return [] if there are none.\n"
                    + json.dumps(evidence)}]
        content.append({"type": "input_text", "text":
                        "Planner assumptions and selected variant to check independently:\n"
                        + json.dumps({"assumptions": assumptions, "selected_variant": variant})})
        if getattr(self, "clarification_context", None):
            content.append({"type": "input_text", "text": self.clarification_context})
        content.extend(self.drawing_content(pdf, workspace))
        for name, png in views.items():
            content.append({"type": "input_text", "text": f"Model view: {name}"})
            content.append(self.image_content(base64.b64decode(png)))
        if self.protocol == "responses" and self.feature_tools:
            content.append({"type": "input_text", "text":
                            "When default sections miss the bore, request one longitudinal section along its axis. "
                            "Submit the verdict after the section results arrive."})
            review = self.request(content, attempt, "verification_review", section_attempt=attempt)
        else:
            review = self.request(content, attempt, "verification_review")
        additional = review["additional_assumptions"]
        validate_assumptions(additional)
        assumptions = [*assumptions, *additional]
        quality = ("approximate" if any(item["basis"] in ("estimated", "assumed")
                                        for item in assumptions) else "drawing_supported")
        gates["geometry_gate"] = bool(evidence.get("watertight") and review.get("geometry_pass") is True)
        if not self.text_only:
            gates["image_gate"] = set(views) == set(MODEL_VIEWS) and review.get("image_pass") is True
        else:
            gates.pop("image_gate")
        write_json(attempt / "verification.json", {
            "gates": gates, "quality": quality, "assumptions": assumptions,
            "selected_variant": variant, "review": review,
            "visual_review_skipped": self.text_only,
        })
        return gates

    @staticmethod
    def image_content(raw):
        return {"type": "input_image", "image_url":
                "data:image/png;base64," + base64.b64encode(raw).decode()}

    def drawing_content(self, pdf, workspace):
        metadata = call(workspace, pdf.name, "inspect_drawing")
        content = []
        for page in range(1, metadata["page_count"] + 1):
            drawing = call(workspace, pdf.name, "inspect_drawing", page=page)
            content.extend(self.drawing_page(drawing))
        return content

    def drawing_page(self, drawing):
        return [{"type": "input_text", "text": f"Page {drawing['page']}\n{drawing['text']}"},
                self.image_content(base64.b64decode(drawing["png"]))]

    def invoke(self, prompt, workspace, name, pdf=None, images=()):
        content = [{"type": "input_text", "text": prompt}]
        content.extend(self.drawing_content(pdf, workspace))
        if getattr(self, "clarification_context", None):
            content.append({"type": "input_text", "text": self.clarification_context})
        spec_path = workspace / "geometry_spec.json"
        if spec_path.is_file():
            content.append({"type": "input_text", "text": spec_path.read_text(encoding="utf-8")})
        contract = (
            "Return one JSON object with source_pdf (exact PDF filename), units='mm', model_family, "
            "source_trace, features, validation, and representation_candidates. Each candidate has id and "
            "operations. Each operation has id, kind, optional position and rotation XYZ. Supported kinds: "
            "box with full size XYZ, cylinder with radius and length, extrude with points [[x,y],...] "
            "and length and optional rear_scale_y or holes [[x,y,radius],...] for circular "
            "through holes parallel to the extrusion axis. For a constant-section part with "
            "full-length parallel holes, use one extrude operation with holes; use the drawing "
            "centers and half the specified diameter, with rear_scale_y omitted. "
            "Use union or subtract with two prior parent IDs. "
            "An array sets count and step XYZ and creates IDs id_0, id_1, etc. Lengths extend along "
            "local +Z before rotation. Declare a single model coordinate frame for the whole part. "
            "Expression order is A-B for subtract. Last object is the result. "
            "Include assumptions as a JSON list, empty if none. For every dimension or feature "
            "you derive, estimate, or assume, record feature, value with units when applicable, "
            "basis (derived, estimated, or assumed), reason, and source (page/view, or explain "
            "which drawing information is missing). Prefer derivation from dimensions, then "
            "estimation from a calibrated view, then a plausible assumption consistent with "
            "the drawing. Use the recorded value in the geometry and proceed autonomously. "
            "Using the nominal of a specified tolerance is a drawing-supported choice. "
            "When the PDF gives alternatives and no variant was requested, choose one named "
            "variant, report it in selected_variant, and apply its dimensions consistently. "
            "Return JSON data only. The fixed adapter performs all API operations. "
            f"The PDF filename is {Path(pdf).name}."
        )
        instructions = "\n".join(skill(item) for item in
                                 ("read-engineering-drawing", "plan-zemax-geometry"))
        payload = [{"type": "input_text", "text": contract + "\n" + instructions}, *content]
        if self.reply_language:
            payload.append({"type": "input_text", "text":
                f"Write user-facing questions and assumption explanations in {self.reply_language}. "
                "Keep JSON keys and schema values unchanged."})
        can_ask = self.clarify and name.startswith("llm_prepare") and not self.clarification_context
        if can_ask:
            payload.append({"type": "input_text", "text":
                'Before estimating a missing dimension critical to geometry, return {"question":"your specific question"}. '
                'If sufficient dimensions exist, return the plan directly.'})
        result = self.request(payload, workspace, name, drawing=pdf)
        if can_ask and result.get("question") and "assumptions" not in result:
            question = str(result["question"])[:2000]
            answer = self.clarify(question)
            self.clarification_context = (
                f"Question: {question}\nUser answer: {answer or 'Timed out without an answer.'}\n"
                "Continue without another question. Use the answer if provided. Otherwise infer values "
                "from the drawing where possible, choose plausible defaults for unresolved dimensions, "
                "and record the values and rationale in assumptions. Preserve them during repair unless corrected."
            )
            payload.append({"type": "input_text", "text":
                self.clarification_context + "\nNow produce the complete valid plan."})
            result = self.request(payload, workspace, name + "_clarified", drawing=pdf)
        if "assumptions" not in result:
            raise ValueError("plan must explicitly report assumptions, even when empty")
        write_json(spec_path, result)
        return json.dumps(result)

    def request(self, content, workspace, name, drawing=None, section_attempt=None):
        if self.protocol == "chat":
            if self.text_only and drawing and not any(
                item["type"] == "input_text" and item["text"].startswith("Page ")
                and item["text"].partition("\n")[2].strip() for item in content
            ):
                raise ValueError("PDF 没有可提取的文本，请使用支持视觉的模型")
            parts = [
                {"type": "text", "text": item["text"]} if item["type"] == "input_text"
                else {"type": "image_url", "image_url": {"url": item["image_url"]}}
                for item in content if not self.text_only or item["type"] == "input_text"
            ]
            if self.text_only:
                parts.append({"type": "text", "text":
                              "Only extracted text and numeric section evidence are available. "
                              "Do not claim to have visually inspected any images."})
            message = "\n\n".join(item["text"] for item in parts) if self.text_only else parts
            body = {"model": self.model, "messages": [{"role": "user", "content": message}],
                    "response_format": {"type": "json_object"}}
            result = self.exchange(body, workspace, name)
            return json.loads(result["choices"][0]["message"]["content"])
        body = {"model": self.model or os.getenv("ZEMAX_GATEWAY_MODEL", "gpt-6-sol"),
                "input": [{"role": "user", "content": content}]}
        if drawing:
            body["tools"] = [{
                "type": "function", "name": "inspect_drawing",
                "description": "Zoom into a region of the current drawing page.",
                "parameters": {"type": "object", "properties": {
                    "page": {"type": "integer", "minimum": 1},
                    "region": {"type": "array", "items": {"type": "number"},
                               "minItems": 4, "maxItems": 4}},
                    "required": ["page", "region"], "additionalProperties": False},
            }]
        if drawing and self.feature_tools:
            body["input"][0]["content"] = [*body["input"][0]["content"], {
                "type": "input_text", "text":
                "If the available drawing information defines a stepped bore, call expand_stepped_bore "
                "before submitting the plan and use the returned operations. If you also need a zoom, "
                "call both tools together when the bore dimensions are already known.",
            }]
            body["tools"][0]["description"] += " Region coordinates are fractions from 0 to 1, never pixels."
            body["tools"].append({
                "type": "function", "name": "expand_stepped_bore",
                "description": "Use for stepped bores. Append the returned operations verbatim after the target solid.",
                "parameters": {"type": "object", "properties": {
                    "target": {"type": "string"}, "result_id": {"type": "string"},
                    "entrance": {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3},
                    "direction": {"enum": ["+X", "-X", "+Y", "-Y", "+Z", "-Z"]},
                    "segments": {"type": "array", "items": {"type": "object", "properties": {
                        "diameter": {"type": "number"}, "depth": {"type": "number", "description": "Cumulative depth in mm"}},
                        "required": ["diameter", "depth"]}},
                    "exit_distance": {"type": ["number", "null"]}},
                    "required": ["target", "result_id", "entrance", "direction", "segments"]},
            })
        if section_attempt:
            body["tools"] = [{
                "type": "function", "name": "measure_section",
                "description": "Cut the reopened model perpendicular to an axis at the given coordinate in mm.",
                "parameters": {"type": "object", "properties": {
                    "axis": {"enum": ["X", "Y", "Z"]}, "coordinate": {"type": "number"}},
                    "required": ["axis", "coordinate"]},
            }]
        rounds = 3 if self.feature_tools else 2
        for index in range(rounds):
            suffix = f"_tool_{index}" if index else ""
            result = self.exchange(body, workspace, name + suffix)
            calls = [item for item in result.get("output", []) if item["type"] == "function_call"]
            if not calls:
                return self.parse(result)
            if not (drawing or section_attempt) or index == rounds - 1:
                raise ValueError("modeling tool call budget exhausted")
            content = []
            for item in calls[:2 if section_attempt else 4]:
                try:
                    arguments = json.loads(item["arguments"])
                    tool = item["name"]
                    if drawing and tool == "inspect_drawing" and set(arguments) == {"page", "region"}:
                        page = call(workspace, drawing.name, tool, **arguments)
                        content.extend(self.drawing_page(page))
                    elif drawing and self.feature_tools and tool == "expand_stepped_bore":
                        evidence = call(workspace, drawing.name, tool, **arguments)
                        content.append({"type": "input_text", "text": tool + ": " + json.dumps(evidence)})
                    elif section_attempt and tool == "measure_section" and set(arguments) == {"axis", "coordinate"}:
                        evidence = call(section_attempt.parents[1], "unused.pdf", tool,
                                        attempt=section_attempt.name, **arguments)
                        png = evidence.pop("png", None)
                        content.append({"type": "input_text", "text": tool + ": " + json.dumps(evidence)})
                        if png:
                            content.append(self.image_content(base64.b64decode(png)))
                    else:
                        raise ValueError("unsupported tool arguments")
                except (ValueError, TypeError) as exc:
                    content.append({"type": "input_text", "text": f"Drawing inspection failed: {exc}"})
            body["input"].append({"role": "user", "content": content})
            if section_attempt or index == rounds - 2:
                body.pop("tools", None)
                body["input"][-1]["content"].append({
                    "type": "input_text", "text": "Use the evidence above and return the required JSON now."})

    def exchange(self, body, workspace, name):
        audit = workspace / "audit"
        write_json(audit / f"{name}.request.json", body)
        request = urllib.request.Request(
            (self.url or os.environ["ZEMAX_GATEWAY_URL"]).rstrip("/") +
            ("/chat/completions" if self.protocol == "chat" else "/responses"),
            data=json.dumps(body).encode("utf-8"),
            headers={"Authorization": "Bearer " + (self.key or os.environ["ZEMAX_GATEWAY_KEY"]),
                     "Content-Type": "application/json"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        timeout = float(os.getenv("ZEMAX_GATEWAY_TIMEOUT_SECONDS", "180"))
        try:
            with opener.open(request, timeout=timeout) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            model = body["model"]
            if exc.code == 400:
                raise ValueError(f"模型 API 拒绝请求（HTTP 400，模型 {model}）。请核对模型 ID 和 API 协议。") from exc
            if exc.code in (401, 403):
                raise ValueError(f"模型 API 鉴权失败（HTTP {exc.code}）。请核对 API Key 和模型访问权限。") from exc
            raise
        write_json(audit / f"{name}.response.json", result)
        return result

    @staticmethod
    def parse(result):
        messages = ["".join(part["text"] for part in item.get("content", [])
                            if part.get("type") == "output_text")
                    for item in result.get("output", []) if item.get("type") == "message"]
        output = next((message for message in reversed(messages) if message.strip()), "")
        output = output.strip()
        if output.startswith("```json") and output.endswith("```"):
            output = output[7:-3].strip()
        parsed = json.loads(output)
        if not isinstance(parsed, dict):
            raise ValueError("expected a JSON object")
        return parsed
