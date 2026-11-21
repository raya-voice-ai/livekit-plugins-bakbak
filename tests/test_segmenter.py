"""The segmenter against conformance/segmenter/cases.json and the streaming invariant."""

from __future__ import annotations

import functools
import json
import random
import sys
import time
import types
from pathlib import Path
from typing import Any

import pytest

from livekit.plugins.bakbak import SentenceTokenizer
from livekit.plugins.bakbak._segmenter import (
    MAX_CHARS_LIMIT,
    Options,
    Segmenter,
    _lookbehind,
    split_sentences,
)

CASES_FILE = Path(__file__).parent.parent / "conformance" / "segmenter" / "cases.json"
CASES: dict[str, Any] = json.loads(CASES_FILE.read_text(encoding="utf-8"))


def _options(raw: dict[str, Any]) -> Options:
    return Options(**{**CASES["defaults"], **raw})


def _streamed(text: str, cuts: list[int], options: Options) -> list[str]:
    seg = Segmenter(options)
    out: list[str] = []
    start = 0
    for cut in [*cuts, len(text)]:
        out += seg.push(text[start:cut])
        start = cut
    return out + seg.flush()


def test_defaults_match_the_spec() -> None:
    assert Options() == _options({})


@pytest.mark.parametrize("case", CASES["cases"], ids=[c["name"] for c in CASES["cases"]])
def test_case(case: dict[str, Any]) -> None:
    assert split_sentences(case["input"], _options(case["options"])) == case["sentences"]


@pytest.mark.parametrize("case", CASES["cases"], ids=[c["name"] for c in CASES["cases"]])
def test_streaming_invariant(case: dict[str, Any]) -> None:
    text: str = case["input"]
    options = _options(case["options"])
    expected = case["sentences"]
    # One piece.
    assert _streamed(text, [], options) == expected
    # One code point per piece.
    assert _streamed(text, list(range(1, len(text))), options) == expected
    # Several random cuts, reproducibly.
    rng = random.Random(case["name"])
    for _ in range(25 if len(text) > 1 else 0):
        cuts = sorted(rng.sample(range(1, len(text)), k=min(len(text) - 1, rng.randint(1, 12))))
        assert _streamed(text, cuts, options) == expected, cuts


def test_sentences_are_released_as_soon_as_their_end_is_certain() -> None:
    seg = Segmenter()
    assert seg.push("नमस्ते, आप कैसे हैं? मैं") == ["नमस्ते, आप कैसे हैं?"]
    assert seg.push(" ठीक हूँ।") == []  # the end of the text is not certain yet
    assert seg.flush() == ["मैं ठीक हूँ।"]


def test_flush_starts_a_new_first_sentence() -> None:
    seg = Segmenter(Options(min_chars=20))
    assert seg.push("Hi. ") == []
    assert seg.flush() == ["Hi."]
    # The first sentence after a flush uses first_min_chars again.
    assert seg.push("Ok. Then") == ["Ok."]


def test_max_chars_is_bounded_by_the_api_limit() -> None:
    with pytest.raises(ValueError, match="max_chars"):
        Options(max_chars=MAX_CHARS_LIMIT + 1)
    with pytest.raises(ValueError, match="max_chars"):
        Options(max_chars=0)


def test_long_tokens_stream_cheaply() -> None:
    # A long sentence pushed a few characters at a time must not rescan the
    # buffer on every push (the skip in Segmenter.push), and must still wrap.
    text = ("यह एक बहुत लंबा वाक्य है जिसमें कोई विराम नहीं है " * 40).strip()
    expected = split_sentences(text)
    assert _streamed(text, list(range(3, len(text), 3)), Options()) == expected
    assert all(len(s) <= Options().max_chars for s in expected)


async def test_tokenizer_stream_matches_tokenize() -> None:
    tokenizer = SentenceTokenizer()
    text = "आपका पैकेज आज पहुँच जाएगा। Delivery boy राहुल call करेगा। Dr. Sharma 3.14 बजे आएँगे!"
    stream = tokenizer.stream()
    for ch in text:
        stream.push_text(ch)
    stream.end_input()
    streamed = [ev.token async for ev in stream]
    assert streamed == tokenizer.tokenize(text)
    assert streamed == split_sentences(text)


async def test_tokenizer_stream_flush_ends_a_segment() -> None:
    stream = SentenceTokenizer().stream()
    stream.push_text("पहला वाक्य। दूसरा")
    stream.flush()
    stream.push_text("तीसरा।")
    stream.end_input()
    events = [ev async for ev in stream]
    assert [ev.token for ev in events] == ["पहला वाक्य।", "दूसरा", "तीसरा।"]
    assert events[0].segment_id == events[1].segment_id != events[2].segment_id


async def test_tokenizer_stream_rejects_text_after_close() -> None:
    stream = SentenceTokenizer().stream()
    await stream.aclose()
    with pytest.raises(RuntimeError):
        stream.push_text("x")


def test_tokenizer_options_are_passed_through() -> None:
    tokenizer = SentenceTokenizer(min_chars=5, max_chars=50, eager_first_clause=True)
    assert tokenizer.options == Options(min_chars=5, max_chars=50, eager_first_clause=True)


def test_indic_sentence_tokenizer_is_a_deprecated_alias() -> None:
    from livekit.plugins.bakbak import IndicSentenceTokenizer

    with pytest.warns(DeprecationWarning, match="SentenceTokenizer"):
        tokenizer = IndicSentenceTokenizer(min_sentence_len=24, max_sentence_len=320)
    assert tokenizer.options.min_chars == 24
    assert tokenizer.options.max_chars == 320


@functools.cache
def _reference() -> Any:
    """conformance/segmenter/reference.py, the specification, loaded as a module.

    Executed from source so that nothing (no bytecode cache) is written into
    conformance/, which must match its manifest exactly."""
    name = "bakbak_segmenter_reference"
    path = CASES_FILE.parent / "reference.py"
    module = types.ModuleType(name)
    module.__file__ = str(path)
    sys.modules[name] = module  # its dataclasses look themselves up here
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), module.__dict__)
    return module


# Characters that exercise every rule: terminators, closers, soft delimiters,
# whitespace and newlines, Devanagari with virama and joiners, digits, titles,
# and words that a hard cut or a list marker makes look back past a group start.
_ALPHABET = [
    *"ab Dr. etc. 3.14 \n\t।॥?!…\"')]—,;:–क्षत्र\u200d\u200cा ि 12. A Z, x",
    "डॉ. ",
    "Mr. ",
    "no. 5 ",
    "e.g. ",
    "https://a.b/c ",
    "  ",
    "dr.",
    "ok1..",
    "\n2. ",
    "abcdefghi",
    "12.",
    # Long unbroken words and long runs of blanks, which the streaming look-behind
    # compresses (BEHAVIOR.md §7), and the less common whitespace characters.
    "क" * 25,
    "abcdefghijklmnop.",
    "1234567.",
    " " * 30,
    "\t \t  \t",
    "\n" + " " * 12 + "4. ",
    *"\r\x0b\x85\u2028\u3000",
]


@pytest.mark.parametrize("seed", range(60))
def test_matches_the_reference_on_random_text(seed: int) -> None:
    reference = _reference()
    rng = random.Random(seed)
    raw: dict[str, Any] = {
        "min_chars": rng.choice([0, 1, 5, 20]),
        "first_min_chars": rng.choice([1, 3, 10]),
        "max_chars": rng.choice([3, 5, 8, 10, 15, 40, 400]),
        "eager_first_clause": rng.random() < 0.5,
        "first_clause_min_chars": rng.choice([3, 10, 24]),
    }
    options = Options(**raw)
    ref_options = reference.Options(**raw)
    for _ in range(30):
        text = "".join(rng.choice(_ALPHABET) for _ in range(rng.randint(0, 120)))
        expected = reference.split_sentences(text, ref_options)
        assert split_sentences(text, options) == expected, (text, raw)
        cuts = sorted(rng.sample(range(1, len(text)), k=min(len(text) - 1, 8))) if text else []
        # Push by push, not just in total: each sentence is released when the
        # reference releases it.
        ours, theirs = Segmenter(options), reference.Segmenter(ref_options)
        start = 0
        for cut in [*cuts, len(text)]:
            piece = text[start:cut]
            assert ours.push(piece) == theirs.push(piece), (text, raw, cuts, cut)
            start = cut
        assert ours.flush() == theirs.flush(), (text, raw, cuts)


def test_lookbehind_matches_the_reference() -> None:
    reference = _reference()
    rng = random.Random(7)
    pieces = [
        *("क" * n for n in (1, 3, 7, 8, 9, 20)),
        " ",
        "   ",
        "\t",
        "\n",
        "ab",
        "12",
        ".",
        "डॉ",
    ]
    for _ in range(5000):
        text = "".join(rng.choice(pieces) for _ in range(rng.randint(1, 12)))
        gs = rng.randint(0, len(text))
        assert _lookbehind(text, gs) == reference._lookbehind(text, gs), (text, gs)


@pytest.mark.parametrize("seed", range(30))
def test_skipped_drains_never_delay_a_sentence(seed: int) -> None:
    """Each push releases exactly what a full rescan would release at that point."""
    rng = random.Random(1000 + seed)
    options = Options(
        min_chars=rng.choice([0, 5, 20]),
        max_chars=rng.choice([5, 10, 40, 400]),
        eager_first_clause=rng.random() < 0.5,
        first_clause_min_chars=rng.choice([3, 24]),
    )
    for _ in range(20):
        text = "".join(rng.choice(_ALPHABET) for _ in range(rng.randint(0, 120)))
        lazy, eager = Segmenter(options), Segmenter(options)
        eager._skip_idle = False
        start = 0
        for cut in (
            sorted(rng.sample(range(1, len(text)), k=min(len(text) - 1, 10))) if text else []
        ):
            assert lazy.push(text[start:cut]) == eager.push(text[start:cut]), (text, cut)
            start = cut
        assert lazy.push(text[start:]) == eager.push(text[start:])
        assert lazy.flush() == eager.flush()


LONG_TEXTS = {
    "no punctuation, no spaces": "क" * 200_000,
    "words, no punctuation": "शब्द " * 40_000,
    "sentences": "यह एक वाक्य है। " * 12_500,
    "mixed": "Dr. Sharma ने कहा, 3.14 ठीक है। " * 6_452,
    "long runs of spaces": ("एक" + " " * 998) * 200,
    "long runs of tabs after sentences": ("एक।" + "\t" * 997) * 200,
}


@pytest.mark.parametrize("name", list(LONG_TEXTS))
def test_segmentation_is_linear(name: str) -> None:
    """BEHAVIOR.md §7: 200,000 characters in well under a second, whole or streamed."""
    text = LONG_TEXTS[name]
    started = time.perf_counter()
    whole = split_sentences(text)
    assert time.perf_counter() - started < 0.5
    assert whole

    # Streamed in LLM-sized tokens, each push costs a few microseconds whatever came
    # before it; a rescan of the text so far would take minutes here.
    part = text[:50_000]
    started = time.perf_counter()
    seg = Segmenter()
    streamed: list[str] = []
    for i in range(0, len(part), 3):
        streamed += seg.push(part[i : i + 3])
    streamed += seg.flush()
    assert time.perf_counter() - started < 1.0
    assert streamed == split_sentences(part)
