"""Offline tests for the bridge: authorisation, path handling, limits, recording switch, goodbye logic."""
import asyncio
import json
import os
import time

import pytest

import server
from conftest import CALL_ID, SECRET, FakeRequest, FakeTwilioSocket, open_stream, stream_path

START = {"event": "start", "start": {"streamSid": "MZtest"}}


@pytest.fixture
def settings(tmp_path):
    calls = tmp_path / "calls"
    calls.mkdir()
    return server.Settings(calls_dir=calls, shared_secret=SECRET, max_call_seconds=5, start_timeout=1)


def prepare_call(settings, call_id=CALL_ID, config=None):
    call_dir = settings.calls_dir / call_id
    call_dir.mkdir()
    (call_dir / "prompt.txt").write_text("You are a test agent.", encoding="utf-8")
    (call_dir / "config.json").write_text(json.dumps(config or {"voice": "Kore"}), encoding="utf-8")
    return call_dir


# --------------------------------------------------------------------------- path traversal

@pytest.mark.parametrize("bad_id", [
    "..", "../etc", "../../etc/passwd", "/etc/passwd", "..\\windows", "calls/../x",
    CALL_ID + "/../x", CALL_ID + "/", CALL_ID.upper(), "20261019_120000", "", None, 123, ["x"],
])
def test_resolve_call_dir_rejects_bad_ids(settings, bad_id):
    with pytest.raises(server.CallRejected):
        server.resolve_call_dir(settings.calls_dir, bad_id)


def test_resolve_call_dir_stays_inside_calls_dir(settings):
    path = server.resolve_call_dir(settings.calls_dir, CALL_ID)
    assert path.parent == settings.calls_dir.resolve()


def test_traversal_does_not_reach_outside_prompt(settings):
    outside = settings.calls_dir.parent / "outside"
    outside.mkdir()
    (outside / "prompt.txt").write_text("stolen", encoding="utf-8")
    (outside / "config.json").write_text("{}", encoding="utf-8")
    expires = int(time.time()) + 600
    token = server.call_token_signature(SECRET, "../outside", expires)
    with pytest.raises(server.CallRejected):
        server.load_call(settings, f"/media/../outside/{expires}/{token}", now=time.time())
    with pytest.raises(server.CallRejected):
        server.load_call(settings, stream_path(call_id="..%2Foutside"), now=time.time())


# --------------------------------------------------------------------------- tokens

def test_valid_token_loads_call(settings):
    prepare_call(settings, config={"voice": "Kore", "record": False})
    spec = server.load_call(settings, stream_path(), now=time.time())
    assert spec.call_id == CALL_ID and spec.prompt == "You are a test agent."


def other_expiry_same_token():
    expires = int(time.time()) + 600
    return stream_path(expires=expires + 1, token=server.call_token_signature(SECRET, CALL_ID, expires))


@pytest.mark.parametrize("path, reason", [
    (lambda: stream_path(token="0" * 64), "bad token signature"),
    (other_expiry_same_token, "bad token signature"),
    (lambda: stream_path(expires="soon", token="0" * 64), None),
    (lambda: stream_path().rsplit("/", 1)[0], None),
    (lambda: stream_path() + "/extra", None),
    (lambda: "/media/" + CALL_ID, None),
], ids=["zero-token", "other-expiry", "expiry-not-a-number", "no-token", "extra-segment", "id-only"])
def test_invalid_token_rejected(settings, path, reason):
    prepare_call(settings)
    with pytest.raises(server.CallRejected, match=reason):
        server.load_call(settings, path(), now=time.time())


def test_expired_token_rejected(settings):
    prepare_call(settings)
    with pytest.raises(server.CallRejected, match="expired"):
        server.load_call(settings, stream_path(ttl=-1), now=time.time())


def test_overlong_token_rejected(settings):
    prepare_call(settings)
    with pytest.raises(server.CallRejected, match="too long"):
        server.load_call(settings, stream_path(ttl=settings.max_token_ttl + 60), now=time.time())


def test_token_for_other_call_rejected(settings):
    other = "20261019_120000_ffffffffffff"
    prepare_call(settings)
    prepare_call(settings, call_id=other)
    _, _, _, expires, token = stream_path(call_id=other).split("/")
    with pytest.raises(server.CallRejected, match="bad token signature"):
        server.load_call(settings, f"/media/{CALL_ID}/{expires}/{token}", now=time.time())


def test_token_with_wrong_secret_rejected(settings):
    prepare_call(settings)
    with pytest.raises(server.CallRejected):
        server.load_call(settings, stream_path(secret="another-secret-" + "y" * 32), now=time.time())


# --------------------------------------------------------------------------- unknown / replayed calls

def test_unknown_call_rejected(settings):
    with pytest.raises(server.CallRejected, match="unknown call"):
        server.load_call(settings, stream_path(), now=time.time())


def test_call_without_config_rejected(settings):
    call_dir = prepare_call(settings)
    (call_dir / "config.json").unlink()
    with pytest.raises(server.CallRejected, match="config.json"):
        server.load_call(settings, stream_path(), now=time.time())


def test_replayed_call_rejected(settings):
    prepare_call(settings)
    path = stream_path()
    server.load_call(settings, path, now=time.time())
    with pytest.raises(server.CallRejected, match="already used"):
        server.load_call(settings, path, now=time.time())


def test_settings_require_strong_secret():
    with pytest.raises(SystemExit):
        server.Settings.from_env({"AICALL_SHARED_SECRET": "short"})
    assert server.Settings.from_env({"AICALL_SHARED_SECRET": SECRET, "GEMINI_API_KEY": "k"}).max_message_bytes > 0


# --------------------------------------------------------------------------- authentication before the upgrade

def run_stream(settings, incoming, path=None, runner=None, headers=None):
    """One stream through the HTTP stage and, if it is accepted, the websocket handler."""
    calls = []

    async def fake_runner(ws, session):
        calls.append(session)
        session.state.on_agent_audio()
        session.record_callee(b"\x01\x00" * 800)
        session.record_agent(b"\x02\x00" * 2400, 24000)
        session.add_turn("agent", "Hello, this is a test.")

    bridge = server.Bridge(settings, runners={"gemini": runner or fake_runner, "openai": runner or fake_runner})
    ws = FakeTwilioSocket(incoming)
    response = asyncio.run(open_stream(bridge, ws, path or stream_path(), headers))
    return response, ws, calls


@pytest.mark.parametrize("path", [
    stream_path(call_id="../../etc"), "/media/../../etc/1/2", stream_path(token="f" * 64),
    stream_path(secret="another-secret-" + "y" * 32, ttl=-5), "/media/x/y/z", "/media/",
], ids=["traversal-id", "traversal-path", "bad-token", "expired-and-unsigned", "garbage", "empty"])
def test_unauthenticated_stream_refused_before_upgrade(settings, path):
    """A bad path never becomes a websocket: HTTP 403, no handler, no model, nothing written."""
    prepare_call(settings)
    response, ws, calls = run_stream(settings, [START], path=path)
    assert response[0] == 403 and ws.close_code is None and calls == []
    assert sorted(p.name for p in (settings.calls_dir / CALL_ID).iterdir()) == ["config.json", "prompt.txt"]


def test_authenticated_stream_is_served(settings):
    prepare_call(settings)
    response, ws, calls = run_stream(settings, [{"event": "connected"}, START])
    assert response is None and [s.spec.call_id for s in calls] == [CALL_ID] and ws.close_code == 1000


def test_replayed_stream_refused_before_upgrade(settings):
    prepare_call(settings)
    path = stream_path()
    first, _, _ = run_stream(settings, [START], path=path)
    second, ws, calls = run_stream(settings, [START], path=path)
    assert first is None and second[0] == 403 and calls == [] and ws.close_code is None


def test_handler_rejects_non_start_first_event(settings):
    prepare_call(settings)
    response, ws, calls = run_stream(settings, [{"event": "media", "media": {"payload": ""}}])
    assert response is None and ws.close_code == 1008 and calls == []


def test_handler_rejects_when_at_capacity(settings):
    prepare_call(settings)
    bridge = server.Bridge(settings, runners={})
    bridge.active_calls = settings.max_concurrent_calls
    ws = FakeTwilioSocket([START])
    asyncio.run(open_stream(bridge, ws, stream_path()))
    assert ws.close_code == 1013


def test_authenticated_socket_without_start_times_out(settings):
    prepare_call(settings)
    quick = server.Settings(calls_dir=settings.calls_dir, shared_secret=SECRET, start_timeout=0.1)
    bridge = server.Bridge(quick, runners={})
    ws = FakeTwilioSocket(hold_open=True)
    asyncio.run(asyncio.wait_for(open_stream(bridge, ws, stream_path()), 2))
    assert ws.close_code == 1008 and bridge.active_calls == 0


def test_call_timeout_cancels_and_awaits_child_tasks(settings):
    """The real supervisor (run_until_first_done) is cancelled by the duration limit mid-call."""
    prepare_call(settings)
    settings = server.Settings(calls_dir=settings.calls_dir, shared_secret=SECRET, max_call_seconds=0.2)
    cleaned = []

    async def child(name):
        try:
            await asyncio.sleep(10)
        finally:
            await asyncio.sleep(0)  # asynchronous cleanup must get to run
            cleaned.append(name)

    async def runner(ws, session):
        await server.run_until_first_done(session, child("twilio"), child("model"))

    async def scenario():
        bridge = server.Bridge(settings, runners={"gemini": runner, "openai": runner})
        await asyncio.wait_for(open_stream(bridge, FakeTwilioSocket([START]), stream_path()), 3)
        return [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]

    assert asyncio.run(scenario()) == []
    assert sorted(cleaned) == ["model", "twilio"]
    transcript = json.loads((settings.calls_dir / CALL_ID / "transcript.json").read_text(encoding="utf-8"))
    assert any("max call duration" in text for _, text in transcript["events"])


def test_supervisor_reports_child_error_and_cancels_sibling(settings):
    session = make_session(settings)
    cancelled = []

    async def failing():
        raise RuntimeError("provider went away")

    async def forever():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    asyncio.run(asyncio.wait_for(server.run_until_first_done(session, failing(), forever()), 2))
    assert cancelled == [True] and "provider went away" in session.events[-1][1]


def test_recording_off_writes_transcript_only(settings):
    call_dir = prepare_call(settings, config={"record": False})
    _, ws, calls = run_stream(settings, [START])
    assert len(calls) == 1 and calls[0].callee_audio is None and calls[0].agent_audio is None
    files = sorted(p.name for p in call_dir.iterdir())
    assert files == [".claimed", "config.json", "outcome.json", "prompt.txt", "transcript.json"]
    transcript = json.loads((call_dir / "transcript.json").read_text(encoding="utf-8"))
    assert transcript["recorded"] is False
    assert transcript["conv"][0][1:] == ["agent", "Hello, this is a test."]


def outcome(call_dir):
    return json.loads((call_dir / "outcome.json").read_text(encoding="utf-8"))


def test_outcome_ok_after_a_completed_exchange(settings):
    call_dir = prepare_call(settings)

    async def runner(ws, session):
        session.state.on_agent_audio()
        session.state.on_agent_turn_end()
        session.state.on_callee_words()
        session.state.on_agent_audio()
        session.ended("twilio stop")

    run_stream(settings, [START], runner=runner)
    result = outcome(call_dir)
    assert (result["status"], result["reason"]) == ("ok", "twilio stop")


def test_outcome_failed_on_provider_error(settings):
    call_dir = prepare_call(settings)

    async def runner(ws, session):
        session.state.on_agent_audio()
        raise server.ProviderError("openai rejected the session configuration: Invalid voice")

    run_stream(settings, [START], runner=runner)
    result = outcome(call_dir)
    assert result["status"] == "failed" and "Invalid voice" in result["reason"]
    if os.name == "posix":  # group = the upload account, which fetches it; nobody else
        assert (call_dir / "outcome.json").stat().st_mode & 0o777 == 0o640


def test_outcome_failed_when_model_never_spoke(settings):
    call_dir = prepare_call(settings)

    async def silent(ws, session):
        session.ended("twilio stop")

    run_stream(settings, [START], runner=silent)
    assert outcome(call_dir) == {**outcome(call_dir), "status": "failed", "reason": "the model produced no audio"}


def test_outcome_no_answer_when_callee_never_spoke(settings):
    call_dir = prepare_call(settings)
    run_stream(settings, [START])  # fake runner: agent audio only
    assert outcome(call_dir)["status"] == "no_answer"


def test_supervisor_task_error_marks_outcome_failed(settings):
    session = make_session(settings)
    session.state.on_agent_audio()

    async def failing():
        raise RuntimeError("provider went away")

    async def forever():
        await asyncio.sleep(10)

    asyncio.run(asyncio.wait_for(server.run_until_first_done(session, failing(), forever()), 2))
    assert session.outcome()["status"] == "failed" and "provider went away" in session.outcome()["reason"]


def test_recording_missing_flag_defaults_off(settings):
    call_dir = prepare_call(settings, config={"voice": "Kore"})
    run_stream(settings, [START])
    assert not list(call_dir.glob("*.wav"))


def test_recording_on_writes_private_wavs(settings):
    call_dir = prepare_call(settings, config={"record": True})
    run_stream(settings, [START])
    assert (call_dir / "callee_8k.wav").is_file() and (call_dir / "agent_24k.wav").is_file()
    if os.name == "posix":
        for name in ("transcript.json", "callee_8k.wav", "agent_24k.wav"):
            assert (call_dir / name).stat().st_mode & 0o777 == 0o640


@pytest.mark.skipif(not hasattr(os, "symlink") or os.name != "posix", reason="POSIX symlinks")
@pytest.mark.parametrize("name", ["prompt.txt", "config.json"])
def test_symlinked_call_file_is_not_followed(settings, name):
    """The upload account can create symlinks in calls/; the bridge can read files that account cannot,
    such as its own /proc/self/environ with every API key, so it must not follow them."""
    call_dir = prepare_call(settings)
    secret = settings.calls_dir.parent / "environ"
    secret.write_text('{"GEMINI_API_KEY": "secret"}', encoding="utf-8")
    (call_dir / name).unlink()
    os.symlink(secret, call_dir / name)
    response, _, calls = run_stream(settings, [START])
    assert response[0] == 403 and calls == [] and "secret" not in outcome(call_dir)["reason"]


def test_call_duration_limit(settings):
    prepare_call(settings)
    settings = server.Settings(calls_dir=settings.calls_dir, shared_secret=SECRET, max_call_seconds=0.2)

    async def slow_runner(ws, session):
        await asyncio.sleep(10)

    run_stream(settings, [START], runner=slow_runner)
    transcript = json.loads((settings.calls_dir / CALL_ID / "transcript.json").read_text(encoding="utf-8"))
    assert any("max call duration" in text for _, text in transcript["events"])


@pytest.mark.parametrize("model, cut_by_gemini_cap", [("gemini-3.1-flash-live-preview", True),
                                                       ("gpt-realtime-2.1", False)])
def test_gemini_calls_end_before_the_live_session_does(settings, monkeypatch, model, cut_by_gemini_cap):
    """Gemini Live sessions last about 10 minutes and the bridge does not resume them, so a Gemini call
    is capped below that even when AICALL_MAX_CALL_SECONDS allows more. OpenAI calls are not."""
    monkeypatch.setattr(server, "GEMINI_MAX_CALL_SECONDS", 0.2)
    call_dir = prepare_call(settings, config={"model": model})
    settings = server.Settings(calls_dir=settings.calls_dir, shared_secret=SECRET, max_call_seconds=1.0)

    async def slow_runner(ws, session):
        await asyncio.sleep(10)

    run_stream(settings, [START], runner=slow_runner)
    transcript = json.loads((call_dir / "transcript.json").read_text(encoding="utf-8"))
    assert "max call duration reached" in [text for _, text in transcript["events"]]
    assert (transcript["secs"] < 0.6) == cut_by_gemini_cap


# --------------------------------------------------------------------------- refusing an authenticated call

def refused_outcome(call_dir):
    """What the driver fetches: outcome.json and transcript.json must both exist."""
    assert (call_dir / "transcript.json").is_file()
    return outcome(call_dir)


@pytest.mark.parametrize("active, incoming, timeout, code", [
    (4, [START], 1, 1013),
    (0, [], 0.05, 1008),
    (0, [{"event": "media", "media": {"payload": "AAA="}}], 1, 1008),
], ids=["at-capacity", "no-start-event", "bad-first-event"])
def test_refused_authenticated_stream_records_a_failed_outcome(settings, active, incoming, timeout, code):
    call_dir = prepare_call(settings)
    quick = server.Settings(calls_dir=settings.calls_dir, shared_secret=SECRET, start_timeout=timeout)
    bridge = server.Bridge(quick, runners={})
    bridge.active_calls = active
    ws = FakeTwilioSocket(incoming, hold_open=not incoming)
    asyncio.run(asyncio.wait_for(open_stream(bridge, ws, stream_path()), 2))
    assert ws.close_code == code and refused_outcome(call_dir)["status"] == "failed"


@pytest.mark.parametrize("files, path", [
    ({"config.json": b"[1, 2]"}, lambda: stream_path()),
    ({"config.json": b"{not json"}, lambda: stream_path()),
    ({"prompt.txt": None}, lambda: stream_path()),
    ({}, lambda: stream_path(ttl=-30)),
    ({}, lambda: stream_path(ttl=7200)),
], ids=["config-not-object", "config-not-json", "prompt-missing", "expired", "lifetime-too-long"])
def test_claimed_call_that_cannot_start_records_a_failed_outcome(settings, files, path):
    call_dir = prepare_call(settings)
    for name, content in files.items():
        (call_dir / name).unlink() if content is None else (call_dir / name).write_bytes(content)
    response, _, calls = run_stream(settings, [START], path=path())
    assert response[0] == 403 and calls == [] and refused_outcome(call_dir)["status"] == "failed"


def test_replayed_stream_does_not_touch_the_first_calls_outcome(settings):
    call_dir = prepare_call(settings)
    path = stream_path()
    run_stream(settings, [START], path=path)
    first = (call_dir / "outcome.json").read_bytes()
    response, _, _ = run_stream(settings, [START], path=path)
    assert response[0] == 403 and (call_dir / "outcome.json").read_bytes() == first


def test_bridge_needs_a_gemini_key_at_startup():
    with pytest.raises(SystemExit, match="GEMINI_API_KEY"):
        server.Settings.from_env({"AICALL_SHARED_SECRET": SECRET})


def test_bridge_refuses_to_start_on_python_older_than_3_11(monkeypatch):
    monkeypatch.setattr(server.sys, "version_info", (3, 10, 14, "final", 0))
    with pytest.raises(SystemExit, match="3.11"):
        server.main()


# --------------------------------------------------------------------------- malformed input never raises

def no_tracebacks(caplog):
    return [r.getMessage() for r in caplog.records if r.exc_info]


def fullwidth_expiry_path():
    """Python's int() reads fullwidth digits, so this expiry has the right value with the right token."""
    expires = int(time.time()) + 600
    fullwidth = str(expires).translate({ord(d): chr(0xFF10 + int(d)) for d in "0123456789"})
    return stream_path(expires=fullwidth, token=server.call_token_signature(SECRET, CALL_ID, expires))


def mangle(part, how):
    """A stream path whose <part> segment is replaced by how(original)."""
    _, _, call_id, expires, token = stream_path().split("/")
    segments = {"call_id": call_id, "expires": expires, "token": token}
    segments[part] = how(segments[part])
    return "/media/{call_id}/{expires}/{token}".format(**segments)


MALFORMED_PATHS = {
    "non-ascii token": lambda: mangle("token", lambda t: "é" * 64),
    "surrogate-escaped token": lambda: mangle("token", lambda t: t[:-2] + "\udcc3\udca9"),
    "fullwidth-digit expiry": fullwidth_expiry_path,
    "infinite expiry": lambda: mangle("expires", lambda e: "Infinity"),
    "underscored expiry": lambda: mangle("expires", lambda e: e[:4] + "_" + e[4:]),
    "signed expiry": lambda: mangle("expires", lambda e: "+" + e),
    "padded expiry": lambda: mangle("expires", lambda e: " " + e),
    "huge expiry": lambda: mangle("expires", lambda e: "9" * 400),
    "uppercase token": lambda: mangle("token", str.upper),
    "short token": lambda: mangle("token", lambda t: t[:-1]),
    "long token": lambda: mangle("token", lambda t: t + "0"),
    "huge token": lambda: mangle("token", lambda t: t * 100),
    "query string": lambda: stream_path() + "?x=1",
    "trailing slash": lambda: stream_path() + "/",
    "double slash": lambda: stream_path().replace("/media/", "/media//"),
    "percent-encoded id": lambda: mangle("call_id", lambda c: c.replace("_", "%5F")),
    "nul byte": lambda: mangle("token", lambda t: t[:-1] + "\x00"),
}


@pytest.mark.parametrize("signed", [False, True], ids=["unsigned-bridge", "signature-checking-bridge"])
@pytest.mark.parametrize("make_path", MALFORMED_PATHS.values(), ids=MALFORMED_PATHS.keys())
def test_malformed_stream_path_is_refused_cleanly(settings, caplog, make_path, signed):
    prepare_call(settings)
    base = "wss://example.test/media"
    settings = server.Settings(calls_dir=settings.calls_dir, shared_secret=SECRET, verify_twilio_signature=signed,
                               twilio_auth_token="tok", public_wss_url=base)
    path = make_path()
    headers = {"X-Twilio-Signature": server.twilio_signature("tok", base + path[len("/media"):])} if \
        path.isascii() else {}
    response = server.Bridge(settings).process_request(FakeTwilioSocket(), FakeRequest(path, headers))
    assert response[0] == 403 and no_tracebacks(caplog) == []
    assert not (settings.calls_dir / CALL_ID / ".claimed").exists()


@pytest.mark.parametrize("headers", [
    [("X-Twilio-Signature", "é" * 28)],
    [("X-Twilio-Signature", "\udcc3\udca9")],
    [("X-Twilio-Signature", "a"), ("X-Twilio-Signature", "b")],
], ids=["non-ascii", "surrogate-escaped", "repeated"])
def test_malformed_twilio_signature_is_refused_cleanly(settings, caplog, headers):
    prepare_call(settings)
    signed = server.Settings(calls_dir=settings.calls_dir, shared_secret=SECRET, verify_twilio_signature=True,
                             twilio_auth_token="tok", public_wss_url="wss://example.test/media")
    response = server.Bridge(signed).process_request(FakeTwilioSocket(), FakeRequest(stream_path(), headers))
    assert response[0] == 403 and no_tracebacks(caplog) == []


@pytest.mark.parametrize("prompt, config", [
    (b"\xff\xfe not utf-8", b"{}"),
    (b"p", b"\xff not utf-8"),
    (b"p", b"[" * 100_000 + b"]" * 100_000),
    (b"p", b"[1, 2]"),
], ids=["prompt-not-utf8", "config-not-utf8", "config-deeply-nested", "config-not-an-object"])
def test_unreadable_call_files_are_refused_cleanly(settings, caplog, prompt, config):
    call_dir = prepare_call(settings)
    (call_dir / "prompt.txt").write_bytes(prompt)
    (call_dir / "config.json").write_bytes(config)
    response = server.Bridge(settings).process_request(FakeTwilioSocket(), FakeRequest(stream_path()))
    assert response[0] == 403 and no_tracebacks(caplog) == []


def closed_without_close_frame():
    from websockets.exceptions import ConnectionClosedError
    return ConnectionClosedError(None, None)


BAD_FIRST_MESSAGES = {
    "deeply nested json": lambda: "[" * 100_000 + "]" * 100_000,
    "json array": lambda: "[1, 2]",
    "start not an object": lambda: {"event": "start", "start": ["streamSid"]},
    "streamSid not a string": lambda: {"event": "start", "start": {"streamSid": {"sid": 1}}},
    "streamSid empty": lambda: {"event": "start", "start": {"streamSid": ""}},
    "streamSid mismatch": lambda: {"event": "start", "streamSid": "MZother", "start": {"streamSid": "MZtest"}},
    "huge event name": lambda: {"event": "x" * 60_000},
    "dropped without close frame": closed_without_close_frame,
}


@pytest.mark.parametrize("first", BAD_FIRST_MESSAGES.values(), ids=BAD_FIRST_MESSAGES.keys())
def test_bad_start_closes_with_1008(settings, caplog, first):
    prepare_call(settings)
    bridge = server.Bridge(settings, runners={})
    ws = FakeTwilioSocket([{"event": "connected"}, first()])
    asyncio.run(open_stream(bridge, ws, stream_path()))  # must not raise
    assert ws.close_code == 1008 and bridge.active_calls == 0 and no_tracebacks(caplog) == []


def test_start_with_matching_stream_sids_is_served(settings):
    prepare_call(settings)
    start = {"event": "start", "streamSid": "MZ" + "0" * 32, "start": {"streamSid": "MZ" + "0" * 32}}
    _, ws, calls = run_stream(settings, [start])
    assert len(calls) == 1 and calls[0].stream_sid == "MZ" + "0" * 32 and ws.close_code == 1000


BAD_CALL_MESSAGES = {
    "deeply nested json": "[" * 100_000 + "]" * 100_000,
    "media not an object": {"event": "media", "media": ["AAA="]},
    "payload not base64": {"event": "media", "media": {"payload": "not base64!"}},
    "payload not a string": {"event": "media", "media": {"payload": 42}},
    "mark not an object": {"event": "mark", "mark": "bye"},
}


@pytest.mark.parametrize("bad", BAD_CALL_MESSAGES.values(), ids=BAD_CALL_MESSAGES.keys())
def test_garbage_during_a_call_is_ignored(settings, caplog, bad):
    session = make_session(settings)
    received = []

    async def on_media(ulaw):
        received.append(ulaw)

    ws = FakeTwilioSocket([bad, {"event": "media", "media": {"payload": "AAA="}}, {"event": "stop"}])
    asyncio.run(server.pump_twilio(ws, session, on_media))
    assert received == [b"\x00\x00"] and session.events[-1][1] == "twilio stop" and no_tracebacks(caplog) == []


def test_twilio_dropping_mid_call_ends_the_call_cleanly(settings, caplog):
    call_dir = prepare_call(settings)

    async def runner(ws, session):
        async def on_media(ulaw):
            pass
        await server.pump_twilio(ws, session, on_media)

    _, ws, _ = run_stream(settings, [START, {"event": "media", "media": {"payload": "AAA="}},
                                     closed_without_close_frame()], runner=runner)
    events = [text for _, text in json.loads((call_dir / "transcript.json").read_text(encoding="utf-8"))["events"]]
    assert "twilio connection lost" in events and not any("error" in e for e in events)
    assert no_tracebacks(caplog) == []


# --------------------------------------------------------------------------- HTTP stage

def test_process_request_paths(settings):
    bridge = server.Bridge(settings)
    assert bridge.process_request(FakeTwilioSocket(), FakeRequest("/healthz"))[0] == 200
    assert bridge.process_request(FakeTwilioSocket(), FakeRequest("/admin"))[0] == 404
    assert bridge.process_request(FakeTwilioSocket(), FakeRequest("/media"))[0] == 404


def test_twilio_signature_covers_the_per_call_url(settings):
    """The signed URL carries the call's credentials, so a signature from one call is useless for another."""
    other = "20261019_120000_ffffffffffff"
    prepare_call(settings)
    prepare_call(settings, call_id=other)
    base = "wss://example.test/media"
    signed = server.Settings(calls_dir=settings.calls_dir, shared_secret=SECRET, verify_twilio_signature=True,
                             twilio_auth_token="tok", public_wss_url=base)
    bridge = server.Bridge(signed)
    path, other_path = stream_path(), stream_path(call_id=other)

    def signature(token, p):
        return {"X-Twilio-Signature": server.twilio_signature(token, base + p.removeprefix("/media"))}

    assert bridge.process_request(FakeTwilioSocket(), FakeRequest(other_path, signature("tok", path)))[0] == 403
    assert bridge.process_request(FakeTwilioSocket(), FakeRequest(path, {}))[0] == 403
    assert bridge.process_request(FakeTwilioSocket(), FakeRequest(path, signature("other", path)))[0] == 403
    assert not (settings.calls_dir / CALL_ID / ".claimed").exists()  # a refused signature claims nothing
    assert bridge.process_request(FakeTwilioSocket(), FakeRequest(path, signature("tok", path))) is None


# --------------------------------------------------------------------------- buffers

def test_bounded_buffer_caps_size():
    buf = server.BoundedBuffer(10)
    buf.extend(b"x" * 6)
    buf.extend(b"y" * 6)
    buf.extend(b"z")
    assert bytes(buf.data) == b"xxxxxxyyyy" and buf.truncated


def test_recording_buffers_are_bounded(settings):
    call_dir = prepare_call(settings, config={"record": True})
    spec = server.CallSpec(CALL_ID, call_dir, "p", {"record": True})
    session = server.CallSession(spec, "MZ", FakeTwilioSocket(), max_call_seconds=1, model="m")
    session.record_callee(b"\0" * 100_000)
    assert len(session.callee_audio.data) == 8000 * 2 and session.callee_audio.truncated


# --------------------------------------------------------------------------- goodbye logic

def make_session(settings, ws=None):
    call_dir = prepare_call(settings)
    spec = server.CallSpec(CALL_ID, call_dir, "p", {})
    return server.CallSession(spec, "MZ", ws or FakeTwilioSocket(), max_call_seconds=5, model="m")


def test_end_call_rejected_after_opening_line_only():
    state = server.ConversationState()
    state.on_agent_audio()  # opening line, nobody answered
    assert not state.end_call_allowed()


def test_end_call_allowed_after_callee_replied_and_agent_spoke():
    state = server.ConversationState()
    state.on_agent_audio()
    state.on_agent_turn_end()
    state.on_callee_words()
    assert not state.end_call_allowed()  # agent has not answered yet
    state.on_agent_audio()
    assert state.end_call_allowed()


def test_more_audio_in_the_opening_turn_is_not_an_answer():
    """Gemini-style: words transcribed while the opening line is still playing need a later turn."""
    state = server.ConversationState()
    state.on_agent_audio()
    state.on_callee_words()
    state.on_agent_audio()  # same (opening) turn
    assert not state.end_call_allowed()
    state.on_agent_turn_end()
    state.on_agent_audio()
    assert state.end_call_allowed()


def test_late_transcription_during_the_reply_turn_counts():
    """Gemini-style: the callee spoke, the agent's reply is already playing, then the transcription lands."""
    state = server.ConversationState()
    state.on_agent_audio()
    state.on_agent_turn_end()
    state.on_agent_audio()  # reply to speech whose transcription has not arrived yet
    assert not state.end_call_allowed()
    state.on_callee_words()
    assert state.end_call_allowed()


def test_late_transcription_after_the_reply_turn_matched_by_item():
    """OpenAI-style: speech_started marks where the callee began; its transcript arrives after the reply."""
    state = server.ConversationState()
    state.on_agent_audio()
    state.on_agent_turn_end()
    state.on_callee_turn_start("item_1")
    state.on_agent_audio()
    state.on_agent_turn_end()
    assert not state.end_call_allowed()
    state.on_callee_words("item_1")
    assert state.end_call_allowed()


def test_barge_in_then_words_need_a_new_agent_turn():
    state = server.ConversationState()
    state.on_agent_audio()
    state.on_agent_turn_end()
    state.on_agent_audio()  # turn 2 starts (e.g. after a rejected end_call)
    state.on_agent_turn_end()  # callee talks over it (speech_started ends the agent turn) ...
    state.on_callee_turn_start("item_2")  # ... so turn 2 cannot be the answer
    state.on_callee_words("item_2")
    assert not state.end_call_allowed()
    state.on_agent_audio()
    assert state.end_call_allowed()


def test_open_callee_turns_are_bounded():
    state = server.ConversationState()
    for i in range(100):
        state.on_callee_turn_start(i)  # noise that never got a transcript
    assert len(state.callee_turns) == state.MAX_OPEN_CALLEE_TURNS


def test_goodbye_logic_ignores_words():
    # The old guard matched "bye" anywhere; state now depends only on turn events, never on keywords.
    assert not hasattr(server, "GOODBYE_WORDS")
    state = server.ConversationState()
    state.on_callee_words()  # e.g. "Please do not say bye yet"
    assert not state.end_call_allowed()


def test_rejected_end_call_sends_no_mark(settings):
    ws = FakeTwilioSocket()
    session = make_session(settings, ws)
    session.state.on_agent_audio()
    assert asyncio.run(session.request_end_call()) is False
    assert ws.sent == []
    assert "end_call rejected" in session.events[-1][1]


def test_accepted_end_call_waits_for_goodbye_mark(settings):
    ws = FakeTwilioSocket()
    session = make_session(settings, ws)
    session.state.on_callee_words()
    session.state.on_agent_audio()

    async def scenario():
        task = asyncio.create_task(session.request_end_call())
        await asyncio.sleep(0.05)
        assert not task.done()  # still playing the goodbye
        session.mark_received.set()  # Twilio echoes the mark after playback (or after a clear)
        return await asyncio.wait_for(task, 2)

    assert asyncio.run(scenario()) is True
    assert ws.sent == [{"event": "mark", "mark": {"name": "bye"}, "streamSid": "MZ"}]


def test_interrupted_goodbye_still_hangs_up(settings):
    """Callee barges in during the goodbye: bridge clears playback, Twilio returns the mark at once."""
    ws = FakeTwilioSocket()
    session = make_session(settings, ws)
    session.state.on_callee_words()
    session.state.on_agent_audio()

    async def scenario():
        task = asyncio.create_task(session.request_end_call())
        await asyncio.sleep(0)
        await session.barge_in()
        session.mark_received.set()
        return await asyncio.wait_for(task, 2)

    assert asyncio.run(scenario()) is True
    assert [m["event"] for m in ws.sent] == ["mark", "clear"]


def test_goodbye_mark_timeout_is_bounded(settings, monkeypatch):
    monkeypatch.setattr(server, "MARK_TIMEOUT_S", 0.05)
    session = make_session(settings)
    session.state.on_callee_words()
    session.state.on_agent_audio()
    assert asyncio.run(asyncio.wait_for(session.request_end_call(), 3)) is True
    assert any("not acknowledged" in text for _, text in session.events)


def test_pump_twilio_handles_mark_stop_and_garbage(settings):
    session = make_session(settings)
    received = []

    async def on_media(ulaw):
        received.append(ulaw)

    ws = FakeTwilioSocket(["not json", "[1, 2]", {"event": "media", "media": {"payload": "AAA="}},
                           {"event": "media", "media": {}}, {"event": "mark", "mark": {"name": "bye"}},
                           {"event": "stop"}, {"event": "media", "media": {"payload": "late"}}])
    asyncio.run(server.pump_twilio(ws, session, on_media))
    assert received == [b"\x00\x00"] and session.mark_received.is_set()
    assert session.events[-1][1] == "twilio stop"


def test_merge_turns_joins_fragments():
    turns = [[0.1, "agent", "Hi, "], [0.2, "agent", "there."], [1.0, "callee", "Hello"]]
    assert server.merge_turns(turns) == [[0.1, "agent", "Hi, there."], [1.0, "callee", "Hello"]]


def test_merge_turns_separates_fragments_with_one_space():
    turns = [[0.1, "agent", "Great."], [0.5, "agent", "Bye!"], [1.0, "callee", "Bye"], [1.2, "callee", " then."]]
    assert server.merge_turns(turns) == [[0.1, "agent", "Great. Bye!"], [1.0, "callee", "Bye then."]]


# --------------------------------------------------------------------------- real websockets server on loopback

def test_real_server_wiring(settings):
    """Bridge hooks work with the installed websockets library (127.0.0.1 only, no external network)."""
    import urllib.error
    import urllib.request

    from websockets.asyncio.client import connect
    from websockets.asyncio.server import serve
    from websockets.exceptions import ConnectionClosed, InvalidStatus

    prepare_call(settings)
    bridge = server.Bridge(settings, runners={})

    async def scenario():
        async with serve(bridge.handler, "127.0.0.1", 0, process_request=bridge.process_request,
                         max_size=settings.max_message_bytes) as srv:
            port = srv.sockets[0].getsockname()[1]
            loop = asyncio.get_running_loop()
            health = await loop.run_in_executor(
                None, lambda: urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5).read())
            try:
                await loop.run_in_executor(
                    None, lambda: urllib.request.urlopen(f"http://127.0.0.1:{port}/other", timeout=5))
                other = None
            except urllib.error.HTTPError as e:
                other = e.code
            with pytest.raises(InvalidStatus) as refused:
                async with connect(f"ws://127.0.0.1:{port}" + stream_path(token="0" * 64)):
                    pass
            async with connect(f"ws://127.0.0.1:{port}" + stream_path()) as ws:
                await ws.send("x" * (settings.max_message_bytes + 1))
                with pytest.raises(ConnectionClosed) as too_big:
                    await asyncio.wait_for(ws.recv(), 5)
            return health, other, refused.value.response.status_code, too_big.value.rcvd.code

    health, other, refused_status, too_big_code = asyncio.run(scenario())
    assert health == b"ok\n" and other == 404
    assert refused_status == 403 and too_big_code == 1009


# --------------------------------------------------------------------------- artifact writes, drain, timeouts

@pytest.mark.parametrize("plant", ["file", "symlink"])
def test_write_private_never_writes_through_a_planted_tmp_name(tmp_path, plant):
    """The upload account can create files and symlinks in calls/<id>/: a planted "<name>.tmp" must survive."""
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me", encoding="utf-8")
    call_dir = tmp_path / "call"
    call_dir.mkdir()
    planted = call_dir / "outcome.json.tmp"
    if plant == "symlink":
        if not hasattr(os, "O_NOFOLLOW"):
            pytest.skip("POSIX symlinks")
        planted.symlink_to(victim)
    else:
        planted.write_text("keep me", encoding="utf-8")
        victim = planted
    server.write_private(call_dir / "outcome.json", b'{"status": "ok"}')
    assert victim.read_text(encoding="utf-8") == "keep me"
    assert (call_dir / "outcome.json").read_bytes() == b'{"status": "ok"}'
    assert sorted(p.name for p in call_dir.iterdir()) == ["outcome.json", "outcome.json.tmp"]


def test_write_private_leaves_no_tmp_file_when_the_replace_fails(tmp_path, monkeypatch):
    def broken(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(server.os, "replace", broken)
    with pytest.raises(OSError):
        server.write_private(tmp_path / "outcome.json", b"{}")
    assert list(tmp_path.iterdir()) == []


def test_late_model_transcription_after_twilio_stop_is_kept(settings, monkeypatch):
    """The callee says bye and hangs up: Twilio's stop arrives before the model's last transcription."""
    monkeypatch.setattr(server, "TWILIO_STOP_DRAIN_S", 1.0)
    session = make_session(settings)
    cancelled = []

    async def twilio():
        session.twilio_stopped = True
        session.ended("twilio stop")

    async def model():
        try:
            await asyncio.sleep(0.05)
            session.add_turn("callee", "Okay, bye!")
            await session.play(b"\xff" * 160)  # the model answers a hung-up call: dropped, not an error
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    asyncio.run(asyncio.wait_for(server.run_until_first_done(session, twilio(), model()), 3))
    assert [t for _, s, t in session.turns] == ["Okay, bye!"]
    assert cancelled == [True] and session.error is None


def test_drain_after_twilio_stop_is_bounded_and_cancels_the_model_side(settings, monkeypatch):
    monkeypatch.setattr(server, "TWILIO_STOP_DRAIN_S", 0.2)
    session = make_session(settings)
    cancelled = []

    async def twilio():
        return None

    async def model():
        try:
            await asyncio.sleep(10)
        finally:
            await asyncio.sleep(0)
            cancelled.append(True)

    started = time.monotonic()
    asyncio.run(asyncio.wait_for(server.run_until_first_done(session, twilio(), model()), 3))
    assert 0.15 < time.monotonic() - started < 1.5 and cancelled == [True]


def test_no_drain_when_the_model_side_finishes_first(settings, monkeypatch):
    monkeypatch.setattr(server, "TWILIO_STOP_DRAIN_S", 5.0)
    session = make_session(settings)

    async def twilio():
        await asyncio.sleep(10)

    async def model():
        return None

    started = time.monotonic()
    asyncio.run(asyncio.wait_for(server.run_until_first_done(session, twilio(), model()), 3))
    assert time.monotonic() - started < 1.0


def test_provider_connection_timeout_is_not_reported_as_max_duration(settings):
    call_dir = prepare_call(settings)

    async def runner(ws, session):
        raise TimeoutError("timed out during opening handshake")

    _, ws, _ = run_stream(settings, [START], runner=runner)
    result = outcome(call_dir)
    assert result["status"] == "failed" and "timed out during opening handshake" in result["reason"]
    events = [text for _, text in json.loads((call_dir / "transcript.json").read_text(encoding="utf-8"))["events"]]
    assert "max call duration reached" not in events


def test_barge_in_reports_what_the_callee_heard_once(settings):
    session = make_session(settings)
    session.note_agent_audio("item_1", b"\xff" * 8000)    # 1 s of audio sent at once
    session.audio_item_start -= 0.25                      # ... of which 250 ms have been played
    item, ms = session.played_audio()
    assert item == "item_1" and 240 <= ms <= 400
    assert session.played_audio() is None                 # one truncate per item

    session.note_agent_audio("item_2", b"\xff" * 160)     # 20 ms sent, long since played
    session.audio_item_start -= 5
    assert session.played_audio() == ("item_2", 20)
    session.note_agent_audio(None, b"\xff" * 160)         # no item id (e.g. Gemini): nothing to truncate
    assert session.played_audio() is None
