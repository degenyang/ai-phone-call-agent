"""Offline tests for the helper tools: input validation and receiver-task cleanup (no network)."""
import asyncio
import sys
import wave

import pytest

from conftest import ROOT

sys.path.insert(0, str(ROOT / "tools"))


def write_wav(path, channels=1, width=2, rate=8000, frames=800):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(b"\0" * frames * channels * width)
    return str(path)


@pytest.fixture
def model_bench():
    pytest.importorskip("google.genai")
    import model_bench
    return model_bench


def test_model_bench_accepts_mono_pcm16_8k(model_bench, tmp_path):
    pcm = model_bench.load_pcm16k(write_wav(tmp_path / "ok.wav"))
    assert abs(len(pcm) - 800 * 2 * 2) <= 4  # resampled to 16 kHz


@pytest.mark.parametrize("fmt", [dict(channels=2), dict(width=1), dict(rate=16000), dict(rate=24000)])
def test_model_bench_rejects_other_wav_formats(model_bench, tmp_path, fmt):
    with pytest.raises(SystemExit, match="need mono 16-bit 8000 Hz"):
        model_bench.load_pcm16k(write_wav(tmp_path / "bad.wav", **fmt))


def test_model_bench_rejects_non_wav(model_bench, tmp_path):
    bad = tmp_path / "x.wav"
    bad.write_bytes(b"not a wav file at all")
    with pytest.raises(SystemExit, match="not a PCM wav"):
        model_bench.load_pcm16k(str(bad))


@pytest.mark.parametrize("auth_token, signed", [("tok", True), (None, False)])
def test_fake_twilio_opens_the_stream_twilio_would(tmp_path, auth_token, signed):
    """fake_twilio builds the per-call URL and, given the auth token, signs it the way Twilio does."""
    import fake_twilio
    import server
    from conftest import CALL_ID, SECRET, FakeRequest, FakeTwilioSocket

    call_dir = tmp_path / "calls" / CALL_ID
    call_dir.mkdir(parents=True)
    (call_dir / "prompt.txt").write_text("p", encoding="utf-8")
    (call_dir / "config.json").write_text("{}", encoding="utf-8")
    base = "wss://example.test/media"
    settings = server.Settings(calls_dir=tmp_path / "calls", shared_secret=SECRET, verify_twilio_signature=True,
                               twilio_auth_token="tok", public_wss_url=base)
    url, headers = fake_twilio.stream_request(base, CALL_ID, SECRET, auth_token)
    assert url.startswith(base + "/" + CALL_ID + "/")
    request = FakeRequest(url.removeprefix("wss://example.test"), headers)
    response = server.Bridge(settings).process_request(FakeTwilioSocket(), request)
    assert (response is None) == signed


@pytest.mark.parametrize("module", ["model_bench", "fake_twilio"])
def test_stop_receiver_awaits_and_surfaces_errors(module, request):
    if module == "model_bench":
        request.getfixturevalue("model_bench")
    tool = __import__(module)
    cleaned = []

    async def idle():
        try:
            await asyncio.sleep(10)
        finally:
            await asyncio.sleep(0)
            cleaned.append(True)

    async def broken():
        raise RuntimeError("receiver crashed")

    async def scenario():
        receiver = asyncio.create_task(idle())
        await asyncio.sleep(0)  # let it start
        await tool.stop_receiver(receiver)  # a cancelled receiver is not an error
        task = asyncio.create_task(broken())
        await asyncio.sleep(0)
        with pytest.raises(RuntimeError, match="receiver crashed"):
            await tool.stop_receiver(task)
        return [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]

    assert asyncio.run(scenario()) == [] and cleaned == [True]
