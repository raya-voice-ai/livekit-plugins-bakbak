# Copyright 2026 Raya Voice AI
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Synthesize text with Bakbak and save it as a WAV file, without a LiveKit room.

The quickest way to check your key and hear a voice:

    export BAKBAK_API_KEY=...
    python examples/synthesize_to_wav.py "नमस्ते! आपका पैकेज आज शाम तक पहुँच जाएगा।"
    python examples/synthesize_to_wav.py --stream --transport http -o out.wav "..."

``--stream`` feeds the text a few characters at a time, the way an LLM does,
and prints when the first audio arrived.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import time

import aiohttp

from livekit import rtc
from livekit.plugins import bakbak

DEFAULT_TEXT = "नमस्ते! आपका package आज शाम छह बजे तक पहुँच जाएगा। Delivery boy राहुल आपको पहले call करेगा।"


async def synthesize(args: argparse.Namespace) -> tuple[rtc.AudioFrame, float]:
    """The audio, and how long the first of it took to arrive (seconds)."""
    # Inside an agent, LiveKit provides the HTTP session; a script brings its own.
    async with aiohttp.ClientSession() as session:
        tts = bakbak.TTS(
            language=os.environ.get("BAKBAK_LANGUAGE", "hi"),
            transport=args.transport,
            http_session=session,
        )
        if voice_id := os.environ.get("BAKBAK_VOICE_ID"):
            tts.update_options(voice_id=voice_id)
        tts.prewarm()
        started = time.perf_counter()
        first_audio: float | None = None
        frames: list[rtc.AudioFrame] = []
        if args.stream:
            async with tts.stream() as stream:
                for i in range(0, len(args.text), 4):
                    stream.push_text(args.text[i : i + 4])
                stream.end_input()
                async for ev in stream:
                    first_audio = first_audio or time.perf_counter()
                    frames.append(ev.frame)
        else:
            async with tts.synthesize(args.text) as stream:
                async for ev in stream:
                    first_audio = first_audio or time.perf_counter()
                    frames.append(ev.frame)
        await tts.aclose()

    return rtc.combine_audio_frames(frames), (first_audio or started) - started


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Synthesize text with Bakbak into a WAV file.")
    parser.add_argument("text", nargs="?", default=DEFAULT_TEXT)
    parser.add_argument("-o", "--output", default="bakbak.wav")
    parser.add_argument("--transport", choices=["websocket", "http"], default="websocket")
    parser.add_argument("--stream", action="store_true", help="feed the text like an LLM")
    args = parser.parse_args()
    audio, ttfa = asyncio.run(synthesize(args))
    with open(args.output, "wb") as f:
        f.write(audio.to_wav_bytes())
    print(f"wrote {args.output}: {audio.duration:.2f} s, first audio after {1000 * ttfa:.0f} ms")
