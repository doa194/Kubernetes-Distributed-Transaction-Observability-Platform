"""Stopping a tool must also stop the processes it started, or port-forwards leak.

This test starts real local processes: a launcher that starts a long-running child, like the
Chocolatey kubectl shim does.
"""

import subprocess
import sys
import time

from txplatform import tools

LAUNCHER = """
import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
print(child.pid, flush=True)
child.wait()
"""


def _alive(pid: int) -> bool:
    if sys.platform == "win32":
        listed = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in listed
    try:
        import os

        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_terminating_a_launcher_also_stops_its_child():
    launcher = tools.start([sys.executable, "-c", LAUNCHER], stdout=subprocess.PIPE)
    child_pid = int(launcher.stdout.readline())
    assert _alive(child_pid)

    tools.terminate_tree(launcher)

    deadline = time.monotonic() + 10
    while _alive(child_pid) and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not _alive(child_pid)
    assert launcher.poll() is not None
