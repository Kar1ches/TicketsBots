# 🎟 Ticket Monitor Bot

Бот мониторит билеты на театральных площадках (etix, eventbrite, ticketleap и др.)
и присылает алерты в Telegram когда появляются или пропадают билеты.

---

## 📁 Структура файлов

```
ticket_bot/
├── bot.py           ← основной код бота
├── events.json      ← список событий для мониторинга
├── requirements.txt ← зависимости Python
├── railway.toml     ← конфиг для Railway
└── README.md
```

---

## ⚙️ Шаг 1 — Создать Telegram бота

1. Открой Telegram, найди **@BotFather**
2. Напиши `/newbot`
3. Придумай имя (например: `Ticket Alert`)
4. Придумай username (например: `my_ticket_alert_bot`)
5. BotFather пришлёт **токен** — сохрани его (выглядит как `7123456789:AAF...`)

**Получить свой Chat ID:**
1. Напиши любое сообщение своему боту
2. Открой в браузере: `https://api.telegram.org/bot<ВАШ_ТОКЕН>/getUpdates`
3. Найди `"chat":{"id": XXXXXXXXX}` — это твой Chat ID

---

## 📋 Шаг 2 — Настроить events.json

Отредактируй файл `events.json` — добавь события которые хочешь мониторить:

```json
[
  {
    "name": "Название шоу",
    "venue": "Название театра",
    "date": "Дата показа",
    "url": "https://www.etix.com/ticket/p/XXXXXXX/..."
  },
  {
    "name": "Второе шоу",
    "venue": "Ellen Eccles Theatre",
    "date": "Apr 5, 2026",
    "url": "https://www.etix.com/ticket/p/YYYYYYY/..."
  }
]
```

**Правила URL:**
- Копируй точный URL страницы события
- Поддерживаются: etix.com, eventbrite.com, ticketleap.com, showclix.com и любые другие сайты театров
- Можно добавлять сколько угодно событий

---

## 🚀 Шаг 3 — Запустить на Railway (бесплатно)

### 3.1 Подготовка
1. Создай аккаунт на [railway.app](https://railway.app) (через GitHub)
2. Установи [GitHub Desktop](https://desktop.github.com) или используй git
3. Создай новый репозиторий на GitHub, загрузи все файлы бота

### 3.2 Деплой
1. На Railway нажми **New Project → Deploy from GitHub repo**
2. Выбери свой репозиторий
3. Railway автоматически определит Python и установит зависимости

### 3.3 Переменные окружения (ОБЯЗАТЕЛЬНО)
В Railway зайди в **Variables** и добавь:

| Variable          | Value                        |
|-------------------|------------------------------|
| `TELEGRAM_TOKEN`  | токен от BotFather           |
| `TELEGRAM_CHAT_ID`| твой Chat ID                 |
| `CHECK_INTERVAL`  | `60` (проверка каждые 60 сек)|

4. После добавления переменных Railway перезапустит бота автоматически

---

## 📱 Что будет приходить в Telegram

**Когда появляются билеты:**
```
🎟 TICKETS AVAILABLE
━━━━━━━━━━━━━━━━━━
🎭 Event: Teelin Irish Dance
📍 Venue: The Weinberg Center For The Arts
📅 Date: Mar 14, 2026 7:00 PM
🔍 Detail: 3 ticket option(s) found
⏰ Checked: Mar 18, 2026 14:32
━━━━━━━━━━━━━━━━━━
🔗 Open page
```

**При старте бота:**
```
🤖 Ticket Monitor started
Checking every 60s

Watching 5 event(s):
  • Teelin Irish Dance
  • Example Show - Ellen Eccles Theatre
  ...
```

---

## ➕ Как добавить новое событие

Просто добавь новый объект в `events.json`, сохрани файл и запушь в GitHub — Railway автоматически перезапустит бота.

---

## 🔧 Переменные окружения

| Variable          | Описание                              | По умолчанию |
|-------------------|---------------------------------------|--------------|
| `TELEGRAM_TOKEN`  | Токен Telegram бота (обязательно)     | —            |
| `TELEGRAM_CHAT_ID`| Твой Telegram Chat ID (обязательно)   | —            |
| `CHECK_INTERVAL`  | Интервал проверки в секундах          | `60`         |
| `EVENTS_FILE`     | Путь к файлу событий                  | `events.json`|

---

## ⚠️ Важные моменты

- **Не ставь интервал меньше 30 секунд** — сайты могут заблокировать по IP
- Бот отправляет алерт только при **изменении статуса** (был sold out → появились билеты)
- Если сайт временно недоступен — бот просто пропускает итерацию и продолжает работу
- Railway бесплатный план даёт $5 кредитов/месяц — на лёгкий бот хватает

---

## 🆘 Проблемы?

Напиши Угуру — он знает как починить 😄
