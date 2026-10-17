"""Connection-path hardening for the REA MCP client, with NO real mcp/node.

``ReaClient._connect`` lazy-imports ``mcp`` and spawns the REA MCP server over
stdio, then handshakes (``session.initialize()``) before running tools. Three
distinct failure modes must each surface as ONE clean, actionable RuntimeError
rather than a raw ``exceptiongroup.ExceptionGroup`` / ``MCPError`` traceback:

  * mcp is not installed                     -> clear "mcp" / REAGRAPH_REA_CMD error
  * the binary/npx is missing (spawn fails)  -> clear binary / REAGRAPH_REA_CMD error
  * the server spawned but the handshake     -> NEW: clean "MCP handshake" error
    closed the connection during initialize

Every seam is patched (a fake ``mcp`` package injected into ``sys.modules``), so
the suite needs neither the real ``mcp`` package nor Node. ``findings()`` is
driven with no cache configured, so the fully-cached fast path cannot
short-circuit ``_connect``.
"""

import sys
import types

import pytest

from reagraph import rea_client


# The exceptiongroup the live failure produced. Prefer the stdlib type (3.11+),
# else the ``exceptiongroup`` backport that mcp/anyio use on older Pythons.
try:  # pragma: no cover - which arm runs depends on the interpreter version
    _ExceptionGroup = ExceptionGroup  # type: ignore[name-defined]
except NameError:  # pragma: no cover
    from exceptiongroup import ExceptionGroup as _ExceptionGroup


class _MCPError(Exception):
    """Stand-in for mcp's error type (we never import real mcp here)."""


# -- a fake ``mcp`` package whose connect seam fails on demand ----------------


def _install_fake_mcp(monkeypatch, *, aenter_exc):
    """Inject a fake ``mcp`` + ``mcp.client.stdio`` whose stdio_client fails.

    ``aenter_exc`` is raised from ``stdio_client(...).__aenter__`` -- i.e. during
    connect, before ``ClientSession``/``initialize`` -- which is exactly where
    the live spawn-then-handshake failure surfaces.
    """

    class _StdioCtx:
        async def __aenter__(self):
            raise aenter_exc

        async def __aexit__(self, *a):
            return False

    def stdio_client(params):
        return _StdioCtx()

    class _StdioServerParameters:
        def __init__(self, command=None, args=None, **kw):
            self.command = command
            self.args = args

    class _ClientSession:  # never reached (connect fails first), kept for import
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):  # pragma: no cover - connect fails first
            return self

        async def __aexit__(self, *a):  # pragma: no cover
            return False

        async def initialize(self):  # pragma: no cover
            return None

    mcp_mod = types.ModuleType("mcp")
    mcp_mod.ClientSession = _ClientSession
    mcp_mod.StdioServerParameters = _StdioServerParameters

    client_pkg = types.ModuleType("mcp.client")
    stdio_mod = types.ModuleType("mcp.client.stdio")
    stdio_mod.stdio_client = stdio_client
    client_pkg.stdio = stdio_mod

    monkeypatch.setitem(sys.modules, "mcp", mcp_mod)
    monkeypatch.setitem(sys.modules, "mcp.client", client_pkg)
    monkeypatch.setitem(sys.modules, "mcp.client.stdio", stdio_mod)


def _client_no_cache():
    # No cache configured -> _cached_only() returns None -> findings() reaches
    # _connect (the fast path cannot short-circuit).
    return rea_client.ReaClient("app.asar", tools=("analyze_javascript_application",))


# -- NEW: handshake / connect failures become one clean RuntimeError ---------


def test_exceptiongroup_connection_closed_becomes_clean_error(monkeypatch):
    # The live bug: the server starts but closes the MCP connection during the
    # handshake, raising an ExceptionGroup wrapping an MCPError "Connection
    # closed". It must surface as ONE actionable RuntimeError, not the raw group.
    group = _ExceptionGroup(
        "unhandled errors in a TaskGroup",
        [_MCPError("Connection closed")],
    )
    _install_fake_mcp(monkeypatch, aenter_exc=group)

    client = _client_no_cache()
    with pytest.raises(RuntimeError) as ei:
        list(client.findings())

    msg = str(ei.value)
    assert "REAGRAPH_REA_CMD" in msg
    assert "MCP" in msg
    # The first sub-exception's message is surfaced for context ...
    assert "Connection closed" in msg
    # ... and it is NOT a raw ExceptionGroup leaking out.
    assert not isinstance(ei.value, _ExceptionGroup)
    assert "sub-exception" not in msg


def test_generic_exception_during_connect_becomes_clean_error(monkeypatch):
    # Any non-group failure during connect/init is also collapsed cleanly.
    _install_fake_mcp(monkeypatch, aenter_exc=RuntimeError("peer went away"))

    client = _client_no_cache()
    with pytest.raises(RuntimeError) as ei:
        list(client.findings())

    msg = str(ei.value)
    assert "REAGRAPH_REA_CMD" in msg
    assert "MCP" in msg
    assert "peer went away" in msg
    assert "handshake" in msg


def test_connection_error_during_connect_becomes_clean_error(monkeypatch):
    # ConnectionError is explicitly in scope for the handshake handling.
    _install_fake_mcp(monkeypatch, aenter_exc=ConnectionError("broken pipe"))

    client = _client_no_cache()
    with pytest.raises(RuntimeError) as ei:
        list(client.findings())
    assert "REAGRAPH_REA_CMD" in str(ei.value)
    assert "MCP" in str(ei.value)


# -- REGRESSION: the pre-existing specific errors still fire ------------------


def test_missing_mcp_still_raises_clear_error(monkeypatch):
    # mcp absent -> the lazy import fails -> the clear "mcp" RuntimeError, not the
    # new handshake message (we never got far enough to connect).
    monkeypatch.setitem(sys.modules, "mcp", None)  # `from mcp import ...` -> ImportError
    client = _client_no_cache()
    with pytest.raises(RuntimeError) as ei:
        list(client.findings())
    msg = str(ei.value)
    assert "mcp" in msg
    assert "REAGRAPH_REA_CMD" in msg
    assert "handshake" not in msg  # this is the install error, not the connect error


def test_binary_not_found_still_raises_clear_error(monkeypatch):
    # FileNotFoundError on spawn (npx/binary missing) keeps its specific message,
    # distinct from the handshake handling.
    _install_fake_mcp(
        monkeypatch, aenter_exc=FileNotFoundError("[Errno 2] No such file: 'npx'")
    )
    client = _client_no_cache()
    with pytest.raises(RuntimeError) as ei:
        list(client.findings())
    msg = str(ei.value)
    assert "REAGRAPH_REA_CMD" in msg
    assert "could not launch REA MCP server" in msg
    assert "handshake" not in msg  # spawn failure, not a handshake failure


# -- the unwrap helper, directly ---------------------------------------------


def test_first_cause_unwraps_nested_exception_groups():
    inner = _ExceptionGroup("inner", [ValueError("the real cause")])
    outer = _ExceptionGroup("outer", [inner])
    assert rea_client._first_cause(outer) == "the real cause"


def test_first_cause_passes_plain_exception_through():
    assert rea_client._first_cause(ValueError("plain")) == "plain"
