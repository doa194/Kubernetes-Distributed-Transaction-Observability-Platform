"""Finds and runs the external command-line tools the platform depends on.

kind, Helm, Docker and `kubectl port-forward` have no dependable Python APIs, so they are
called as programs. Arguments are always passed as a list and never through a shell,
because the repository path contains spaces and Windows quoting rules differ from Unix.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path

from txplatform import paths


class ToolError(RuntimeError):
    """Raised when a required tool is missing or a command fails."""


@dataclass(frozen=True)
class CommandResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str


def find_tool(name: str) -> Path | None:
    """Looks on PATH first, then in the project-local `.tools/bin` folder."""
    found = shutil.which(name)
    if found:
        return Path(found)
    local = shutil.which(name, path=str(paths.project_tools_dir()))
    return Path(local) if local else None


def require_tool(name: str) -> Path:
    tool = find_tool(name)
    if tool is None:
        raise ToolError(f"'{name}' was not found on PATH or in {paths.project_tools_dir()}")
    return tool


def run(
    tool: str,
    *args: str,
    check: bool = True,
    capture: bool = True,
    input_text: str | None = None,
    timeout: float | None = None,
    cwd: Path | None = None,
) -> CommandResult:
    executable = require_tool(tool)
    command = [str(executable), *args]
    process = start(
        command,
        stdin=subprocess.PIPE if input_text is not None else None,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        cwd=cwd,
    )
    try:
        stdout, stderr = process.communicate(input_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        terminate_tree(process)
        raise
    result = CommandResult(command, process.returncode, stdout or "", stderr or "")
    if check and process.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ToolError(f"{tool} {' '.join(args[:3])} failed (exit {process.returncode}): {detail[-2000:]}")
    return result


def start(command: list[str], *, stdin=None, stdout=None, stderr=None, cwd: Path | None = None) -> subprocess.Popen:
    """Starts a tool so that it, and every process it starts, can be stopped with `terminate_tree`."""
    return subprocess.Popen(
        command,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(cwd) if cwd else None,
        env=tool_env(),
        # On Linux and macOS the tool gets its own process group, which is stopped as a whole.
        start_new_session=os.name != "nt",
    )


def terminate_tree(process: subprocess.Popen, timeout: float = 10) -> None:
    """Stops a started tool together with its child processes.

    Some installers put small launcher programs ("shims", such as Chocolatey's kubectl.exe) on
    the PATH, which start the real program as a child process. Stopping only the launcher would
    leave the real program running, for example a port-forward that never ends.
    """
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=timeout)


def tool_env() -> dict[str, str]:
    """Environment for long-running child processes such as port-forwards."""
    env = os.environ.copy()
    env["PATH"] = str(paths.project_tools_dir()) + os.pathsep + env.get("PATH", "")
    return env
