"""Unit tests for offlane.

Network-free: the transport is monkeypatched at the `_http_post` seam.
Run against an install of the package: `pip install -e ".[test]" && pytest`
(or `uv run --extra test pytest`).
"""
from __future__ import annotations

import io
import json

import pytest

import offlane


# --- headers ---------------------------------------------------------------
def test_build_headers_includes_pins():
    cfg = offlane.Config(env={"OFFLANE_MCP_TOKEN": "tok", "OFFLANE_SESSION_ID": "sesn_1"})
    h = offlane._build_headers(cfg)
    assert h["Authorization"] == "Bearer tok"
    assert h["Accept"] == "application/json, text/event-stream"
    assert h["MCP-Protocol-Version"] == offlane.DEFAULT_PROTOCOL_VERSION
    assert h["X-Offlane-Session"] == "sesn_1"


def test_build_headers_omits_session_when_unset():
    h = offlane._build_headers(offlane.Config(env={"OFFLANE_MCP_TOKEN": "tok"}))
    assert "X-Offlane-Session" not in h


def test_token_from_env():
    cfg = offlane.Config(env={"OFFLANE_MCP_TOKEN": "tok"})
    assert cfg.token == "tok"
    assert offlane._build_headers(cfg)["Authorization"] == "Bearer tok"


def test_no_token_omits_authorization():
    # offlane is a general MCP client — an unauthenticated server needs no bearer.
    h = offlane._build_headers(offlane.Config(env={"OFFLANE_MCP_URL": "https://x/mcp"}))
    assert "Authorization" not in h


# --- SSE parsing -----------------------------------------------------------
def test_parse_sse_single_data_frame():
    body = 'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n\n'
    assert offlane._parse_sse(body) == {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}


def test_parse_sse_multiline_data_and_comment():
    body = ': keep-alive\nevent: message\ndata: {"jsonrpc":"2.0",\ndata: "id":1,"result":{"a":1}}\n\n'
    assert offlane._parse_sse(body)["result"] == {"a": 1}


def test_parse_sse_bare_json_fallback():
    body = '{"jsonrpc":"2.0","id":1,"result":{"x":2}}'
    assert offlane._parse_sse(body)["result"] == {"x": 2}


def test_parse_sse_unparseable_raises():
    with pytest.raises(offlane.LaneError):
        offlane._parse_sse("event: ping\n\n")


# --- payload extraction ----------------------------------------------------
def _mk_result(obj):
    return {"content": [{"type": "text", "text": json.dumps(obj)}]}


def test_extract_payload_list():
    assert offlane._extract_payload(_mk_result([1, 2, 3])) == [1, 2, 3]


def test_extract_payload_object():
    assert offlane._extract_payload(_mk_result({"a": 1})) == {"a": 1}


def test_extract_payload_structured_content_preferred():
    r = {"content": [{"type": "text", "text": "ignored"}], "structuredContent": {"b": 2}}
    assert offlane._extract_payload(r) == {"b": 2}


def test_extract_payload_non_json_text_kept():
    assert offlane._extract_payload({"content": [{"type": "text", "text": "hi there"}]}) == "hi there"


def test_extract_payload_tool_error_raises():
    r = {"isError": True, "content": [{"type": "text", "text": "Invalid input: limit must be a number"}]}
    with pytest.raises(offlane.LaneError):
        offlane._extract_payload(r)


# --- summarize (the guardrail) ---------------------------------------------
def test_summarize_list():
    out = offlane.summarize([1, 2, 3, 4], nbytes=123)
    assert "bytes:   123" in out
    assert "records: 4" in out


def test_summarize_single_array_dict():
    out = offlane.summarize({"results": [1, 2, 3], "total": 3}, nbytes=50)
    assert "records: 3" in out
    assert "results" in out and "total" in out


def test_summarize_multi_array_dict():
    assert "n/a (multiple arrays" in offlane.summarize({"a": [1], "b": [1, 2]}, nbytes=10)


def test_summarize_scalar():
    assert "n/a (scalar)" in offlane.summarize("hi", nbytes=4)


def test_summarize_never_contains_values():
    payload = {"secret_value": "TOPSECRET-DEADBEEF", "rows": [{"x": 1}]}
    out = offlane.summarize(payload, nbytes=99)
    assert "TOPSECRET" not in out  # key names are fine; values never leak


# --- arg resolution --------------------------------------------------------
def test_resolve_args_inline():
    assert offlane._resolve_args('{"limit":3}') == {"limit": 3}


def test_resolve_args_empty():
    assert offlane._resolve_args("") == {}


def test_resolve_args_file(tmp_path):
    p = tmp_path / "a.json"
    p.write_text('{"q":"x"}')
    assert offlane._resolve_args(str(p)) == {"q": "x"}


def test_resolve_args_stdin(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO('{"s":1}'))
    assert offlane._resolve_args("-") == {"s": 1}


def test_resolve_args_bad_json_raises():
    with pytest.raises(offlane.LaneError):
        offlane._resolve_args("{not json}")


def test_resolve_args_non_object_raises():
    with pytest.raises(offlane.LaneError):
        offlane._resolve_args("[1,2]")


# --- retry + chokepoint ----------------------------------------------------
def _http_error(url, code, msg, body=b""):
    return offlane.urllib.error.HTTPError(url, code, msg, {}, io.BytesIO(body))


def _cfg(**extra):
    """A Config with a URL set so real `_post` gets past the endpoint check."""
    env = {"OFFLANE_MCP_URL": "https://x/mcp", "OFFLANE_MCP_TOKEN": "t", "OFFLANE_MCP_BACKOFF": "0"}
    env.update(extra)
    return offlane.Config(env=env)


def test_retry_on_502_then_success(monkeypatch):
    calls = {"n": 0}

    def fake(url, data, headers, timeout):
        calls["n"] += 1
        if calls["n"] < 3:
            raise _http_error(url, 502, "Bad Gateway")
        return 200, 'data: {"jsonrpc":"2.0","id":1,"result":{"ok":1}}\n\n'

    monkeypatch.setattr(offlane, "_http_post", fake)
    monkeypatch.setattr(offlane.time, "sleep", lambda *_: None)
    assert offlane._post("tools/list", {}, _cfg()) == {"ok": 1}
    assert calls["n"] == 3


def test_no_retry_on_400(monkeypatch):
    calls = {"n": 0}

    def fake(url, data, headers, timeout):
        calls["n"] += 1
        raise _http_error(url, 400, "Bad Request", b"nope")

    monkeypatch.setattr(offlane, "_http_post", fake)
    with pytest.raises(offlane.LaneError):
        offlane._post("tools/list", {}, _cfg())
    assert calls["n"] == 1


def test_post_requires_url():
    # URL is required; token is optional (unauthenticated servers exist).
    with pytest.raises(offlane.LaneError):
        offlane._post("tools/list", {}, offlane.Config(env={"OFFLANE_MCP_TOKEN": "t"}))


# --- call end-to-end (transport mocked) ------------------------------------
def test_cmd_call_writes_out_and_summary_only(monkeypatch, tmp_path, capsys):
    payload = {"rows": [{"id": 1, "secret": "XYZZY"}], "total": 1}
    monkeypatch.setattr(
        offlane, "_post", lambda *a, **k: {"content": [{"type": "text", "text": json.dumps(payload)}]}
    )
    out = tmp_path / "r.json"
    rc = offlane.cmd_call(offlane.Config(env={"OFFLANE_MCP_TOKEN": "t"}), "some_tool", "", str(out))
    printed = capsys.readouterr().out
    assert rc == 0
    assert "XYZZY" not in printed  # payload is NEVER dumped to stdout
    assert "records: 1" in printed
    assert json.loads(out.read_text()) == payload  # full payload lands on disk


# --- transport edge cases --------------------------------------------------
def test_tool_error_text_is_capped():
    # An unbounded isError body would re-enter the caller's context via stderr.
    r = {"isError": True, "content": [{"type": "text", "text": "x" * 5000}]}
    with pytest.raises(offlane.LaneError) as ei:
        offlane._extract_payload(r)
    assert len(str(ei.value)) < 500


def test_retry_on_midread_reset_then_success(monkeypatch):
    # A ConnectionResetError during resp.read() is NOT a URLError — must still retry.
    calls = {"n": 0}

    def fake(url, data, headers, timeout):
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionResetError("connection reset by peer")
        return 200, 'data: {"jsonrpc":"2.0","id":1,"result":{"ok":1}}\n\n'

    monkeypatch.setattr(offlane, "_http_post", fake)
    monkeypatch.setattr(offlane.time, "sleep", lambda *_: None)
    assert offlane._post("tools/list", {}, _cfg()) == {"ok": 1}
    assert calls["n"] == 3


def test_retry_on_incomplete_read(monkeypatch):
    import http.client

    calls = {"n": 0}

    def fake(url, data, headers, timeout):
        calls["n"] += 1
        if calls["n"] < 2:
            raise http.client.IncompleteRead(partial=b"")
        return 200, 'data: {"jsonrpc":"2.0","id":1,"result":{"ok":2}}\n\n'

    monkeypatch.setattr(offlane, "_http_post", fake)
    monkeypatch.setattr(offlane.time, "sleep", lambda *_: None)
    assert offlane._post("tools/list", {}, _cfg()) == {"ok": 2}


def test_midread_reset_exhausted_becomes_laneerror(monkeypatch):
    def fake(url, data, headers, timeout):
        raise ConnectionResetError("reset")

    monkeypatch.setattr(offlane, "_http_post", fake)
    monkeypatch.setattr(offlane.time, "sleep", lambda *_: None)
    with pytest.raises(offlane.LaneError):  # not a raw traceback
        offlane._post("tools/list", {}, _cfg())


def test_cmd_call_write_failure_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(offlane, "_post", lambda *a, **k: {"content": [{"type": "text", "text": "[]"}]})
    bad = tmp_path / "does-not-exist" / "r.json"  # parent dir absent
    with pytest.raises(offlane.LaneError):
        offlane.cmd_call(offlane.Config(env={"OFFLANE_MCP_TOKEN": "t"}), "t", "", str(bad))


def test_list_tools_malformed_result_raises(monkeypatch):
    monkeypatch.setattr(offlane, "_post", lambda *a, **k: ["not", "a", "dict"])
    with pytest.raises(offlane.LaneError):
        offlane._list_tools(offlane.Config(env={"OFFLANE_MCP_TOKEN": "t"}))


def test_list_tools_bad_cursor_type_stops(monkeypatch):
    monkeypatch.setattr(
        offlane, "_post",
        lambda method, params, cfg: {"tools": [{"name": "a"}], "nextCursor": ["unhashable"]},
    )
    out = offlane._list_tools(offlane.Config(env={"OFFLANE_MCP_TOKEN": "t"}))
    assert [t["name"] for t in out] == ["a"]  # stopped, no TypeError


def test_list_tools_match_name_short_circuits(monkeypatch):
    pages = [
        {"tools": [{"name": "a"}], "nextCursor": "c1"},
        {"tools": [{"name": "target"}], "nextCursor": "c2"},
        {"tools": [{"name": "z"}]},
    ]
    seq = {"i": 0}

    def fake(method, params, cfg):
        p = pages[seq["i"]]
        seq["i"] += 1
        return p

    monkeypatch.setattr(offlane, "_post", fake)
    out = offlane._list_tools(offlane.Config(env={"OFFLANE_MCP_TOKEN": "t"}), match_name="target")
    assert seq["i"] == 2  # stopped after the matching page; page 3 never fetched
    assert any(t["name"] == "target" for t in out)


def test_peek_zero_is_honored(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(offlane, "_post", lambda *a, **k: {"content": [{"type": "text", "text": "[1,2,3]"}]})
    out = tmp_path / "r.json"
    offlane.cmd_call(offlane.Config(env={"OFFLANE_MCP_TOKEN": "t"}), "t", "", str(out), peek=0)
    assert "--- peek ---" in capsys.readouterr().out


def test_peek_nonlist_reuses_data_prefix():
    payload = {"a": 1}
    data = json.dumps(payload, indent=2, ensure_ascii=False)
    assert offlane._peek(payload, data, 4) == data[:4]


def test_peek_list_returns_first_n_items():
    assert json.loads(offlane._peek([1, 2, 3, 4], "ignored", 2)) == [1, 2]


def test_config_empty_protocol_version_falls_back():
    cfg = offlane.Config(env={"OFFLANE_MCP_PROTOCOL_VERSION": ""})
    assert cfg.protocol_version == offlane.DEFAULT_PROTOCOL_VERSION


def test_help_epilog_carries_the_protocol():
    # An agent's prompt can point at `offlane --help` for the full protocol, so it
    # must actually convey schema-first + the call/project/never-cat loop.
    p = offlane.build_parser()
    assert p.epilog is offlane._HELP_EPILOG
    epi = offlane._HELP_EPILOG.lower()
    assert "offlane schema" in epi and "before first use" in epi
    assert "--out" in epi and "never `cat`" in epi
