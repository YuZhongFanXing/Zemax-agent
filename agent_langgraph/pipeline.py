from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PIPELINE = ROOT / "scripts" / "pipeline"


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def run_script(script: Path, request: Path, log: Path) -> None:
    command = [sys.executable, "-u", str(script)]
    if script.name == "zos_runner.py":
        command.append(str(request))
    else:
        command.extend(("--request", str(request)))
    with log.open("a", encoding="utf-8") as stream:
        stream.write("\n$ " + " ".join(command) + "\n")
        stream.flush()
        completed = subprocess.run(command, cwd=ROOT, stdin=subprocess.DEVNULL,
                                   stdout=stream, stderr=subprocess.STDOUT, timeout=180)
    if completed.returncode:
        raise RuntimeError(f"{script.name}:exit={completed.returncode}")


def stage_pdf(pdf: Path, workspace: Path) -> Path:
    target = workspace / "input" / pdf.name
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.resolve() != pdf.resolve():
        shutil.copy2(pdf, target)
    return target
