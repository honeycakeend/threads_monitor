from bot.i18n import language_from_code
from bot.models import SearchResult
from bot.search_service import format_search_result


def test_language_selection_uses_russian_only_for_ru_locale():
    assert language_from_code("ru") == "ru"
    assert language_from_code("ru-RU") == "ru"
    assert language_from_code("RU_ru") == "ru"
    assert language_from_code("en") == "en"
    assert language_from_code("uk") == "en"
    assert language_from_code(None) == "en"


def test_empty_search_result_is_localized():
    result = SearchResult(query="example", posts=[], total=0)
    assert "ничего не найдено" in format_search_result(result, language="ru")[0]
    assert "No results found" in format_search_result(result, language="en")[0]
