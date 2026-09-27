import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import shopping_optimizer as so


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(so, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(so, "WEBHOOK_SECRET", "geheim")
    monkeypatch.setattr(so, "_pending", None)
    monkeypatch.setattr(so, "CLAUDE_FALLBACKS", "default")
    monkeypatch.setattr(so, "CLAUDE_EFFORT", "low")


def claude_message(payload, stop_reason="end_turn"):
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=[
            SimpleNamespace(type="thinking", thinking=""),
            SimpleNamespace(type="text", text=json.dumps(payload)),
        ],
    )


def fake_client(message):
    client = MagicMock()
    client.beta.messages.create.return_value = message
    client.messages.create.return_value = message
    return client


# ── strip_prefix ──────────────────────────────────────────────────────────────

def test_strip_prefix_removes_known_prefixes_repeatedly():
    assert so.strip_prefix("[Groente & Fruit] [Groente & Fruit] 3 uien") == "3 uien"
    assert so.strip_prefix(so.strip_prefix("[Dranken] cola")) == "cola"


def test_strip_prefix_keeps_unknown_brackets():
    assert so.strip_prefix("[2x] melk") == "[2x] melk"


# ── optimize_with_claude ─────────────────────────────────────────────────────

def test_optimize_reads_text_block_after_thinking(monkeypatch):
    msg = claude_message({"items": [{"name": "3 uien", "category": "Groente & Fruit", "sources": [1, 2]}]})
    client = fake_client(msg)
    monkeypatch.setattr(so, "_client", client)

    result = so.optimize_with_claude(["ui", "2 uien"])

    assert result == [{"name": "3 uien", "category": "Groente & Fruit", "sources": [1, 2]}]
    kwargs = client.beta.messages.create.call_args.kwargs
    assert kwargs["fallbacks"] == "default"
    assert kwargs["betas"] == ["server-side-fallback-2026-07-01"]
    assert kwargs["output_config"]["effort"] == "low"
    assert kwargs["output_config"]["format"]["type"] == "json_schema"


def test_optimize_without_fallbacks_uses_plain_create(monkeypatch):
    msg = claude_message({"items": [{"name": "melk", "category": "Zuivel & Eieren", "sources": [1]}]})
    client = fake_client(msg)
    monkeypatch.setattr(so, "_client", client)
    monkeypatch.setattr(so, "CLAUDE_FALLBACKS", "")
    monkeypatch.setattr(so, "CLAUDE_EFFORT", "")

    so.optimize_with_claude(["melk"])

    client.beta.messages.create.assert_not_called()
    assert "effort" not in client.messages.create.call_args.kwargs["output_config"]


def test_optimize_raises_on_refusal(monkeypatch):
    msg = SimpleNamespace(stop_reason="refusal", content=[])
    monkeypatch.setattr(so, "_client", fake_client(msg))
    with pytest.raises(RuntimeError, match="weigerde"):
        so.optimize_with_claude(["melk"])


def test_optimize_raises_on_max_tokens(monkeypatch):
    msg = claude_message({"items": []}, stop_reason="max_tokens")
    monkeypatch.setattr(so, "_client", fake_client(msg))
    with pytest.raises(RuntimeError, match="afgekapt"):
        so.optimize_with_claude(["melk"])


# ── validate_result ──────────────────────────────────────────────────────────

def test_validate_rejects_missing_source():
    items = [{"name": "melk", "category": "Zuivel & Eieren", "sources": [1]}]
    with pytest.raises(ValueError, match="weg"):
        so.validate_result(items, 2)


def test_validate_rejects_out_of_range_and_empty_sources():
    with pytest.raises(ValueError, match="Ongeldig"):
        so.validate_result([{"name": "melk", "category": "Overig", "sources": [3]}], 1)
    with pytest.raises(ValueError, match="geen enkel"):
        so.validate_result([{"name": "melk", "category": "Overig", "sources": []}], 1)


def test_validate_rejects_unknown_category():
    with pytest.raises(ValueError, match="categorie"):
        so.validate_result([{"name": "melk", "category": "Zuivel", "sources": [1]}], 1)


def test_validate_allows_repeated_sources_and_strips_prefix():
    items = [
        {"name": "[Zuivel & Eieren] melk", "category": "Zuivel & Eieren", "sources": [1, 1]},
        {"name": "kaas", "category": "Zuivel & Eieren", "sources": [1, 2]},
    ]
    so.validate_result(items, 2)
    assert items[0]["name"] == "melk"


# ── HA-lijst ophalen ──────────────────────────────────────────────────────────

def test_get_shopping_list_parses_service_response(monkeypatch):
    response = {
        "changed_states": [],
        "service_response": {
            "todo.shopping_list": {"items": [
                {"uid": "a", "summary": "melk", "status": "needs_action"},
                {"uid": "b", "summary": "brood", "status": "completed"},
            ]},
        },
    }
    call = MagicMock(return_value=response)
    monkeypatch.setattr(so, "_call_service", call)

    assert so.get_shopping_list() == [{"uid": "a", "name": "melk"}]
    assert call.call_args.kwargs["return_response"] is True


# ── apply_plan ────────────────────────────────────────────────────────────────

PLAN = {
    "original": [{"uid": "u1", "name": "ui"}, {"uid": "u2", "name": "2 uien"}],
    "categories": {"Groente & Fruit": ["3 uien"]},
}


def test_apply_plan_adds_before_removing_by_uid(monkeypatch):
    calls = []
    monkeypatch.setattr(so, "get_shopping_list", lambda: [{"uid": "u1", "name": "ui"}, {"uid": "u2", "name": "2 uien"}])
    monkeypatch.setattr(so, "add_item", lambda name: calls.append(("add", name)))
    monkeypatch.setattr(so, "remove_items", lambda uids: calls.append(("remove", uids)))

    so.apply_plan(PLAN)

    assert calls == [("add", "[Groente & Fruit] 3 uien"), ("remove", ["u1", "u2"])]


def test_apply_plan_aborts_on_stale_list_before_any_add(monkeypatch):
    add = MagicMock()
    remove = MagicMock()
    monkeypatch.setattr(so, "get_shopping_list", lambda: [{"uid": "u1", "name": "ui"}])
    monkeypatch.setattr(so, "add_item", add)
    monkeypatch.setattr(so, "remove_items", remove)

    with pytest.raises(so.StaleListError):
        so.apply_plan(PLAN)
    add.assert_not_called()
    remove.assert_not_called()


def test_apply_plan_keeps_originals_when_add_fails(monkeypatch):
    plan = {"original": PLAN["original"], "categories": {"Groente & Fruit": ["3 uien", "appels"]}}
    remove = MagicMock()
    monkeypatch.setattr(so, "get_shopping_list", lambda: PLAN["original"])
    monkeypatch.setattr(so, "add_item", MagicMock(side_effect=[None, RuntimeError("HA weg")]))
    monkeypatch.setattr(so, "remove_items", remove)

    with pytest.raises(so.PartialApplyError, match=r"1 van 2.*\[Groente & Fruit\] 3 uien"):
        so.apply_plan(plan)
    remove.assert_not_called()


def test_confirm_after_partial_failure_clears_pending(monkeypatch):
    so._set_pending(PLAN)
    monkeypatch.setattr(so, "apply_plan", MagicMock(side_effect=so.PartialApplyError("half")))
    monkeypatch.setattr(so, "dismiss", MagicMock())

    code, body = so.run_confirm()

    assert code == 409 and body["message"] == "half"
    assert so._pending is None


# ── Pending + confirm ────────────────────────────────────────────────────────

def test_pending_survives_reload(monkeypatch):
    so._set_pending(PLAN)
    monkeypatch.setattr(so, "_pending", None)
    so._load_pending()
    assert so._pending == PLAN


def test_confirm_on_stale_list_clears_pending(monkeypatch):
    so._set_pending(PLAN)
    monkeypatch.setattr(so, "apply_plan", MagicMock(side_effect=so.StaleListError("gewijzigd")))
    monkeypatch.setattr(so, "dismiss", MagicMock())

    code, body = so.run_confirm()

    assert code == 409 and "gewijzigd" in body["message"]
    assert so._pending is None


def test_confirm_without_pending_is_409():
    code, _ = so.run_confirm()
    assert code == 409


# ── HTTP server ───────────────────────────────────────────────────────────────

@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), so.WebhookHandler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def post(url, secret="geheim"):
    req = urllib.request.Request(url, data=b"{}", method="POST", headers={"X-Webhook-Secret": secret})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_wrong_secret_is_401(server):
    assert post(f"{server}/preview", secret="fout")[0] == 401


def test_busy_returns_409(server):
    so._busy.acquire()
    try:
        assert post(f"{server}/preview")[0] == 409
        assert post(f"{server}/confirm")[0] == 409
    finally:
        so._busy.release()


def test_preview_runs_in_background_and_returns_202(server, monkeypatch):
    done = threading.Event()
    monkeypatch.setitem(so.WebhookHandler.BACKGROUND, "/preview", ("preview", done.set))

    code, body = post(f"{server}/preview")

    assert code == 202 and body["status"] == "accepted"
    assert done.wait(2)
