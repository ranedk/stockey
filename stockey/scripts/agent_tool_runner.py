#!/usr/bin/env python3

import argparse
import json
import subprocess
import sys
from pathlib import Path

from utils.fallback_telemetry import record_local_fallback_event


REPO_ROOT = Path(__file__).resolve().parent.parent
REGISTRY_PATH = REPO_ROOT / "docs" / "tool_registry.json"


def load_registry() -> dict:
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))


def registry_by_name() -> dict[str, dict]:
    registry = load_registry()
    return {tool["name"]: tool for tool in registry["tools"]}


def list_tools(category: str | None = None) -> dict:
    tools = load_registry()["tools"]
    if category:
        tools = [tool for tool in tools if tool["category"] == category]
    return {
        "status": "ok",
        "registry": str(REGISTRY_PATH.relative_to(REPO_ROOT)),
        "count": len(tools),
        "tools": tools,
    }


def run_tool(tool_name: str, tool_args: list[str], allow_writes: bool) -> dict:
    tools = registry_by_name()
    if tool_name not in tools:
        raise ValueError(f"Unknown tool: {tool_name}")

    tool = tools[tool_name]
    effective_read_only = tool.get("read_only", False) or (
        tool_name == "sql_query" and "--read-only" in tool_args
    )

    if not allow_writes and not effective_read_only:
        raise ValueError(
            f"Tool '{tool_name}' is not read-only. Re-run with --allow-writes to execute it."
        )

    command = [*tool["command"], *tool_args]
    if command and command[0] in {"python", "python3"}:
        command[0] = sys.executable
    proc = subprocess.run(
        command,
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    stdout = proc.stdout.strip()
    stderr = proc.stderr.strip()
    parsed_stdout = None
    if stdout:
        try:
            parsed_stdout = json.loads(stdout)
        except json.JSONDecodeError as exc:
            record_local_fallback_event(
                module="scripts.agent_tool_runner",
                fallback_type="agent_tool_runner_stdout_json_parse_failed",
                source=tool_name,
                severity="warn",
                reason="Agent tool runner could not parse command stdout as JSON and kept raw stdout.",
                error=exc,
                metadata={"stdout_length": len(stdout), "stdout_excerpt": stdout[:240]},
            )
            parsed_stdout = None

    return {
        "status": "ok" if proc.returncode == 0 else "error",
        "tool": tool_name,
        "category": tool["category"],
        "read_only": effective_read_only,
        "command": command,
        "exit_code": proc.returncode,
        "stdout": parsed_stdout if parsed_stdout is not None else stdout,
        "stderr": stderr,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Curated runner for agent-safe stockey tools."
    )
    sub = parser.add_subparsers(dest="action", required=True)

    list_p = sub.add_parser("list", help="List registered tools")
    list_p.add_argument("--category", help="Optional category filter")

    run_p = sub.add_parser("run", help="Run one registered tool")
    run_p.add_argument("tool", help="Tool name from docs/tool_registry.json")
    run_p.add_argument(
        "--allow-writes",
        action="store_true",
        help="Required for tools that are not marked read-only",
    )
    run_p.add_argument(
        "tool_args",
        nargs=argparse.REMAINDER,
        help="Arguments forwarded to the registered tool after '--'",
    )

    args = parser.parse_args()

    try:
        if args.action == "list":
            result = list_tools(category=args.category)
        elif args.action == "run":
            forwarded = args.tool_args
            allow_writes = args.allow_writes
            if "--allow-writes" in forwarded:
                allow_writes = True
                forwarded = [value for value in forwarded if value != "--allow-writes"]
            if forwarded and forwarded[0] == "--":
                forwarded = forwarded[1:]
            result = run_tool(
                tool_name=args.tool,
                tool_args=forwarded,
                allow_writes=allow_writes,
            )
        else:
            raise ValueError(f"Unsupported action: {args.action}")

        print(json.dumps(result, indent=2, ensure_ascii=False))
        sys.exit(0 if result["status"] == "ok" else 1)
    except Exception as exc:
        record_local_fallback_event(
            module="scripts.agent_tool_runner",
            source="agent_tool_runner",
            fallback_type="agent_tool_runner_failed",
            severity="error",
            reason="Agent tool runner failed before returning a normal tool result.",
            error=exc,
            metadata={"action": getattr(args, "action", None)},
        )
        print(
            json.dumps(
                {
                    "status": "error",
                    "error": str(exc),
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
