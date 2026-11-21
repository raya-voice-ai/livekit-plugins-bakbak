"""Builds cases.json from the inputs below, using reference.py as the oracle.

Run from this directory:  python3 build_cases.py
Review the diff of cases.json by eye before committing: the reference is the
spec, but a human decides whether each expected output is what we want.
Also checks the streaming invariant for every case under several chunkings.
"""

from __future__ import annotations

import json
import random

from reference import VERSION, Options, Segmenter, split_sentences


class _WholeText(Segmenter):
    """The oracle's oracle: never trims its buffer, so every look-behind sees the whole text."""

    _trim = False

# (name, input, options-overrides). Inputs are real-looking agent replies.
INPUTS: list[tuple[str, str, dict]] = [
    ("hindi_danda", "आपका पैकेज आज शाम तक पहुँच जाएगा। राहुल आपको कॉल करेगा।", {}),
    ("double_danda", "पहला वाक्य यहाँ समाप्त होता है॥ दूसरा वाक्य यहाँ है॥", {}),
    ("code_mixed", "आपका package आज पहुँच जाएगा। Delivery boy राहुल call करेगा।", {}),
    ("latin_mix", "Hello there, how are you doing today? I am fine! Thanks for asking.", {}),
    ("decimal_and_domain", "The value is 3.14 today. Visit getraya.app for details. Mail a@b.com now.", {}),
    ("titles_latin", "Mr. Sharma and Dr. Rao will join us. Prof. Iyer is travelling.", {}),
    ("titles_devanagari", "डॉ. शर्मा आज क्लिनिक में हैं। प्रो. वर्मा कल आएँगे।", {}),
    ("initials", "A. P. J. Abdul Kalam was a scientist. He was also president.", {}),
    ("plan_b", "We will go with plan B. Next week we review it again.", {}),
    ("ambiguous_no_digit", "Please call extension No. 5 for billing support today. Thanks.", {}),
    ("ambiguous_etc_lower", "Bring pens, paper, etc. and also a calculator please. See you.", {}),
    ("ambiguous_etc_upper", "We sell pens, paper, etc. Our store opens at nine tomorrow.", {}),
    ("ambiguous_inc_devanagari", "मैं Acme Inc. के साथ काम करता हूँ। यह अच्छी कंपनी है।", {}),
    ("time_am_pm", "मैं कल 10 a.m. पर आऊँगा, ठीक है? आपका दिन शुभ हो।", {}),
    ("list_markers", "आपके विकल्प हैं:\n1. रिचार्ज करें\n2. बैलेंस देखें\n3. एजेंट से बात करें", {}),
    ("newlines", "पहली पंक्ति बिना विराम के\nदूसरी पंक्ति भी बिना विराम के\nतीसरी", {}),
    ("quotes_closers", "उसने कहा, \"मैं कल ज़रूर आऊँगा।\" फिर वह चला गया।", {}),
    ("question_exclaim_runs", "क्या सच में?! यह तो कमाल हो गया!!! अब आगे क्या करें?", {}),
    ("ellipsis", "रुकिए... मैं देखता हूँ। हाँ, आपका ऑर्डर मिल गया है।", {}),
    ("no_space_after_danda", "यह पहला है।यह उसी वाक्य का हिस्सा माना जाएगा। अंत।", {}),
    ("short_first_goes_alone", "जी हाँ। आपका रिचार्ज सफलतापूर्वक हो गया है और बैलेंस अपडेट हो गया है।", {}),
    ("short_middle_merges", "आपका रिचार्ज हो गया है। ठीक है। आपका नया बैलेंस पाँच सौ रुपये है।", {}),
    ("short_first_min_20", "जी हाँ। ठीक है। आपका रिचार्ज सफलतापूर्वक हो गया है।", {"first_min_chars": 20}),
    ("punctuation_only_groups", "। ... नमस्ते, मैं आपकी मदद कर सकता हूँ। !!", {}),
    ("emoji_only_tail", "धन्यवाद, आपका दिन शुभ हो। 🙏", {}),
    ("trailing_no_terminator", "आपका बैलेंस पाँच सौ रुपये है। कुछ और मदद चाहिए", {}),
    ("whitespace_only", "   \n\t  ", {}),
    ("empty", "", {}),
    ("zwj_zwnj_inside_words", "क्‍ष और र‍य जैसे रूप सही रहें। यह दूसरा वाक्य है।", {}),
    ("tamil", "வணக்கம், நான் உங்களுக்கு எப்படி உதவ முடியும்? உங்கள் ஆர்டர் நாளை வரும்.", {}),
    ("bengali", "আপনার অর্ডার আগামীকাল পৌঁছাবে। ধন্যবাদ, ভালো থাকবেন।", {}),
    ("telugu", "మీ ఆర్డర్ రేపు వస్తుంది. ధన్యవాదాలు, మళ్లీ కలుద్దాం.", {}),
    ("long_run_wrapped_at_comma", ("यह एक बहुत लंबा वाक्य है, जिसमें कई उपवाक्य हैं " * 6).strip() + "।", {"max_chars": 120}),
    ("long_run_wrapped_at_space", ("शब्द " * 80).strip(), {"max_chars": 100}),
    ("long_run_hard_cut_conjunct", "क्ष" * 70, {"max_chars": 50}),
    ("wrapped_tail_not_merged", ("लंबा " * 30) + "अंत। यह अगला वाक्य काफ़ी लंबा है ताकि मिलाया न जाए।", {"max_chars": 60}),
    ("eager_first_clause", "अगर आप चाहें, तो मैं आपको कल सुबह दस बजे कॉल कर सकता हूँ। धन्यवाद।",
     {"eager_first_clause": True, "first_clause_min_chars": 10}),
    ("eager_clause_too_short", "हाँ, मैं कल सुबह दस बजे आपको कॉल कर सकता हूँ। धन्यवाद।",
     {"eager_first_clause": True, "first_clause_min_chars": 24}),
    ("eager_only_first", "पहले, यह होगा और फिर वह होगा। दूसरे, यह भी होगा, और वह भी होगा।",
     {"eager_first_clause": True, "first_clause_min_chars": 4}),
    ("rupee_abbrev", "आपका बिल Rs. 450 है। कृपया रु. 450 का भुगतान आज करें।", {}),
    ("crlf", "पहली पंक्ति।\r\nदूसरी पंक्ति।\r\n", {}),
    ("url_and_path", "Open https://example.com/a.b/c.html now. Then press enter.", {}),
    # Look-behind across what a streaming segmenter has already emitted:
    ("lookbehind_list_marker", "ok1.. 1. 1", {"min_chars": 0}),
    ("lookbehind_word_after_hard_cut", "abcdefghidr. Next sentence is here.", {"max_chars": 10, "min_chars": 0}),
    ("lookbehind_digits_after_hard_cut", "abcdefghi12. Next one.", {"max_chars": 10, "min_chars": 0}),
]


def chunkings(text: str, rng: random.Random) -> list[list[str]]:
    cps = list(text)  # Python indexes by code point already
    out = [[text], cps]
    for size in (2, 3, 7):
        out.append(["".join(cps[i : i + size]) for i in range(0, len(cps), size)])
    for _ in range(5):
        cuts = sorted(rng.sample(range(1, len(cps)), k=min(len(cps) - 1, rng.randint(1, 12)))) if len(cps) > 1 else []
        pieces, prev = [], 0
        for c in cuts + [len(cps)]:
            pieces.append("".join(cps[prev:c]))
            prev = c
        out.append(pieces)
    return out


def main() -> None:
    rng = random.Random(20261121)
    cases = []
    for name, text, overrides in INPUTS:
        opts = Options(**overrides)
        expected = split_sentences(text, opts)
        whole = _WholeText(opts)
        assert whole.push(text) + whole.flush() == expected, (name, "batch differs from whole text")
        for pieces in chunkings(text, rng):
            seg = Segmenter(opts)
            got: list[str] = []
            for p in pieces:
                got += seg.push(p)
            got += seg.flush()
            assert got == expected, (name, pieces, got, expected)
        for s in expected:
            assert len(s) <= opts.max_chars, (name, s)
        cases.append({"name": name, "options": overrides, "input": text, "sentences": expected})
    doc = {
        "version": VERSION,
        "defaults": Options().__dict__,
        "note": "Generated by build_cases.py from reference.py. Lengths are in code points.",
        "cases": cases,
    }
    with open("cases.json", "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"{len(cases)} cases, streaming invariant holds for all chunkings")


if __name__ == "__main__":
    main()
