"""Preflight "doctor" for the live REA pull: is this machine ready to ``pull``?

REA's #1 reported friction is setup/readiness -- people hit a wall before any
analysis runs because a prerequisite (the ``mcp`` package, node/npx, a working
REA command) is missing, and the failure shows up deep inside a spawn. This
module front-loads that: it CHECKS the prerequisites and gives actionable
guidance, and it does exactly nothing else.

It is fast and safe by construction: pure inspection via the standard library
(``importlib.util.find_spec`` for an importable package, ``shutil.which`` for a
PATH lookup). It NEVER spawns REA, never runs ``npx``, never touches the
network, and never runs any analysis.

Honest by design: a clean doctor means the prerequisites are PRESENT, not that a
real REA run has been verified. ``report`` says so out loud -- "ready" here is
"nothing is obviously missing", never "a live pull has succeeded".

The ``reagraph doctor`` CLI command is wired separately; this module only
supplies ``check``/``report``/``ready``.
"""

from __future__ import annotations

import importlib.util
import os
import shlex
import shutil
import sys

# Single source of truth for the npx-based default REA command.
from .rea_client import DEFAULT_SERVER_CMD


def _resolve_server_cmd(server_cmd=None) -> list:
    """Explicit arg > $REAGRAPH_REA_CMD > npx default. Never spawns (mirrors
    ``reagraph.rea_client.ReaClient._resolve_cmd`` so the doctor reports the same
    command a real pull would launch)."""
    if server_cmd:
        return list(server_cmd)
    env = os.environ.get("REAGRAPH_REA_CMD")
    if env:
        return shlex.split(env)
    return list(DEFAULT_SERVER_CMD)


def _uses_npx(cmd) -> bool:
    """Does the resolved command spawn REA via npx (the default path)?"""
    return bool(cmd) and os.path.basename(cmd[0]) == "npx"


def _check_python() -> dict:
    v = sys.version_info
    pretty = f"{v.major}.{v.minor}.{v.micro}"
    if v[:2] >= (3, 9):
        return {
            "name": "python",
            "status": "ok",
            "detail": f"Python {pretty} (>= 3.9)",
            "fix": "",
        }
    return {
        "name": "python",
        "status": "fail",
        "detail": f"Python {pretty} is older than the required 3.9",
        "fix": "install Python 3.9 or newer",
    }


def _check_mcp() -> dict:
    # find_spec is import-free inspection: it locates the package without running
    # it. A None spec (or a raised exception for a truly absent name) means the
    # live path is unavailable.
    try:
        spec = importlib.util.find_spec("mcp")
    except (ImportError, ValueError):
        spec = None
    if spec is not None:
        return {
            "name": "mcp",
            "status": "ok",
            "detail": "the 'mcp' package is importable (live `reagraph pull` can reach REA)",
            "fix": "",
        }
    return {
        "name": "mcp",
        "status": "fail",
        "detail": (
            "the 'mcp' package is NOT importable; it is required for live "
            "`reagraph pull` (everything else in REAgraph works without it)"
        ),
        "fix": "pip install mcp",
    }


def _check_npx(cmd) -> dict:
    # npx is only needed to spawn REA via the npx default; a custom
    # $REAGRAPH_REA_CMD may launch REA some other way and not need it at all.
    if not _uses_npx(cmd):
        return {
            "name": "node/npx",
            "status": "ok",
            "detail": (
                "resolved REA command does not use npx; node/npx not required"
            ),
            "fix": "",
        }
    if shutil.which("npx"):
        return {
            "name": "node/npx",
            "status": "ok",
            "detail": "npx found on PATH (used to spawn REA via the npx default)",
            "fix": "",
        }
    return {
        "name": "node/npx",
        "status": "warn",
        "detail": (
            "npx not found on PATH; the default REA command spawns REA via npx"
        ),
        "fix": (
            "install Node.js (it bundles npx), or set REAGRAPH_REA_CMD to a REA "
            "MCP server command that does not use npx"
        ),
    }


def _check_rea_cmd(cmd) -> dict:
    shown = " ".join(shlex.quote(part) for part in cmd)
    first = cmd[0] if cmd else ""
    # Inspection only: we report what WOULD be spawned and whether its first
    # token resolves on PATH. We do NOT execute it.
    if first and shutil.which(first):
        return {
            "name": "rea-command",
            "status": "ok",
            "detail": f"will spawn: {shown}  (first token '{first}' resolves on PATH)",
            "fix": "",
        }
    return {
        "name": "rea-command",
        "status": "warn",
        "detail": (
            f"will spawn: {shown}  (first token '{first}' does NOT resolve on PATH)"
        ),
        "fix": (
            f"ensure '{first}' is installed and on PATH, or set REAGRAPH_REA_CMD "
            "to a working REA MCP server command"
        ),
    }


def _check_graphify() -> dict:
    # Optional: only `reagraph query` shells out to graphify.
    if shutil.which("graphify"):
        return {
            "name": "graphify",
            "status": "ok",
            "detail": "graphify found on PATH (used by `reagraph query`)",
            "fix": "",
        }
    return {
        "name": "graphify",
        "status": "warn",
        "detail": "graphify not found on PATH; optional, only needed for `reagraph query`",
        "fix": "uv tool install graphifyy   # optional; only for `reagraph query`",
    }


def check(*, server_cmd=None) -> list:
    """Run every preflight check and return an ordered list of result dicts.

    Each result is ``{"name", "status": "ok"|"warn"|"fail", "detail", "fix"}``.
    Pure inspection: no subprocess spawn, no network, no analysis. ``server_cmd``
    (explicit) takes precedence over $REAGRAPH_REA_CMD and the npx default when
    resolving the REA command the checks report on.
    """
    cmd = _resolve_server_cmd(server_cmd)
    return [
        _check_python(),
        _check_mcp(),
        _check_npx(cmd),
        _check_rea_cmd(cmd),
        _check_graphify(),
    ]


def ready() -> bool:
    """True iff no check failed (warnings are allowed; fails are not)."""
    return not any(c["status"] == "fail" for c in check())


def report() -> str:
    """A readable per-check summary ending in a one-line verdict.

    Honest framing: a clean doctor means the prerequisites are present, NOT that
    a real REA run has been verified.
    """
    checks = check()
    glyph = {"ok": "ok  ", "warn": "warn", "fail": "FAIL"}

    lines = [
        "REAgraph doctor -- live `pull` preflight",
        "=" * 40,
        "",
    ]
    for c in checks:
        lines.append(f"[{glyph.get(c['status'], c['status'])}] {c['name']}: {c['detail']}")
        if c["fix"]:
            lines.append(f"       fix: {c['fix']}")

    fails = [c for c in checks if c["status"] == "fail"]
    lines.append("")
    if fails:
        needs = "; ".join(c["fix"] for c in fails if c["fix"])
        lines.append(f"verdict: live pull needs: {needs}")
    else:
        lines.append(
            "verdict: live pull ready (prerequisites present; this does NOT mean "
            "a real REA run has been verified)"
        )
    return "\n".join(lines)
