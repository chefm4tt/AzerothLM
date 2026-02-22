#!/usr/bin/env python3
"""
AzerothLM Debug Mode Verification Test

Spawns the relay as an MCP server with --debug, exercises all 6 tools via
a minimal JSON-RPC client, then verifies debug.log against expected patterns.

Usage:
    python test_debug.py           # TESTING_MODE=true (fast, no API cost)
    python test_debug.py --live    # real AI call (costs API credits, slower)

Adding checks for new features:
    Append a (label, regex) tuple to CHECKS_TESTING or CHECKS_LIVE at the bottom.
"""
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

LOG_FILE = os.path.join(os.path.dirname(__file__), "test_debug_output.log")
RELAY_SCRIPT = os.path.join(os.path.dirname(__file__), "AzerothLM_Relay.py")
LIVE_MODE = "--live" in sys.argv
AI_WAIT = 15 if LIVE_MODE else 3

# -----------------------------------------------------------------------------
# Minimal MCP Client
# -----------------------------------------------------------------------------
class MCPClient:
    def __init__(self, proc):
        self.proc = proc
        self._id = 0
        self._responses = queue.Queue()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self):
        while True:
            try:
                line = self.proc.stdout.readline()
                if not line:
                    break
                data = json.loads(line.decode())
                if "id" in data:
                    self._responses.put(data)
            except Exception:
                break

    def _send(self, method, params=None, notify=False):
        msg = {"jsonrpc": "2.0", "method": method}
        if not notify:
            self._id += 1
            msg["id"] = self._id
        if params is not None:
            msg["params"] = params
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        self.proc.stdin.flush()
        if not notify:
            try:
                return self._responses.get(timeout=30)
            except queue.Empty:
                return {"error": "timeout waiting for response"}
        return None

    def initialize(self):
        resp = self._send("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "AzerothLM Debug Test", "version": "0.1"},
        })
        self._send("initialized", notify=True)
        return resp

    def tool(self, name, arguments=None):
        return self._send("tools/call", {
            "name": name,
            "arguments": arguments or {},
        })

# -----------------------------------------------------------------------------
# Pattern Checks
# -----------------------------------------------------------------------------
CHECKS_COMMON = [
    # Startup
    ("Startup: debug enabled",              r"\[DBG\] Debug enabled \| mode=mcp"),

    # Sync subsystem (fires on every tool call)
    ("Sync: start",                         r"\[DBG\] sync: start"),
    ("Sync: pending done",                  r"\[DBG\] sync: pending done, max_ts=\d+"),
    ("Sync: complete",                      r"\[DBG\] sync: complete"),

    # Pending actions
    ("Pending: found N actions",            r"\[DBG\] pending: found \d+ pending actions"),
    ("Pending: max_ts",                     r"\[DBG\] pending: max_ts=\d+"),

    # SavedVariables read
    ("read_sv: reading path",               r"\[DBG\] read_sv: reading .+AzerothLM\.lua"),
    ("read_sv: parsed OK",                  r"\[DBG\] read_sv: parsed OK \(\d+ chars\)"),

    # Signal file
    ("Signal: writing topics",              r"\[DBG\] signal: writing \d+ topics to .+AzerothLM_Signal\.lua"),
    ("Signal: write OK",                    r"\[DBG\] signal: write OK"),

    # list_topics
    ("[TOOL] list_topics: entry",           r"\[DBG\] \[TOOL\] list_topics$"),
    ("[TOOL] list_topics: count",           r"\[DBG\] \[TOOL\] list_topics \| \d+ topics"),

    # get_character_context
    ("[TOOL] get_character_context: entry", r"\[DBG\] \[TOOL\] get_character_context$"),
    ("read_context: player info",           r"\[DBG\] read_context: player=\w+ lv\d+, gear slots=\d+, quests=\d+"),
    ("[TOOL] get_character_context: size",  r"\[DBG\] \[TOOL\] get_character_context \| \d+ chars"),

    # create_topic
    ("[TOOL] create_topic: entry",          r"\[DBG\] \[TOOL\] create_topic \| title='.+'"),
    ("[TOOL] create_topic: OK",             r"\[DBG\] \[TOOL\] create_topic \| OK \| slug='.+'"),

    # ask_question
    ("[TOOL] ask_question: entry",          r"\[DBG\] \[TOOL\] ask_question \| slug='.+' \| q='.+'"),
    ("[TOOL] ask_question: context",        r"\[DBG\] \[TOOL\] ask_question \| context=\d+ chars \| history=\d+ entries"),
    ("[TOOL] ask_question: response",       r"\[DBG\] \[TOOL\] ask_question \| response=\d+ chars \| truncated=(True|False)"),

    # get_topic
    ("[TOOL] get_topic: entry",             r"\[DBG\] \[TOOL\] get_topic \| slug='.+'"),
    ("[TOOL] get_topic: entries count",     r"\[DBG\] \[TOOL\] get_topic \| \d+ entries returned"),

    # delete_topic
    ("[TOOL] delete_topic: entry",          r"\[DBG\] \[TOOL\] delete_topic \| slug='.+'"),
    ("[TOOL] delete_topic: OK",             r"\[DBG\] \[TOOL\] delete_topic \| OK \| '.+' removed"),
]

# Testing mode: mock response path
CHECKS_TESTING = CHECKS_COMMON + [
    ("call_ai: TESTING_MODE mock",          r"\[DBG\] call_ai: TESTING_MODE"),
]

# Live mode: real AI path (cache miss + token counts)
CHECKS_LIVE = CHECKS_COMMON + [
    ("call_ai: cache state",                r"\[DBG\] call_ai: cache (HIT|MISS)"),
    ("call_ai: message count",              r"\[DBG\] call_ai: \d+ messages, user content=\d+ chars"),
    ("call_ai: response + tokens",          r"\[DBG\] call_ai: response \d+ chars \| tokens: prompt=\d+ completion=\d+"),
]

# -----------------------------------------------------------------------------
# Test Runner
# -----------------------------------------------------------------------------
def run_test():
    console = Console()
    mode_label = "[bold red]LIVE (real AI)[/bold red]" if LIVE_MODE else "[bold yellow]TESTING (mock)[/bold yellow]"
    console.print(Panel(
        f"Mode: {mode_label}\n"
        f"Log:  {LOG_FILE}",
        title="[bold]AzerothLM Debug Verification[/bold]",
        border_style="cyan",
    ))

    # Clear log
    with open(LOG_FILE, "w") as f:
        f.write("")

    # Build relay environment
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"  # ensure log file is written as UTF-8
    if not LIVE_MODE:
        env["TESTING_MODE"] = "true"

    # Spawn relay
    log_handle = open(LOG_FILE, "w")
    proc = subprocess.Popen(
        [sys.executable, RELAY_SCRIPT, "--mcp", "--debug"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=log_handle,
        env=env,
    )

    checks = CHECKS_LIVE if LIVE_MODE else CHECKS_TESTING

    try:
        client = MCPClient(proc)

        console.print("\n[dim]Initializing MCP handshake...[/dim]")
        client.initialize()
        time.sleep(0.3)

        console.print("[dim]Running tools...[/dim]")

        client.tool("list_topics")
        time.sleep(0.5)

        client.tool("get_character_context")
        time.sleep(0.5)

        client.tool("create_topic", {"title": "DBG Test"})
        time.sleep(0.5)

        console.print(f"[dim]ask_question (waiting up to {AI_WAIT}s)...[/dim]")
        client.tool("ask_question", {
            "topic_slug": "dbg-test",
            "question": "What should I focus on for gear upgrades?",
        })
        time.sleep(AI_WAIT)

        client.tool("get_topic", {"topic_slug": "dbg-test"})
        time.sleep(0.5)

        client.tool("delete_topic", {"topic_slug": "dbg-test"})
        time.sleep(0.5)

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_handle.close()

    # Read log
    with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
        log_content = f.read()

    # Run checks
    results = [
        (label, bool(re.search(pattern, log_content, re.MULTILINE)))
        for label, pattern in checks
    ]

    # Report
    passed = sum(1 for _, ok in results if ok)
    total = len(results)

    table = Table(show_header=True, header_style="bold", show_lines=False)
    table.add_column("Check", style="bold")
    table.add_column("Result", justify="center", width=8)

    for label, ok in results:
        table.add_row(label, "[green]PASS[/green]" if ok else "[red]FAIL[/red]")

    console.print()
    console.print(table)

    if passed == total:
        console.print(f"\n[bold green]{passed}/{total} checks passed — debug mode verified.[/bold green]")
    else:
        console.print(f"\n[bold red]{passed}/{total} checks passed — {total - passed} FAILED.[/bold red]")
        console.print("\n[bold]debug.log contents:[/bold]")
        console.print(log_content if log_content.strip() else "[dim](empty — was --debug flag active?)[/dim]")

    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(run_test())
