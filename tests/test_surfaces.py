"""Rule engine + CDP-pipe framing — pure, no browser (mirrors the modules'
in-file self-checks so CI guards them too)."""

from __future__ import annotations

import json
import os
import time

import pytest

from zerodom.cdp_pipe import CDPPipe, _DELIM
from zerodom.surfaces import evaluate, load_rules

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
    assert {"sensitive-field-name", "exposed-admin-link", "form-without-csrf"} <= fired


def test_csrf_gate_suppresses_when_token_present():
    graph = {**GRAPH, "metadata": {**GRAPH["metadata"],
             "hidden_fields": [{"name": "csrf_token"}]}}
    assert "form-without-csrf" not in {f.rule for f in evaluate(graph, load_rules())}


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
