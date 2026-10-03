"""Start the coding assistant (Claude Code) in headless mode on an incident."""
import json
import os
import shlex
import subprocess
from datetime import datetime, timezone

from incident_response.incidents import write_json


# Enough to investigate, edit the code, and run the tests; anything else is denied in headless mode.
ALLOWED_TOOLS = [
    "Read", "Edit", "Write", "Glob", "Grep",
    "Bash(uv run:*)", "Bash(curl:*)",
    "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)",
]

PROMPT = """\
A Grafana alert fired for the order-tracker service in this repository. The incident \
context is in {incident}: start with incident.md (endpoint, request counts, grouped \
error logs with stack traces, failed traces). The raw data is in alert.json and context.json.

1. Find the root cause in the code, using the logs, stack traces, and traces as evidence. \
You can reproduce it against the running app at {app_url} (for example with curl).
2. Fix it with the smallest change that addresses the cause, and add a regression test under tests/.
3. Run the tests with `uv run --frozen pytest -q` and make sure they pass.
4. Write {incident}/report.md with: summary, impact (endpoint, time window, how many requests failed), \
root cause with evidence, the fix, how you verified it, and follow-ups.

Do not commit, push, or rebuild or restart containers: a person reviews and deploys the change.
"""


def build_command(command, incident, app_url):
    prompt = PROMPT.format(incident=incident, app_url=app_url)
    return [
        *shlex.split(command), "-p", prompt,
        "--output-format", "stream-json", "--verbose",
        "--permission-mode", "acceptEdits",
        "--allowedTools", ",".join(ALLOWED_TOOLS),
    ]


def run_assistant(incident, repo_dir, command, app_url):
    """Run the assistant to completion, saving its transcript in the incident folder."""
    argv = build_command(command, incident, app_url)
    # Lets the assistant start even when this service itself was launched from a Claude Code session.
    env = {key: value for key, value in os.environ.items() if key != "CLAUDECODE"}
    run = {"command": argv, "cwd": str(repo_dir), "started_at": datetime.now(timezone.utc).isoformat()}
    write_json(incident / "assistant.json", run)
    with (incident / "assistant.jsonl").open("w") as stdout, (incident / "assistant.stderr.log").open("w") as stderr:
        try:
            process = subprocess.Popen(argv, cwd=repo_dir, env=env, stdout=stdout, stderr=stderr, stdin=subprocess.DEVNULL)
        except OSError as error:
            run.update(error=str(error), finished_at=datetime.now(timezone.utc).isoformat())
            write_json(incident / "assistant.json", run)
            return run
        run["pid"] = process.pid
        write_json(incident / "assistant.json", run)
        run["exit_code"] = process.wait()
    run["finished_at"] = datetime.now(timezone.utc).isoformat()
    run.update(final_result(incident / "assistant.jsonl"))
    write_json(incident / "assistant.json", run)
    return run


def final_result(transcript):
    """The session ID (for `claude --resume`) and outcome, from the last line of the transcript."""
    for line in reversed(transcript.read_text().splitlines()):
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if message.get("type") == "result":
            return {key: message.get(key) for key in ("session_id", "subtype", "is_error", "total_cost_usd", "num_turns")}
    return {}
