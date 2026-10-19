"""Replay a recorded callee track (8 kHz wav) into several Gemini Live models.

    GEMINI_API_KEY=... python3 tools/model_bench.py callee_8k.wav prompt.txt <model> [<model> ...]

The wav must be mono PCM16 at 8 kHz, like the bridge's callee_8k.wav.

Prints what each model heard and what it replied: the quickest way to see which model
understands your language on an 8 kHz phone line.
"""
import asyncio
import audioop  # audioop-lts on Python 3.13+
import os
import sys
import time
import wave

from google import genai
from google.genai import types

CHUNK = 640  # 20 ms of PCM16 at 16 kHz
TRAILING_SILENCE_CHUNKS = 400  # ~8 s for the model to answer


def load_pcm16k(wav_path: str) -> bytes:
    """Read a mono PCM16 8 kHz wav and resample it to 16 kHz; anything else is refused."""
    try:
        with wave.open(wav_path) as w:
            fmt = (w.getnchannels(), w.getsampwidth(), w.getframerate())
            if fmt != (1, 2, 8000):
                raise SystemExit(f"{wav_path}: need mono 16-bit 8000 Hz audio, got {fmt[0]} channel(s), "
                                 f"{8 * fmt[1]}-bit, {fmt[2]} Hz (convert with: "
                                 "ffmpeg -i in.wav -ac 1 -ar 8000 -sample_fmt s16 callee_8k.wav)")
            pcm8k = w.readframes(w.getnframes())
    except (wave.Error, EOFError) as e:
        raise SystemExit(f"{wav_path}: not a PCM wav file ({e})") from None
    return audioop.ratecv(pcm8k, 2, 1, 8000, 16000, None)[0]


async def stop_receiver(task: asyncio.Task) -> None:
    """Cancel the receiver, wait for it to finish, and re-raise anything it failed with."""
    task.cancel()
    (result,) = await asyncio.gather(task, return_exceptions=True)
    if isinstance(result, Exception):
        raise result


async def bench(client, model: str, prompt: str, pcm: bytes) -> None:
    cfg = types.LiveConnectConfig(response_modalities=["AUDIO"], system_instruction=prompt,
                                  input_audio_transcription=types.AudioTranscriptionConfig(),
                                  output_audio_transcription=types.AudioTranscriptionConfig())
    heard, said = [], []
    try:
        async with client.aio.live.connect(model=model, config=cfg) as session:
            async def receive():
                while True:
                    async for r in session.receive():
                        content = r.server_content
                        if content and content.input_transcription and content.input_transcription.text:
                            heard.append(content.input_transcription.text)
                        if content and content.output_transcription and content.output_transcription.text:
                            said.append(content.output_transcription.text)

            receiver = asyncio.create_task(receive())
            try:
                for i in range(0, len(pcm), CHUNK):
                    await session.send_realtime_input(
                        audio=types.Blob(data=pcm[i:i + CHUNK], mime_type="audio/pcm;rate=16000"))
                    await asyncio.sleep(0.02)
                for _ in range(TRAILING_SILENCE_CHUNKS):
                    await session.send_realtime_input(
                        audio=types.Blob(data=b"\0" * CHUNK, mime_type="audio/pcm;rate=16000"))
                    await asyncio.sleep(0.02)
            finally:
                await stop_receiver(receiver)
    except Exception as e:
        print(f"### {model}: ERROR {e!r}"[:300])
        return
    print(f"### {model}\n  heard:   {''.join(heard).strip()}\n  replied: {''.join(said).strip()}", flush=True)


async def run(models: list[str], prompt: str, pcm: bytes) -> None:
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    for model in models:
        t0 = time.time()
        await bench(client, model, prompt, pcm)
        print(f"  ({time.time() - t0:.0f}s)")


def main() -> None:
    if len(sys.argv) < 4:
        sys.exit(__doc__)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    wav_path, prompt_path, models = sys.argv[1], sys.argv[2], sys.argv[3:]
    with open(prompt_path, encoding="utf-8") as f:
        prompt = f.read()
    asyncio.run(run(models, prompt, load_pcm16k(wav_path)))


if __name__ == "__main__":
    main()
