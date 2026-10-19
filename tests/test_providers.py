"""Offline tests for the provider adapters (run_gemini, run_openai) through the real Bridge.

The Gemini SDK client and the OpenAI websocket client are replaced by scripted fakes, so no
network connection is opened and nothing is billed.
"""
import asyncio
import base64
import contextlib
import json
import time
from types import SimpleNamespace

import pytest

import server
from conftest import CALL_ID, SECRET, FakeTwilioSocket, open_stream, stream_path

ULAW_FRAME = base64.b64encode(b"\xff" * 160).decode()  # 20 ms of mu-law silence from the callee
PCM24K_FRAME = b"\x00\x00" * 480  # 20 ms of PCM16 at 24 kHz from Gemini
IDLE = object()  # script marker: the provider goes quiet (but stays connected)
CLOSE = object()  # script marker: the provider closes the socket normally


@pytest.fixture
def settings(tmp_path):
    calls = tmp_path / "calls"
    calls.mkdir()
    return server.Settings(calls_dir=calls, shared_secret=SECRET, max_call_seconds=5, start_timeout=1)


@pytest.fixture(autouse=True)
def fast_goodbye(monkeypatch):
    monkeypatch.setattr(server, "MARK_TIMEOUT_S", 0.05)


def prepare_call(settings, config):
    call_dir = settings.calls_dir / CALL_ID
    call_dir.mkdir()
    (call_dir / "prompt.txt").write_text("You are a test agent.", encoding="utf-8")
    (call_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")
    return call_dir


def twilio_socket():
    start = {"event": "start", "start": {"streamSid": "MZtest"}}
    media = {"event": "media", "media": {"payload": ULAW_FRAME}}
    return FakeTwilioSocket([{"event": "connected"}, start, media, media], hold_open=True)


def run_call(settings, ws, timeout=5):
    """Run one call through Bridge.handler with the real provider runners; return leftover tasks."""
    async def scenario():
        bridge = server.Bridge(settings)
        assert await asyncio.wait_for(open_stream(bridge, ws, stream_path()), timeout) is None
        return [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]

    return asyncio.run(scenario())


def transcript(call_dir):
    return json.loads((call_dir / "transcript.json").read_text(encoding="utf-8"))


def event_texts(call_dir):
    return [text for _, text in transcript(call_dir)["events"]]


async def play_script(script):
    """Yield scripted items; numbers are delays; IDLE (or the end) blocks like a quiet live session."""
    for item in script:
        if item is CLOSE:
            return
        if item is IDLE:
            break
        if isinstance(item, (int, float)):
            await asyncio.sleep(item)
            continue
        yield item
    await asyncio.Event().wait()


# --------------------------------------------------------------------------- Gemini Live fakes

def gemini_response(data=None, heard=None, said=None, interrupted=False, turn_complete=False, end_call_id=None):
    content = None
    if heard or said or interrupted or turn_complete:
        content = SimpleNamespace(
            input_transcription=SimpleNamespace(text=heard) if heard else None,
            output_transcription=SimpleNamespace(text=said) if said else None,
            interrupted=interrupted, turn_complete=turn_complete)
    tool_call = None
    if end_call_id:
        tool_call = SimpleNamespace(function_calls=[SimpleNamespace(name="end_call", id=end_call_id)])
    return SimpleNamespace(data=data, server_content=content, tool_call=tool_call)


class FakeGeminiSession:
    def __init__(self, script):
        self.script = script
        self.texts, self.audio, self.tool_responses = [], [], []
        self._stream = None

    async def send_realtime_input(self, text=None, audio=None):
        if text is not None:
            self.texts.append(text)
        if audio is not None:
            self.audio.append(audio)

    async def send_tool_response(self, function_responses):
        self.tool_responses.extend(function_responses)

    def receive(self):
        if self._stream is None:
            self._stream = play_script(self.script)
        return self._stream


class FakeGeminiLive:
    def __init__(self, script=(), error=None):
        self.session = FakeGeminiSession(list(script))
        self.error = error
        self.connected_with = None
        self.closed = False

    @contextlib.asynccontextmanager
    async def connect(self, *, model, config):
        self.connected_with = (model, config)
        if self.error:
            raise self.error
        try:
            yield self.session
        finally:
            self.closed = True


@pytest.fixture
def gemini(monkeypatch):
    pytest.importorskip("google.genai")

    def install(**kwargs):
        live = FakeGeminiLive(**kwargs)
        monkeypatch.setattr(server, "gemini_client", lambda: SimpleNamespace(aio=SimpleNamespace(live=live)))
        return live

    return install


def test_gemini_config_rejection_ends_call_cleanly(settings, gemini):
    call_dir = prepare_call(settings, {"voice": "NoSuchVoice"})
    live = gemini(error=ValueError("invalid voice name"))
    ws = twilio_socket()
    assert run_call(settings, ws) == []
    assert live.connected_with[0] == server.DEFAULT_MODEL
    assert any("session error" in t and "invalid voice name" in t for t in event_texts(call_dir))
    assert ws.close_code == 1000 and not [m for m in ws.sent if m["event"] == "media"]


def test_gemini_conversation_with_interruption_delayed_transcript_and_end_call(settings, gemini):
    call_dir = prepare_call(settings, {"voice": "Kore", "record": True})
    live = gemini(script=[
        gemini_response(data=PCM24K_FRAME, said="Hi, this is an AI assistant."),
        gemini_response(end_call_id="fc1"),                    # too early: nobody has answered yet
        0.05,
        gemini_response(interrupted=True),                     # callee talks over the agent
        gemini_response(heard="Yes, speaking."),               # transcription arrives late
        gemini_response(data=PCM24K_FRAME, said="Great. Bye!"),
        gemini_response(end_call_id="fc2"),
    ])
    ws = twilio_socket()
    assert run_call(settings, ws) == []

    session = live.session
    assert session.texts == [server.DEFAULT_KICKOFF]
    assert session.audio and all(b.mime_type == "audio/pcm;rate=16000" for b in session.audio)
    (rejected,) = session.tool_responses
    assert rejected.id == "fc1" and rejected.response == {"result": server.END_CALL_REJECTED}
    events = [m["event"] for m in ws.sent]
    assert "media" in events and "clear" in events and events.index("clear") < events.index("mark")
    texts = event_texts(call_dir)
    assert "interrupted" in texts and "end_call" in texts
    assert any("end_call rejected" in t for t in texts)
    conv = transcript(call_dir)["conv"]
    assert [speaker for _, speaker, _ in conv] == ["agent", "callee", "agent"]
    assert conv[1][2] == "Yes, speaking."
    assert (call_dir / "callee_8k.wav").is_file() and (call_dir / "agent_24k.wav").is_file()
    assert live.closed and ws.close_code == 1000


def test_gemini_transcription_after_reply_audio_allows_end_call(settings, gemini):
    """The callee's words are transcribed only after the agent's reply has started playing."""
    call_dir = prepare_call(settings, {})
    live = gemini(script=[
        gemini_response(data=PCM24K_FRAME, said="Hi, may I speak with Alex?"),
        gemini_response(turn_complete=True),
        0.05,                                                  # callee answers (audio only, no text yet)
        gemini_response(data=PCM24K_FRAME, said="Great, thanks. Bye!"),
        gemini_response(heard="Yes, speaking."),               # late input transcription
        gemini_response(end_call_id="fc1"),
        gemini_response(turn_complete=True),
    ])
    ws = twilio_socket()
    assert run_call(settings, ws) == []
    assert live.session.tool_responses == []  # accepted at the first attempt
    texts = event_texts(call_dir)
    assert "end_call" in texts and not any("rejected" in t for t in texts)
    assert json.loads((call_dir / "outcome.json").read_text(encoding="utf-8"))["status"] == "ok"


def test_gemini_words_during_opening_turn_do_not_allow_end_call(settings, gemini):
    call_dir = prepare_call(settings, {})
    live = gemini(script=[
        gemini_response(data=PCM24K_FRAME, said="Hi, may I speak with Alex?"),
        gemini_response(heard="Hello?"),                       # heard while the opening line plays
        gemini_response(data=PCM24K_FRAME, said=" Hello?"),    # still the opening turn
        gemini_response(end_call_id="fc1"),
        IDLE,
    ])
    settings = server.Settings(calls_dir=settings.calls_dir, shared_secret=SECRET, max_call_seconds=0.3,
                               start_timeout=1)
    assert run_call(settings, twilio_socket()) == []
    (rejected,) = live.session.tool_responses
    assert rejected.response == {"result": server.END_CALL_REJECTED}
    assert any("end_call rejected" in t for t in event_texts(call_dir))


def test_gemini_cancelled_by_call_timeout(settings, gemini):
    settings = server.Settings(calls_dir=settings.calls_dir, shared_secret=SECRET, max_call_seconds=0.3,
                               start_timeout=1)
    call_dir = prepare_call(settings, {})
    live = gemini(script=[gemini_response(data=PCM24K_FRAME, said="Hello?"), IDLE])
    ws = twilio_socket()
    assert run_call(settings, ws) == []  # no pump task survives the timeout
    assert "max call duration reached" in event_texts(call_dir)
    assert live.closed and ws.close_code == 1000


# --------------------------------------------------------------------------- OpenAI Realtime fakes

ACTIVE_RESPONSE_ERROR = {"type": "error", "error": {
    "type": "invalid_request_error", "code": "conversation_already_has_active_response",
    "message": "Conversation already has an active response in progress"}}


class FakeOpenAISocket:
    """Scripted OpenAI Realtime socket that enforces the API's one-active-response rule: a response is
    active from response.create (or a scripted response.created) until its response.done, and a
    response.create sent meanwhile is refused with conversation_already_has_active_response."""

    def __init__(self, script):
        self.script = list(script)
        self.sent = []
        self.refused = []
        self.pending_errors = []
        self.active = False
        self.connected_with = None
        self.closed = False

    async def send(self, data):
        msg = json.loads(data)
        self.sent.append(msg)
        if msg["type"] == "response.create":
            if self.active:
                self.refused.append(msg)
                self.pending_errors.append(ACTIVE_RESPONSE_ERROR)
            self.active = True

    def __aiter__(self):
        async def messages():
            async for item in play_script(self.script):
                while self.pending_errors:
                    yield json.dumps(self.pending_errors.pop(0))
                if item["type"] in ("response.created", "response.done"):
                    self.active = item["type"] == "response.created"
                yield json.dumps(item)
        return messages()


@pytest.fixture
def openai(monkeypatch):
    from websockets.asyncio import client as ws_client
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    def install(script):
        sock = FakeOpenAISocket(script)

        @contextlib.asynccontextmanager
        async def fake_connect(url, additional_headers=None, max_size=None):
            sock.connected_with = (url, additional_headers, max_size)
            try:
                yield sock
            finally:
                sock.closed = True

        monkeypatch.setattr(ws_client, "connect", fake_connect)
        return sock

    return install


def audio_delta():
    return {"type": "response.output_audio.delta", "delta": ULAW_FRAME}


def end_call(call_id):
    """What the API streams for an end_call tool call: the arguments, then the response that holds it."""
    return [{"type": "response.function_call_arguments.done", "name": "end_call", "call_id": call_id,
             "arguments": "{}"},
            {"type": "response.done", "response": {"status": "completed", "output": [
                {"type": "function_call", "name": "end_call", "call_id": call_id, "arguments": "{}"}]}}]


def function_outputs(sock):
    return [m["item"] for m in sock.sent
            if m["type"] == "conversation.item.create" and m["item"]["type"] == "function_call_output"]


def test_openai_config_rejection_terminates_promptly(settings, openai):
    call_dir = prepare_call(settings, {"model": "gpt-realtime-2.1", "voice": "marin"})
    sock = openai([{"type": "session.created"},
                   {"type": "error", "error": {"type": "invalid_request_error", "message": "Invalid voice"}},
                   audio_delta(), IDLE])
    ws = twilio_socket()
    started = time.monotonic()
    assert run_call(settings, ws, timeout=3) == []
    assert time.monotonic() - started < settings.max_call_seconds  # did not wait for the duration limit
    texts = event_texts(call_dir)
    assert "openai error Invalid voice" in texts
    assert any("ProviderError" in t for t in texts)
    assert "max call duration reached" not in texts
    assert sock.sent[0]["type"] == "session.update"
    assert not [m for m in ws.sent if m["event"] == "media"]  # nothing after the error was played
    assert sock.closed and ws.close_code == 1000


def test_openai_premature_end_call_is_answered_once_the_response_is_done(settings, openai):
    """A rejected end_call needs a follow-up response.create, which the API refuses while the response
    that made the call is still active. Sent too early, it is dropped and the line goes silent."""
    settings = server.Settings(calls_dir=settings.calls_dir, shared_secret=SECRET, max_call_seconds=0.4,
                               start_timeout=1)
    call_dir = prepare_call(settings, {"model": "gpt-realtime-2.1"})
    sock = openai([
        {"type": "session.updated"},
        audio_delta(),
        {"type": "response.output_audio_transcript.done", "transcript": "Hi, this is an AI assistant."},
        *end_call("c1"),                                       # nobody has answered yet
        0.05,
        IDLE,
    ])
    assert run_call(settings, twilio_socket()) == []
    assert sock.refused == []
    assert function_outputs(sock) == [{"type": "function_call_output", "call_id": "c1",
                                       "output": server.END_CALL_REJECTED}]
    assert [m["type"] for m in sock.sent if m["type"] == "response.create"] == ["response.create"] * 2
    assert sock.active  # the follow-up response is running
    assert not any("active response" in t for t in event_texts(call_dir))


def test_openai_conversation_with_interruption_delayed_transcript_and_end_call(settings, openai):
    call_dir = prepare_call(settings, {"model": "gpt-realtime-2.1", "voice": "marin"})
    sock = openai([
        {"type": "session.created"},
        {"type": "session.updated"},
        audio_delta(),
        {"type": "response.output_audio_transcript.done", "transcript": "Hi, this is an AI assistant."},
        *end_call("c1"),                                       # too early
        0.05,
        {"type": "input_audio_buffer.speech_started"},
        # a non-fatal error once the session is configured: logged, the call continues
        {"type": "error", "error": {"type": "invalid_request_error", "message": "no active response"}},
        {"type": "conversation.item.input_audio_transcription.completed", "transcript": "Yes, speaking."},
        audio_delta(),
        {"type": "response.output_audio_transcript.done", "transcript": "Great. Bye!"},
        *end_call("c2"),
    ])
    ws = twilio_socket()
    assert run_call(settings, ws) == []

    url, headers, _ = sock.connected_with
    assert url.endswith("model=gpt-realtime-2.1") and headers["Authorization"] == "Bearer test-key"
    kinds = [m["type"] for m in sock.sent]
    assert kinds[:3] == ["session.update", "conversation.item.create", "response.create"]
    assert sock.sent[0]["session"]["audio"]["output"]["voice"] == "marin"
    appended = [m["audio"] for m in sock.sent if m["type"] == "input_audio_buffer.append"]
    assert appended and set(appended) == {ULAW_FRAME}  # mu-law passes straight through
    assert function_outputs(sock) == [{"type": "function_call_output", "call_id": "c1",
                                       "output": server.END_CALL_REJECTED}]
    assert sock.refused == []

    events = [m["event"] for m in ws.sent]
    assert events.count("media") == 2 and "clear" in events and events[-1] == "mark"
    texts = event_texts(call_dir)
    assert "interrupted" in texts and "end_call" in texts and "openai error no active response" in texts
    conv = transcript(call_dir)["conv"]
    assert [speaker for _, speaker, _ in conv] == ["agent", "callee", "agent"]
    assert sock.closed and ws.close_code == 1000


def test_openai_transcription_during_the_reply_allows_end_call(settings, openai):
    """speech_started -> agent reply -> late transcript of that speech -> the reply's end_call is accepted."""
    call_dir = prepare_call(settings, {"model": "gpt-realtime-2.1"})
    sock = openai([
        {"type": "session.updated"},
        audio_delta(),
        {"type": "response.done"},
        {"type": "input_audio_buffer.speech_started", "item_id": "item_1"},
        {"type": "response.created"},
        audio_delta(),
        {"type": "response.output_audio_transcript.done", "transcript": "Great, thanks. Bye!"},
        {"type": "conversation.item.input_audio_transcription.completed", "item_id": "item_1",
         "transcript": "Yes, speaking."},
        *end_call("c1"),
    ])
    ws = twilio_socket()
    assert run_call(settings, ws) == []
    assert function_outputs(sock) == []  # accepted at the first attempt
    assert ws.sent[-1]["event"] == "mark"
    assert json.loads((call_dir / "outcome.json").read_text(encoding="utf-8"))["status"] == "ok"


def test_openai_reply_before_speech_started_is_not_an_answer(settings, openai):
    """Audio that began before the callee started speaking does not count, even with a transcript."""
    settings = server.Settings(calls_dir=settings.calls_dir, shared_secret=SECRET, max_call_seconds=0.3,
                               start_timeout=1)
    call_dir = prepare_call(settings, {"model": "gpt-realtime-2.1"})
    sock = openai([
        {"type": "session.updated"},
        audio_delta(),
        {"type": "response.done"},
        {"type": "response.created"},
        audio_delta(),                                         # second agent turn, nobody spoke yet
        {"type": "input_audio_buffer.speech_started", "item_id": "item_1"},
        {"type": "conversation.item.input_audio_transcription.completed", "item_id": "item_1",
         "transcript": "Hello?"},
        *end_call("c1"),
        IDLE,
    ])
    assert run_call(settings, twilio_socket()) == []
    assert [item["output"] for item in function_outputs(sock)] == [server.END_CALL_REJECTED]
    assert any("end_call rejected" in t for t in event_texts(call_dir))


def test_openai_config_rejection_outcome_is_failed(settings, openai):
    call_dir = prepare_call(settings, {"model": "gpt-realtime-2.1"})
    openai([{"type": "error", "error": {"message": "Invalid voice"}}, IDLE])
    assert run_call(settings, twilio_socket(), timeout=3) == []
    result = json.loads((call_dir / "outcome.json").read_text(encoding="utf-8"))
    assert result["status"] == "failed" and "Invalid voice" in result["reason"]


def test_openai_cancelled_by_call_timeout(settings, openai):
    settings = server.Settings(calls_dir=settings.calls_dir, shared_secret=SECRET, max_call_seconds=0.3,
                               start_timeout=1)
    call_dir = prepare_call(settings, {"model": "gpt-realtime-2.1"})
    sock = openai([{"type": "session.updated"}, audio_delta(), IDLE])
    ws = twilio_socket()
    assert run_call(settings, ws) == []
    assert "max call duration reached" in event_texts(call_dir)
    assert sock.closed and ws.close_code == 1000


# --------------------------------------------------------------------------- failures, drain, truncate

def failed_done(message="The model hit an error"):
    return {"type": "response.done", "response": {"status": "failed", "output": [],
                                                  "status_details": {"type": "failed", "error": {
                                                      "type": "server_error", "message": message}}}}


def incomplete_done(reason):
    return {"type": "response.done", "response": {"status": "incomplete", "output": [],
                                                  "status_details": {"type": "incomplete", "reason": reason}}}


def outcome(call_dir):
    return json.loads((call_dir / "outcome.json").read_text(encoding="utf-8"))


def test_openai_failed_response_ends_the_call_as_failed(settings, openai):
    call_dir = prepare_call(settings, {"model": "gpt-realtime-2.1"})
    sock = openai([{"type": "session.updated"}, audio_delta(), failed_done("quota exceeded for this model"),
                   audio_delta(), IDLE])
    ws = twilio_socket()
    started = time.monotonic()
    assert run_call(settings, ws, timeout=3) == []
    assert time.monotonic() - started < settings.max_call_seconds
    result = outcome(call_dir)
    assert result["status"] == "failed" and "quota exceeded for this model" in result["reason"]
    assert sock.closed and ws.close_code == 1000


@pytest.mark.parametrize("reason", ["max_output_tokens", "content_filter"])
def test_openai_incomplete_response_for_a_real_reason_fails_the_call(settings, openai, reason):
    call_dir = prepare_call(settings, {"model": "gpt-realtime-2.1"})
    openai([{"type": "session.updated"}, audio_delta(), incomplete_done(reason), IDLE])
    assert run_call(settings, twilio_socket(), timeout=3) == []
    result = outcome(call_dir)
    assert result["status"] == "failed" and reason in result["reason"]


@pytest.mark.parametrize("done", [incomplete_done("turn_detected"), incomplete_done("client_cancelled"),
                                  {"type": "response.done", "response": {"status": "cancelled", "output": []}}],
                         ids=["turn_detected", "client_cancelled", "cancelled"])
def test_openai_interrupted_response_is_not_a_failure(settings, openai, done):
    settings = server.Settings(calls_dir=settings.calls_dir, shared_secret=SECRET, max_call_seconds=0.3,
                               start_timeout=1)
    call_dir = prepare_call(settings, {"model": "gpt-realtime-2.1"})
    openai([{"type": "session.updated"}, audio_delta(), done, IDLE])
    assert run_call(settings, twilio_socket()) == []
    assert "max call duration reached" in event_texts(call_dir)
    assert outcome(call_dir)["status"] != "failed"


def test_openai_error_after_setup_that_is_not_recoverable_fails_the_call(settings, openai):
    call_dir = prepare_call(settings, {"model": "gpt-realtime-2.1"})
    openai([{"type": "session.updated"}, audio_delta(),
            {"type": "error", "error": {"type": "server_error", "message": "The server had an error"}},
            audio_delta(), IDLE])
    ws = twilio_socket()
    assert run_call(settings, ws, timeout=3) == []
    result = outcome(call_dir)
    assert result["status"] == "failed" and "The server had an error" in result["reason"]
    assert "max call duration reached" not in event_texts(call_dir)
    assert len([m for m in ws.sent if m["event"] == "media"]) == 1  # nothing after the error was played


@pytest.mark.parametrize("error", [
    {"type": "invalid_request_error", "code": "response_cancel_not_active", "message": "nothing to cancel"},
    {"type": "invalid_request_error", "code": "conversation_already_has_active_response", "message": "busy"},
    {"type": "server_error", "code": "response_cancel_not_active", "message": "nothing to cancel"},
    {"type": "invalid_request_error", "message": "no active response"},
], ids=["cancel-not-active", "active-response", "recoverable-code-any-type", "invalid-request"])
def test_openai_recoverable_errors_do_not_end_the_call(settings, openai, error):
    settings = server.Settings(calls_dir=settings.calls_dir, shared_secret=SECRET, max_call_seconds=0.3,
                               start_timeout=1)
    call_dir = prepare_call(settings, {"model": "gpt-realtime-2.1"})
    openai([{"type": "session.updated"}, audio_delta(), {"type": "error", "error": error}, audio_delta(), IDLE])
    ws = twilio_socket()
    assert run_call(settings, ws) == []
    assert "max call duration reached" in event_texts(call_dir)
    assert len([m for m in ws.sent if m["event"] == "media"]) == 2


def truncates(sock):
    return [m for m in sock.sent if m["type"] == "conversation.item.truncate"]


def test_openai_barge_in_truncates_the_item_at_what_was_played(settings, openai):
    settings = server.Settings(calls_dir=settings.calls_dir, shared_secret=SECRET, max_call_seconds=0.4,
                               start_timeout=1)
    call_dir = prepare_call(settings, {"model": "gpt-realtime-2.1"})
    delta = {"type": "response.output_audio.delta", "item_id": "item_9", "content_index": 0, "delta": ULAW_FRAME}
    sock = openai([{"type": "session.updated"}, delta, delta, delta, delta, delta,   # 5 x 20 ms sent at once
                   0.04, {"type": "input_audio_buffer.speech_started", "item_id": "item_10"},
                   {"type": "input_audio_buffer.speech_started", "item_id": "item_11"}, IDLE])
    ws = twilio_socket()
    assert run_call(settings, ws, timeout=3) == []
    (cut,) = truncates(sock)  # a second speech_started with nothing playing truncates nothing
    assert cut["item_id"] == "item_9" and cut["content_index"] == 0 and 0 < cut["audio_end_ms"] < 100
    kinds = [m["type"] for m in sock.sent]
    assert kinds.index("conversation.item.truncate") > kinds.index("response.create")
    assert [m["event"] for m in ws.sent].count("clear") == 2
    assert "interrupted" in event_texts(call_dir)


def test_openai_barge_in_without_item_audio_sends_no_truncate(settings, openai):
    sock = openai([{"type": "session.updated"}, audio_delta(),
                   {"type": "input_audio_buffer.speech_started", "item_id": "item_1"}, IDLE])
    prepare_call(settings, {"model": "gpt-realtime-2.1"})
    settings = server.Settings(calls_dir=settings.calls_dir, shared_secret=SECRET, max_call_seconds=0.3,
                               start_timeout=1)
    assert run_call(settings, twilio_socket()) == []
    assert truncates(sock) == []


def test_gemini_keeps_the_last_transcription_after_the_callee_hangs_up(settings, gemini, monkeypatch):
    monkeypatch.setattr(server, "TWILIO_STOP_DRAIN_S", 1.0)
    call_dir = prepare_call(settings, {})
    live = gemini(script=[gemini_response(data=PCM24K_FRAME, said="Anything else?"),
                          0.1, gemini_response(heard="No thanks, bye!")])
    start = {"event": "start", "start": {"streamSid": "MZtest"}}
    ws = FakeTwilioSocket([{"event": "connected"}, start, {"event": "stop"}], hold_open=True)
    assert run_call(settings, ws) == []
    conv = transcript(call_dir)["conv"]
    assert [(speaker, text) for _, speaker, text in conv] == [("agent", "Anything else?"),
                                                              ("callee", "No thanks, bye!")]
    assert outcome(call_dir)["status"] == "ok" and live.closed


# --------------------------------------------------------------------------- hangup before the agent spoke

def hung_up_twilio_socket():
    start = {"event": "start", "start": {"streamSid": "MZtest"}}
    return FakeTwilioSocket([{"event": "connected"}, start, {"event": "stop"}], hold_open=True)


def assert_agent_never_spoke(call_dir, ws):
    conv = transcript(call_dir)["conv"]
    assert [(speaker, text) for _, speaker, text in conv] == [("callee", "Hello?")]
    assert not [m for m in ws.sent if m["event"] == "media"]
    result = outcome(call_dir)
    assert result["status"] == "failed" and result["reason"] == "the callee hung up before the agent spoke"


def test_gemini_opening_line_generated_after_the_callee_hung_up_is_not_recorded_as_spoken(settings, gemini, monkeypatch):
    monkeypatch.setattr(server, "TWILIO_STOP_DRAIN_S", 1.0)
    call_dir = prepare_call(settings, {})
    gemini(script=[0.1, gemini_response(data=PCM24K_FRAME, said="Hi, this is the assistant"),
                   gemini_response(heard="Hello?")])
    ws = hung_up_twilio_socket()
    assert run_call(settings, ws) == []
    assert_agent_never_spoke(call_dir, ws)


def test_openai_opening_line_generated_after_the_callee_hung_up_is_not_recorded_as_spoken(settings, openai, monkeypatch):
    monkeypatch.setattr(server, "TWILIO_STOP_DRAIN_S", 1.0)
    call_dir = prepare_call(settings, {"model": "gpt-realtime-2.1"})
    openai([{"type": "session.updated"}, 0.1,
            {"type": "response.output_audio.delta", "item_id": "item_1", "delta": ULAW_FRAME},
            {"type": "response.output_audio_transcript.done", "item_id": "item_1",
             "transcript": "Hi, this is the assistant"},
            {"type": "response.done", "response": {"status": "completed", "output": []}},
            {"type": "conversation.item.input_audio_transcription.completed", "item_id": "in_1",
             "transcript": "Hello?"}, IDLE])
    ws = hung_up_twilio_socket()
    assert run_call(settings, ws) == []
    assert_agent_never_spoke(call_dir, ws)


def test_agent_turn_under_way_when_the_callee_hangs_up_keeps_its_transcript(settings, openai, monkeypatch):
    monkeypatch.setattr(server, "TWILIO_STOP_DRAIN_S", 1.0)
    call_dir = prepare_call(settings, {"model": "gpt-realtime-2.1"})
    openai([{"type": "session.updated"},
            {"type": "response.output_audio.delta", "item_id": "item_1", "delta": ULAW_FRAME},
            0.1, {"type": "response.output_audio_transcript.done", "item_id": "item_1", "transcript": "Goodbye!"},
            IDLE])
    start = {"event": "start", "start": {"streamSid": "MZtest"}}

    class LateStopSocket(FakeTwilioSocket):  # the callee hangs up while the agent is mid-sentence
        async def __anext__(self):
            if self.incoming and self.incoming[0].get("event") == "stop":
                await asyncio.sleep(0.05)
            return await super().__anext__()

    ws = LateStopSocket([{"event": "connected"}, start, {"event": "stop"}], hold_open=True)
    assert run_call(settings, ws, timeout=3) == []
    assert [t for _, s, t in transcript(call_dir)["conv"] if s == "agent"] == ["Goodbye!"]


# --------------------------------------------------------------------------- provider closes the session

def test_openai_normal_close_while_the_callee_is_on_the_line_is_a_failure(settings, openai):
    call_dir = prepare_call(settings, {"model": "gpt-realtime-2.1"})
    openai([{"type": "session.updated"}, audio_delta(), CLOSE])
    started = time.monotonic()
    assert run_call(settings, twilio_socket()) == []
    assert time.monotonic() - started < 2
    result = outcome(call_dir)
    assert result["status"] == "failed" and result["reason"] == "provider closed the session"


# --------------------------------------------------------------------------- late audio of a truncated item

def test_openai_late_audio_of_a_truncated_item_is_ignored(settings, openai):
    settings = server.Settings(calls_dir=settings.calls_dir, shared_secret=SECRET, max_call_seconds=0.5,
                               start_timeout=1)
    prepare_call(settings, {"model": "gpt-realtime-2.1"})
    delta = {"type": "response.output_audio.delta", "item_id": "item_9", "content_index": 0, "delta": ULAW_FRAME}
    sock = openai([{"type": "session.updated"}, delta, delta, delta, 0.04,
                   {"type": "input_audio_buffer.speech_started", "item_id": "item_10"}, 0.02,
                   delta, delta,  # in flight when the truncate was sent
                   {"type": "input_audio_buffer.speech_started", "item_id": "item_11"}, IDLE])
    ws = twilio_socket()
    assert run_call(settings, ws, timeout=3) == []
    assert len(truncates(sock)) == 1
    assert len([m for m in ws.sent if m["event"] == "media"]) == 3
