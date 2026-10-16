from typing import Literal

Language = Literal["ru", "en"]


INSTRUCTIONS: dict[Language, str] = {
    "ru": """<b>Как пользоваться ботом</b>

Бот ищет фразы в тексте самих постов Threads, без комментариев, и присылает новые совпадения в рабочий чат.

<b>Для начала</b>
Нажмите «Меню» внизу чата → подключите Threads → добавьте фразы → включите уведомления.
Подключить аккаунт может администратор в личном чате с ботом.

<b>Кнопки в меню</b>
• <b>Фразы для отслеживания</b> — добавить или удалить слова и фразы, которые нужно искать. Например: «ищу дизайнера» найдёт и «ищу хороших дизайнеров». Формы русских слов учитываются; все слова должны быть в самом посте.
• <b>Частота проверки</b> — выбрать, как часто искать новые публикации.
• <b>Включить / Приостановить уведомления</b> — управлять отправкой в указанный рабочий чат.
• <b>Проверить сейчас</b> — запустить проверку всех сохранённых фраз.
• <b>Найти публикации</b> — разовый поиск последних или популярных постов. Фраза не сохраняется.
• <b>Подключение Threads</b> — подключить, сменить или отключить аккаунт. Доступно администратору в личном чате.

<b>Куда приходят посты</b>
Добавьте бота в нужную группу → отправьте там /menu → «Получать уведомления в этой группе» → подтвердите. Выбрать группу может пользователь с правами «Настройки + Threads». Новая группа заменит прежнюю; пауза уведомлений сохранится.

<b>Как сменить аккаунт</b>
«Меню» → «Подключение Threads» → «Сменить аккаунт». Откройте ссылку, авторизуйте нужный аккаунт и проверьте имя профиля перед подтверждением. Если открылся прежний аккаунт, переключитесь на нужный в браузере.
После подключения вернитесь в «Подключение Threads» и проверьте новый @username. Фразы и настройки сохранятся. Старое подключение работает до успешной авторизации нового.

Список фраз и частота проверки общие для команды. При первой проверке бот запоминает существующие посты; уведомления приходят только о новых.

<b>Доступ — для владельца</b>
«Меню» → «Пользователи и доступ» → «Добавить пользователя» → числовой Telegram ID → права → «Сохранить права». «Настройки + Threads» разрешает менять аккаунт. Отзыв доступа — в карточке пользователя. Изменения действуют сразу.

Вернуться к настройкам: кнопка «Меню». Эта инструкция: кнопка «Инструкция». Если нижние кнопки скрыты, раскройте клавиатуру значком рядом с полем ввода. Первый запуск: /start; также доступны /menu и /instructions.""",
    "en": """<b>How to use the bot</b>

The bot finds your phrases in the text of Threads posts, excluding replies, and sends new matches to your work chat.

<b>Getting started</b>
Tap Menu below → connect Threads → add phrases → enable notifications.
An administrator connects the account in a private chat with the bot.

<b>Menu buttons</b>
• <b>Tracked phrases</b> — add or remove words and phrases to search for. All query words must be in the post itself; extra words are allowed. Russian word forms are matched automatically.
• <b>Check frequency</b> — choose how often to look for new posts.
• <b>Enable / Pause notifications</b> — control delivery to the displayed work chat.
• <b>Check now</b> — check all saved phrases immediately.
• <b>Find posts</b> — search recent or popular posts once, without saving the phrase.
• <b>Threads connection</b> — connect, change or disconnect an account. Available to administrators in private chats.

<b>Where posts arrive</b>
Add the bot to your group → send /menu there → Receive notifications in this group → confirm. Requires Settings + Threads access. The new group replaces the previous one; paused notifications stay paused.

<b>Changing accounts</b>
Menu → Threads connection → Change account. Open the link, authorize the account you want to use, and check the profile name before confirming. If the previous account opens, switch accounts in your browser.
Return to Threads connection and check the new @username. Phrases and settings will be kept. The previous connection remains active until the new authorization succeeds.

Phrases and check frequency are shared by the team. The first check remembers existing posts; notifications are sent only for newer matches.

<b>Access — owner only</b>
Menu → Users and access → Add user → numeric Telegram ID → role → Save access. Settings + Threads also permits account changes. Revoke access from the user's card. Changes take effect immediately.

Settings: the Menu button. This guide: the Instructions button. If the bottom buttons are hidden, expand the keyboard using the icon next to the input field. First start: /start; /menu and /instructions are also available.""",
}


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

/menu — настройки с кнопками
/instructions — короткая инструкция

<b>Фразы для отслеживания</b> (общая база для всех пользователей бота)
/add &lt;фраза&gt; — добавить фразу в список
/add фраза1 | фраза2 — несколько фраз сразу
/pool — посмотреть весь список
/list — то же, что /pool
/remove &lt;id&gt; — удалить фразу по номеру
/remove &lt;текст фразы&gt; — удалить по тексту

<b>Мониторинг</b>
/monitor on — включить уведомления в этот чат
/monitor off — выключить уведомления
/interval — текущий интервал и расход лимита
/interval 30 — интервал проверки в минутах
/run — проверить фразы прямо сейчас
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

/menu — settings with buttons
/instructions — short usage guide

<b>Tracked phrases</b> (shared by the bot workspace)
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
        ("start", "Запустить бота и открыть меню"),
        ("menu", "Настройки бота"),
        ("instructions", "Инструкция — как пользоваться ботом"),
        ("help", "Показать все команды"),
        ("search", "Разовый поиск в Threads"),
        ("add", "Добавить фразу для отслеживания"),
        ("pool", "Показать список фраз"),
        ("remove", "Удалить фразу из списка"),
        ("monitor", "Включить или выключить уведомления"),
        ("interval", "Показать или изменить интервал"),
        ("run", "Проверить фразы сейчас"),
        ("status", "Показать статус бота"),
        ("connect_threads", "Подключить Threads через OAuth"),
        ("threads_status", "Показать статус Threads"),
        ("disconnect_threads", "Отключить Threads и удалить токен"),
    ),
    "en": (
        ("start", "Start the bot and open the menu"),
        ("menu", "Bot settings"),
        ("instructions", "Instructions — how to use the bot"),
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
