from typing import Literal

Language = Literal["ru", "en"]


def language_from_code(language_code: str | None) -> Language:
    if not language_code:
        return "en"
    normalized = language_code.strip().lower().replace("_", "-")
    return "ru" if normalized.split("-", 1)[0] == "ru" else "en"


def choose(language: Language, *, ru: str, en: str) -> str:
    return ru if language == "ru" else en


HELP_TEXT: dict[Language, str] = {
    "ru": """
<b>Threads Monitor Bot</b>

<b>Пул фраз</b> (общая база для всех пользователей бота)
/add &lt;фраза&gt; — добавить фразу в пул
/add фраза1 | фраза2 — несколько фраз сразу
/pool — посмотреть весь пул
/list — то же, что /pool
/remove &lt;id&gt; — удалить фразу по номеру
/remove &lt;текст фразы&gt; — удалить по тексту

<b>Мониторинг</b>
/monitor on — включить уведомления в этот чат
/monitor off — выключить уведомления
/interval — текущий интервал и расход лимита
/interval 30 — интервал проверки в минутах
/run — проверить пул прямо сейчас
/status — статус

<b>Разовый поиск</b>
/search &lt;фраза&gt; — поиск один раз без сохранения

<b>Подключение Threads (администратор, личный чат)</b>
/connect_threads — безопасно подключить аккаунт через OAuth
/threads_status — статус подключения
/disconnect_threads — удалить сохранённый токен и отключить аккаунт

<b>Примеры</b>
/add python asyncio
/add startup idea | marketing threads
/pool
/remove 2
/monitor on
/interval 30
""".strip(),
    "en": """
<b>Threads Monitor Bot</b>

<b>Phrase pool</b> (shared by the bot workspace)
/add &lt;phrase&gt; — add a phrase to the pool
/add phrase one | phrase two — add several phrases
/pool — show the complete pool
/list — same as /pool
/remove &lt;id&gt; — remove a phrase by number
/remove &lt;phrase&gt; — remove a phrase by text

<b>Monitoring</b>
/monitor on — enable notifications in this chat
/monitor off — disable notifications
/interval — show the interval and estimated usage
/interval 30 — set the interval in minutes
/run — check the pool now
/status — show status

<b>One-time search</b>
/search &lt;phrase&gt; — search once without saving

<b>Threads connection (administrator, private chat)</b>
/connect_threads — securely connect through OAuth
/threads_status — show connection status
/disconnect_threads — delete the saved token and disconnect

<b>Examples</b>
/add python asyncio
/add startup idea | marketing threads
/pool
/remove 2
/monitor on
/interval 30
""".strip(),
}


REVIEW_HELP_TEXT: dict[Language, str] = {
    "ru": """
<b>Доступ для проверки Meta App Review</b>

/connect_threads — подключить тестовый Threads-аккаунт через OAuth
/threads_status — показать статус изолированного review-подключения
/search &lt;фраза&gt; — выполнить поиск недавних публикаций
/search top &lt;фраза&gt; — выполнить поиск популярных публикаций
/disconnect_threads — удалить изолированный review-токен
/help — показать эту инструкцию

Review-доступ временный. Он не позволяет менять рабочий пул, чат уведомлений,
интервал или основной Threads-аккаунт.
""".strip(),
    "en": """
<b>Meta App Review access</b>

/connect_threads — connect a Threads test account through OAuth
/threads_status — show the isolated review connection status
/search &lt;keyword&gt; — run a recent keyword search
/search top &lt;keyword&gt; — run a top-post keyword search
/disconnect_threads — delete the isolated review token
/help — show these instructions

Review access is temporary. It cannot change the production monitoring pool,
notification chat, interval, or production Threads connection.
""".strip(),
}


BOT_COMMANDS: dict[Language, tuple[tuple[str, str], ...]] = {
    "ru": (
        ("start", "Запустить бота и показать справку"),
        ("help", "Показать все команды"),
        ("search", "Разовый поиск в Threads"),
        ("add", "Добавить фразу в общий пул"),
        ("pool", "Показать пул фраз"),
        ("remove", "Удалить фразу из пула"),
        ("monitor", "Включить или выключить уведомления"),
        ("interval", "Показать или изменить интервал"),
        ("run", "Проверить пул сейчас"),
        ("status", "Показать статус бота"),
        ("connect_threads", "Подключить Threads через OAuth"),
        ("threads_status", "Показать статус Threads"),
        ("disconnect_threads", "Отключить Threads и удалить токен"),
    ),
    "en": (
        ("start", "Start the bot and show help"),
        ("help", "Show all commands"),
        ("search", "Run a one-time Threads search"),
        ("add", "Add a phrase to the shared pool"),
        ("pool", "Show the phrase pool"),
        ("remove", "Remove a phrase from the pool"),
        ("monitor", "Enable or disable notifications"),
        ("interval", "Show or change the interval"),
        ("run", "Check the pool now"),
        ("status", "Show bot status"),
        ("connect_threads", "Connect Threads through OAuth"),
        ("threads_status", "Show Threads connection status"),
        ("disconnect_threads", "Disconnect Threads and delete the token"),
    ),
}
