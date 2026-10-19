"""Twilio Media Streams <-> Gemini Live / OpenAI Realtime bridge.

Runs on the VPS behind Caddy (TLS). Twilio opens a websocket to /media/<call_id>/<expires>/<token>;
the bridge streams the call audio to a realtime voice model and back.

Every call must have been prepared by aicall.py:
  * calls/<call_id>/prompt.txt and calls/<call_id>/config.json exist on this server, and
  * token is an HMAC-SHA256 over "<call_id>.<expires>" with the shared secret AICALL_SHARED_SECRET.
The path is checked in the HTTP request, before the websocket upgrade: anything else gets a 403
and never becomes a websocket.

Environment (see .env.example): AICALL_DIR, AICALL_SHARED_SECRET, GEMINI_API_KEY,
OPENAI_API_KEY (gpt-* models only), optional limits and Twilio signature checking.
"""
from __future__ import annotations

import asyncio
import audioop  # Python <= 3.12 stdlib; audioop-lts on 3.13+
import base64
import contextlib
import functools
import hashlib
import hmac
import http
import io
import json
import logging
import os
import re
import secrets
import sys
import time
import wave
from dataclasses import dataclass
from pathlib import Path

from websockets.exceptions import ConnectionClosed

log = logging.getLogger("aicall.bridge")

CALL_ID_RE = re.compile(r"[0-9]{8}_[0-9]{6}_[0-9a-f]{12}")
# Exactly what aicall.stream_url produces: ASCII only, a canonical decimal expiry, a lowercase hex token.
STREAM_PATH_RE = re.compile(rf"/media/({CALL_ID_RE.pattern})/([1-9][0-9]{{0,11}})/([0-9a-f]{{64}})")
STREAM_SID_RE = re.compile(r"[A-Za-z0-9]{1,64}")
DEFAULT_MODEL = "gemini-3.1-flash-live-preview"
DEFAULT_KICKOFF = ("(The call is now connected and the other person is listening. "
                   "Say your first line now, in the language your instructions specify.)")
END_CALL_DESCRIPTION = "Hang up the phone call. Call this only AFTER you have said goodbye."
END_CALL_REJECTED = ("Not hung up: the other person has not replied to you yet. Keep the conversation going "
                     "naturally. When it is time to finish, say a natural goodbye in the conversation's "
                     "language, then call end_call.")
OPENAI_TOOL_RULE = ("\n\n(Tool rule: only call end_call after the other person has replied and you have said "
                    "goodbye. Never call it right after your first line.)")
OAI_VOICES = {"alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse", "marin", "cedar"}
GOODBYE_MARK = "bye"
MARK_TIMEOUT_S = 15.0
# After Twilio's "stop" (the callee hung up) the model side gets this long to deliver what it was still
# transcribing, e.g. the callee's last words, before it is cancelled.
TWILIO_STOP_DRAIN_S = 2.5
# A Gemini Live connection lasts about 10 minutes (GoAway shortly before) and the bridge does not
# resume sessions, so Gemini calls end here whatever AICALL_MAX_CALL_SECONDS allows. aicall.py has the same cap.
GEMINI_MAX_CALL_SECONDS = 540
OPENAI_MAX_MESSAGE_BYTES = 8 * 1024 * 1024
# OpenAI "error" events that leave the session usable. Besides these codes, any invalid_request_error
# (a request of ours was refused) is logged and the call goes on; every other error type (server_error,
# authentication, rate limit, quota, ...) ends the call as failed.
OPENAI_RECOVERABLE_ERROR_CODES = {"response_cancel_not_active", "conversation_already_has_active_response",
                                  "item_truncate_invalid_item_id", "input_audio_buffer_commit_empty"}
# response.done with status "incomplete" for these reasons is a normal barge-in, not a fault.
OPENAI_BENIGN_INCOMPLETE_REASONS = {"turn_detected", "client_cancelled"}


class CallRejected(Exception):
    """The incoming stream is not an authorised, prepared call."""


class ProviderError(Exception):
    """The model provider reported an error the call cannot recover from."""


# --------------------------------------------------------------------------- settings

@dataclass(frozen=True)
class Settings:
    calls_dir: Path
    shared_secret: str
    max_concurrent_calls: int = 4
    max_call_seconds: int = 900
    max_token_ttl: int = 3600
    max_message_bytes: int = 64 * 1024
    start_timeout: float = 10.0
    verify_twilio_signature: bool = False
    twilio_auth_token: str = ""
    public_wss_url: str = ""
    host: str = "127.0.0.1"
    port: int = 8765

    @classmethod
    def from_env(cls, env=None) -> "Settings":
        env = os.environ if env is None else env
        secret = env.get("AICALL_SHARED_SECRET", "")
        if len(secret) < 32:
            raise SystemExit("AICALL_SHARED_SECRET must be set to a random string of at least 32 characters")
        verify = env.get("AICALL_VERIFY_TWILIO_SIGNATURE", "0") == "1"
        if verify and not (env.get("TWILIO_AUTH_TOKEN") and env.get("PUBLIC_WSS_URL")):
            raise SystemExit("AICALL_VERIFY_TWILIO_SIGNATURE=1 needs TWILIO_AUTH_TOKEN and PUBLIC_WSS_URL")
        if not env.get("GEMINI_API_KEY"):  # the default model; OPENAI_API_KEY is only needed for gpt-* calls
            raise SystemExit("GEMINI_API_KEY must be set (see .env.example)")
        return cls(
            calls_dir=Path(env.get("AICALL_DIR", "/srv/aicall")) / "calls",
            shared_secret=secret,
            max_concurrent_calls=int(env.get("AICALL_MAX_CONCURRENT_CALLS", "4")),
            max_call_seconds=int(env.get("AICALL_MAX_CALL_SECONDS", "900")),
            verify_twilio_signature=verify,
            twilio_auth_token=env.get("TWILIO_AUTH_TOKEN", ""),
            public_wss_url=env.get("PUBLIC_WSS_URL", ""),
        )


# --------------------------------------------------------------------------- authorisation

def call_token_signature(secret: str, call_id: str, expires: int) -> str:
    """HMAC-SHA256 over "<call_id>.<expires>". Must match aicall.call_token_signature."""
    message = f"{call_id}.{expires}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def check_token_lifetime(expires: int, *, now: float, max_ttl: int) -> None:
    if expires < now:
        raise CallRejected("token expired")
    if expires - now > max_ttl:
        raise CallRejected("token lifetime too long")


def twilio_signature(auth_token: str, url: str) -> str:
    """Twilio's X-Twilio-Signature for a request without POST parameters: base64(HMAC-SHA1(url))."""
    digest = hmac.new(auth_token.encode(), url.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def stream_credentials(path: str) -> tuple[str, int, str]:
    """Parse /media/<call_id>/<expires>/<token>, the <Stream> URL path aicall.py builds."""
    match = STREAM_PATH_RE.fullmatch(path)
    if match is None:
        raise CallRejected("malformed stream path")
    return match[1], int(match[2]), match[3]


def resolve_call_dir(calls_dir: Path, call_id) -> Path:
    """Map a call id to its directory, refusing anything that is not a plain child of calls_dir."""
    if not isinstance(call_id, str) or not CALL_ID_RE.fullmatch(call_id):
        raise CallRejected("invalid call id")
    base = calls_dir.resolve()
    path = (base / call_id).resolve()
    if path.parent != base:
        raise CallRejected("call directory escapes the calls directory")
    return path


@dataclass
class CallSpec:
    call_id: str
    call_dir: Path
    prompt: str
    config: dict


def read_upload(path: Path) -> str:
    """Read a file the upload account wrote. Symlinks are refused: that account can create them, and the
    bridge can read files it cannot, such as /proc/self/environ with every API key."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as f:
        return f.read().decode("utf-8")


def record_failure(call_dir: Path, reason: str) -> None:
    """Results for a claimed call that never got a model session, so the driver reports a definite failure."""
    write_private(call_dir / "outcome.json", json.dumps({"status": "failed", "reason": reason}).encode("utf-8"))
    transcript = {"conv": [], "events": [[0.0, reason]], "secs": 0.0, "model": None, "recorded": False}
    write_private(call_dir / "transcript.json", json.dumps(transcript).encode("utf-8"))


def load_call(settings: Settings, path: str, *, now: float) -> CallSpec:
    """Authenticate the stream path, claim the prepared call and load it. Raises CallRejected.

    Nothing is written unless the token signature is valid and this is the call's first stream. From then
    on every refusal is recorded with record_failure."""
    call_id, expires, token = stream_credentials(path)
    call_dir = resolve_call_dir(settings.calls_dir, call_id)
    if not hmac.compare_digest(call_token_signature(settings.shared_secret, call_id, expires), token):
        raise CallRejected("bad token signature")
    try:  # one stream per call id: a replayed URL can neither start a second session nor touch the first's results
        os.close(os.open(call_dir / ".claimed", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    except FileExistsError:
        raise CallRejected("call already used") from None
    except FileNotFoundError:
        raise CallRejected("unknown call") from None
    try:
        check_token_lifetime(expires, now=now, max_ttl=settings.max_token_ttl)
        try:
            prompt = read_upload(call_dir / "prompt.txt")
            config = json.loads(read_upload(call_dir / "config.json"))
        except OSError as e:
            raise CallRejected(f"cannot read {Path(e.filename or 'call files').name}: {e.strerror}") from None
        except (ValueError, RecursionError) as e:  # not UTF-8, not JSON, nested too deep
            raise CallRejected(f"prompt.txt or config.json is malformed ({type(e).__name__})") from None
        if not isinstance(config, dict):
            raise CallRejected("config.json must be an object")
    except CallRejected as e:
        record_failure(call_dir, str(e))
        raise
    return CallSpec(call_id, call_dir, prompt, config)


# --------------------------------------------------------------------------- conversation state

class ConversationState:
    """Decides whether the agent's end_call request is honoured.

    end_call is accepted only after a completed exchange: the callee said something and the agent
    answered it in an agent turn that started after the callee began speaking. This stops models
    that hang up right after their opening line, without keyword matching on transcripts.

    It is built on turn events, because the callee's transcription often arrives after the agent
    has already started (or finished) its reply:
      * agent turns: the first audio chunk after a turn end starts a new turn; the provider's
        turn-complete / response-done / interruption ends it. Turn 1 is the opening line.
      * callee turns: OpenAI announces the start of speech (speech_started, with an item id), so
        late transcription of that item is matched to where the speech began. Gemini has no such
        event: words transcribed while a later agent turn is playing are what that turn answers;
        otherwise the next agent turn has to answer them.
    """

    MAX_OPEN_CALLEE_TURNS = 32

    def __init__(self):
        self.agent_turns = 0          # agent turns that produced audio
        self.agent_speaking = False
        self.callee_turns: dict = {}  # callee turn key -> first agent turn that would answer it
        self.answer_from = None       # first agent turn that answers something the callee said

    def on_agent_audio(self) -> None:
        if not self.agent_speaking:
            self.agent_turns += 1
            self.agent_speaking = True

    def on_agent_turn_end(self) -> None:
        self.agent_speaking = False

    def on_callee_turn_start(self, key) -> None:
        if len(self.callee_turns) >= self.MAX_OPEN_CALLEE_TURNS:  # starts that never got a transcript
            self.callee_turns.pop(next(iter(self.callee_turns)))
        self.callee_turns[key] = self.agent_turns + 1

    def on_callee_words(self, key=None) -> None:
        """The callee's speech was transcribed to non-empty text."""
        first = self.callee_turns.pop(key, None)
        if first is None:
            first = self.agent_turns if (self.agent_speaking and self.agent_turns >= 2) else self.agent_turns + 1
        self.answer_from = first if self.answer_from is None else min(self.answer_from, first)

    @property
    def callee_has_spoken(self) -> bool:
        return self.answer_from is not None

    def end_call_allowed(self) -> bool:
        return self.answer_from is not None and self.agent_turns >= self.answer_from


async def wait_for_mark(event: asyncio.Event, timeout: float) -> bool:
    """Wait until Twilio echoes our goodbye mark (playback finished or cleared). False on timeout."""
    try:
        await asyncio.wait_for(event.wait(), timeout)
        return True
    except asyncio.TimeoutError:
        return False


# --------------------------------------------------------------------------- artifacts

class BoundedBuffer:
    """Byte buffer that silently stops growing at max_bytes (recordings cannot exhaust memory)."""

    def __init__(self, max_bytes: int):
        self.max_bytes = int(max_bytes)
        self.data = bytearray()
        self.truncated = False

    def extend(self, chunk: bytes) -> None:
        room = self.max_bytes - len(self.data)
        if len(chunk) > room:
            self.truncated = True
            chunk = chunk[:max(room, 0)]
        self.data.extend(chunk)


def write_private(path: Path, data: bytes) -> None:
    """Atomically write a call artifact as 0640. Its group is shared only with the SFTP upload account,
    which fetches the results (docs/deployment.md); nobody else can read it."""
    # A random name opened with O_EXCL|O_NOFOLLOW: the upload account can create symlinks in this directory,
    # and a planted "<name>.tmp" must never be written through.
    tmp = path.with_name(f"{path.name}.{secrets.token_hex(8)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(tmp, flags, 0o640)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def wav_bytes(rate: int, pcm16: bytes) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(pcm16))
    return out.getvalue()


def merge_turns(turns: list) -> list:
    """Join consecutive transcript fragments from the same speaker, with one space between them."""
    merged = []
    for ts, speaker, text in sorted(turns, key=lambda t: t[0]):
        if merged and merged[-1][1] == speaker:
            joined = merged[-1][2][-1:].isspace() or text[:1].isspace()
            merged[-1][2] += text if joined else " " + text
        else:
            merged.append([ts, speaker, text])
    return merged


class CallSession:
    """Per-call state shared by the provider runners: transcript, events, optional recording."""

    def __init__(self, spec: CallSpec, stream_sid: str, ws, max_call_seconds: int, model: str):
        self.spec = spec
        self.stream_sid = stream_sid
        self.ws = ws
        self.model = model
        self.max_call_seconds = max_call_seconds
        self.t0 = time.monotonic()
        self.turns: list = []
        self.events: list = []
        self.state = ConversationState()
        self.mark_received = asyncio.Event()
        self.twilio_stopped = False    # Twilio ended the stream: nothing more can be sent to it
        # Assistant audio item being played to the callee (OpenAI only), for conversation.item.truncate
        self.audio_item: str | None = None
        self.audio_item_start = 0.0    # monotonic time its first audio went to Twilio
        self.audio_item_ms = 0.0       # ms of that item sent to Twilio so far
        self.truncated_items: set = set()  # items cut by a barge-in: their late audio deltas are dropped
        self.error: str | None = None  # first unrecoverable error (provider, network, task)
        self.end_reason = ""           # how the call ended when it did not fail
        # one switch for bridge-side audio: aicall.py --record writes "record": true
        self.record = spec.config.get("record") is True
        self.callee_audio = BoundedBuffer(8000 * 2 * max_call_seconds) if self.record else None
        self.agent_audio: BoundedBuffer | None = None
        self.agent_rate = 0

    def elapsed(self) -> float:
        return round(time.monotonic() - self.t0, 1)

    def event(self, text: str) -> None:
        self.events.append((self.elapsed(), text))

    def fail(self, reason: str) -> None:
        self.event(reason)
        if self.error is None:
            self.error = reason

    def ended(self, reason: str) -> None:
        self.event(reason)
        if not self.end_reason:
            self.end_reason = reason

    def outcome(self) -> dict:
        """Structured result for the driver: status "ok", "failed" or "no_answer", plus a reason."""
        if self.error:
            return {"status": "failed", "reason": self.error}
        if self.state.agent_turns == 0:
            if self.twilio_stopped:  # anything the model said after the hangup never reached the callee
                return {"status": "failed", "reason": "the callee hung up before the agent spoke"}
            return {"status": "failed", "reason": "the model produced no audio"}
        if not self.state.callee_has_spoken:
            return {"status": "no_answer", "reason": "the callee never said anything"}
        return {"status": "ok", "reason": self.end_reason or "call ended"}

    def add_turn(self, speaker: str, text: str) -> None:
        if text:
            self.turns.append([self.elapsed(), speaker, text])

    def agent_audible(self) -> bool:
        """False once Twilio has stopped: a new agent turn can no longer be heard, so it is not recorded as
        spoken (not in the transcript or the turn count). A turn already under way when the callee hung up
        was heard, so its transcript still counts."""
        return not self.twilio_stopped or self.state.agent_speaking

    def record_callee(self, pcm8k: bytes) -> None:
        if self.callee_audio is not None:
            self.callee_audio.extend(pcm8k)

    def record_agent(self, pcm: bytes, rate: int) -> None:
        if not self.record:
            return
        if self.agent_audio is None:
            self.agent_rate = rate
            self.agent_audio = BoundedBuffer(rate * 2 * self.max_call_seconds)
        self.agent_audio.extend(pcm)

    async def send(self, message: dict) -> None:
        if self.twilio_stopped:
            return
        await self.ws.send(json.dumps({**message, "streamSid": self.stream_sid}))

    async def play(self, ulaw: bytes) -> None:
        await self.send({"event": "media", "media": {"payload": base64.b64encode(ulaw).decode()}})

    async def barge_in(self) -> None:
        self.event("interrupted")
        await self.send({"event": "clear"})

    def note_agent_audio(self, item_id, ulaw: bytes) -> None:
        """Count mu-law audio (8000 bytes = 1 s) of the assistant item that was just sent to Twilio."""
        if not item_id:
            return
        if item_id != self.audio_item:
            self.audio_item, self.audio_item_start, self.audio_item_ms = item_id, time.monotonic(), 0.0
        self.audio_item_ms += len(ulaw) / 8

    def played_audio(self) -> tuple[str, int] | None:
        """(item id, ms the callee heard) of the assistant audio in flight, once; None if there is none.

        Twilio plays audio at real-time speed however fast we send it, so what was heard is the time since
        the item's first audio went out, never more than what was sent. Twilio's small jitter buffer is ignored."""
        if not self.audio_item:
            return None
        item = self.audio_item
        self.truncated_items.add(item)
        heard = min((time.monotonic() - self.audio_item_start) * 1000, self.audio_item_ms)
        self.audio_item = None
        return item, int(heard)

    async def request_end_call(self) -> bool:
        """Handle the agent's end_call. True = goodbye played, close the stream."""
        if not self.state.end_call_allowed():
            self.event("end_call rejected (callee has not replied yet)")
            return False
        self.ended("end_call")
        await self.send({"event": "mark", "mark": {"name": GOODBYE_MARK}})
        if not await wait_for_mark(self.mark_received, MARK_TIMEOUT_S):
            self.event("goodbye mark not acknowledged, hanging up")
        await asyncio.sleep(0.5)
        return True

    def save(self) -> None:
        # outcome.json first: once the driver can fetch transcript.json, the outcome is there too
        outcome = {**self.outcome(), "secs": self.elapsed(), "model": self.model}
        write_private(self.spec.call_dir / "outcome.json", json.dumps(outcome, ensure_ascii=False).encode("utf-8"))
        transcript = {"conv": merge_turns(self.turns), "events": self.events, "secs": self.elapsed(),
                      "model": self.model, "recorded": self.record}
        write_private(self.spec.call_dir / "transcript.json",
                      json.dumps(transcript, ensure_ascii=False, indent=1).encode("utf-8"))
        if not self.record:
            return
        write_private(self.spec.call_dir / "callee_8k.wav", wav_bytes(8000, self.callee_audio.data))
        if self.agent_audio is not None:
            name = f"agent_{self.agent_rate // 1000}k.wav"
            write_private(self.spec.call_dir / name, wav_bytes(self.agent_rate, self.agent_audio.data))


# --------------------------------------------------------------------------- twilio side

def twilio_message(raw) -> dict:
    """One Twilio websocket message as a dict; {} for anything that is not a JSON object."""
    try:
        msg = json.loads(raw)
    except (TypeError, ValueError, RecursionError):
        return {}
    return msg if isinstance(msg, dict) else {}


def field(msg: dict, outer: str, inner: str):
    """msg[outer][inner], or None when either level is missing or not an object."""
    value = msg.get(outer)
    return value.get(inner) if isinstance(value, dict) else None


async def wait_for_start(ws, timeout: float) -> dict:
    """Return the Twilio "start" payload; anything unexpected rejects the connection."""
    try:
        async with asyncio.timeout(timeout):
            async for raw in ws:
                msg = twilio_message(raw)
                kind = msg.get("event")
                if kind == "connected":
                    continue
                if kind != "start":
                    raise CallRejected(f"unexpected first event {str(kind)[:40]!r}")
                sid = field(msg, "start", "streamSid")
                # Twilio repeats the stream sid outside the start payload; both must name one stream.
                if not (isinstance(sid, str) and STREAM_SID_RE.fullmatch(sid)) or msg.get("streamSid", sid) != sid:
                    raise CallRejected("start event without one valid streamSid")
                return msg["start"]
    except TimeoutError:
        raise CallRejected("no start event") from None
    except ConnectionClosed:
        pass
    raise CallRejected("connection closed before start")


async def pump_twilio(ws, session: CallSession, on_media) -> None:
    """Read Twilio events until "stop": forward the callee's mu-law audio, note our goodbye mark."""
    try:
        async for raw in ws:
            msg = twilio_message(raw)
            kind = msg.get("event")
            if kind == "media":
                payload = field(msg, "media", "payload")
                try:
                    ulaw = base64.b64decode(payload, validate=True) if isinstance(payload, str) else None
                except ValueError:
                    ulaw = None
                if ulaw:
                    await on_media(ulaw)
            elif kind == "mark" and field(msg, "mark", "name") == GOODBYE_MARK:
                session.mark_received.set()
            elif kind == "stop":
                session.twilio_stopped = True
                session.ended("twilio stop")
                return
    except ConnectionClosed:
        session.twilio_stopped = True
        session.ended("twilio connection lost")


async def run_until_first_done(session: CallSession, *coros) -> None:
    """Run the Twilio pump (the first coroutine) and the model pumps until one finishes. The others are
    always cancelled and awaited, also when this coroutine itself is cancelled (e.g. by the call-duration
    timeout). When the Twilio side finishes first (the callee hung up), the model side gets up to
    TWILIO_STOP_DRAIN_S more to deliver pending transcription before it is cancelled."""
    tasks = [asyncio.create_task(c) for c in coros]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if pending and tasks[0] in done and not tasks[0].cancelled() and tasks[0].exception() is None:
            drained, _ = await asyncio.wait(pending, timeout=TWILIO_STOP_DRAIN_S)
            done |= drained
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    for task in done:
        if not task.cancelled() and task.exception():
            log.error("task error: %r", task.exception())
            session.fail(f"error {task.exception()!r}")


# --------------------------------------------------------------------------- Gemini Live

@functools.cache
def gemini_client():
    from google import genai
    return genai.Client(api_key=os.environ["GEMINI_API_KEY"])


def gemini_config(session: CallSession):
    from google.genai import types
    cfg = session.spec.config
    vad = None
    if cfg.get("vad", True):
        # default VAD kept "listening" for ~25 s on noisy phone lines -> end turns faster
        vad = types.RealtimeInputConfig(automatic_activity_detection=types.AutomaticActivityDetection(
            end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_HIGH, silence_duration_ms=700))
    voice = types.SpeechConfig(voice_config=types.VoiceConfig(
        prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=cfg.get("voice", "Kore"))))
    end_call = types.Tool(function_declarations=[
        types.FunctionDeclaration(name="end_call", description=END_CALL_DESCRIPTION)])
    return types.LiveConnectConfig(
        response_modalities=["AUDIO"], system_instruction=session.spec.prompt, tools=[end_call],
        realtime_input_config=vad, speech_config=voice,
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
    )


async def run_gemini(ws, session: CallSession) -> None:
    """Gemini Live: mu-law 8 kHz -> PCM16 16 kHz up, PCM16 24 kHz -> mu-law 8 kHz down."""
    from google.genai import types
    resample = {"up": None, "down": None}
    async with gemini_client().aio.live.connect(model=session.model, config=gemini_config(session)) as gs:
        await gs.send_realtime_input(text=session.spec.config.get("kickoff", DEFAULT_KICKOFF))

        async def on_media(ulaw: bytes) -> None:
            lin = audioop.ulaw2lin(ulaw, 2)
            session.record_callee(lin)
            pcm, resample["up"] = audioop.ratecv(lin, 2, 1, 8000, 16000, resample["up"])
            await gs.send_realtime_input(audio=types.Blob(data=pcm, mime_type="audio/pcm;rate=16000"))

        async def from_model() -> None:
            while True:
                async for r in gs.receive():
                    if r.data and not session.twilio_stopped:  # nobody is listening any more
                        session.state.on_agent_audio()
                        session.record_agent(r.data, 24000)
                        pcm, resample["down"] = audioop.ratecv(r.data, 2, 1, 24000, 8000, resample["down"])
                        await session.play(audioop.lin2ulaw(pcm, 2))
                    content = r.server_content
                    if content:
                        if content.input_transcription and content.input_transcription.text:
                            session.add_turn("callee", content.input_transcription.text)
                            if content.input_transcription.text.strip():
                                session.state.on_callee_words()
                        if (content.output_transcription and content.output_transcription.text
                                and session.agent_audible()):
                            session.add_turn("agent", content.output_transcription.text)
                        if content.interrupted:
                            session.state.on_agent_turn_end()
                            await session.barge_in()
                        if content.turn_complete:
                            session.state.on_agent_turn_end()
                    if r.tool_call:
                        for fc in r.tool_call.function_calls:
                            if fc.name != "end_call":
                                continue
                            if await session.request_end_call():
                                return
                            await gs.send_tool_response(function_responses=[types.FunctionResponse(
                                id=fc.id, name=fc.name, response={"result": END_CALL_REJECTED})])

        await run_until_first_done(session, pump_twilio(ws, session, on_media), from_model())


# --------------------------------------------------------------------------- OpenAI Realtime

def openai_session_update(session: CallSession) -> dict:
    voice = str(session.spec.config.get("voice", "")).lower()
    return {"type": "session.update", "session": {
        "type": "realtime",
        "instructions": session.spec.prompt + OPENAI_TOOL_RULE,
        "audio": {
            "input": {"format": {"type": "audio/pcmu"},
                      "turn_detection": {"type": "server_vad", "silence_duration_ms": 700},
                      "transcription": {"model": "gpt-realtime-whisper"}},
            "output": {"format": {"type": "audio/pcmu"}, "voice": voice if voice in OAI_VOICES else "marin"},
        },
        "tools": [{"type": "function", "name": "end_call", "description": END_CALL_DESCRIPTION,
                   "parameters": {"type": "object", "properties": {}}}],
    }}


def openai_response_failure(response) -> str | None:
    """Why a response.done means the model could not answer ("failed: ..."), or None for a normal one."""
    if not isinstance(response, dict):
        return None
    status = response.get("status")
    details = response.get("status_details")
    details = details if isinstance(details, dict) else {}
    if status == "failed":
        error = details.get("error")
        return f"failed: {(error or {}).get('message') if isinstance(error, dict) else error or 'no details'}"
    if status == "incomplete" and details.get("reason") not in OPENAI_BENIGN_INCOMPLETE_REASONS:
        return f"incomplete: {details.get('reason')}"
    return None


def openai_error_recoverable(error) -> bool:
    error = error if isinstance(error, dict) else {}
    return error.get("code") in OPENAI_RECOVERABLE_ERROR_CODES or error.get("type") == "invalid_request_error"


async def run_openai(ws, session: CallSession) -> None:
    """OpenAI Realtime: audio/pcmu both ways, so Twilio's mu-law passes straight through."""
    from websockets.asyncio.client import connect
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise ProviderError("OPENAI_API_KEY is not set on the bridge (needed for gpt-* models)")
    url = f"wss://api.openai.com/v1/realtime?model={session.model}"
    headers = {"Authorization": f"Bearer {key}"}
    async with connect(url, additional_headers=headers, max_size=OPENAI_MAX_MESSAGE_BYTES) as oa:
        kickoff = session.spec.config.get("kickoff", DEFAULT_KICKOFF)
        await oa.send(json.dumps(openai_session_update(session)))
        await oa.send(json.dumps({"type": "conversation.item.create", "item": {
            "type": "message", "role": "user", "content": [{"type": "input_text", "text": kickoff}]}}))
        await oa.send(json.dumps({"type": "response.create"}))

        async def on_media(ulaw: bytes) -> None:
            session.record_callee(audioop.ulaw2lin(ulaw, 2))
            await oa.send(json.dumps({"type": "input_audio_buffer.append", "audio": base64.b64encode(ulaw).decode()}))

        async def from_model() -> None:
            configured = False
            async for raw in oa:
                m = json.loads(raw)
                kind = m.get("type")
                if kind == "response.output_audio.delta":
                    if session.twilio_stopped or m.get("item_id") in session.truncated_items:
                        continue  # nobody is listening, or the barge-in already cut this item
                    ulaw = base64.b64decode(m["delta"])
                    session.state.on_agent_audio()
                    session.note_agent_audio(m.get("item_id"), ulaw)
                    session.record_agent(audioop.ulaw2lin(ulaw, 2), 8000)
                    await session.play(ulaw)
                elif kind == "response.output_audio_transcript.done":
                    if session.agent_audible():
                        session.add_turn("agent", m.get("transcript", ""))
                elif kind == "response.done":
                    session.state.on_agent_turn_end()
                    failure = openai_response_failure(m.get("response"))
                    if failure:
                        session.fail(f"openai response {failure}")
                        return
                    # Tool calls are handled here, not on function_call_arguments.done: the follow-up
                    # response.create is refused while the response that made the call is still active.
                    calls = [item for item in (m.get("response") or {}).get("output") or []
                             if item.get("type") == "function_call" and item.get("name") == "end_call"]
                    if calls:
                        if await session.request_end_call():
                            return
                        for call in calls:
                            await oa.send(json.dumps({"type": "conversation.item.create", "item": {
                                "type": "function_call_output", "call_id": call["call_id"],
                                "output": END_CALL_REJECTED}}))
                        await oa.send(json.dumps({"type": "response.create"}))
                elif kind == "conversation.item.input_audio_transcription.completed":
                    if m.get("transcript", "").strip():
                        session.add_turn("callee", m["transcript"])
                        session.state.on_callee_words(m.get("item_id"))
                elif kind == "input_audio_buffer.speech_started":
                    session.state.on_agent_turn_end()
                    session.state.on_callee_turn_start(m.get("item_id"))
                    await session.barge_in()
                    cut = session.played_audio()
                    if cut:  # the model must not believe the callee heard what the clear threw away
                        await oa.send(json.dumps({"type": "conversation.item.truncate", "item_id": cut[0],
                                                  "content_index": 0, "audio_end_ms": cut[1]}))
                elif kind == "session.updated":
                    configured = True
                elif kind == "error":
                    message = (m.get("error") or {}).get("message")
                    log.error("openai error %s", m)
                    session.event(f"openai error {message}")
                    if not configured:  # session.update rejected: don't carry on with a default session
                        raise ProviderError(f"openai rejected the session configuration: {message}")
                    if not openai_error_recoverable(m.get("error")):
                        session.fail(f"openai error {message}")
                        return
            # The loop only ends when the provider closed the socket. Every deliberate end (end_call, a
            # failed response, an error event) returns above, and the time limit or a Twilio stop cancels us.
            if not session.twilio_stopped:
                session.fail("provider closed the session")

        await run_until_first_done(session, pump_twilio(ws, session, on_media), from_model())


# --------------------------------------------------------------------------- server

async def refuse(ws, spec: CallSpec, code: int, reason: str) -> None:
    """Close an authenticated stream without a model session; the driver still gets a failed outcome."""
    log.warning("refused call %s: %s", spec.call_id, reason)
    record_failure(spec.call_dir, reason)
    await ws.close(code, "busy" if code == 1013 else "rejected")


class Bridge:
    def __init__(self, settings: Settings, runners: dict | None = None):
        self.settings = settings
        self.runners = runners or {"gemini": run_gemini, "openai": run_openai}
        self.active_calls = 0

    def process_request(self, connection, request):
        """HTTP stage: health check, and the call's credentials in the path, all before the websocket upgrade."""
        if request.path == "/healthz":
            return connection.respond(http.HTTPStatus.OK, "ok\n")
        if not request.path.startswith("/media/"):
            return connection.respond(http.HTTPStatus.NOT_FOUND, "not found\n")
        try:
            stream_credentials(request.path)  # strict parse first: only a well-formed ASCII path is used at all
            if self.settings.verify_twilio_signature:
                self.check_twilio_signature(request)
            connection.call_spec = load_call(self.settings, request.path, now=time.time())
        except CallRejected as e:
            log.warning("rejected stream: %s", e)
            return connection.respond(http.HTTPStatus.FORBIDDEN, "forbidden\n")
        return None

    def check_twilio_signature(self, request) -> None:
        # Twilio signs the URL it opened, which is the per-call <Stream> URL aicall.py built.
        url = self.settings.public_wss_url.rstrip("/") + request.path.removeprefix("/media")
        want = twilio_signature(self.settings.twilio_auth_token, url)
        got = request.headers.get_all("X-Twilio-Signature")
        if len(got) != 1 or not got[0].isascii() or not hmac.compare_digest(got[0], want):
            raise CallRejected("bad X-Twilio-Signature")

    async def handler(self, ws) -> None:
        spec = ws.call_spec  # authenticated and claimed by process_request
        # No await between the capacity check and the increment, so this cannot race.
        if self.active_calls >= self.settings.max_concurrent_calls:
            await refuse(ws, spec, 1013, f"bridge at capacity ({self.active_calls} calls active)")
            return
        self.active_calls += 1
        try:
            try:
                start = await wait_for_start(ws, self.settings.start_timeout)
            except CallRejected as e:
                await refuse(ws, spec, 1008, str(e))
                return
            await self.serve_call(ws, start, spec)
        finally:
            self.active_calls -= 1

    async def serve_call(self, ws, start: dict, spec: CallSpec) -> None:
        model = str(spec.config.get("model") or DEFAULT_MODEL)
        provider = "openai" if model.startswith("gpt-") else "gemini"
        limit = self.settings.max_call_seconds
        if provider == "gemini":
            limit = min(limit, GEMINI_MAX_CALL_SECONDS)
        session = CallSession(spec, start.get("streamSid", ""), ws, limit, model)
        log.info("call start %s model=%s record=%s", spec.call_id, model, session.record)
        deadline = asyncio.timeout(limit)
        try:
            async with deadline:
                await self.runners[provider](ws, session)
        except Exception as e:  # provider/network failure: keep what we have
            if isinstance(e, TimeoutError) and deadline.expired():
                session.ended("max call duration reached")
            else:  # including a TimeoutError of the provider connection itself
                log.exception("session error")
                session.fail(f"session error {e!r}")
        finally:
            session.save()
            log.info("call end %s %.1fs", spec.call_id, session.elapsed())
            await ws.close()


async def serve_forever(settings: Settings) -> None:
    from websockets.asyncio.server import serve
    bridge = Bridge(settings)
    async with serve(bridge.handler, settings.host, settings.port,
                     process_request=bridge.process_request, max_size=settings.max_message_bytes):
        log.info("listening %s:%d", settings.host, settings.port)
        await asyncio.Future()


def main() -> None:
    if sys.version_info < (3, 11):
        raise SystemExit("server.py needs Python 3.11 or newer (asyncio.timeout)")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.from_env()
    os.umask(0o027)  # call artifacts: the service user and the upload account's shared group only
    settings.calls_dir.mkdir(parents=True, exist_ok=True)
    asyncio.run(serve_forever(settings))


if __name__ == "__main__":
    main()
