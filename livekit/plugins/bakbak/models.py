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

"""Models, languages and audio constants of the Bakbak text-to-speech API.

The literal types list the values known today. Every option that takes one also
accepts any other string, so a model or language added on the server works
without a new plugin release.
"""

from __future__ import annotations

from typing import Literal

from livekit.agents.language import LanguageCode

TTSModels = Literal["m1", "standard"]
"""Known models. A voice belongs to exactly one model."""

TTSLanguages = Literal[
    "hi",  # Hindi
    "mr",  # Marathi
    "te",  # Telugu
    "kn",  # Kannada
    "bn",  # Bengali
    "as",  # Assamese
    "gu",  # Gujarati
    "ne",  # Nepali
    "ml",  # Malayalam
    "ta",  # Tamil
    "pa",  # Punjabi
    "en",  # English (the m1 English voices)
    "en-in",  # Indian English (standard voices)
    "en-us",  # US English (standard voices)
]
"""Known language codes. The m1 English voices use ``en``; standard ones ``en-in``/``en-us``."""

TTSTransport = Literal["websocket", "http"]
"""``websocket`` multiplexes every request on one socket; ``http`` sends one SSE request each."""

TTSSampleRates = Literal[8000, 16000, 22050, 24000]

SUPPORTED_SAMPLE_RATES: frozenset[int] = frozenset({8000, 16000, 22050, 24000})

DEFAULT_BASE_URL = "https://hub.getraya.app"
DEFAULT_MODEL: TTSModels = "m1"
DEFAULT_LANGUAGE: TTSLanguages = "hi"
DEFAULT_SAMPLE_RATE = 24000
DEFAULT_VOICE_ID = "009ae8b5-4a03-492d-af1c-3055989ac734"
"""Anika M1: an m1 Hindi voice."""

_KNOWN_LANGUAGES: frozenset[str] = frozenset(TTSLanguages.__args__)

# English names of the API's languages, including the ones LiveKit's LanguageCode
# does not know ("assamese") and common alternatives ("bangla").
_LANGUAGE_NAMES = {
    "hindi": "hi",
    "marathi": "mr",
    "telugu": "te",
    "kannada": "kn",
    "bengali": "bn",
    "bangla": "bn",
    "assamese": "as",
    "gujarati": "gu",
    "nepali": "ne",
    "malayalam": "ml",
    "tamil": "ta",
    "punjabi": "pa",
    "panjabi": "pa",
    "english": "en",
}


def normalize_language(language: str) -> str:
    """Map a language code, BCP-47 tag or language name to the API's code.

    Known codes pass through unchanged. ``en-IN`` and ``en-US`` keep their region
    (``en-in``, ``en-us``) because the API treats them as different languages;
    other English tags become ``en``. Everything else is reduced to its base
    language, so ``hi-IN`` and ``"hindi"`` both become ``hi``. Unknown codes are
    passed on for the server to judge.
    """
    lowered = language.strip().lower().replace("_", "-")
    if lowered in _KNOWN_LANGUAGES:
        return lowered
    if lowered in _LANGUAGE_NAMES:
        return _LANGUAGE_NAMES[lowered]
    code = LanguageCode(language)
    base = code.language
    if base == "en":
        regional = f"en-{(code.region or '').lower()}"
        return regional if regional in _KNOWN_LANGUAGES else "en"
    return base
