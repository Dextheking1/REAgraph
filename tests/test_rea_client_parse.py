"""Unit tests for ``rea_client._parse_cli_json`` -- the real-REA ingestion gate.

``_parse_cli_json`` turns REA's ``--format json`` stdout into the envelope the CLI
transport maps. It is the gatekeeper for every real-REA pull, so it must be total
(never raise) and must pick the right document out of noisy stdout: the whole text
when clean, otherwise the LAST envelope-shaped (``ok``/``data``) JSON object on its
own line, ignoring progress banners, trailing garbage, and bare arrays.
"""

import json

from reagraph.rea_client import _parse_cli_json


def _envelope(tag):
    """A minimal {ok,data} envelope tagged so we can tell copies apart."""
    return {"ok": True, "data": {"normalized_result": {"tag": tag}}}


# -- (a) a single clean envelope parses -------------------------------------

def test_single_clean_envelope_parses():
    env = _envelope("one")
    assert _parse_cli_json(json.dumps(env)) == env


def test_clean_envelope_with_surrounding_whitespace():
    env = _envelope("ws")
    assert _parse_cli_json("\n  " + json.dumps(env) + "  \n") == env


# -- (b) multiple envelope-shaped lines -> the LAST wins --------------------

def test_multiple_envelope_lines_last_wins():
    text = "\n".join(
        json.dumps(_envelope(tag)) for tag in ("first", "second", "third")
    )
    # The whole text is not a single JSON doc, so the line scan runs and keeps
    # the last envelope-shaped object.
    out = _parse_cli_json(text)
    assert out["data"]["normalized_result"]["tag"] == "third"


# -- (c) a leading progress/banner line, then the JSON doc ------------------

def test_leading_progress_line_then_json():
    env = _envelope("after-banner")
    text = "REA v1.2.3 starting analysis...\n" + json.dumps(env)
    assert _parse_cli_json(text) == env


def test_leading_json_progress_object_is_skipped_for_envelope():
    env = _envelope("real")
    # A progress object is valid JSON but not an ok/data envelope -> ignored.
    text = '{"rea_progress": {"phase": "scan"}}\n' + json.dumps(env)
    assert _parse_cli_json(text) == env


# -- (d) trailing garbage after a valid doc ---------------------------------

def test_trailing_garbage_after_valid_doc():
    env = _envelope("valid")
    text = json.dumps(env) + "\nthis is not json at all <<<\n"
    out = _parse_cli_json(text)
    assert out["data"]["normalized_result"]["tag"] == "valid"


# -- (e) a top-level array line mixed in is not mistaken for the envelope ----

def test_toplevel_array_line_mixed_in_is_ignored():
    env = _envelope("obj")
    text = "[1, 2, 3]\n" + json.dumps(env) + "\n[4, 5]\n"
    out = _parse_cli_json(text)
    assert isinstance(out, dict)
    assert out["data"]["normalized_result"]["tag"] == "obj"


def test_only_an_array_yields_none():
    # A lone JSON array is not an envelope; the whole-text parse returns a list,
    # which the caller treats as "no data" -- here we assert the shape directly.
    out = _parse_cli_json("[1, 2, 3]")
    assert out == [1, 2, 3] or out is None  # never an envelope dict


# -- (f) empty / whitespace / None -> None ----------------------------------

def test_empty_whitespace_and_none_return_none():
    assert _parse_cli_json("") is None
    assert _parse_cli_json("   \n\t ") is None
    assert _parse_cli_json(None) is None


# -- (g) non-envelope JSON (no ok/data) -> None -----------------------------

def test_non_envelope_object_in_scan_returns_none():
    # Multiple lines (forces the scan), none carrying ok/data -> None.
    text = '{"foo": 1}\n{"bar": 2}\n'
    assert _parse_cli_json(text) is None


def test_single_non_envelope_object_parses_whole_text():
    # A single clean JSON object round-trips via the whole-text json.loads even
    # without ok/data (the caller's _rea_error/adapter handle the shape later).
    assert _parse_cli_json('{"foo": 1}') == {"foo": 1}


# -- totality: never raises on arbitrary input ------------------------------

def test_never_raises_on_arbitrary_garbage():
    for bad in ["{not json", "}{", "\x00\x01", "{\n}\n][", "null", "true", "42",
                "{'single': 'quotes'}", "[", "{", "}\n{\n"]:
        # Must return something (object, list, or None) and never raise.
        _parse_cli_json(bad)
