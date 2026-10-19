#!/usr/bin/env python3
"""Place an AI phone call.

Twilio dials out and streams the call audio to the bridge on the VPS (server/server.py),
which talks to a realtime voice model. This driver uploads the per-call prompt/config,
creates the call, waits for it to end and fetches the transcript (and, only with
--record, Twilio's recording).

    python3 aicall.py +<E.164 number> prompts.local/my_booking.txt [--voice Aoede] [--model ...]
    python3 aicall.py --resume calls/<call_id>      # fetch results of an earlier call again

Exit codes: 0 the call connected and the bridge reports outcome "ok"; 1 the call did not connect, the
model session failed or the callee never spoke (outcome "failed" / "no_answer"), or a setup error
before anything was dialled; 2 results are not available yet (retry with --resume). Once the call
has been placed, network or Twilio API errors never give 1: polling retries, then exits 2.

Settings come from environment variables (see .env.example). Standard library only.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import http.client
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import quoteattr

TERMINAL_STATUSES = {"completed", "busy", "failed", "no-answer", "canceled"}
HTTP_TIMEOUT_S = 20
SSH_TIMEOUT_S = 60
# The stream token is checked once, when Twilio opens the media stream (after queueing and ringing),
# so its lifetime is a connection-start window, independent of the call duration. Must stay below the
# bridge's max token lifetime (3600 s).
STREAM_START_WINDOW_S = 600
DEFAULT_BRIDGE_MAX_CALL_S = 900  # server default for AICALL_MAX_CALL_SECONDS
GEMINI_MAX_CALL_S = 540  # a Gemini Live session lasts about 10 minutes; same cap as server.GEMINI_MAX_CALL_SECONDS
REPO_ROOT = Path(__file__).resolve().parent
POLL_SLACK_S = 180  # how long past --limit we keep polling Twilio for the call to end
POLL_RETRIES = 6    # consecutive failed status polls (about 2 minutes of backoff) before giving up with exit 2

BASE_SUFFIX = """

Conversation rules (added automatically by aicall.py):
- Keep speaking the language your instructions above specify.
- Before each reply, respond naturally to what the other person just said (laugh along with a joke, answer their question). Never ignore it and jump straight to the next step.
- When the conversation is finished, say a natural goodbye, and only then call the end_call tool."""

CHAT_SUFFIX = """
- Don't rush to hang up. Once the required questions are done, make light, friendly small talk for a minute or two, until the other person wants to go or the chat winds down, then say goodbye."""

RECORDING_SUFFIX = """
- This call is being recorded. In your first sentence, tell the other person that the call may be recorded."""

RECORDING_NOTICE = "This call may be recorded."


class DriverError(Exception):
    """A step of placing or collecting a call failed."""


class TwilioUnreachable(DriverError):
    """No HTTP response came back from Twilio, so whether the request took effect is unknown."""


class TwilioHTTPError(DriverError):
    """Twilio answered with an HTTP error status."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class TwilioBadResponse(DriverError):
    """Twilio answered 2xx with a body that is not the JSON object the API documents."""


def dial_outcome_unknown(error: DriverError) -> bool:
    """True when a failed dial request may still have created the call: no response, a 5xx, or a 2xx
    body that cannot be read. A 4xx is Twilio refusing the call, so nothing was placed."""
    return (isinstance(error, (TwilioUnreachable, TwilioBadResponse))
            or isinstance(error, TwilioHTTPError) and error.status >= 500)


# --------------------------------------------------------------------------- configuration

@dataclass(frozen=True)
class Config:
    vps_ssh: str
    remote_dir: str
    wss_url: str
    out_dir: str
    account_sid: str
    auth_token: str
    from_twilio: str
    from_verified: str
    shared_secret: str
    api_key: str = ""  # a Twilio API key SID (SK...); then auth_token holds that key's secret

    @classmethod
    def from_env(cls, env=None) -> "Config":
        env = os.environ if env is None else env
        api_key = env.get("TWILIO_API_KEY", "")
        required = ["AICALL_VPS_SSH", "PUBLIC_WSS_URL", "TWILIO_ACCOUNT_SID",
                    "TWILIO_API_SECRET" if api_key else "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER",
                    "AICALL_SHARED_SECRET"]
        missing = [name for name in required if not env.get(name)]
        if missing:
            raise DriverError(f"missing environment variables: {', '.join(missing)} (see .env.example)")
        return cls(
            vps_ssh=env["AICALL_VPS_SSH"],
            remote_dir=env.get("AICALL_REMOTE_DIR", "/"),  # as the SFTP-only upload account sees it
            wss_url=env["PUBLIC_WSS_URL"],
            out_dir=env.get("AICALL_OUT_DIR", "calls"),
            account_sid=env["TWILIO_ACCOUNT_SID"],
            auth_token=env["TWILIO_API_SECRET"] if api_key else env["TWILIO_AUTH_TOKEN"],
            api_key=api_key,
            from_twilio=env["TWILIO_FROM_NUMBER"],
            from_verified=env.get("TWILIO_VERIFIED_CALLER_ID", ""),
            shared_secret=env["AICALL_SHARED_SECRET"],
        )


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def bridge_max_call_seconds(env=None) -> int:
    """The bridge's hard cap per call (AICALL_MAX_CALL_SECONDS, same value on both sides; default 900)."""
    env = os.environ if env is None else env
    raw = env.get("AICALL_MAX_CALL_SECONDS", "").strip() or str(DEFAULT_BRIDGE_MAX_CALL_S)
    try:
        value = int(raw)
    except ValueError:
        raise DriverError(f"AICALL_MAX_CALL_SECONDS must be an integer, got {raw!r}") from None
    if value <= 0:
        raise DriverError("AICALL_MAX_CALL_SECONDS must be positive")
    return value


def out_dir_warning(out_dir: str, repo: Path = REPO_ROOT, runner=subprocess.run) -> str | None:
    """Warn when call artifacts would land inside this git checkout without being git-ignored."""
    target = Path(out_dir).resolve()
    try:
        target.relative_to(repo)
    except ValueError:
        return None
    if not (repo / ".git").exists():
        return None
    probe = target / "transcript.json"  # a file inside the directory, so "dir/" patterns match too
    try:
        result = runner(["git", "-C", str(repo), "check-ignore", "-q", "--no-index", str(probe)],
                        capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode == 1:
        return (f"warning: AICALL_OUT_DIR={out_dir} is inside the repository but not git-ignored; "
                "transcripts and recordings could be committed. Add it to .gitignore.")
    return None


# --------------------------------------------------------------------------- pure helpers

def new_call_id(now: float | None = None) -> str:
    """Timestamp for humans plus 48 random bits, so calls started in the same second never collide."""
    return time.strftime("%Y%m%d_%H%M%S", time.gmtime(now)) + "_" + secrets.token_hex(6)


def call_token_signature(secret: str, call_id: str, expires: int) -> str:
    """HMAC-SHA256 over "<call_id>.<expires>". Must match server.call_token_signature."""
    message = f"{call_id}.{expires}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def choose_caller_id(to: str, explicit: str | None, twilio_number: str, verified_number: str) -> str:
    """Default caller ID. An explicit --from always wins.

    Singapore rejects calls that enter from abroad with a local +65 caller ID, so +65
    destinations default to the Twilio-owned number; others use the verified number if set.
    """
    if explicit:
        return explicit
    if to.startswith("+65") or not verified_number:
        return twilio_number
    return verified_number


def build_prompt(base_prompt: str, *, chat: bool, record: bool) -> str:
    prompt = base_prompt.rstrip() + BASE_SUFFIX
    if chat:
        prompt += CHAT_SUFFIX
    if record:
        prompt += RECORDING_SUFFIX
    return prompt


def build_call_config(voice: str, model: str | None, record: bool) -> dict:
    config = {"voice": voice, "record": record}
    if model:
        config["model"] = model
    return config


def stream_url(wss_url: str, call_id: str, expires: int, token: str) -> str:
    """The <Stream> URL. Twilio allows no query string there, so the credentials are path segments that
    the bridge checks before the websocket upgrade."""
    return f"{wss_url.rstrip('/')}/{call_id}/{expires}/{token}"


def build_twiml(stream: str, record: bool = False) -> str:
    # Twilio speaks the notice itself, before the AI is connected, so it is said even if the model forgets.
    notice = f"<Say>{RECORDING_NOTICE}</Say>" if record else ""
    return f"<Response>{notice}<Connect><Stream url={quoteattr(stream)}/></Connect></Response>"


def build_call_params(to: str, from_: str, twiml: str, limit: int, record: bool) -> dict:
    params = {"To": to, "From": from_, "Twiml": twiml, "TimeLimit": str(limit)}
    if record:
        params.update({"Record": "true", "RecordingChannels": "dual"})
    return params


def write_private(path: Path, data: bytes | str) -> None:
    """Write a file readable only by its owner (0600 on POSIX)."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


# --------------------------------------------------------------------------- Twilio REST

def origin(url: str) -> tuple:
    parts = urllib.parse.urlsplit(url)
    return parts.scheme, parts.hostname, parts.port


class KeepCredentialOnHost(urllib.request.HTTPRedirectHandler):
    """urllib copies every request header onto a redirect, Authorization included. Recording downloads
    redirect to a storage host, which must not receive the Twilio credential."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and origin(newurl) != origin(req.full_url):
            new.remove_header("Authorization")
        return new


class TwilioClient:
    def __init__(self, account_sid: str, auth_token: str, timeout: float = HTTP_TIMEOUT_S, opener=None,
                 username: str = ""):
        self.base = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}"
        credential = f"{username or account_sid}:{auth_token}"
        self.auth = "Basic " + base64.b64encode(credential.encode()).decode()
        self.timeout = timeout
        self.opener = opener or urllib.request.build_opener(KeepCredentialOnHost).open

    @classmethod
    def from_config(cls, cfg: Config, opener=None) -> "TwilioClient":
        """With TWILIO_API_KEY set, the API key and its secret sign in instead of the account's auth token."""
        return cls(cfg.account_sid, cfg.auth_token, opener=opener, username=cfg.api_key)

    def _request(self, path: str, data: dict | None = None) -> bytes:
        body = urllib.parse.urlencode(data).encode() if data else None
        req = urllib.request.Request(self.base + path, data=body, headers={"Authorization": self.auth})
        try:
            with self.opener(req, timeout=self.timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode(errors="replace")[:500]
            except (OSError, http.client.HTTPException):
                detail = "(error body unreadable)"
            raise TwilioHTTPError(e.code, f"Twilio {e.code}: {detail}") from None
        except (OSError, http.client.HTTPException) as e:  # URLError, timeouts, resets, truncated bodies
            raise TwilioUnreachable(f"Twilio request failed: {e!r}") from None

    @staticmethod
    def _object(body: bytes) -> dict:
        try:
            value = json.loads(body)
        except ValueError:
            value = None
        if not isinstance(value, dict):
            raise TwilioBadResponse(f"Twilio returned something other than a JSON object: {body[:200]!r}")
        return value

    def get_json(self, path: str) -> dict:
        return self._object(self._request(path))

    def post_json(self, path: str, data: dict) -> dict:
        return self._object(self._request(path, data))

    def download(self, path: str) -> bytes:
        return self._request(path)


def wait_for_call_end(twilio, call_sid: str, max_wait: float, poll: float = 2.0,
                      sleep=time.sleep, clock=time.monotonic) -> dict:
    """Poll the call until it reaches a terminal status or max_wait passes; returns the last status.

    A failed poll is retried with backoff; after POLL_RETRIES failures in a row the DriverError is raised."""
    deadline = clock() + max_wait
    last = None
    failures = 0
    while True:
        try:
            status = twilio.get_json(f"/Calls/{call_sid}.json")
        except DriverError as e:
            failures += 1
            if failures > POLL_RETRIES:
                raise
            print(f"status poll failed ({failures}/{POLL_RETRIES}), retrying: {e}", flush=True)
            sleep(min(30.0, poll * 2 ** failures))
            continue
        failures = 0
        if status.get("status") != last:
            last = status.get("status")
            print(time.strftime("%H:%M:%S"), last, flush=True)
        if last in TERMINAL_STATUSES or clock() >= deadline:
            return status
        sleep(poll)


def wait_for_recording(twilio, call_sid: str, attempts: int = 20, delay: float = 3.0,
                       sleep=time.sleep) -> str | None:
    """Return the sid of a *completed* recording, or None if none completes in time."""
    for attempt in range(attempts):
        recordings = twilio.get_json(f"/Calls/{call_sid}/Recordings.json").get("recordings", [])
        for rec in recordings:
            if rec.get("status") == "completed":
                return rec["sid"]
        if attempt < attempts - 1:
            sleep(delay)
    return None


# --------------------------------------------------------------------------- VPS transfer

def run_checked(cmd: list[str], runner=subprocess.run, **kwargs) -> None:
    try:
        result = runner(cmd, capture_output=True, text=True, timeout=SSH_TIMEOUT_S, **kwargs)
    except subprocess.TimeoutExpired:
        raise DriverError(f"timed out: {' '.join(cmd[:2])}") from None
    if result.returncode != 0:
        raise DriverError(f"{cmd[0]} failed ({result.returncode}): {(result.stderr or '').strip()[:300]}")


def remote_call_dir(cfg: Config, call_id: str) -> str:
    return f"{cfg.remote_dir.rstrip('/')}/calls/{call_id}"


def sftp(cfg: Config, commands: list[str], local_dir: Path, runner=subprocess.run) -> None:
    """Run SFTP batch commands on the VPS. The upload account allows SFTP only (no shell), and local
    paths are relative to local_dir, so a path with spaces never needs quoting for sftp."""
    run_checked(["sftp", "-q", "-b", "-", "-o", "BatchMode=yes", cfg.vps_ssh], runner,
                input="\n".join(commands) + "\n", cwd=str(local_dir))


def upload_call_files(cfg: Config, call_id: str, local_dir: Path, runner=subprocess.run) -> None:
    remote = remote_call_dir(cfg, call_id)
    commands = [f'mkdir "{remote}"', f'chmod 770 "{remote}"']  # the bridge writes its results here
    for name in ("prompt.txt", "config.json"):  # 640: the bridge reads them through the shared group
        commands += [f'put {name} "{remote}/{name}"', f'chmod 640 "{remote}/{name}"']
    sftp(cfg, commands, local_dir, runner)


def fetch_results(cfg: Config, call_id: str, local_dir: Path, attempts: int = 5, delay: float = 3.0,
                  runner=subprocess.run, sleep=time.sleep) -> tuple[dict, dict] | None:
    """Copy outcome.json and transcript.json from the VPS, retrying while the bridge finishes writing them.

    Returns (transcript, outcome), or None if they are still missing after the last attempt."""
    remote = remote_call_dir(cfg, call_id)
    names = ("outcome.json", "transcript.json")
    for attempt in range(attempts):
        try:
            sftp(cfg, [f'get "{remote}/{name}" {name}' for name in names], local_dir, runner)
            for name in names:  # sftp copies the remote mode (group-readable on the VPS); keep them private here
                os.chmod(local_dir / name, 0o600)
            transcript = json.loads((local_dir / "transcript.json").read_text(encoding="utf-8"))
            outcome = json.loads((local_dir / "outcome.json").read_text(encoding="utf-8"))
            return transcript, outcome
        except (DriverError, OSError, ValueError) as e:
            print(f"transcript not available yet ({attempt + 1}/{attempts}): {e}", flush=True)
        if attempt < attempts - 1:
            sleep(delay)
    return None


# --------------------------------------------------------------------------- results

def print_transcript(transcript: dict) -> None:
    for ts, speaker, text in transcript.get("conv", []):
        print(f"[{ts}s] {speaker}: {text.strip()}")
    print("events", transcript.get("events"))


def download_recording(twilio, call_sid: str, local_dir: Path) -> bool:
    """Save Twilio's recording as call.mp3 once it has completed; False if it is not available (yet)."""
    try:
        rec_sid = wait_for_recording(twilio, call_sid)
        if rec_sid is None:
            print("recording not completed; nothing downloaded")
            return False
        write_private(local_dir / "call.mp3", twilio.download(f"/Recordings/{rec_sid}.mp3"))
    except (DriverError, OSError) as e:
        print(f"recording not downloaded: {e}")
        return False
    print("REC", local_dir / "call.mp3")
    return True


def collect_results(cfg: Config, twilio, meta: dict, local_dir: Path) -> int:
    """Wait for a placed call to end and fetch its results. Exit 1 only when the call or its session
    definitely failed: a bot treats 1 as "not connected" and may dial again."""
    retry = f"python3 aicall.py --resume {local_dir}"
    try:
        status = wait_for_call_end(twilio, meta["call_sid"], meta["limit"] + POLL_SLACK_S)
    except DriverError as e:
        print(f"lost contact with Twilio ({e}); the call may still be live. Fetch later with: {retry}")
        return 2
    print("duration", status.get("duration"), "price", status.get("price"), status.get("price_unit"))
    if status.get("status") not in TERMINAL_STATUSES:
        print(f"call still {status.get('status')}; fetch later with: {retry}")
        return 2
    if status["status"] != "completed":
        print("call did not connect:", status["status"])
        return 1

    exit_code = 0
    results = fetch_results(cfg, meta["call_id"], local_dir)
    if results is None:
        print(f"transcript missing; retry with: {retry}")
        exit_code = 2
    else:
        transcript, outcome = results
        print_transcript(transcript)
        status = outcome.get("status") if isinstance(outcome, dict) else None
        print("outcome", status, "-", outcome.get("reason") if isinstance(outcome, dict) else "")
        if status != "ok":  # "failed" (model/provider error, no audio) or "no_answer" (callee never spoke)
            exit_code = 1

    if meta.get("record") and not download_recording(twilio, meta["call_sid"], local_dir):
        exit_code = exit_code or 2
    print("DIR", local_dir)
    return exit_code


def read_prompt(path: str) -> str:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, ValueError) as e:  # missing, unreadable, not UTF-8
        raise DriverError(f"cannot read prompt file {path}: {e}") from None
    if not text.strip():
        raise DriverError(f"prompt file {path} is empty")
    return text


def place_call(args, cfg: Config, twilio) -> int:
    # Everything that can be checked locally is checked before anything is created, uploaded or paid for.
    bridge_max = bridge_max_call_seconds()
    if args.limit > bridge_max:
        raise DriverError(f"--limit {args.limit} exceeds the bridge's AICALL_MAX_CALL_SECONDS ({bridge_max}); "
                          "the bridge would cut the call off earlier")
    if not (args.model or "").startswith("gpt-") and args.limit > GEMINI_MAX_CALL_S:
        raise DriverError(f"--limit {args.limit} is too long for a Gemini model: a Gemini Live session lasts "
                          f"about 10 minutes and the bridge ends Gemini calls at {GEMINI_MAX_CALL_S} s")
    base_prompt = read_prompt(args.prompt)
    warning = out_dir_warning(cfg.out_dir)
    if warning:
        print(warning, file=sys.stderr, flush=True)
    call_id = new_call_id()
    local_dir = Path(cfg.out_dir) / call_id
    local_dir.mkdir(parents=True, exist_ok=False)

    write_private(local_dir / "prompt.txt", build_prompt(base_prompt, chat=args.chat, record=args.record))
    write_private(local_dir / "config.json", json.dumps(build_call_config(args.voice, args.model, args.record)))
    upload_call_files(cfg, call_id, local_dir)

    expires = int(time.time()) + STREAM_START_WINDOW_S
    token = call_token_signature(cfg.shared_secret, call_id, expires)
    caller_id = choose_caller_id(args.to, args.from_, cfg.from_twilio, cfg.from_verified)
    print("FROM", caller_id, "TO", args.to, "RECORD", args.record, flush=True)

    twiml = build_twiml(stream_url(cfg.wss_url, call_id, expires, token), record=args.record)
    try:
        call = twilio.post_json("/Calls.json", build_call_params(args.to, caller_id, twiml, args.limit, args.record))
        if not isinstance(call.get("sid"), str):
            raise TwilioBadResponse(f"Twilio's reply to the dial request has no call sid: {str(call)[:200]}")
    except DriverError as e:
        if not dial_outcome_unknown(e):  # a 4xx: Twilio refused, nothing was placed
            raise
        # the request may have reached Twilio and the phone may be ringing
        print(f"error: {e}\nthe call may have been placed: check the Twilio call log for {args.to} "
              f"before calling again (call_id {call_id})", file=sys.stderr)
        return 3
    print("CALL", call["sid"], call.get("status"), flush=True)

    meta = {"call_id": call_id, "call_sid": call["sid"], "to": args.to, "from": caller_id,
            "record": args.record, "limit": args.limit, "model": args.model}
    try:
        write_private(local_dir / "call.json", json.dumps(meta, indent=1))
    except OSError as e:  # the call is live: keep collecting, only --resume is lost
        print(f"warning: could not save call.json ({e}); --resume will not work for this call", flush=True)
    return collect_results(cfg, twilio, meta, local_dir)


def resume(args, cfg: Config, twilio) -> int:
    local_dir = Path(args.resume)
    try:
        meta = json.loads((local_dir / "call.json").read_text(encoding="utf-8"))
        if not (isinstance(meta, dict) and isinstance(meta.get("call_sid"), str) and meta.get("call_id")
                and isinstance(meta.get("limit"), (int, float))):
            raise ValueError("not a call record")
    except (OSError, ValueError) as e:  # a call was attempted earlier; without its record we cannot say whether it was placed
        print(f"error: cannot read {local_dir / 'call.json'} ({e}); the call may have been placed: "
              f"check the Twilio call log before calling again", file=sys.stderr)
        return 3
    return collect_results(cfg, twilio, meta, local_dir)


# --------------------------------------------------------------------------- CLI

def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Place an AI phone call via Twilio and a realtime voice model.",
                                 epilog="Settings are read from environment variables; see .env.example.")
    ap.add_argument("to", nargs="?", help="destination number in E.164 format, e.g. +12025550100")
    ap.add_argument("prompt", nargs="?", help="prompt file (keep personal ones in prompts.local/)")
    ap.add_argument("--voice", default="Aoede", help="Gemini voice, or an OpenAI voice for gpt-* models")
    ap.add_argument("--model", help="realtime model (default on the server: gemini-3.1-flash-live-preview)")
    ap.add_argument("--from", dest="from_", help="caller ID; overrides the automatic choice")
    ap.add_argument("--limit", type=int, default=300,
                    help="hard call time limit in seconds (default 300); must not exceed the bridge's "
                         f"AICALL_MAX_CALL_SECONDS (default 900), nor {GEMINI_MAX_CALL_S} for Gemini models")
    ap.add_argument("--record", action=argparse.BooleanOptionalAction, default=env_flag("AICALL_RECORD"),
                    help="record audio: Twilio dual-channel recording AND bridge-side wavs "
                         "(default off, or AICALL_RECORD=1)")
    ap.add_argument("--chat", action=argparse.BooleanOptionalAction, default=False,
                    help="ask the agent to make a minute or two of small talk before hanging up (default off)")
    ap.add_argument("--resume", metavar="CALL_DIR", help="fetch transcript/recording of an earlier call")
    args = ap.parse_args(argv)
    if not args.resume and not (args.to and args.prompt):
        ap.error("to and prompt are required (or use --resume CALL_DIR)")
    if args.limit <= 0:
        ap.error("--limit must be positive")
    return args


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = parse_args(argv)
    try:
        cfg = Config.from_env()
        twilio = TwilioClient.from_config(cfg)
        return resume(args, cfg, twilio) if args.resume else place_call(args, cfg, twilio)
    except (DriverError, OSError, ValueError) as e:  # collect_results handles its own errors once a call exists
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
