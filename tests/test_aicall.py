"""Offline tests for the local driver. No Twilio, SSH or network: everything is faked."""
import base64
import functools
import http.client
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import pytest

import aicall
import server
from conftest import ROOT, SECRET


def make_config(tmp_path, **overrides):
    values = dict(vps_ssh="aicall-upload@vps.example", remote_dir="/", wss_url="wss://vps.example/media",
                  out_dir=str(tmp_path / "calls"), account_sid="AC" + "0" * 32, auth_token="tok",
                  from_twilio="+12025550100", from_verified="+12025550111", shared_secret=SECRET)
    values.update(overrides)
    return aicall.Config(**values)


class FakeTwilio:
    def __init__(self, call_status="completed", recordings=(), poll_errors=(), download_error=None, post_error=None):
        self.call_status = call_status
        self.post_error = post_error
        self.recordings = list(recordings)
        self.poll_errors = list(poll_errors)  # raised, in order, by the next call-status polls
        self.download_error = download_error
        self.posts = []
        self.gets = []
        self.downloads = []

    def post_json(self, path, data):
        self.posts.append((path, data))
        if self.post_error:
            raise self.post_error
        return {"sid": "CA" + "1" * 32, "status": "queued"}

    def get_json(self, path):
        self.gets.append(path)
        if path.endswith("/Recordings.json"):
            return {"recordings": self.recordings}
        if self.poll_errors:
            raise self.poll_errors.pop(0)
        return {"status": self.call_status, "duration": "42", "price": None, "price_unit": "USD"}

    def download(self, path):
        self.downloads.append(path)
        if self.download_error:
            raise self.download_error
        return b"ID3fake-mp3"


def no_sleep(_):
    return None


OK = {"status": "ok", "reason": "end_call"}


def results(conv=(), outcome=OK):
    """Stand-in for fetch_results: (transcript, outcome)."""
    return lambda c, call_id, d: ({"conv": list(conv), "events": []}, outcome)


# --------------------------------------------------------------------------- CLI

def test_help_works_without_environment():
    env = {k: v for k, v in os.environ.items() if k in ("SYSTEMROOT", "PATH", "HOME", "USERPROFILE")}
    result = subprocess.run([sys.executable, str(ROOT / "aicall.py"), "--help"], capture_output=True, text=True,
                            env=env, timeout=30)
    assert result.returncode == 0 and "--record" in result.stdout


def test_missing_environment_is_a_clean_error(monkeypatch, capsys):
    for name in ("AICALL_VPS_SSH", "PUBLIC_WSS_URL", "TWILIO_ACCOUNT_SID", "AICALL_SHARED_SECRET"):
        monkeypatch.delenv(name, raising=False)
    assert aicall.main(["+12025550100", "prompts/riddle_en.txt"]) == 1
    assert "missing environment variables" in capsys.readouterr().err


def test_record_and_chat_default_off(monkeypatch):
    monkeypatch.delenv("AICALL_RECORD", raising=False)
    args = aicall.parse_args(["+12025550100", "p.txt"])
    assert args.record is False and args.chat is False
    monkeypatch.setenv("AICALL_RECORD", "1")
    assert aicall.parse_args(["+12025550100", "p.txt"]).record is True
    assert aicall.parse_args(["+12025550100", "p.txt", "--no-record"]).record is False


# --------------------------------------------------------------------------- call ids and tokens

def test_overlapping_call_ids_are_unique(monkeypatch):
    fixed = time.time()
    ids = {aicall.new_call_id(fixed) for _ in range(2000)}  # same second, as if launched in parallel
    assert len(ids) == 2000
    assert all(server.CALL_ID_RE.fullmatch(i) for i in ids)


def test_driver_token_matches_server():
    call_id = aicall.new_call_id()
    expires = int(time.time()) + 900
    token = aicall.call_token_signature(SECRET, call_id, expires)
    assert token == server.call_token_signature(SECRET, call_id, expires)


def stream_of(twiml):
    return ET.fromstring(twiml).find("./Connect/Stream")


def test_stream_url_carries_the_credentials_in_its_path():
    """Twilio allows no query string in <Stream url>, so call id, expiry and token are path segments."""
    call_id, expires = aicall.new_call_id(), int(time.time()) + 600
    token = aicall.call_token_signature(SECRET, call_id, expires)
    url = aicall.stream_url("wss://h.example/media/", call_id, expires, token)
    stream = stream_of(aicall.build_twiml(url))
    assert stream.get("url") == f"wss://h.example/media/{call_id}/{expires}/{token}"
    assert "?" not in stream.get("url") and stream.findall("Parameter") == []


# --------------------------------------------------------------------------- caller ID

SG_MOBILE = "+65" + "8" * 8  # any Singapore number


def test_caller_id_rules():
    assert aicall.choose_caller_id(SG_MOBILE, None, "+12025550100", "+12025550111") == "+12025550100"
    assert aicall.choose_caller_id("+12025550199", None, "+12025550100", "+12025550111") == "+12025550111"
    assert aicall.choose_caller_id("+12025550199", None, "+12025550100", "") == "+12025550100"
    assert aicall.choose_caller_id(SG_MOBILE, "+12025550122", "+12025550100", "+12025550111") == "+12025550122"


# --------------------------------------------------------------------------- prompt suffix

def test_chat_suffix_optional_and_language_neutral():
    plain = aicall.build_prompt("Base prompt.", chat=False, record=False)
    chatty = aicall.build_prompt("Base prompt.", chat=True, record=False)
    assert aicall.CHAT_SUFFIX not in plain and aicall.CHAT_SUFFIX in chatty
    assert "end_call" in plain
    for suffix in (aicall.BASE_SUFFIX, aicall.CHAT_SUFFIX, aicall.RECORDING_SUFFIX):
        assert suffix.isascii()  # no hard-coded Cantonese in a suffix appended to every prompt


def test_recording_disclosure_only_when_recording():
    assert aicall.RECORDING_SUFFIX not in aicall.build_prompt("p", chat=False, record=False)
    assert aicall.RECORDING_SUFFIX in aicall.build_prompt("p", chat=False, record=True)


# --------------------------------------------------------------------------- recording opt-out

def test_record_off_sends_no_record_param():
    params = aicall.build_call_params("+12025550199", "+12025550100", "<Response/>", 300, record=False)
    assert "Record" not in params and "RecordingChannels" not in params
    on = aicall.build_call_params("+12025550199", "+12025550100", "<Response/>", 300, record=True)
    assert on["Record"] == "true" and on["RecordingChannels"] == "dual"
    assert aicall.build_call_config("Aoede", None, False) == {"voice": "Aoede", "record": False}


def test_place_call_without_recording(tmp_path, monkeypatch):
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Say hello.", encoding="utf-8")
    cfg = make_config(tmp_path)
    uploads = []
    monkeypatch.setattr(aicall, "upload_call_files", lambda c, call_id, d: uploads.append(call_id))
    monkeypatch.setattr(aicall, "fetch_results", results([[0.5, "agent", "Hi"]]))
    twilio = FakeTwilio()
    args = aicall.parse_args(["+12025550199", str(prompt), "--no-record"])

    assert aicall.place_call(args, cfg, twilio) == 0
    (path, data), = twilio.posts
    assert "Record" not in data
    call_dir = tmp_path / "calls" / uploads[0]
    assert json.loads((call_dir / "config.json").read_text())["record"] is False
    assert json.loads((call_dir / "call.json").read_text())["call_sid"].startswith("CA")
    assert not (call_dir / "call.mp3").exists()
    assert not any(p.endswith("/Recordings.json") for p in twilio.gets) and twilio.downloads == []
    assert ET.fromstring(data["Twiml"]).find("Say") is None


@pytest.mark.parametrize("record", [True, False])
def test_recorded_call_starts_with_a_fixed_spoken_notice(tmp_path, monkeypatch, record):
    """With --record, Twilio itself says the notice before the AI is connected; the model may forget."""
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Say hello.", encoding="utf-8")
    monkeypatch.setattr(aicall, "upload_call_files", lambda *a: None)
    monkeypatch.setattr(aicall, "fetch_results", results())
    monkeypatch.setattr(aicall, "download_recording", lambda *a: True)
    twilio = FakeTwilio()
    flag = "--record" if record else "--no-record"
    aicall.place_call(aicall.parse_args(["+12025550199", str(prompt), flag]), make_config(tmp_path), twilio)
    (_, data), = twilio.posts
    verbs = [(child.tag, (child.text or "").strip()) for child in ET.fromstring(data["Twiml"])]
    if record:
        assert verbs[0][0] == "Say" and "recorded" in verbs[0][1] and verbs[1][0] == "Connect"
    else:
        assert [tag for tag, _ in verbs] == ["Connect"]


# --------------------------------------------------------------------------- transfers and missing artifacts

def fake_runner(results):
    """Stands in for subprocess.run: records (cmd, kwargs); a successful sftp batch with "get" lines
    leaves the fetched files in its working directory, like sftp does."""
    calls = []

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        assert kwargs.get("timeout")  # every transfer is bounded
        code = results.pop(0) if results else 1
        if code == 0 and cmd[0] == "sftp" and "get " in kwargs.get("input", ""):
            dest = Path(kwargs["cwd"])
            (dest / "transcript.json").write_text(json.dumps({"conv": [], "events": []}), encoding="utf-8")
            (dest / "outcome.json").write_text(json.dumps(OK), encoding="utf-8")
        return SimpleNamespace(returncode=code, stderr="no such file" if code else "")

    run.calls = calls
    return run


def test_upload_uses_only_sftp_inside_the_upload_jail(tmp_path):
    """The upload account may use SFTP only, chrooted at the data directory: no remote shell command."""
    runner = fake_runner([0])
    cfg = make_config(tmp_path)
    aicall.upload_call_files(cfg, "id", tmp_path, runner=runner)
    (cmd, kwargs), = runner.calls
    assert cmd[0] == "sftp" and cmd[-1] == cfg.vps_ssh and kwargs["cwd"] == str(tmp_path)
    batch = kwargs["input"].splitlines()
    assert batch[0] == 'mkdir "/calls/id"'
    for name in ("prompt.txt", "config.json"):
        assert f'put {name} "/calls/id/{name}"' in batch
        assert f'chmod 640 "/calls/id/{name}"' in batch  # the bridge reads it through the shared group


def test_failed_upload_raises(tmp_path):
    runner = fake_runner([1])
    with pytest.raises(aicall.DriverError, match="sftp failed"):
        aicall.upload_call_files(make_config(tmp_path), "id", tmp_path, runner=runner)


def test_missing_transcript_returns_none_after_bounded_retries(tmp_path):
    runner = fake_runner([])
    sleeps = []
    result = aicall.fetch_results(make_config(tmp_path), "id", tmp_path, attempts=4, delay=1,
                                  runner=runner, sleep=sleeps.append)
    assert result is None and len(runner.calls) == 4 and sleeps == [1, 1, 1]


def test_transcript_fetched_after_retry(tmp_path):
    runner = fake_runner([1, 0])
    result = aicall.fetch_results(make_config(tmp_path), "id", tmp_path, runner=runner, sleep=no_sleep)
    assert result == ({"conv": [], "events": []}, OK) and len(runner.calls) == 2
    cmd, kwargs = runner.calls[-1]
    assert cmd[0] == "sftp" and kwargs["cwd"] == str(tmp_path)
    assert kwargs["input"].splitlines() == ['get "/calls/id/outcome.json" outcome.json',
                                             'get "/calls/id/transcript.json" transcript.json']
    if os.name == "posix":
        assert all((tmp_path / n).stat().st_mode & 0o077 == 0 for n in ("outcome.json", "transcript.json"))


def test_incomplete_recording_is_not_downloaded(tmp_path, monkeypatch):
    monkeypatch.setattr(aicall, "wait_for_recording", functools.partial(aicall.wait_for_recording, sleep=no_sleep))
    monkeypatch.setattr(aicall, "fetch_results", results())
    twilio = FakeTwilio(recordings=[{"sid": "RE1", "status": "processing"}])
    meta = {"call_id": "id", "call_sid": "CA1", "record": True, "limit": 60}
    tmp_path.mkdir(exist_ok=True)

    assert aicall.collect_results(make_config(tmp_path), twilio, meta, tmp_path) == 2
    assert twilio.downloads == [] and not (tmp_path / "call.mp3").exists()


def test_completed_recording_is_downloaded(tmp_path, monkeypatch):
    monkeypatch.setattr(aicall, "fetch_results", results())
    twilio = FakeTwilio(recordings=[{"sid": "RE1", "status": "completed"}])
    meta = {"call_id": "id", "call_sid": "CA1", "record": True, "limit": 60}
    assert aicall.collect_results(make_config(tmp_path), twilio, meta, tmp_path) == 0
    assert twilio.downloads == ["/Recordings/RE1.mp3"] and (tmp_path / "call.mp3").read_bytes() == b"ID3fake-mp3"


def test_missing_transcript_reports_resume(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(aicall, "fetch_results", lambda c, call_id, d: None)
    meta = {"call_id": "id", "call_sid": "CA1", "record": False, "limit": 60}
    assert aicall.collect_results(make_config(tmp_path), FakeTwilio(), meta, tmp_path) == 2
    assert "--resume" in capsys.readouterr().out


@pytest.mark.parametrize("outcome, code", [
    ({"status": "ok", "reason": "end_call"}, 0),
    ({"status": "failed", "reason": "session error ProviderError('invalid voice')"}, 1),
    ({"status": "failed", "reason": "the model produced no audio"}, 1),
    ({"status": "no_answer", "reason": "the callee never said anything"}, 1),
    ({}, 1),
    (["not", "an", "object"], 1),
])
def test_session_outcome_sets_exit_code(tmp_path, monkeypatch, capsys, outcome, code):
    """A fetched transcript is not success by itself: the bridge's outcome decides."""
    monkeypatch.setattr(aicall, "fetch_results", results(outcome=outcome))
    meta = {"call_id": "id", "call_sid": "CA1", "record": False, "limit": 60}
    assert aicall.collect_results(make_config(tmp_path), FakeTwilio(), meta, tmp_path) == code
    assert "outcome" in capsys.readouterr().out


def test_failed_session_wins_over_missing_recording(tmp_path, monkeypatch):
    monkeypatch.setattr(aicall, "wait_for_recording", lambda *a, **k: None)
    monkeypatch.setattr(aicall, "fetch_results", results(outcome={"status": "failed", "reason": "x"}))
    meta = {"call_id": "id", "call_sid": "CA1", "record": True, "limit": 60}
    assert aicall.collect_results(make_config(tmp_path), FakeTwilio(), meta, tmp_path) == 1


def test_call_polling_is_bounded():
    clock = iter(range(0, 1000, 10))
    status = aicall.wait_for_call_end(FakeTwilio(call_status="in-progress"), "CA1", max_wait=30,
                                      sleep=no_sleep, clock=lambda: next(clock))
    assert status["status"] == "in-progress"


def test_twilio_client_uses_timeout():
    seen = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"ok": true}'

    def opener(req, timeout):
        seen["timeout"] = timeout
        return Resp()

    client = aicall.TwilioClient("AC1", "tok", timeout=7, opener=opener)
    assert client.get_json("/Calls/CA1.json") == {"ok": True} and seen["timeout"] == 7


# --------------------------------------------------------------------------- duration vs token lifetime

def test_token_expiry_is_a_start_window_not_the_call_length(tmp_path, monkeypatch):
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Say hello.", encoding="utf-8")
    monkeypatch.setenv("AICALL_MAX_CALL_SECONDS", "3600")
    monkeypatch.setattr(aicall, "upload_call_files", lambda c, call_id, d: None)
    monkeypatch.setattr(aicall, "fetch_results", results())
    twilio = FakeTwilio()
    before = int(time.time())
    args = aicall.parse_args(["+12025550199", str(prompt), "--limit", "3600", "--model", "gpt-realtime-2.1"])
    aicall.place_call(args, make_config(tmp_path), twilio)
    (_, data), = twilio.posts
    path = urllib.parse.urlsplit(stream_of(data["Twiml"]).get("url")).path
    _, _, call_id, expires, _ = path.split("/")
    assert int(expires) - before <= aicall.STREAM_START_WINDOW_S + 5
    assert data["TimeLimit"] == "3600"
    # the bridge accepts the URL Twilio was given, whatever the call length
    settings = server.Settings(calls_dir=tmp_path / "calls", shared_secret=SECRET)
    assert server.load_call(settings, path, now=time.time()).call_id == call_id


def test_limit_above_bridge_max_rejected_before_anything_happens(tmp_path, monkeypatch):
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Say hello.", encoding="utf-8")
    monkeypatch.delenv("AICALL_MAX_CALL_SECONDS", raising=False)
    monkeypatch.setattr(aicall, "upload_call_files", lambda *a: pytest.fail("uploaded"))
    twilio = FakeTwilio()
    args = aicall.parse_args(["+12025550199", str(prompt), "--limit", "901"])
    with pytest.raises(aicall.DriverError, match="AICALL_MAX_CALL_SECONDS"):
        aicall.place_call(args, make_config(tmp_path), twilio)
    assert twilio.posts == [] and not (tmp_path / "calls").exists()


@pytest.mark.parametrize("extra, refused", [
    (["--limit", "540"], False),
    (["--limit", "541"], True),
    (["--limit", "541", "--model", "gemini-2.5-flash-native-audio-preview"], True),
    (["--limit", "900", "--model", "gpt-realtime-2.1"], False),
], ids=["gemini-540", "gemini-541", "other-gemini-541", "openai-900"])
def test_gemini_calls_fit_one_live_session(driver, tmp_path, extra, refused):
    """Gemini Live sessions end after about 10 minutes and the bridge does not resume them: a longer
    --limit for a Gemini model is refused before anything is uploaded or dialled."""
    twilio = FakeTwilio()
    code = driver(twilio, ["+12025550199", str(tmp_path / "prompt.txt"), *extra])
    assert (code, len(twilio.posts)) == ((1, 0) if refused else (0, 1))


def test_bridge_max_call_seconds_from_env():
    assert aicall.bridge_max_call_seconds({}) == 900
    assert aicall.bridge_max_call_seconds({"AICALL_MAX_CALL_SECONDS": "1200"}) == 1200
    for bad in ("abc", "0"):
        with pytest.raises(aicall.DriverError):
            aicall.bridge_max_call_seconds({"AICALL_MAX_CALL_SECONDS": bad})


# --------------------------------------------------------------------------- output directory inside the repo

def test_out_dir_warning_when_inside_repo_and_not_ignored(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    seen = []

    def runner(cmd, **kwargs):
        seen.append(cmd)
        return SimpleNamespace(returncode=1)  # git check-ignore: not ignored

    warning = aicall.out_dir_warning(str(repo / "my_calls"), repo=repo, runner=runner)
    assert warning and "not git-ignored" in warning
    assert seen[0][:4] == ["git", "-C", str(repo), "check-ignore"]
    ignored = aicall.out_dir_warning(str(repo / "calls"), repo=repo,
                                     runner=lambda cmd, **kw: SimpleNamespace(returncode=0))
    assert ignored is None


def test_out_dir_outside_repo_is_not_checked(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    assert aicall.out_dir_warning(str(tmp_path / "elsewhere"), repo=repo,
                                  runner=lambda *a, **k: pytest.fail("git called")) is None


def test_default_out_dir_is_ignored_by_this_repo():
    if not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    assert aicall.out_dir_warning(str(ROOT / "calls")) is None


@pytest.mark.parametrize("path, ignored", [
    (".env", True), (".env.local", True), (".env.vps", True), (".env.example", False),
    ("sim/prompt.txt", True), ("sim/config.json", True), ("clips/u1.pcm", True), ("clips/notes.txt", True),
])
def test_secrets_and_working_files_are_git_ignored(path, ignored):
    """.env copies and the sim/ and clips/ folders that docs/deployment.md creates must never be committed."""
    if not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    result = subprocess.run(["git", "-C", str(ROOT), "check-ignore", "-q", "--no-index", path],
                            capture_output=True, timeout=10)
    assert (result.returncode == 0) == ignored


def test_example_phone_numbers_are_valid_e164():
    """Docs, examples and tests use fictional +1 202 555 01xx numbers: valid E.164, never a real line."""
    files = [ROOT / "README.md", ROOT / "aicall.py", *(ROOT / "examples").iterdir(), *(ROOT / "tests").glob("*.py")]
    numbers = {n for f in files for n in re.findall(r"(?<!\\)\+1\d{6,}", f.read_text(encoding="utf-8"))}
    assert numbers and all(re.fullmatch(r"\+1202555\d{4}", n) for n in numbers), sorted(numbers)


# --------------------------------------------------------------------------- exit codes: never 1 once the call exists

@pytest.fixture
def driver(tmp_path, monkeypatch):
    """aicall.main with Twilio, SSH and sleeping faked; call driver(twilio, argv) to run it."""
    env = {"AICALL_VPS_SSH": "aicall-upload@vps.example", "PUBLIC_WSS_URL": "wss://vps.example/media",
           "TWILIO_ACCOUNT_SID": "AC" + "0" * 32, "TWILIO_AUTH_TOKEN": "tok", "TWILIO_FROM_NUMBER": "+12025550100",
           "AICALL_SHARED_SECRET": SECRET, "AICALL_OUT_DIR": str(tmp_path / "calls")}
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("AICALL_MAX_CALL_SECONDS", raising=False)
    monkeypatch.setattr(aicall, "upload_call_files", lambda *a, **k: None)
    monkeypatch.setattr(aicall, "fetch_results", results())
    monkeypatch.setattr(aicall, "wait_for_call_end", functools.partial(aicall.wait_for_call_end, sleep=no_sleep))
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Say hello.", encoding="utf-8")

    def run(twilio, argv=None):
        monkeypatch.setattr(aicall, "TwilioClient", SimpleNamespace(from_config=lambda cfg: twilio))
        return aicall.main(argv or ["+12025550199", str(prompt)])

    run.out_dir = tmp_path / "calls"
    return run


def poll_failure():
    return aicall.DriverError("Twilio request failed: timed out")


def test_transient_poll_errors_are_retried(driver):
    twilio = FakeTwilio(poll_errors=[poll_failure(), poll_failure()])
    assert driver(twilio) == 0
    assert len(twilio.posts) == 1


def test_lost_contact_with_a_placed_call_exits_2_not_1(driver, capsys):
    """Exit 1 tells a bot the call did not connect, and it redials a person who may still be on the line."""
    twilio = FakeTwilio(poll_errors=[poll_failure()] * 100)
    assert driver(twilio) == 2
    assert "--resume" in capsys.readouterr().out and len(twilio.posts) == 1


def test_dial_request_that_gets_no_answer_exits_3_not_1(driver, capsys):
    """Twilio may have placed the call before the response was lost; exit 1 would invite a redial."""
    twilio = FakeTwilio(post_error=aicall.TwilioUnreachable("Twilio request failed: TimeoutError('timed out')"))
    assert driver(twilio) == 3
    assert "may have been placed" in capsys.readouterr().err and len(twilio.posts) == 1


def test_dial_refused_by_twilio_is_a_definite_failure(driver):
    twilio = FakeTwilio(post_error=aicall.DriverError("Twilio 400: invalid To number"))
    assert driver(twilio) == 1


def test_error_response_whose_body_cannot_be_read_is_a_driver_error():
    class Body(io.BytesIO):
        def read(self, *a):
            raise TimeoutError("timed out")

    def opener(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 503, "Service Unavailable", {}, Body())

    with pytest.raises(aicall.DriverError, match="Twilio 503"):
        aicall.TwilioClient("AC1", "tok", opener=opener).get_json("/Calls/CA1.json")


@pytest.mark.parametrize("error", [
    lambda: http.client.RemoteDisconnected("Remote end closed connection"),
    lambda: ConnectionResetError(104, "Connection reset by peer"),
    lambda: TimeoutError("timed out"),
    lambda: urllib.error.URLError("no route"),
    lambda: http.client.IncompleteRead(b"{"),
], ids=["remote-disconnected", "connection-reset", "timeout", "url-error", "incomplete-read"])
def test_twilio_client_turns_transport_errors_into_driver_errors(error):
    def opener(req, timeout):
        raise error()

    with pytest.raises(aicall.DriverError):
        aicall.TwilioClient("AC1", "tok", opener=opener).get_json("/Calls/CA1.json")


@pytest.mark.parametrize("body", [b"<html>bad gateway</html>", b'{"status": "comp', b"[1, 2]"])
def test_twilio_client_rejects_bodies_that_are_not_a_json_object(body):
    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return body

    with pytest.raises(aicall.DriverError):
        aicall.TwilioClient("AC1", "tok", opener=lambda req, timeout: Resp()).get_json("/Calls/CA1.json")


@pytest.mark.parametrize("failure", [
    {"download_error": aicall.DriverError("Twilio request failed: reset")},
    {"recordings": [{"sid": "RE1", "status": "completed"}], "download_error": OSError(28, "No space left")},
], ids=["download-fails", "saving-fails"])
def test_recording_failure_after_an_ok_call_exits_2(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(aicall, "fetch_results", results())
    twilio = FakeTwilio(**{"recordings": [{"sid": "RE1", "status": "completed"}], **failure})
    meta = {"call_id": "id", "call_sid": "CA1", "record": True, "limit": 60}
    assert aicall.collect_results(make_config(tmp_path), twilio, meta, tmp_path) == 2


@pytest.mark.parametrize("content", [None, b"\xff\xfe not utf-8", b"  \n"], ids=["missing", "not-utf8", "empty"])
def test_bad_prompt_is_refused_before_anything_is_created(driver, tmp_path, capsys, content):
    prompt = tmp_path / "bad_prompt.txt"
    if content is not None:
        prompt.write_bytes(content)
    twilio = FakeTwilio()
    assert driver(twilio, ["+12025550199", str(prompt)]) == 1
    assert capsys.readouterr().err.startswith("error:")
    assert twilio.posts == [] and not (driver.out_dir.exists() and any(driver.out_dir.iterdir()))


@pytest.mark.parametrize("content", [None, '{"call_sid": ', "[]", '{"call_id": "x"}'])
def test_unreadable_call_json_on_resume_exits_3_not_1(driver, tmp_path, capsys, content):
    """--resume means a call was attempted earlier: without a readable record we cannot say it was not placed."""
    call_dir = tmp_path / "calls" / "20261019_120000_0123456789ab"
    call_dir.mkdir(parents=True)
    if content is not None:
        (call_dir / "call.json").write_text(content, encoding="utf-8")
    assert driver(FakeTwilio(), ["--resume", str(call_dir)]) == 3
    err = capsys.readouterr().err
    assert err.startswith("error:") and "may have been placed" in err


# --------------------------------------------------------------------------- Twilio credential handling

@pytest.fixture
def two_hosts():
    """Two local HTTP servers on different ports (different origins). /Recordings/RE1.mp3 on the first
    redirects to the second; /same redirects within the first. Each records the Authorization it saw."""
    import http.server
    import threading

    seen = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen[(self.server.name, self.path)] = self.headers.get("Authorization")
            if self.path in self.server.redirects:
                self.send_response(302)
                self.send_header("Location", self.server.redirects[self.path])
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ID3data")

        def log_message(self, *args):
            pass

    servers = {}
    for name in ("api", "cdn"):
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        srv.name, srv.redirects = name, {}
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers[name] = srv
    api, cdn = (f"http://127.0.0.1:{servers[n].server_address[1]}" for n in ("api", "cdn"))
    servers["api"].redirects.update({"/Recordings/RE1.mp3": cdn + "/signed/rec.mp3", "/same": "/final"})
    yield SimpleNamespace(api=api, seen=seen)
    for srv in servers.values():
        srv.shutdown()
        srv.server_close()


def test_recording_redirect_to_another_host_does_not_carry_the_credential(two_hosts):
    client = aicall.TwilioClient("AC1", "tok")
    client.base = two_hosts.api  # instead of https://api.twilio.com/...
    assert client.download("/Recordings/RE1.mp3") == b"ID3data"
    assert client.download("/same") == b"ID3data"
    assert two_hosts.seen[("api", "/Recordings/RE1.mp3")] == client.auth
    assert two_hosts.seen[("cdn", "/signed/rec.mp3")] is None
    assert two_hosts.seen[("api", "/final")] == client.auth  # a same-host redirect keeps it


def test_twilio_api_key_replaces_the_auth_token():
    env = {"AICALL_VPS_SSH": "aicall-upload@vps.example", "PUBLIC_WSS_URL": "wss://vps.example/media",
           "TWILIO_ACCOUNT_SID": "AC" + "0" * 32, "TWILIO_API_KEY": "SK" + "1" * 32, "TWILIO_API_SECRET": "s3cret",
           "TWILIO_FROM_NUMBER": "+12025550100", "AICALL_SHARED_SECRET": SECRET}
    seen = []

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    def opener(req, timeout):
        seen.append((req.full_url, req.get_header("Authorization")))
        return Resp()

    aicall.TwilioClient.from_config(aicall.Config.from_env(env), opener=opener).get_json("/Calls/CA1.json")
    (url, auth), = seen
    assert url.startswith("https://api.twilio.com/2010-04-01/Accounts/AC" + "0" * 32 + "/")
    assert auth == "Basic " + base64.b64encode(("SK" + "1" * 32 + ":s3cret").encode()).decode()
    with pytest.raises(aicall.DriverError, match="TWILIO_API_SECRET"):
        aicall.Config.from_env({k: v for k, v in env.items() if k != "TWILIO_API_SECRET"})


def twilio_answering_the_dial(status=200, body=b'{"sid": "CA11111111111111111111111111111111", "status": "queued"}'):
    """A real TwilioClient whose HTTP layer answers every request with this status and body."""
    def opener(req, timeout):
        if status >= 400:
            raise urllib.error.HTTPError(req.full_url, status, "error", {}, io.BytesIO(body))
        return io.BytesIO(body)

    return aicall.TwilioClient("AC" + "0" * 32, "tok", opener=opener)


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_dial_that_gets_a_5xx_may_have_placed_the_call_so_it_exits_3(driver, capsys, status):
    """Twilio can create the call and still answer 5xx; exit 1 would invite a redial."""
    assert driver(twilio_answering_the_dial(status, b"upstream error")) == 3
    assert "may have been placed" in capsys.readouterr().err


@pytest.mark.parametrize("body", [b"<html>bad gateway</html>", b"", b"[]", b'{"status": "queued"}'],
                         ids=["html", "empty", "not-an-object", "no-sid"])
def test_dial_with_an_unusable_success_response_exits_3(driver, capsys, body):
    assert driver(twilio_answering_the_dial(200, body)) == 3
    assert "may have been placed" in capsys.readouterr().err


@pytest.mark.parametrize("status", [400, 401, 404, 429])
def test_dial_that_gets_a_4xx_is_still_a_definite_failure(driver, capsys, status):
    assert driver(twilio_answering_the_dial(status, b'{"message": "refused"}')) == 1
    assert "may have been placed" not in capsys.readouterr().err


def test_a_5xx_on_a_status_poll_is_still_retried(driver):
    polls = iter([urllib.error.HTTPError("u", 503, "Service Unavailable", {}, io.BytesIO(b"busy")), None])

    def opener(req, timeout):
        if req.get_method() == "POST":
            return io.BytesIO(b'{"sid": "CA11111111111111111111111111111111", "status": "queued"}')
        failure = next(polls)
        if failure:
            raise failure
        return io.BytesIO(b'{"status": "completed", "duration": "5"}')

    assert driver(aicall.TwilioClient("AC" + "0" * 32, "tok", opener=opener)) == 0
