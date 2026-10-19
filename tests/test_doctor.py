"""The preflight doctor, tested with NO real subprocess and NO network.

Every branch is forced by monkeypatching the two inspection primitives the
doctor uses -- ``shutil.which`` (PATH lookup) and ``importlib.util.find_spec``
(importable package) -- in the ``doctor`` module's own namespace. A present
tool returns a fake path; an absent tool returns ``None``. Nothing is ever
spawned and nothing reaches the network.
"""

from reagraph import doctor

# A truthy stand-in for a real ModuleSpec; the doctor only checks for non-None.
_FAKE_SPEC = object()


def _all_present(monkeypatch):
    """Make every PATH tool resolve and the ``mcp`` package importable."""
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        doctor.importlib.util,
        "find_spec",
        lambda name: _FAKE_SPEC if name == "mcp" else None,
    )


def _nothing_present(monkeypatch):
    """Make every PATH tool missing and ``mcp`` not importable."""
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    monkeypatch.setattr(doctor.importlib.util, "find_spec", lambda name: None)


def test_all_present_is_ready_with_no_fail(monkeypatch):
    _all_present(monkeypatch)
    checks = doctor.check()

    assert {c["status"] for c in checks} <= {"ok", "warn"}  # no fail
    assert all(c["status"] != "fail" for c in checks)
    assert doctor.ready() is True

    names = [c["name"] for c in checks]
    assert names == ["python", "mcp", "node/npx", "rea-command", "graphify"]


def test_mcp_absent_fails_with_pip_fix_and_not_ready(monkeypatch):
    _nothing_present(monkeypatch)
    checks = doctor.check()

    mcp_check = next(c for c in checks if c["name"] == "mcp")
    assert mcp_check["status"] == "fail"
    assert "pip install mcp" in mcp_check["fix"]
    assert doctor.ready() is False


def test_npx_absent_warns_for_default_command(monkeypatch):
    # mcp present so the only remaining issues are warnings (node/npx missing,
    # npx-default not on PATH, graphify missing) -- still ready, never failed.
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        doctor.importlib.util,
        "find_spec",
        lambda name: _FAKE_SPEC if name == "mcp" else None,
    )
    monkeypatch.delenv("REAGRAPH_REA_CMD", raising=False)

    checks = doctor.check()
    npx_check = next(c for c in checks if c["name"] == "node/npx")
    assert npx_check["status"] == "warn"
    assert "npx" in npx_check["detail"]
    assert doctor.ready() is True  # a missing optional tool is a warn, not a fail


def test_env_override_is_reflected_in_rea_command_check(monkeypatch):
    _all_present(monkeypatch)
    monkeypatch.setenv("REAGRAPH_REA_CMD", "myrea --stdio serve")

    rea_check = next(c for c in doctor.check() if c["name"] == "rea-command")
    assert "myrea --stdio serve" in rea_check["detail"]

    # A custom command that does not use npx means node/npx is not required.
    npx_check = next(c for c in doctor.check() if c["name"] == "node/npx")
    assert npx_check["status"] == "ok"
    assert "does not use npx" in npx_check["detail"]


def test_explicit_server_cmd_takes_precedence(monkeypatch):
    _all_present(monkeypatch)
    monkeypatch.setenv("REAGRAPH_REA_CMD", "env-rea serve")

    rea_check = next(
        c for c in doctor.check(server_cmd=["explicit-rea", "go"])
        if c["name"] == "rea-command"
    )
    assert "explicit-rea go" in rea_check["detail"]
    assert "env-rea" not in rea_check["detail"]


def test_report_is_a_string_mentioning_the_verdict(monkeypatch):
    _all_present(monkeypatch)
    text = doctor.report()
    assert isinstance(text, str)
    assert "verdict:" in text
    assert "live pull ready" in text

    _nothing_present(monkeypatch)
    failed = doctor.report()
    assert "live pull needs:" in failed
    assert "pip install mcp" in failed


def test_graphify_fix_names_the_graphifyy_pypi_package(monkeypatch):
    # The PyPI distribution is `graphifyy` (double y); the CLI binary it installs
    # is `graphify`. The doctor's install hint must point at the real package.
    _nothing_present(monkeypatch)  # graphify absent -> the warn branch with a fix
    graphify_check = next(c for c in doctor.check() if c["name"] == "graphify")
    assert "uv tool install graphifyy" in graphify_check["fix"]
    # Must NOT tell the user to install a package named "graphify " (single y).
    assert "install graphify " not in graphify_check["fix"]
    assert "install graphify\n" not in graphify_check["fix"]

    # The same corrected hint must surface in the rendered report.
    report = doctor.report()
    assert "uv tool install graphifyy" in report
    assert "uv tool install graphify " not in report


def test_check_does_not_spawn_or_hit_the_network(monkeypatch):
    # Guard: if any branch tried to run a process, this would blow up.
    import subprocess

    def _boom(*a, **k):
        raise AssertionError("doctor must not spawn a subprocess")

    monkeypatch.setattr(subprocess, "run", _boom, raising=False)
    monkeypatch.setattr(subprocess, "Popen", _boom, raising=False)
    monkeypatch.setattr(subprocess, "call", _boom, raising=False)
    _all_present(monkeypatch)

    assert doctor.check()  # non-empty, and nothing was spawned
    assert isinstance(doctor.report(), str)
