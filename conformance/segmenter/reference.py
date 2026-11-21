"""Reference implementation of the Bakbak sentence segmenter (version 1).

This file is the specification. The Python SDK, the JavaScript SDK and the
LiveKit plugin each carry their own implementation, and every one of them must
produce exactly the sentences this one produces for every case in cases.json.

Goal: split text into the sentence-sized requests the TTS models voice best, as
early as possible when the text arrives a token at a time from an LLM, without
ever splitting where a sentence does not end. The guiding rule is "when in
doubt, don't split": a missed boundary only makes one request a little longer,
while a false split puts a falling intonation in the middle of a sentence.

Definitions (all lengths are in Unicode code points, never UTF-16 units):

- whitespace: the characters in WHITESPACE below, exactly. Zero-width joiners
  (U+200C, U+200D), which Indic scripts use inside words, are NOT whitespace.
- terminators: TERMINATORS. A run is one or more consecutive terminators.
- closers: CLOSERS. Any closers directly after a run belong to the sentence.
- A boundary candidate is a terminator run plus its closers, followed by at least
  one whitespace character. A newline is also a candidate on its own: the
  sentence ends just before it. Nothing else is a candidate, so "3.14",
  "getraya.app", "a@b.com" and "है।राहुल" never split.
- A candidate is DECIDED once the text contains a non-whitespace character after
  the whitespace that follows it, or when the input is final. A streaming
  segmenter only acts on decided candidates; this is what makes streaming output
  identical to batch output.
- A decided candidate is a boundary unless one of these exceptions holds. The
  exceptions apply only when the run is exactly one "." (any closers allowed):
  1. the word before it is a single Latin letter (initials, "e.g.", "a.m.");
  2. the word before it, lowercased, is in TITLES ("Dr.", "Mr.", "डॉ.");
  3. the word before it is 1-3 ASCII digits at the start of a line (list markers);
  4. the word before it, lowercased, is in AMBIGUOUS ("etc.", "Inc.", "no.") and
     the next non-whitespace character is not an ASCII capital letter A-Z.
  "The word before it" is the maximal run of word characters (Latin letters,
  ASCII digits, and characters of the Indic blocks U+0900-U+0DFF other than the
  dandas U+0964/U+0965) that ends right before the ".".
- A sentence is the text between two boundaries with leading and trailing
  whitespace removed. Nothing inside it is changed, so the concatenated
  sentences keep every non-whitespace character of the input, in order.

Grouping (applied left to right, identically in batch and streaming):

- Sentences shorter than the minimum are merged with the following sentence: the
  group extends to the next boundary and is re-measured. The minimum is
  `first_min_chars` for the first group emitted and `min_chars` after that.
- A group longer than `max_chars` is wrapped into pieces of at most `max_chars`:
  in each window of `max_chars` code points, cut after the last soft delimiter
  (SOFT_DELIMITERS) that lies in the second half of the window, else at the last
  whitespace in the window, else hard at `max_chars`. Each piece is stripped.
  Wrapped pieces are never merged, including the last piece of a wrapped group.
- `eager_first_clause` (off by default): until the first group has been emitted,
  a soft delimiter followed by whitespace is also a candidate, provided the
  stripped text from the group start through the delimiter has at least
  `first_clause_min_chars` code points. It trades a little prosody for an
  earlier first request when the first sentence is long.
- When the input is final, any remaining text is emitted as the last group (or
  wrapped), whatever its length.
- A group with nothing speakable in it (no character of Unicode category L* or
  N*, e.g. "।" or "..." or an emoji) is not emitted at a boundary: it is merged
  with what follows. Left over at the end of the input, it is dropped, because
  the API refuses text with no sentence in it.
- A hard cut never falls before a combining mark (category M*) or a joiner, nor
  right after a virama (VIRAMAS) or a joiner: it moves left until it is between
  two clusters, which keeps Indic conjuncts whole.

Streaming invariant: for every way of cutting the input into pushed pieces,
push(p1) + push(p2) + ... + flush() returns the same list as split_sentences on
the whole text. Every implementation must test this property.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

VERSION = 1

def _cps(*codepoints: int) -> frozenset[str]:
    return frozenset(chr(c) for c in codepoints)


# Every character class is listed by code point so that ports cannot drift.
WHITESPACE = _cps(
    0x20, 0x09, 0x0A, 0x0D, 0x0C, 0x0B, 0x85, 0xA0, 0x1680,
    0x2000, 0x2001, 0x2002, 0x2003, 0x2004, 0x2005, 0x2006, 0x2007, 0x2008,
    0x2009, 0x200A, 0x2028, 0x2029, 0x202F, 0x205F, 0x3000,
)
# . ! ? … । ॥ 。 ！ ？
TERMINATORS = _cps(0x2E, 0x21, 0x3F, 0x2026, 0x0964, 0x0965, 0x3002, 0xFF01, 0xFF1F)
# " ' ) ] } ’ ” » › 》 」 』
CLOSERS = _cps(0x22, 0x27, 0x29, 0x5D, 0x7D, 0x2019, 0x201D, 0xBB, 0x203A, 0x300B, 0x300D, 0x300F)
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
)
AMBIGUOUS = frozenset(
    {
        "etc", "inc", "ltd", "co", "corp", "approx", "dept", "govt", "est",
        "fig", "no", "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep",
        "sept", "oct", "nov", "dec",
    }
)


@dataclass(frozen=True)
class Options:
    min_chars: int = 20
    first_min_chars: int = 1
    max_chars: int = 400
    eager_first_clause: bool = False
    first_clause_min_chars: int = 24


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


def _candidates(text: str, start: int, final: bool, clause: tuple[int, int] | None) -> list[_Candidate]:
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
        if _is_ws(text[j - 1]):
            # Cut before the whitespace; never produce an empty piece.
            if _strip(text[start : j - 1]):
                return j - 1
    # Hard cut. Never cut before a combining mark or a joiner, or right after a
    # virama or a joiner, which would split an Indic conjunct or a cluster.
    cut = window_end
    while cut > start + 1 and (
        _is_mark(text[cut]) or text[cut] in JOINERS
        or text[cut - 1] in VIRAMAS or text[cut - 1] in JOINERS
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

    # Trimming the buffer between pushes is an optimisation only; tests turn it
    # off to compare against the whole text held at once.
    _trim = True

    def __init__(self, options: Options | None = None) -> None:
        self.opts = options or Options()
        self._buf = ""
        self._gs = 0  # where the current group starts in _buf; before it is look-behind only
        self._first = True
        self._wrapping = False  # inside a wrapped group: no merging until it ends

    def push(self, text: str) -> list[str]:
        self._buf += text
        return self._drain(final=False)

    def flush(self) -> list[str]:
        out = self._drain(final=True)
        self._buf = ""
        self._gs = 0
        self._first = True
        self._wrapping = False
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
        while True:
            clause = (gs, o.first_clause_min_chars) if (o.eager_first_clause and self._first) else None
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
        keep = _keep_from(text, gs) if self._trim else 0
        self._buf = text[keep:]
        self._gs = gs - keep
        return out


def split_sentences(text: str, options: Options | None = None) -> list[str]:
    s = Segmenter(options)
    return s.push(text) + s.flush()
