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

"""The Bakbak sentence segmenter as a LiveKit ``SentenceTokenizer``."""

from __future__ import annotations

import warnings

from livekit.agents import tokenize, utils

from ._segmenter import Options, Segmenter, split_sentences

__all__ = ["IndicSentenceTokenizer", "SentenceTokenizer"]


class SentenceTokenizer(tokenize.SentenceTokenizer):
    """Splits text into the sentence-sized requests Bakbak voices best.

    It understands the Devanagari danda (``।``, ``॥``) and Latin punctuation,
    never splits inside numbers, URLs, e-mail addresses, initials or titles such
    as "Dr." and "डॉ.", and keeps Indic conjuncts whole when a long sentence has
    to be wrapped. Streaming gives exactly the sentences that splitting the
    whole text gives, and each sentence is released as soon as its end is
    certain, so the first request leaves while the LLM is still writing.

    Args:
        min_chars: Sentences shorter than this are merged with the next one.
        first_min_chars: The minimum for the first sentence. The default, 1,
            sends the first sentence as soon as it ends: the lowest latency.
        max_chars: Longer sentences are wrapped, preferably at a comma, else at
            a space. At most 5,000, the API's limit.
        eager_first_clause: Until the first sentence is out, also end it at a
            clause (``,`` ``;`` ``:`` or a dash followed by a space). Trades a
            little intonation for an earlier first request when replies open
            with a long sentence.
        first_clause_min_chars: The shortest first clause that
            ``eager_first_clause`` sends on its own.
    """

    def __init__(
        self,
        *,
        min_chars: int = 20,
        first_min_chars: int = 1,
        max_chars: int = 400,
        eager_first_clause: bool = False,
        first_clause_min_chars: int = 24,
    ) -> None:
        self._options = Options(
            min_chars=min_chars,
            first_min_chars=first_min_chars,
            max_chars=max_chars,
            eager_first_clause=eager_first_clause,
            first_clause_min_chars=first_clause_min_chars,
        )

    @property
    def options(self) -> Options:
        return self._options

    def tokenize(self, text: str, *, language: str | None = None) -> list[str]:
        """Split a whole text. ``language`` is accepted for the interface and unused."""
        return split_sentences(text, self._options)

    def stream(self, *, language: str | None = None) -> tokenize.SentenceStream:
        """A stream that releases each sentence as soon as its end is certain."""
        return _SegmenterStream(self._options)


class _SegmenterStream(tokenize.SentenceStream):
    """`SentenceStream` driven directly by the segmenter, with no extra buffering.

    ``flush()`` ends a segment: the remaining text is released and the next
    sentence counts as a first sentence again (``first_min_chars``).
    """

    def __init__(self, options: Options) -> None:
        super().__init__()
        self._segmenter = Segmenter(options)
        self._segment_id = utils.shortuuid()

    def push_text(self, text: str) -> None:
        self._check_not_closed()
        self._send(self._segmenter.push(text))

    def flush(self) -> None:
        self._check_not_closed()
        self._send(self._segmenter.flush())
        self._segment_id = utils.shortuuid()

    def end_input(self) -> None:
        self.flush()
        self._do_close()

    async def aclose(self) -> None:
        self._do_close()

    def _send(self, sentences: list[str]) -> None:
        for sentence in sentences:
            self._event_ch.send_nowait(
                tokenize.TokenData(segment_id=self._segment_id, token=sentence)
            )


class IndicSentenceTokenizer(SentenceTokenizer):
    """Deprecated name kept for code written against ``livekit-plugins-raya``.

    ``min_sentence_len`` and ``max_sentence_len`` map to ``min_chars`` and
    ``max_chars``. ``stream_context_len`` is ignored: the segmenter waits exactly
    as long as a boundary needs and no longer. Use :class:`SentenceTokenizer`.
    """

    def __init__(
        self,
        *,
        min_sentence_len: int = 20,
        max_sentence_len: int = 400,
        stream_context_len: int | None = None,
    ) -> None:
        warnings.warn(
            "IndicSentenceTokenizer is deprecated; use bakbak.SentenceTokenizer(min_chars=..., "
            "max_chars=...) instead",
            DeprecationWarning,
            stacklevel=2,
        )
        super().__init__(min_chars=min_sentence_len, max_chars=max_sentence_len)
