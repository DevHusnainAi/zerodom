"""Rule engine + CDP-pipe framing — pure, no browser (mirrors the modules'
in-file self-checks so CI guards them too)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from zerodom.cdp_pipe import CDPPipe, _DELIM
from zerodom.surfaces import evaluate, load_rules

_PACKS = sorted((Path(__file__).resolve().parent.parent / "surfaces").glob("*.yaml"))


@pytest.mark.parametrize("pack", _PACKS, ids=lambda p: p.name)
def test_every_community_rule_pack_loads(pack):
    """Each surfaces/*.yaml must parse (unknown keys / bad severity raise at load),
    so a malformed contributed rule fails CI instead of silently never firing."""
    rules = load_rules(pack)
    assert rules, f"{pack.name} has no rules"
    assert all(r.id and r.description for r in rules), pack.name

GRAPH = {
    "metadata": {"url": "http://x", "hidden_fields": [{"name": "session_id"}]},
    "nodes": [
        {"id": "node_01", "type": "input", "role": "textbox",
         "label": "API_KEY", "selector": "#k", "value": ""},
        {"id": "node_02", "type": "a", "role": "link",
         "label": "Panel", "selector": "#p", "href": "/admin/users"},
        {"id": "node_03", "type": "input", "role": "textbox",
         "label": "Search", "selector": "#s", "value": ""},
    ],
}


def test_bundled_ruleset_fires_the_expected_rules():
    fired = {f.rule for f in evaluate(GRAPH, load_rules())}
    assert {"sensitive-field-name", "exposed-admin-link"} <= fired
    assert "form-without-csrf" not in fired  # retired as noise; see csrf_posture


_PW_GRAPH = {
    "metadata": {"url": "http://x", "hidden_fields": []},
    "nodes": [{"id": "n1", "type": "input", "input_type": "password",
               "role": "textbox", "label": "Password", "selector": "#p"}],
}


def test_csrf_gate_suppresses_when_token_present():
    # The page-level missing_hidden_regex gate, via the kept password rule.
    assert "password-field-outside-form-token" in {f.rule for f in evaluate(_PW_GRAPH, load_rules())}
    with_token = {**_PW_GRAPH, "metadata": {**_PW_GRAPH["metadata"],
                  "hidden_fields": [{"name": "csrf_token"}]}}
    assert "password-field-outside-form-token" not in {f.rule for f in evaluate(with_token, load_rules())}


def test_unknown_match_key_is_a_load_error(tmp_path):
    bad = tmp_path / "r.yaml"
    bad.write_text("rules:\n  - id: b\n    match: {lable_regex: x}\n")
    with pytest.raises(ValueError, match="unknown match keys"):
        load_rules(bad)


def test_pipe_framing_splits_on_nul_and_buffers_events():
    r, w = os.pipe()
    pipe = CDPPipe(write_fd=w, read_fd=r, proc=None, profile_dir=None)  # type: ignore[arg-type]
    os.write(
        w,
        json.dumps({"method": "E.evt", "params": {}}).encode() + _DELIM
        + json.dumps({"id": 1, "result": {"ok": True}}).encode() + _DELIM
        + b'{"id":2,"result":',  # partial frame, no delimiter
    )
    assert pipe._await(1, time.monotonic() + 2) == {"ok": True}
    assert pipe.events[0]["method"] == "E.evt"
    os.write(w, b'{"v":9}}' + _DELIM)
    assert pipe._await(2, time.monotonic() + 2) == {"v": 9}
    os.close(w)
    os.close(r)
