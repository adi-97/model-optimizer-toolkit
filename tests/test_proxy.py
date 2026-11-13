import json
import os
import sys
import tempfile
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import httpx
from starlette.testclient import TestClient

from adtok.proxy import load_config
from adtok.proxy.policy import adapt_thinking
from adtok.proxy.pricing import Pricing
from adtok.proxy.server import SSEUsage, create_app
from adtok.proxy.tracker import Tracker

MAIN_SYSTEM = [{"type": "text", "text": "You are the main coding agent."}]
TOOLS = [{"name": "Read", "input_schema": {"type": "object"}}, {"name": "Bash", "input_schema": {"type": "object"}}]
USAGE = {"input_tokens": 1000, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 200}


class FakeClassifier:
    def __init__(self):
        self.calls = []

    def classify(self, text):
        self.calls.append(text)
        t = text.lower()
        tier = "complex" if ("refactor" in t or "architecture" in t) else "mid" if "endpoint" in t else "simple"
        return {"tier": tier, "confidence": 0.9, "probabilities": {}, "escalated": False, "latency_ms": 1.0}


class Upstream:
    def __init__(self):
        self.requests = []
        self.reject_models = set()

    def __call__(self, request):
        body = json.loads(request.content) if request.content else {}
        self.requests.append({"path": request.url.path, "body": body, "headers": dict(request.headers)})
        if body.get("model") in self.reject_models:
            return httpx.Response(400, json={"type": "error", "error": {"type": "invalid_request_error",
                                                                        "message": "nope"}})
        if body.get("stream"):
            events = [
                {"type": "message_start", "message": {"id": "m", "model": body.get("model"), "usage": dict(USAGE, output_tokens=1)}},
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hi"}},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 200}},
                {"type": "message_stop"},
            ]
            payload = b"".join(b"event: %s\ndata: %s\n\n" % (e["type"].encode(), json.dumps(e).encode()) for e in events)
            return httpx.Response(200, content=payload, headers={"content-type": "text/event-stream"})
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": []})
        return httpx.Response(200, json={"id": "m", "model": body.get("model"), "content": [{"type": "text", "text": "ok"}],
                                         "usage": USAGE})


def user(text):
    return {"role": "user", "content": [{"type": "text", "text": "<system-reminder>ignore me</system-reminder>"},
                                        {"type": "text", "text": text}]}


def tool_result():
    return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "file contents"}]}


def assistant():
    return {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]}


def request(messages, model="claude-opus-5-5", session="s1", system=MAIN_SYSTEM, tools=TOOLS, **extra):
    body = {"model": model, "max_tokens": 1000, "messages": messages, "system": system,
            "metadata": {"user_id": "user_x_session_" + session}, **extra}
    if tools is not None:
        body["tools"] = tools
    return body


def make(**overrides):
    tmp = tempfile.mkdtemp()
    config = load_config(None, db_path=os.path.join(tmp, "usage.sqlite"), **overrides)
    upstream = Upstream()
    classifier = FakeClassifier()
    tracker = Tracker(config.db_path)
    app = create_app(config, classifier=classifier, transport=httpx.MockTransport(upstream), tracker=tracker)
    return app, upstream, classifier, tracker


def test_classifier_off_keeps_models_and_disables_classify():
    tmp = tempfile.mkdtemp()
    config = load_config(None, db_path=os.path.join(tmp, "usage.sqlite"), classifier=False,
                         classifier_model="models/does-not-exist")
    up = Upstream()
    app = create_app(config, transport=httpx.MockTransport(up), tracker=Tracker(config.db_path))
    with TestClient(app) as c:
        r = post(c, request([user("Refactor the whole architecture of the auth layer")]))
        assert c.post("/__adtok/classify", json={"text": "Fix the typo"}).status_code == 503
        assert c.get("/__adtok/health").status_code == 200
    assert r.status_code == 200 and up.requests[-1]["body"]["model"] == "claude-opus-5-5"
    assert r.headers["x-adtok-source"] == "classifier_off" and r.headers["x-adtok"] == "-"


def post(client, body, headers=None):
    return client.post("/v1/messages", json=body, headers={"x-api-key": "test", "anthropic-version": "2023-06-01",
                                                           **(headers or {})})


def test_simple_prompt_routes_to_haiku_with_low_effort():
    app, up, clf, tracker = make()
    with TestClient(app) as c:
        r = post(c, request([user("Fix the typo in the README file please")]))
    assert r.status_code == 200, r.text
    sent = up.requests[-1]["body"]
    assert sent["model"] == "claude-haiku-5-5", sent["model"]
    assert sent["output_config"]["effort"] == "low"
    assert r.headers["x-adtok"] == "simple" and r.headers["x-adtok-role"] == "main"
    assert clf.calls == ["Fix the typo in the README file please"], clf.calls
    row = tracker.recent(1)[0]
    assert row["applied"] == 1 and row["sent_model"] == "claude-haiku-5-5"
    assert abs(row["cost_usd"] - (1000 * 0.10 + 200 * 0.50) / 1e6) < 1e-12
    assert abs(row["counterfactual_usd"] - (1000 * 4.0 + 200 * 20.0) / 1e6) < 1e-12


def test_main_thread_stays_locked_for_the_session():
    app, up, clf, _ = make()
    first = [user("Fix the typo in the README file please")]
    with TestClient(app) as c:
        post(c, request(first))
        post(c, request(first + [assistant(), tool_result()]))
        later = first + [assistant(), tool_result(), {"role": "assistant", "content": "done"},
                         user("Now refactor the whole architecture into services")]
        post(c, request(later))
    models = [q["body"]["model"] for q in up.requests]
    assert models == ["claude-haiku-5-5"] * 3, models
    assert len(clf.calls) == 1, clf.calls


def test_compaction_keeps_main_thread_identity():
    app, up, clf, _ = make()
    with TestClient(app) as c:
        post(c, request([user("Please refactor the payment architecture end to end")]))
        post(c, request([user("Summary of the conversation so far: we refactored payments")]))
    assert [q["body"]["model"] for q in up.requests] == ["claude-opus-5-5", "claude-opus-5-5"]
    assert len(clf.calls) == 1


def test_main_thread_survives_tool_list_change():
    app, up, clf, _ = make()
    first = [user("Please refactor the payment architecture end to end")]
    more_tools = TOOLS + [{"name": "WebSearch", "input_schema": {"type": "object"}}]
    with TestClient(app) as c:
        post(c, request(first))
        r = post(c, request(first + [assistant(), tool_result()], tools=more_tools))
        post(c, request([user("Summary of the conversation so far: payments refactor")], tools=more_tools))
    assert r.headers["x-adtok-role"] == "main" and r.headers["x-adtok-source"] == "locked"
    assert [q["body"]["model"] for q in up.requests] == ["claude-opus-5-5"] * 3
    assert len(clf.calls) == 1


def test_short_first_prompt_does_not_lock():
    app, up, clf, _ = make()
    with TestClient(app) as c:
        r1 = post(c, request([user("hi")]))
        post(c, request([user("hi"), {"role": "assistant", "content": "hello"},
                         user("Add a new endpoint for listing invoices with pagination")]))
    assert r1.headers["x-adtok-source"] == "pre_lock"
    assert [q["body"]["model"] for q in up.requests] == ["claude-opus-5-5", "claude-sonnet-5-5"]
    assert up.requests[1]["body"]["output_config"]["effort"] == "medium"


def test_manual_model_switch_is_respected():
    app, up, _, tracker = make()
    first = [user("Fix the typo in the README file please")]
    with TestClient(app) as c:
        post(c, request(first))
        r = post(c, request(first + [assistant(), tool_result()], model="claude-fable-5-2"))
    assert up.requests[-1]["body"]["model"] == "claude-fable-5-2"
    assert r.headers["x-adtok-source"] == "manual_switch"


def test_branches_are_routed_independently():
    app, up, clf, _ = make()
    with TestClient(app) as c:
        post(c, request([user("Please refactor the payment architecture end to end")]))
        r = post(c, request([user("Find which file defines the parse_date helper")],
                            system=[{"type": "text", "text": "You are a file search subagent."}],
                            tools=[{"name": "Grep", "input_schema": {"type": "object"}}]))
        post(c, request([user("Please refactor the payment architecture end to end"), assistant(), tool_result()]))
    assert [q["body"]["model"] for q in up.requests] == ["claude-opus-5-5", "claude-haiku-5-5", "claude-opus-5-5"]
    assert r.headers["x-adtok-role"] == "branch"


def test_small_toolless_side_calls_go_to_utility_tier():
    app, up, clf, tracker = make()
    with TestClient(app) as c:
        post(c, request([user("Please refactor the payment architecture end to end")]))
        r = post(c, request([user("<session> Please refactor the payment architecture end to end </session> "
                                  "Write a 5-word title.")], system="Generate a title.", tools=None))
        big = "x" * 30000
        post(c, request([user("Summarise this conversation and refactor notes: " + big)], system="Summarise.",
                        tools=None))
    assert [q["body"]["model"] for q in up.requests] == ["claude-opus-5-5", "claude-haiku-5-5", "claude-opus-5-5"]
    assert r.headers["x-adtok-role"] == "utility" and r.headers["x-adtok-source"] == "utility"
    assert len(clf.calls) == 2


def hinted(cls, agent_type=None, agent_id=None, session="S1"):
    h = {"x-claude-code-session-id": session, "x-claude-code-request-class": cls}
    if agent_type:
        h["x-claude-code-agent-type"] = agent_type
    if agent_id:
        h["x-claude-code-agent-id"] = agent_id
    return h


def test_hint_headers_classify_traffic_without_guessing():
    app, up, clf, tracker = make()
    sub_system = [{"type": "text", "text": "You are a search agent."}]
    with TestClient(app) as c:
        post(c, request([user("Please refactor the payment architecture end to end")]), headers=hinted("main"))
        post(c, request([user("Write a short title for this session")], tools=None), headers=hinted("auxiliary"))
        post(c, request([user("Find which file defines the parse_date helper")], system=sub_system),
             headers=hinted("subagent", "general-purpose", "a1"))
        post(c, request([user("Please refactor the payment architecture end to end")]),
             headers=hinted("subagent", "fork", "f1"))
        post(c, request([user("Summarise the conversation so far for compaction")]), headers=hinted("compaction"))
        post(c, request([user("Please refactor the payment architecture end to end"), assistant(), tool_result()],
                        tools=TOOLS + [{"name": "WebSearch", "input_schema": {"type": "object"}}]),
             headers=hinted("main"))
    models = [q["body"]["model"] for q in up.requests]
    assert models == ["claude-opus-5-5", "claude-haiku-5-5", "claude-opus-5-5", "claude-opus-5-5",
                      "claude-opus-5-5", "claude-opus-5-5"], models
    rows = list(reversed(tracker.recent(6)))
    assert [r["role"] for r in rows] == ["main", "utility", "branch", "fork", "compaction", "main"], rows
    assert [r["source"] for r in rows][1:5] == ["auxiliary", "native_subagent", "fork", "compaction"]
    assert rows[5]["source"] == "locked"
    assert len({r["session"] for r in rows}) == 1


def test_main_routing_off_observes_tier_but_keeps_model():
    app, up, clf, tracker = make(main_routing=False)
    with TestClient(app) as c:
        post(c, request([user("Fix the typo in the README file please")]), headers=hinted("main"))
    assert up.requests[-1]["body"]["model"] == "claude-opus-5-5"
    row = tracker.recent(1)[0]
    assert row["tier"] == "simple" and row["reason"] == "main_routing_off" and row["applied"] == 0


def test_subagents_can_be_routed_when_native_choice_is_not_respected():
    app, up, clf, tracker = make(respect_native_subagents=False)
    with TestClient(app) as c:
        post(c, request([user("Find which file defines the parse_date helper")],
                        system=[{"type": "text", "text": "search agent"}]), headers=hinted("subagent", "Explore", "a9"))
        post(c, request([user("Find which file defines the parse_date helper"), assistant(), tool_result()],
                        system=[{"type": "text", "text": "search agent"}]), headers=hinted("subagent", "Explore", "a9"))
    assert [q["body"]["model"] for q in up.requests] == ["claude-haiku-5-5", "claude-haiku-5-5"]
    assert tracker.recent(1)[0]["source"] == "locked" and tracker.recent(1)[0]["thread"] == "agent:a9"


def test_haiku_requests_are_never_upgraded():
    app, up, _, tracker = make()
    with TestClient(app) as c:
        post(c, request([user("Please refactor the payment architecture end to end")]))
        post(c, request([user("Generate a short title for: refactor the payment architecture")],
                        model="claude-haiku-5-5", system="Write a title.", tools=None))
        post(c, request([user("Explore the codebase and refactor the architecture of the billing module")],
                        model="claude-haiku-5-5", system="You are an explore subagent.",
                        tools=[{"name": "Grep", "input_schema": {"type": "object"}}]))
    assert [q["body"]["model"] for q in up.requests[1:]] == ["claude-haiku-5-5", "claude-haiku-5-5"]
    assert tracker.recent(1)[0]["reason"] == "never_upgrade"


def test_complex_upgrades_sonnet_to_opus_keeping_effort():
    app, up, _, _ = make()
    with TestClient(app) as c:
        post(c, request([user("Please refactor the payment architecture end to end")], model="claude-sonnet-5-5",
                        output_config={"effort": "xhigh"}))
    sent = up.requests[-1]["body"]
    assert sent["model"] == "claude-opus-5-5" and sent["output_config"] == {"effort": "xhigh"}


def test_streaming_passes_bytes_and_tracks_usage():
    app, up, _, tracker = make()
    with TestClient(app) as c:
        with c.stream("POST", "/v1/messages", json=request([user("Fix the typo in the README file please")], stream=True),
                      headers={"x-api-key": "t"}) as r:
            data = b"".join(r.iter_raw())
    assert r.status_code == 200
    assert b"message_start" in data and b"text_delta" in data
    row = tracker.recent(1)[0]
    assert row["stream"] == 1 and row["input_tokens"] == 1000 and row["output_tokens"] == 200, row


def test_rejected_rewrite_falls_back_to_original():
    app, up, _, tracker = make()
    up.reject_models.add("claude-haiku-5-5")
    with TestClient(app) as c:
        r = post(c, request([user("Fix the typo in the README file please")]))
    assert r.status_code == 200
    assert [q["body"]["model"] for q in up.requests] == ["claude-haiku-5-5", "claude-opus-5-5"]
    row = tracker.recent(1)[0]
    assert row["retried"] == 1 and row["applied"] == 0 and row["sent_model"] == "claude-opus-5-5"


def test_shadow_mode_forwards_original_body():
    app, up, _, tracker = make(shadow=True)
    body = request([user("Fix the typo in the README file please")])
    with TestClient(app) as c:
        post(c, body)
    assert up.requests[-1]["body"] == body
    row = tracker.recent(1)[0]
    assert row["shadow"] == 1 and row["target_model"] == "claude-haiku-5-5" and row["sent_model"] == "claude-opus-5-5"
    assert row["counterfactual_usd"] < row["cost_usd"]


def test_budget_block_returns_429():
    app, up, _, _ = make()
    app.state.config.budget.daily_usd = 0.000001
    app.state.config.budget.action = "block"
    with TestClient(app) as c:
        assert post(c, request([user("Fix the typo in the README file please")])).status_code == 200
        r = post(c, request([user("Fix the typo in the README file please")]))
    assert r.status_code == 429 and "budget" in r.json()["error"]["message"]
    assert len(up.requests) == 1


def test_budget_downgrade_forces_simple():
    app, up, _, _ = make()
    app.state.config.budget.daily_usd = 0.000001
    app.state.config.budget.action = "downgrade"
    with TestClient(app) as c:
        post(c, request([user("Please refactor the payment architecture end to end")], session="a"))
        post(c, request([user("Please refactor the billing architecture end to end")], session="b"))
    assert [q["body"]["model"] for q in up.requests] == ["claude-opus-5-5", "claude-haiku-5-5"]


def test_forced_tool_choice_blocks_routing():
    app, up, _, tracker = make()
    with TestClient(app) as c:
        post(c, request([user("Add a new endpoint for listing invoices with pagination")], model="claude-fable-5",
                        tool_choice={"type": "tool", "name": "Read"}))
    assert up.requests[-1]["body"]["model"] == "claude-fable-5"
    assert tracker.recent(1)[0]["reason"] == "forced_tool_choice"


def test_context_editing_adds_beta_header():
    app, up, _, _ = make(context_editing_tiers=["simple"])
    with TestClient(app) as c:
        post(c, request([user("Fix the typo in the README file please")]), headers={"anthropic-beta": "other-beta"})
    sent = up.requests[-1]
    assert sent["body"]["context_management"] == {"edits": [{"type": "clear_tool_uses_20250919"}]}
    assert sent["headers"]["anthropic-beta"] == "other-beta,context-management-2025-06-27"


def test_other_paths_pass_through():
    app, up, _, tracker = make()
    with TestClient(app) as c:
        r = c.get("/v1/models", headers={"x-api-key": "t"})
    assert r.status_code == 200 and up.requests[-1]["path"] == "/v1/models"
    assert tracker.recent(1) == []


def test_thinking_is_adapted_per_target():
    body = {"thinking": {"type": "disabled"}}
    adapt_thinking(body, "claude-sonnet-5-5", "medium")
    assert body["thinking"] == {"type": "between_tools"}
    body = {"thinking": {"type": "disabled"}}
    adapt_thinking(body, "claude-opus-5-5", "low")
    assert "thinking" not in body
    body = {"thinking": {"type": "between_tools"}}
    adapt_thinking(body, "claude-haiku-5-5", "low")
    assert body["thinking"] == {"type": "disabled"}
    body = {"thinking": {"type": "enabled", "budget_tokens": 4000, "display": "summarized"}}
    adapt_thinking(body, "claude-haiku-5-5", "low")
    assert body["thinking"] == {"type": "adaptive", "display": "summarized"}
    body = {"thinking": {"type": "disabled"}}
    adapt_thinking(body, "claude-haiku-5-5", "xhigh")
    assert "thinking" not in body


def test_pricing_and_sse_parser():
    p = Pricing()
    assert p.lookup("claude-opus-5-5")["input"] == 4.0 and p.lookup("claude-opus-5")["input"] == 5.0
    assert p.lookup("claude-opus-5-5[1m]")["input"] == 4.0 and p.lookup("gpt-x") is None
    long = p.cost("claude-haiku-5-5", {"input_tokens": 150000, "output_tokens": 1000})
    assert abs(long - (150000 * 0.50 + 1000 * 2.50) / 1e6) < 1e-12
    cached = p.cost("claude-opus-5-5", {"input_tokens": 10, "cache_creation_input_tokens": 100,
                                        "cache_read_input_tokens": 1000, "output_tokens": 0})
    assert abs(cached - (10 * 4.0 + 100 * 5.0 + 1000 * 0.20) / 1e6) < 1e-12
    hour = p.cost("claude-opus-5-5", {"input_tokens": 2, "cache_creation_input_tokens": 39510, "output_tokens": 4,
                                      "cache_creation": {"ephemeral_1h_input_tokens": 39510,
                                                         "ephemeral_5m_input_tokens": 0}})
    assert abs(hour - (2 * 4.0 + 39510 * 8.0 + 4 * 20.0) / 1e6) < 1e-12, hour
    parser = SSEUsage()
    stream = (b'event: message_start\r\ndata: {"type":"message_start","message":{"usage":{"input_tokens":7,'
              b'"output_tokens":1}}}\r\n\r\nevent: message_delta\r\ndata: {"type":"message_delta","usage":'
              b'{"output_tokens":42}}\r\n\r\n')
    for i in range(0, len(stream), 5):
        parser.feed(stream[i:i + 5])
    assert parser.usage == {"input_tokens": 7, "output_tokens": 42}, parser.usage


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print("PASS", name)
        except Exception:
            failed += 1
            print("FAIL", name)
            traceback.print_exc()
    print("%d/%d passed" % (len(tests) - failed, len(tests)))
    sys.exit(1 if failed else 0)
