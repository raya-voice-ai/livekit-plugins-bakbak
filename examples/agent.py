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

"""A Hindi voice agent: Deepgram speech-to-text, an OpenAI LLM and Bakbak speech.

Setup (from the repository root):

    pip install "livekit-plugins-bakbak[examples]"   # or: uv pip install -e ".[examples]"
    cp examples/.env.example examples/.env           # then fill it in

Talk to it in your terminal:

    python examples/agent.py console

or run it as a worker for a LiveKit room (``dev``), e.g. with the Agents Playground.
"""

from __future__ import annotations

import os
from pathlib import Path

from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    JobContext,
    MetricsCollectedEvent,
    cli,
    metrics,
)
from livekit.plugins.deepgram import STT as DeepgramSTT
from livekit.plugins.openai import LLM as OpenAILLM

from livekit.plugins import bakbak


def load_env(path: Path) -> None:
    """A minimal ``.env`` reader; variables already set win."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if sep and value.strip():
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


load_env(Path(__file__).resolve().parent / ".env")


class Assistant(Agent):
    def __init__(self) -> None:
        super().__init__(
            instructions=(
                "आप एक मददगार वॉइस असिस्टेंट हैं। उपयोगकर्ता से हिंदी में बात करें और जवाब छोटे "
                "और स्वाभाविक रखें। इमोजी, markdown या सूचियों का उपयोग न करें। "
                "Common English words may be mixed in naturally, as people do."
            ),
        )

    async def on_enter(self) -> None:
        self.session.generate_reply(instructions="उपयोगकर्ता का अभिवादन करें।")


server = AgentServer()


@server.rtc_session()  # pyright: ignore[reportUntypedFunctionDecorator]
async def entrypoint(ctx: JobContext) -> None:
    # The plugin opens its WebSocket when the session starts (prewarm), sends each
    # sentence of the reply the moment the LLM finishes it, and cancels the rest
    # when the user interrupts.
    tts = bakbak.TTS(language=os.environ.get("BAKBAK_LANGUAGE", "hi"))
    if voice_id := os.environ.get("BAKBAK_VOICE_ID"):
        tts.update_options(voice_id=voice_id)

    session = AgentSession[None](
        stt=DeepgramSTT(model="nova-3", language="multi"),
        llm=OpenAILLM(model="gpt-4o-mini"),
        tts=tts,
    )

    @session.on("metrics_collected")  # pyright: ignore[reportUntypedFunctionDecorator]
    def _on_metrics(ev: MetricsCollectedEvent) -> None:
        metrics.log_metrics(ev.metrics)

    await session.start(agent=Assistant(), room=ctx.room)


if __name__ == "__main__":
    cli.run_app(server)
