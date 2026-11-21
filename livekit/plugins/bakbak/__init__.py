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

"""Bakbak plugin for LiveKit Agents.

Low-latency text-to-speech for Indian languages from Bakbak by Raya
(https://getraya.app).
"""

from livekit.agents import Plugin

from .log import logger
from .models import TTSLanguages, TTSModels, TTSTransport
from .tokenizer import IndicSentenceTokenizer, SentenceTokenizer
from .tts import TTS, ChunkedStream, SynthesizeStream, Voice
from .version import __version__

__all__ = [
    "TTS",
    "ChunkedStream",
    "IndicSentenceTokenizer",
    "SentenceTokenizer",
    "SynthesizeStream",
    "TTSLanguages",
    "TTSModels",
    "TTSTransport",
    "Voice",
    "__version__",
]


class BakbakPlugin(Plugin):
    def __init__(self) -> None:
        super().__init__(__name__, __version__, __package__ or __name__, logger)


Plugin.register_plugin(BakbakPlugin())

# Hide the modules that are not exported from the generated API docs.
_module = dir()
NOT_IN_ALL = [m for m in _module if m not in __all__]

__pdoc__ = {}

for n in NOT_IN_ALL:
    __pdoc__[n] = False
