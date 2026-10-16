"""Match query words using known Russian dictionary forms, within post text."""

import re
from functools import lru_cache
from unicodedata import normalize

from pymorphy3 import MorphAnalyzer


@lru_cache(maxsize=1)
def morphology() -> MorphAnalyzer:
    return MorphAnalyzer(lang="ru")


@lru_cache(maxsize=16384)
def word_forms(word: str) -> frozenset[str]:
    # Keep identifiers and foreign words exact. Do not guess stems of unknown
    # words: that could make an unrelated word pass the post-text filter.
    forms = {word}
    if re.fullmatch(r"[а-яё]+", word):
        forms.update(
            parsed.normal_form for parsed in morphology().parse(word) if parsed.is_known
        )
    return frozenset(forms)


def matches_words(text: str, query: str) -> bool:
    words = set(re.findall(r"\w+", normalize("NFC", query).casefold()))
    text_words = set(re.findall(r"\w+", normalize("NFC", text).casefold()))
    if not words:
        return False
    forms = {form for word in text_words for form in word_forms(word)}
    return all(word_forms(word) & forms for word in words)
