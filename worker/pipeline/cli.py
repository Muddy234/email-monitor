"""Claude CLI subprocess runner — shared utility for analyzer and draft generator."""

import logging
import os
import subprocess
import sys

logger = logging.getLogger("email_monitor")

# Windows-specific: suppress console window for subprocesses
_CREATION_FLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
_CWD = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Ensure Claude CLI can find git-bash on Windows
if sys.platform == "win32" and not os.environ.get("CLAUDE_CODE_GIT_BASH_PATH"):
    _git_bash = os.path.expandvars(
        r"%LOCALAPPDATA%\Programs\Git\bin\bash.exe"
    )
    if os.path.isfile(_git_bash):
        os.environ["CLAUDE_CODE_GIT_BASH_PATH"] = _git_bash


def run_claude_cli(cmd, timeout, stdin_text=None):
    """Run a Claude CLI command, return (stdout, stderr, returncode).

    Args:
        cmd: Command list (executable + arguments).
        timeout: Timeout in seconds.
        stdin_text: Optional text to pipe via stdin (avoids command-line
            length limits on Windows).

    Raises:
        subprocess.TimeoutExpired: if the process exceeds timeout.
        FileNotFoundError: if the CLI binary is not found.
    """
    # Ensure git-bash env var is set at call time (not just import time)
    if sys.platform == "win32" and not os.environ.get("CLAUDE_CODE_GIT_BASH_PATH"):
        _gb = os.path.expandvars(r"%LOCALAPPDATA%\Programs\Git\bin\bash.exe")
        if os.path.isfile(_gb):
            os.environ["CLAUDE_CODE_GIT_BASH_PATH"] = _gb

    result = subprocess.run(
        cmd,
        input=stdin_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        cwd=_CWD,
        creationflags=_CREATION_FLAGS,
        env=os.environ.copy(),
    )
    return result.stdout.strip(), result.stderr or "", result.returncode
