from __future__ import annotations

import os
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Iterable, Protocol


class Planner(Protocol):
    def invoke(self, prompt: str, workspace: Path, name: str,
               pdf: Path | None = None, images: Iterable[Path] = ()) -> str: ...


class CodexCLIPlanner:
    """LangChain-compatible model boundary backed by the local Codex CLI.

    LangGraph owns the workflow and checkpointing. This adapter keeps the
    existing local Codex runtime usable without requiring an HTTP model key.
    """

    def invoke(self, prompt: str, workspace: Path, name: str,
               pdf: Path | None = None, images: Iterable[Path] = ()) -> str:
        audit = workspace / "audit"
        audit.mkdir(parents=True, exist_ok=True)
        prompt_path = audit / f"{name}.prompt.md"
        response_path = audit / f"{name}.response.md"
        prompt_path.write_text(prompt, encoding="utf-8")
        command = [
            shutil.which(os.environ.get("AGENT_V2_CODEX", "codex"))
            or os.environ.get("AGENT_V2_CODEX", "codex"),
            "exec", "--ephemeral", "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox", "-C", str(workspace),
            "-o", str(response_path),
        ]
        gateway = os.getenv("ZEMAX_GATEWAY_URL")
        if gateway:
            command.extend([
                "-m", os.getenv("ZEMAX_GATEWAY_MODEL", "gpt-6-sol"),
                "-c", 'model_provider="zemax_gateway"',
                "-c", 'model_providers.zemax_gateway.name="Zemax Gateway"',
                "-c", "model_providers.zemax_gateway.base_url=" + json.dumps(gateway),
                "-c", 'model_providers.zemax_gateway.env_key="ZEMAX_GATEWAY_KEY"',
            ])
        for image in images:
            command.extend(("-i", str(image)))
        command.append("-")
        timeout = int(os.environ.get("LANGGRAPH_LLM_TIMEOUT_SECONDS", "600"))
        stdout = audit / f"{name}.stdout.log"
        stderr = audit / f"{name}.stderr.log"
        with stdout.open("w", encoding="utf-8", errors="replace") as out, \
                stderr.open("w", encoding="utf-8", errors="replace") as err:
            completed = subprocess.run(command, input=prompt, text=True,
                                       encoding="utf-8", errors="replace",
                                       stdout=out, stderr=err, timeout=timeout,
                                       cwd=str(workspace))
        if completed.returncode:
            raise RuntimeError(f"codex_exec_failed:{completed.returncode}")
        return response_path.read_text(encoding="utf-8", errors="replace")


class StubPlanner:
    """Small deterministic planner used by offline graph tests."""

    def __init__(self, callback):
        self.callback = callback

    def invoke(self, prompt: str, workspace: Path, name: str,
               pdf: Path | None = None, images: Iterable[Path] = ()) -> str:
        return self.callback(prompt, workspace, name, pdf, tuple(images))


class LangChainPlanner:
    """Adapter for any LangChain ``BaseChatModel``.

    The model still receives the current workspace instructions; file editing is
    performed by the model's configured tool/runtime. This keeps the graph
    independent from a particular provider such as ``ChatOpenAI``.
    """

    def __init__(self, chat_model: Any):
        self.chat_model = chat_model

    def invoke(self, prompt: str, workspace: Path, name: str,
               pdf: Path | None = None, images: Iterable[Path] = ()) -> str:
        response = self.chat_model.invoke(prompt)
        content = getattr(response, "content", response)
        return str(content)
