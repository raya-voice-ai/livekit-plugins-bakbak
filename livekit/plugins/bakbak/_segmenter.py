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

import re
import unicodedata
from collections.abc import Iterator
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
# Whitespace that is not a newline: pushing only these can change no decision. A
# candidate is decided by the first non-whitespace character after it, no
# candidate starts with one, and none is text beyond the wrap limit.
_BLANK = WHITESPACE - {"\n"}


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


_SPECIAL = re.compile("[\n" + "".join(re.escape(c) for c in sorted(TERMINATORS)) + "]")
_SPECIAL_CLAUSE = re.compile(
    "[\n" + "".join(re.escape(c) for c in sorted(TERMINATORS | SOFT_DELIMITERS)) + "]"
)


class _NextSpecial:
    """Finds the next character that can start a candidate (a newline, a
    terminator, or with `clause` a soft delimiter), remembering its last answer:
    the wrap path asks again from a later start many times, and without the
    memo a long text with no punctuation would be scanned once per piece."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.memo: dict[bool, tuple[int, int]] = {}

    def __call__(self, i: int, clause: bool) -> int:
        lo, at = self.memo.get(clause, (-1, -1))
        if lo <= i <= at:
            return at
        m = (_SPECIAL_CLAUSE if clause else _SPECIAL).search(self.text, i)
        at = m.start() if m else len(self.text)
        self.memo[clause] = (i, at)
        return at


@dataclass(frozen=True)
class _Candidate:
    end: int  # where the sentence ends (exclusive); the next one starts after whitespace
    decided: bool
    boundary: bool  # meaningful only when decided


def _candidates(
    text: str,
    start: int,
    final: bool,
    clause: tuple[int, int] | None,
    nxt: _NextSpecial | None = None,
) -> Iterator[_Candidate]:
    """Boundary candidates in text[start:], in order, produced lazily: callers stop
    at the first one that ends a group, which keeps segmentation linear in the
    length of the text.

    clause, when given, is (group_start, first_clause_min_chars): soft delimiters
    followed by whitespace also count, if the stripped text from group_start
    through the delimiter is long enough.
    """
    n = len(text)
    nxt = nxt or _NextSpecial(text)
    i = start
    while i < n:
        i = nxt(i, clause is not None)  # skip ordinary characters
        if i >= n:
            break
        ch = text[i]
        if ch == "\n":
            j = i
            j = _skip_ws(text, j)
            decided = final or j < n
            yield _Candidate(end=i, decided=decided, boundary=True)
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
                yield _Candidate(end=end, decided=False, boundary=False)
            break
        if not _is_ws(text[j]):
            i = j  # e.g. "3.14", "a.com", "है।राहुल": not a candidate
            continue
        k = j
        k = _skip_ws(text, k)
        if k >= n and not final:
            yield _Candidate(end=end, decided=False, boundary=False)
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
        yield _Candidate(end=end, decided=True, boundary=boundary)
        i = k


def _strip(s: str) -> str:
    a, b = 0, len(s)
    while a < b and _is_ws(s[a]):
        a += 1
    while b > a and _is_ws(s[b - 1]):
        b -= 1
    return s[a:b]


_NOT_WS = re.compile("[^" + "".join(re.escape(c) for c in sorted(WHITESPACE)) + "]")


def _skip_ws(text: str, i: int) -> int:
    """The first index at or after i that is not whitespace (len(text) if none)."""
    m = _NOT_WS.search(text, i)
    return m.start() if m else len(text)


def _wrap_cut(text: str, start: int, max_chars: int) -> int:
    """Where to cut the window text[start:start+max_chars]; returns an index > start."""
    window_end = start + max_chars
    half = start + max_chars // 2
    # The reference walks the window back one character at a time; finding the
    # last delimiter and the last whitespace with str.rfind gives the same cut.
    delimiter = max(text.rfind(d, half, window_end) for d in SOFT_DELIMITERS)
    if delimiter >= 0:
        return delimiter + 1
    space = max(text.rfind(w, start, window_end) for w in WHITESPACE)
    # Cut before the whitespace; never produce an empty piece. If everything before
    # the last whitespace is whitespace, so is everything before any earlier one.
    if space > start and _strip(text[start:space]):
        return space
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


# The longest word any rule compares (TITLES, AMBIGUOUS, list-marker digits) has
# 6 code points, so 8 code points of a word are enough to know it is none of them.
_LOOKBEHIND_WORD = 8


def _lookbehind(text: str, gs: int) -> str:
    """The text a streaming segmenter keeps before the group start.

    Two decisions look behind the group start: the word before a "." (which,
    after a hard cut, began before the group) and whether a list marker sits at
    the start of a line. They need only the last few characters of that word and,
    when the word is short, what precedes it: a run of spaces or tabs (one stands
    for any number) and the character before the run, or nothing at the start of
    the text. Keeping exactly that bounds the buffer however long the text is
    and gives every decision the answer it would have on the whole text. The
    kept text is never emitted, so it need not be a slice of the input.
    """
    w = gs
    floor = max(0, gs - _LOOKBEHIND_WORD)  # the reference walks the whole word; this
    while w > floor and _is_word_char(text[w - 1]):  # stops at 8, with the same result
        w -= 1
    if gs - w >= _LOOKBEHIND_WORD:
        return text[gs - _LOOKBEHIND_WORD : gs]
    i = w
    while i > 0 and text[i - 1] in (" ", "\t"):
        i -= 1
    gap = " " if i < w else ""
    before = text[i - 1] if i > 0 else ""
    return before + gap + text[w:gs]


class Segmenter:
    """Streaming segmenter. `push` returns the sentences that are now final;
    `flush` returns the rest. Batch splitting is `push(text) + flush()`."""

    # Skipping drains that cannot emit anything is an optimisation only; tests turn
    # it off to check that it never delays a sentence.
    _skip_idle = True

    def __init__(self, options: Options | None = None) -> None:
        self.opts = options or Options()
        self._buf = ""
        # Pushed text not yet in _buf: joined only when a drain runs, so a push that
        # skips the drain costs nothing however large the buffer has grown.
        self._tail: list[str] = []
        self._len = 0  # len(_buf) plus the tail
        self._gs = 0  # where the current group starts in _buf; before it is look-behind only
        self._first = True
        self._wrapping = False  # inside a wrapped group: no merging until it ends
        # Whether the last drain stopped at an undecided candidate. While it did not,
        # a push without any candidate character cannot emit anything unless the
        # group outgrows max_chars: decided candidates keep their verdict, and only
        # a new candidate can end a group. So the rescan is skipped. A skipped drain
        # never changes the result, which does not depend on how text is cut.
        self._undecided = False

    def push(self, text: str) -> list[str]:
        if not text:
            return []
        self._tail.append(text)
        self._len += len(text)
        if self._skip_idle and (
            _BLANK.issuperset(text)
            or (
                not self._undecided
                and self._len - self._gs <= self.opts.max_chars
                and _CANDIDATE_CHARS.isdisjoint(text)
            )
        ):
            return []
        return self._drain(final=False)

    def flush(self) -> list[str]:
        out = self._drain(final=True)
        self._buf = ""
        self._len = 0
        self._gs = 0
        self._first = True
        self._wrapping = False
        self._undecided = False
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
        if self._tail:
            self._buf += "".join(self._tail)
            self._tail.clear()
        text = self._buf
        nxt = _NextSpecial(text)
        gs = _skip_ws(text, self._gs)  # group start
        undecided = False
        while True:
            clause = (
                (gs, o.first_clause_min_chars) if (o.eager_first_clause and self._first) else None
            )
            emitted = False
            undecided = False
            first_open: int | None = None  # the first candidate that is a boundary or undecided
            for c in _candidates(text, gs, final, clause, nxt):
                if first_open is None and (not c.decided or c.boundary):
                    first_open = c.end
                if not c.decided:
                    undecided = True
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
            # The stripped rest is longer than max_chars exactly when something
            # other than whitespace lies at or beyond `limit` (gs is not whitespace).
            long_rest = _skip_ws(text, limit) < len(text)
            certainly_long = long_rest and (final or first_open is None or first_open > limit)
            if certainly_long:
                cut = _wrap_cut(text, gs, o.max_chars)
                self._emit(out, _strip(text[gs:cut]))
                self._wrapping = True
                gs = _skip_ws(text, cut)
                continue
            if final:
                self._emit(out, _strip(text[gs:]))
                gs = len(text)
            break
        self._undecided = undecided
        context = _lookbehind(text, gs)
        self._buf = context + text[gs:]
        self._len = len(self._buf)
        self._gs = len(context)
        return out


def split_sentences(text: str, options: Options | None = None) -> list[str]:
    """Split a whole text into sentences, exactly as streaming it would."""
    s = Segmenter(options)
    return s.push(text) + s.flush()
