from __future__ import annotations

import argparse
import json
from pathlib import Path

from .graph import build_graph


def main() -> int:
    parser = argparse.ArgumentParser(description="LangGraph PDF-to-OpticStudio agent")
    parser.add_argument("--pdf", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--max-attempts", type=int, default=8)
    parser.add_argument("--max-minutes", type=float, default=60.0)
    parser.add_argument("--responses", action="store_true", help="Use the tool-free Responses API planner")
    args = parser.parse_args()
    initial = {"pdf": str(Path(args.pdf).resolve()),
               "workspace": str(Path(args.workspace).resolve()),
               "max_attempts": args.max_attempts, "max_minutes": args.max_minutes,
               "attempt": 0, "strategy_index": 0, "history": []}
    if args.responses:
        from .responses import ResponsesPlanner
        if Path(args.workspace).exists() and any(Path(args.workspace).iterdir()):
            parser.error("--responses requires a fresh workspace")
        initial["max_attempts"] = min(args.max_attempts, 3)
        initial["max_minutes"] = min(args.max_minutes, 20.0)
        graph = build_graph(ResponsesPlanner())
    else:
        graph = build_graph()
    result = graph.invoke(initial, config={"configurable": {"thread_id": Path(args.workspace).name}})
    print(json.dumps({k: v for k, v in result.items() if not k.startswith("_")},
                     ensure_ascii=False, indent=2))
    return 0 if result.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
