"""Simulate Twilio Media Streams against the bridge, playing pre-recorded clips as the callee.

    PUBLIC_WSS_URL=wss://your-domain.example/media AICALL_SHARED_SECRET=... \
        python3 tools/fake_twilio.py <call_id> <clips_dir>

<clips_dir> holds u1.pcm u2.pcm u3.pcm: raw PCM16 mono 16 kHz, e.g.
    ffmpeg -i u1.mp3 -f s16le -ac 1 -ar 16000 u1.pcm
The server must already have calls/<call_id>/prompt.txt and config.json (a call id can be
used only once). It connects to the same per-call URL aicall.py gives Twilio; with
TWILIO_AUTH_TOKEN set it also sends the X-Twilio-Signature Twilio would send, for bridges
that run with AICALL_VERIFY_TWILIO_SIGNATURE=1. No Twilio call is placed and nothing is
billed by Twilio.
"""
import asyncio
import audioop  # audioop-lts on Python 3.13+
import base64
import json
import os
import sys
import time
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "server")]
from aicall import STREAM_START_WINDOW_S, call_token_signature, stream_url  # noqa: E402
from server import twilio_signature  # noqa: E402

STREAM_SID = "MZtest"
SILENCE = b"\xff" * 160  # 20 ms of mu-law silence


def stream_request(base_url: str, call_id: str, secret: str, auth_token: str | None = None) -> tuple[str, dict]:
    """The URL Twilio opens for this call and, given the Twilio auth token, the signature header it sends."""
    expires = int(time.time()) + STREAM_START_WINDOW_S
    url = stream_url(base_url, call_id, expires, call_token_signature(secret, call_id, expires))
    headers = {"X-Twilio-Signature": twilio_signature(auth_token, url)} if auth_token else {}
    return url, headers


async def stop_receiver(task: asyncio.Task) -> None:
    """Cancel the receiver, wait for it to finish, and re-raise anything it failed with."""
    task.cancel()
    (result,) = await asyncio.gather(task, return_exceptions=True)
    if isinstance(result, Exception) and not isinstance(result, websockets.ConnectionClosed):
        raise result


def load_clips(clips_dir: str) -> list[bytes]:
    clips = []
    for i in (1, 2, 3):
        pcm16k = Path(clips_dir, f"u{i}.pcm").read_bytes()
        pcm8k = audioop.ratecv(pcm16k, 2, 1, 16000, 8000, None)[0]
        clips.append(audioop.lin2ulaw(pcm8k, 2))
    return clips


async def run(url: str, headers: dict, clips: list[bytes]) -> None:
    stats = {"last_audio": 0.0, "frames": 0}

    async with websockets.connect(url, additional_headers=headers, user_agent_header="Twilio.TmeWs/1.0") as ws:
        await ws.send(json.dumps({"event": "connected", "protocol": "Call", "version": "1.0.0"}))
        await ws.send(json.dumps({"event": "start", "streamSid": STREAM_SID, "start": {"streamSid": STREAM_SID}}))

        async def receive():
            async for raw in ws:
                msg = json.loads(raw)
                if msg["event"] == "media":
                    stats["last_audio"] = time.time()
                    stats["frames"] += 1
                elif msg["event"] == "mark":  # echo like Twilio does after playback
                    print("got mark -> echo")
                    await ws.send(json.dumps({"event": "mark", "streamSid": STREAM_SID, "mark": msg["mark"]}))
                else:
                    print("RX", msg["event"])

        async def send_audio(ulaw: bytes):
            for i in range(0, len(ulaw), 160):
                frame = ulaw[i:i + 160].ljust(160, b"\xff")
                await ws.send(json.dumps({"event": "media", "streamSid": STREAM_SID,
                                          "media": {"payload": base64.b64encode(frame).decode()}}))
                await asyncio.sleep(0.02)

        async def wait_for_agent(max_s: float = 20):
            t0 = time.time()
            while time.time() - t0 < max_s:
                await send_audio(SILENCE * 5)
                if stats["last_audio"] > t0 and time.time() - stats["last_audio"] > 1.5:
                    break

        receiver = asyncio.create_task(receive())
        try:
            await wait_for_agent()
            for i, clip in enumerate(clips, 1):
                print(f"TX u{i}")
                await send_audio(clip)
                await wait_for_agent()
            await send_audio(SILENCE * 100)
            await ws.send(json.dumps({"event": "stop"}))
        except websockets.ConnectionClosed:
            print("server closed (end_call)")
        finally:
            await stop_receiver(receiver)
    print("agent media frames", stats["frames"])


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    call_id, clips_dir = sys.argv[1], sys.argv[2]
    url, headers = stream_request(os.environ["PUBLIC_WSS_URL"], call_id, os.environ["AICALL_SHARED_SECRET"],
                                  os.environ.get("TWILIO_AUTH_TOKEN"))
    asyncio.run(run(url, headers, load_clips(clips_dir)))


if __name__ == "__main__":
    main()
