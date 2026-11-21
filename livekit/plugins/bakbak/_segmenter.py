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

"""The Bakbak sentence segmenter (version 1).

A port of ``conformance/segmenter/reference.py``, which is the specification:
it must produce exactly the reference's sentences for every case in
``conformance/segmenter/cases.json``, and pushing text in any pieces must give
the same sentences as splitting it whole. Keep the structure parallel to the
reference so the two stay easy to compare.

Lengths are in Unicode code points (Python ``str`` indices).
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

VERSION = 1

MAX_CHARS_LIMIT = 5000
"""The API refuses longer text, so ``max_chars`` may not exceed it."""


def _cps(*codepoints: int) -> frozenset[str]:
    return frozenset(chr(c) for c in codepoints)


# Every character class is listed by code point, exactly as in the reference.
WHITESPACE = _cps(
    0x20, 0x09, 0x0A, 0x0D, 0x0C, 0x0B, 0x85, 0xA0, 0x1680,
    0x2000, 0x2001, 0x2002, 0x2003, 0x2004, 0x2005, 0x2006, 0x2007, 0x2008,
    0x2009, 0x200A, 0x2028, 0x2029, 0x202F, 0x205F, 0x3000,
)  # fmt: skip
# . ! ? … । ॥ 。 ！ ？
TERMINATORS = _cps(0x2E, 0x21, 0x3F, 0x2026, 0x0964, 0x0965, 0x3002, 0xFF01, 0xFF1F)
# " ' ) ] } ’ ” » › 》 」 』
CLOSERS = _cps(
    0x22, 0x27, 0x29, 0x5D, 0x7D, 0x2019, 0x201D, 0xBB, 0x203A, 0x300B, 0x300D, 0x300F
)  # fmt: skip
# , ; : — – ، (Arabic comma, used in Urdu)
SOFT_DELIMITERS = _cps(0x2C, 0x3B, 0x3A, 0x2014, 0x2013, 0x060C)
# ZWNJ, ZWJ
JOINERS = _cps(0x200C, 0x200D)
# The viramas of Devanagari, Bengali, Gurmukhi, Gujarati, Odia, Tamil, Telugu,
# Kannada, Malayalam and Sinhala.
VIRAMAS = _cps(0x094D, 0x09CD, 0x0A4D, 0x0ACD, 0x0B4D, 0x0BCD, 0x0C4D, 0x0CCD, 0x0D4D, 0x0DCA)

TITLES = frozenset(
    {
        # Latin titles and fixed abbreviations that precede a name or a number.
        "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "rs", "smt",
        "shri", "sh", "lt", "col", "gen", "capt", "sgt", "hon", "rev", "mt",
        # Devanagari: Dr. (डॉ), Prof. (प्रो), Kumari (कु), late (स्व), Mr. (मि), Rs. (रु)
        "".join(map(chr, (0x0921, 0x0949))),
        "".join(map(chr, (0x092A, 0x094D, 0x0930, 0x094B))),
        "".join(map(chr, (0x0915, 0x0941))),
        "".join(map(chr, (0x0938, 0x094D, 0x0935))),
        "".join(map(chr, (0x092E, 0x093F))),
        "".join(map(chr, (0x0930, 0x0941))),
    }
)  # fmt: skip
AMBIGUOUS = frozenset(
    {
        "etc", "inc", "ltd", "co", "corp", "approx", "dept", "govt", "est",
        "fig", "no", "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep",
        "sept", "oct", "nov", "dec",
    }
)  # fmt: skip

# Characters that can open a boundary candidate. A push of text containing none
# of these can still decide an earlier candidate or trigger wrapping, which
# `Segmenter.push` checks separately.
_CANDIDATE_CHARS = TERMINATORS | SOFT_DELIMITERS | {"\n"}


@dataclass(frozen=True)
class Options:
    """Segmenter options. The defaults are the specification's."""

    min_chars: int = 20
    """Sentences shorter than this are merged with the next one."""
    first_min_chars: int = 1
    """The minimum for the first sentence; 1 sends it as soon as it ends."""
    max_chars: int = 400
    """Longer sentences are wrapped, preferably at a comma or a space."""
    eager_first_clause: bool = False
    """Until the first sentence is out, a clause ending in ``,;:`` also ends it."""
    first_clause_min_chars: int = 24
    """The shortest first clause that `eager_first_clause` sends on its own."""

    def __post_init__(self) -> None:
        if not 1 <= self.max_chars <= MAX_CHARS_LIMIT:
            raise ValueError(f"max_chars must be between 1 and {MAX_CHARS_LIMIT}")
        if self.min_chars < 0 or self.first_min_chars < 0 or self.first_clause_min_chars < 0:
            raise ValueError("min_chars, first_min_chars and first_clause_min_chars must be >= 0")


def _is_ws(ch: str) -> bool:
    return ch in WHITESPACE


def _speakable(s: str) -> bool:
    """True if s contains a letter or a number (Unicode categories L* and N*)."""
    return any(unicodedata.category(ch)[0] in "LN" for ch in s)


def _is_mark(ch: str) -> bool:
    return unicodedata.category(ch)[0] == "M"


def _is_word_char(ch: str) -> bool:
    if ("a" <= ch <= "z") or ("A" <= ch <= "Z") or ("0" <= ch <= "9"):
        return True
    cp = ord(ch)
    return 0x0900 <= cp <= 0x0DFF and cp not in (0x0964, 0x0965)


def _word_before(text: str, dot: int) -> tuple[str, int]:
    """The word ending right before index `dot`, and the index where it starts."""
    i = dot
    while i > 0 and _is_word_char(text[i - 1]):
        i -= 1
    return text[i:dot], i


def _at_line_start(text: str, start: int) -> bool:
    i = start
    while i > 0 and text[i - 1] in (" ", "\t"):
        i -= 1
    return i == 0 or text[i - 1] == "\n"


def _is_ascii_digits(word: str) -> bool:
    return 1 <= len(word) <= 3 and all("0" <= c <= "9" for c in word)


def _single_latin_letter(word: str) -> bool:
    return len(word) == 1 and (("a" <= word <= "z") or ("A" <= word <= "Z"))


@dataclass(frozen=True)
class _Candidate:
    end: int  # where the sentence ends (exclusive); the next one starts after whitespace
    decided: bool
    boundary: bool  # meaningful only when decided


def _candidates(
    text: str, start: int, final: bool, clause: tuple[int, int] | None
) -> list[_Candidate]:
    """Boundary candidates in text[start:], in order.

    clause, when given, is (group_start, first_clause_min_chars): soft delimiters
    followed by whitespace also count, if the stripped text from group_start
    through the delimiter is long enough.
    """
    out: list[_Candidate] = []
    n = len(text)
    i = start
    while i < n:
        ch = text[i]
        if ch == "\n":
            j = i
            while j < n and _is_ws(text[j]):
                j += 1
            decided = final or j < n
            out.append(_Candidate(end=i, decided=decided, boundary=True))
            i = j
            continue
        is_clause = (
            clause is not None
            and ch in SOFT_DELIMITERS
            and len(_strip(text[clause[0] : i + 1])) >= clause[1]
        )
        if ch not in TERMINATORS and not is_clause:
            i += 1
            continue
        run_start = i
        if is_clause:
            j = i + 1
        else:
            j = i
            while j < n and text[j] in TERMINATORS:
                j += 1
        run_end = j
        while j < n and text[j] in CLOSERS:
            j += 1
        end = j  # sentence ends after the closers
        if j >= n:
            # Nothing after the run yet: undecided unless final (end of text is a
            # boundary anyway, so there is nothing to record).
            if not final:
                out.append(_Candidate(end=end, decided=False, boundary=False))
            break
        if not _is_ws(text[j]):
            i = j  # e.g. "3.14", "a.com", "है।राहुल": not a candidate
            continue
        k = j
        while k < n and _is_ws(text[k]):
            k += 1
        if k >= n and not final:
            out.append(_Candidate(end=end, decided=False, boundary=False))
            break
        next_char = text[k] if k < n else ""
        boundary = True
        if not is_clause and run_end - run_start == 1 and text[run_start] == ".":
            word, wstart = _word_before(text, run_start)
            lw = word.lower()
            if _single_latin_letter(word) or lw in TITLES:
                boundary = False
            elif _is_ascii_digits(word) and _at_line_start(text, wstart):
                boundary = False
            elif lw in AMBIGUOUS and not ("A" <= next_char <= "Z"):
                boundary = False
        out.append(_Candidate(end=end, decided=True, boundary=boundary))
        i = k
    return out


def _strip(s: str) -> str:
    a, b = 0, len(s)
    while a < b and _is_ws(s[a]):
        a += 1
    while b > a and _is_ws(s[b - 1]):
        b -= 1
    return s[a:b]


def _skip_ws(text: str, i: int) -> int:
    while i < len(text) and _is_ws(text[i]):
        i += 1
    return i


def _wrap_cut(text: str, start: int, max_chars: int) -> int:
    """Where to cut the window text[start:start+max_chars]; returns an index > start."""
    window_end = start + max_chars
    half = start + max_chars // 2
    for j in range(window_end, half, -1):
        if text[j - 1] in SOFT_DELIMITERS:
            return j
    for j in range(window_end, start, -1):
        # Cut before the whitespace; never produce an empty piece.
        if _is_ws(text[j - 1]) and _strip(text[start : j - 1]):
            return j - 1
    # Hard cut. Never cut before a combining mark or a joiner, or right after a
    # virama or a joiner, which would split an Indic conjunct or a cluster.
    cut = window_end
    while cut > start + 1 and (
        _is_mark(text[cut])
        or text[cut] in JOINERS
        or text[cut - 1] in VIRAMAS
        or text[cut - 1] in JOINERS
    ):
        cut -= 1
    return cut if cut > start + 1 else window_end


def _keep_from(text: str, gs: int) -> int:
    """How much text before the group start a streaming segmenter must keep.

    Two decisions look behind the group start: the word before a "." (which,
    after a hard cut, began before the group) and whether a list marker sits at
    the start of a line. Keeping the word the group starts in, the spaces or tabs
    before it and one more character gives both the answer they would have on the
    whole text.
    """
    i = gs
    while i > 0 and _is_word_char(text[i - 1]):
        i -= 1
    while i > 0 and text[i - 1] in (" ", "\t"):
        i -= 1
    return i - 1 if i > 0 else 0


class Segmenter:
    """Streaming segmenter. `push` returns the sentences that are now final;
    `flush` returns the rest. Batch splitting is `push(text) + flush()`."""

    def __init__(self, options: Options | None = None) -> None:
        self.opts = options or Options()
        self._buf = ""
        self._gs = 0  # where the current group starts in _buf; before it is look-behind only
        self._first = True
        self._wrapping = False  # inside a wrapped group: no merging until it ends
        # Whether the last drain left a candidate that more text could decide. While
        # it did not, a push without any candidate character cannot emit anything
        # unless the buffer outgrows max_chars, so the rescan is skipped. Skipping
        # a drain is always safe: the result does not depend on how text is cut.
        self._open_candidate = False

    def push(self, text: str) -> list[str]:
        if not text:
            return []
        self._buf += text
        if (
            not self._open_candidate
            and len(self._buf) - self._gs <= self.opts.max_chars
            and _CANDIDATE_CHARS.isdisjoint(text)
        ):
            return []
        return self._drain(final=False)

    def flush(self) -> list[str]:
        out = self._drain(final=True)
        self._buf = ""
        self._gs = 0
        self._first = True
        self._wrapping = False
        self._open_candidate = False
        return out

    def _min(self) -> int:
        return self.opts.first_min_chars if self._first else self.opts.min_chars

    def _emit(self, out: list[str], piece: str) -> None:
        # A piece with nothing to say (only punctuation or symbols) is dropped:
        # the API refuses text that has no sentences in it.
        if piece and _speakable(piece):
            out.append(piece)
            self._first = False

    def _drain(self, final: bool) -> list[str]:
        o = self.opts
        out: list[str] = []
        text = self._buf
        gs = _skip_ws(text, self._gs)  # group start
        cands: list[_Candidate] = []
        while True:
            clause = (
                (gs, o.first_clause_min_chars) if (o.eager_first_clause and self._first) else None
            )
            cands = _candidates(text, gs, final, clause)
            emitted = False
            for c in cands:
                if not c.decided:
                    break
                if not c.boundary:
                    continue
                group = _strip(text[gs : c.end])
                if len(group) > o.max_chars:
                    break  # handled by wrapping below
                if not _speakable(group):
                    continue  # punctuation only: nothing to say yet, extend
                if self._wrapping or len(group) >= self._min():
                    self._emit(out, group)
                    self._wrapping = False
                    gs = _skip_ws(text, c.end)
                    emitted = True
                    break
                # too short: extend the group to the next boundary
            if emitted:
                continue
            # No boundary closes a group of acceptable size. Wrap if the group is
            # certainly longer than max_chars.
            limit = gs + o.max_chars
            first_open = next((c.end for c in cands if not c.decided or c.boundary), None)
            certainly_long = (first_open is None or first_open > limit) and len(text) > limit
            if final and len(_strip(text[gs:])) > o.max_chars:
                certainly_long = True
            if certainly_long and len(_strip(text[gs:])) > o.max_chars:
                cut = _wrap_cut(text, gs, o.max_chars)
                self._emit(out, _strip(text[gs:cut]))
                self._wrapping = True
                gs = _skip_ws(text, cut)
                continue
            if final:
                self._emit(out, _strip(text[gs:]))
                gs = len(text)
            break
        # Any candidate left means more text can change the outcome: an undecided
        # one waits for its next character, and a decided boundary that did not
        # close a group (too short, or punctuation only) is reconsidered once a
        # later boundary lets the group grow. The eager first clause also counts
        # soft delimiters, which `_CANDIDATE_CHARS` covers.
        self._open_candidate = bool(cands)
        keep = _keep_from(text, gs)
        self._buf = text[keep:]
        self._gs = gs - keep
        return out


def split_sentences(text: str, options: Options | None = None) -> list[str]:
    """Split a whole text into sentences, exactly as streaming it would."""
    s = Segmenter(options)
    return s.push(text) + s.flush()
