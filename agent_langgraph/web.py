"""Local chat interface for the declarative modeling agent."""
import os
import re
import shutil
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Empty, Queue
from threading import Lock
from time import time
from urllib.parse import urlsplit

from flask import Flask, abort, jsonify, render_template, request, send_from_directory
from werkzeug.utils import secure_filename

from .graph import build_graph
from .pipeline import read_json, write_json
from .responses import ResponsesPlanner


def create_app(root):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 40 * 1024 * 1024
    executor = ThreadPoolExecutor(max_workers=1)
    status_lock = Lock()
    pending = {}
    inputs = {"geometry_spec.json": "建模方案", "request.json": "构建输入",
              "native_readback.json": "模型读回", "verification.json": "验收报告"}

    def run(directory, pdf, messages, config):
        def update(**status):
            with status_lock:
                write_json(directory / "status.json", status)

        def clarify(question):
            answers = Queue(maxsize=1)
            question_id = uuid.uuid4().hex
            with status_lock:
                pending[directory.name] = (question_id, answers)
            update(status="QUESTION", question=question, question_id=question_id, deadline=time() + 30)
            try:
                answer = answers.get(timeout=30)
            except Empty:
                answer = ""
            finally:
                with status_lock:
                    pending.pop(directory.name, None)
            transcript = directory / "clarifications.json"
            with status_lock:
                history = read_json(transcript) if transcript.exists() else []
                write_json(transcript, [*history, {"question": question, "answer": answer}])
            update(status="RUNNING", stage="继续规划")
            return answer

        try:
            update(status="RUNNING", stage="准备图纸")
            graph = build_graph(ResponsesPlanner(**config, clarify=clarify, reply_language="简体中文"))
            workflow = directory / "workflow"
            initial = {"pdf": str(pdf), "workspace": str(workflow), "max_attempts": 3,
                       "max_minutes": 20, "user_prompt": "\n".join(messages)}
            for state in graph.stream(initial, {"configurable": {"thread_id": directory.name}},
                                      stream_mode="values"):
                update(status="RUNNING", stage=state.get("status", "PREFLIGHT"),
                       attempt=state.get("attempt", 0))
            if state["status"] != "PASS":
                update(status="FAIL", error=state.get("error") or "未通过建模验收")
                return
            final = workflow / "final"
            model = final / "model.zos"
            preview = final / "native_views" / "axonometric.png"
            if not model.is_file() or not preview.is_file():
                raise RuntimeError("交付文件或轴测图缺失")
            update(status="PASS", model_path=str(model), gates=state.get("gates", {}),
                   text_only=config["text_only"])
        except Exception as exc:
            update(status="FAIL", error=str(exc))

    @app.get("/")
    def index():
        return render_template("modeling_chat.html", url=os.getenv("ZEMAX_GATEWAY_URL", ""),
                               model=os.getenv("ZEMAX_GATEWAY_MODEL", "gpt-6-sol"),
                               has_key=bool(os.getenv("ZEMAX_GATEWAY_KEY")))

    @app.post("/api/agent/chat")
    def submit():
        pdf = request.files.get("pdf")
        prompt = request.form.get("prompt", "").strip()
        if not prompt or len(prompt) > 8000:
            abort(400, "请输入 1 至 8000 字的建模要求")
        url = request.form.get("url", "").strip() or os.getenv("ZEMAX_GATEWAY_URL", "")
        key = request.form.get("key", "").strip()
        model = request.form.get("model", "").strip() or os.getenv("ZEMAX_GATEWAY_MODEL", "gpt-6-sol")
        try:
            endpoint = urlsplit(url)
            if endpoint.scheme not in {"http", "https"} or not endpoint.hostname or endpoint.username or endpoint.query or endpoint.fragment:
                raise ValueError
        except ValueError:
            abort(400, "请输入有效的 API Base URL，例如 https://api.openai.com/v1")
        # Only use the configured secret with its configured endpoint.
        key = key or (os.getenv("ZEMAX_GATEWAY_KEY") if url == os.getenv("ZEMAX_GATEWAY_URL") else None)
        if not key:
            abort(400, "请输入 API Key")
        protocol = request.form.get("protocol", "auto")
        if protocol not in {"auto", "chat", "responses"}:
            abort(400, "API 协议无效")
        base = url.rstrip("/")
        deepseek = endpoint.hostname == "api.deepseek.com" or model.lower().startswith("deepseek")
        if endpoint.hostname == "api.deepseek.com" and model.lower() == "deepseek":
            model = "deepseek-chat"
        if protocol == "auto":
            protocol = "chat" if deepseek or base.endswith("/chat/completions") else "responses"
        config = {"url": base.removesuffix("/responses").removesuffix("/chat/completions"),
                  "key": key, "model": model, "protocol": protocol,
                  "text_only": protocol == "chat" and (deepseek or request.form.get("text_only") == "on")}
        previous = request.form.get("previous")
        source = None
        messages = []
        if pdf and pdf.filename:
            if not pdf.filename.lower().endswith(".pdf") or pdf.stream.read(5) != b"%PDF-":
                abort(400, "请选择有效的 PDF 文件")
            pdf.stream.seek(0)
            filename = secure_filename(pdf.filename)
            if not filename.lower().endswith(".pdf"):
                filename = "drawing.pdf"
        elif previous:
            try:
                parent = root / uuid.UUID(previous).hex
            except ValueError:
                abort(400, "对话编号无效")
            if not (parent / "conversation.json").is_file():
                abort(404)
            conversation = read_json(parent / "conversation.json")
            filename = conversation["filename"]
            source = parent / filename
            messages = conversation["messages"]
        else:
            abort(400, "请先添加 PDF 图纸")

        identifier = uuid.uuid4()
        directory = root / identifier.hex
        directory.mkdir()
        if source:
            shutil.copyfile(source, directory / filename)
        else:
            pdf.save(directory / filename)
        messages.append(prompt)
        write_json(directory / "conversation.json", {"filename": filename, "messages": messages})
        write_json(directory / "status.json", {"status": "QUEUED"})
        executor.submit(run, directory, directory / filename, messages, config)
        return jsonify(id=str(identifier)), 202

    @app.get("/api/agent/jobs/<uuid:identifier>")
    def progress(identifier):
        status_file = root / identifier.hex / "status.json"
        with status_lock:
            if not status_file.is_file():
                abort(404)
            status = read_json(status_file)
        directory = status_file.parent
        attempts = directory / "workflow" / "attempts"
        status["inputs"] = [
            {"attempt": attempt.name, "name": filename, "label": label}
            for attempt in sorted(attempts.glob("attempt_*"))
            for filename, label in inputs.items() if (attempt / filename).is_file()
        ]
        with status_lock:
            if (directory / "clarifications.json").is_file():
                status["clarifications"] = read_json(directory / "clarifications.json")
        return jsonify(status)

    @app.post("/api/agent/jobs/<uuid:identifier>/answer")
    def answer(identifier):
        body = request.get_json(silent=True)
        value = body.get("answer") if isinstance(body, dict) else None
        if not isinstance(value, str) or len(value) > 4000:
            abort(400)
        with status_lock:
            entry = pending.get(identifier.hex)
            if not entry or entry[0] != body.get("question_id"):
                abort(409, "这条追问已经结束")
            pending.pop(identifier.hex)
            entry[1].put_nowait(value.strip())
        return jsonify(received=True)

    @app.get("/api/agent/jobs/<uuid:identifier>/inputs/<attempt>/<filename>")
    def intermediate(identifier, attempt, filename):
        if filename not in inputs or not re.fullmatch(r"attempt_[0-9]{2,}", attempt):
            abort(404)
        directory = root / identifier.hex / "workflow" / "attempts" / attempt
        return send_from_directory(directory, filename, as_attachment=True)

    @app.get("/api/agent/jobs/<uuid:identifier>/<artifact>")
    def artifact(identifier, artifact):
        directory = root / identifier.hex
        status_file = directory / "status.json"
        with status_lock:
            if not status_file.is_file() or read_json(status_file)["status"] != "PASS":
                abort(404)
        final = directory / "workflow" / "final"
        if artifact == "model":
            return send_from_directory(final, "model.zos", as_attachment=True)
        if artifact == "preview":
            return send_from_directory(final / "native_views", "axonometric.png")
        abort(404)

    return app


if __name__ == "__main__":
    create_app(os.getenv("ZEMAX_BLIND_JOB_ROOT", "output/web")).run(
        host="127.0.0.1", port=int(os.getenv("ZEMAX_WEB_PORT", "8765"))
    )
