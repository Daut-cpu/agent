# Агенты на Claude API

В репозитории два агента:

- **System Analyst Agent** (`analyst/`, `web/`) — AI-агент для системного и бизнес-аналитика: интервью, требования, ревью, архитектура, API, диаграммы, тест-кейсы. Подробнее в [analyst/README.md](analyst/README.md).
- **Виртуальный персонаж** — описан ниже.

# Виртуальный персонаж

ИИ-собеседник с характером и памятью на базе Claude API. По умолчанию это **Ася**:
24 года, Калининград, иллюстратор детских книг и астроном-любитель
(`personas/asya.json`).

Что умеет:

- **Держит образ.** Предыстория, характер, манера речи и границы задаются в JSON.
- **Помнит собеседника.** Когда ты рассказываешь о себе что-то важное, модель
  вызывает инструмент `remember_fact`. Факты и история диалога сохраняются в
  `data/<персона>.json` и остаются после перезапуска.
- **Отвечает потоково**, как в мессенджере.
- **Длинные разговоры** сжимаются на стороне сервера (compaction), а личность
  персонажа кэшируется (prompt caching), чтобы запросы были дешевле.
- **Отказ модели.** Если запрос отклонён, API сам повторяет его на резервной
  модели (`fallbacks: "default"`). Если отказ остался, персонаж мягко меняет
  тему, а отклонённая реплика не попадает в историю.

## Запуск

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=...        # или `ant auth login`
python -m character
```

Параметры:

```bash
python -m character --persona personas/asya.json --memory data/asya.json \
                    --model claude-opus-5 --effort medium
```

Команды в чате: `/memory` (что персонаж о тебе помнит), `/reset` (новый разговор,
факты сохраняются), `/forget` (стереть всё), `/exit`.

## Свой персонаж

Скопируй `personas/asya.json` и поменяй поля: `name`, `age`, `city`, `tagline`,
`backstory`, `personality`, `speech_style`, `interests`, `boundaries`, `greeting`.
Затем запусти с `--persona personas/<файл>.json`. У каждой персоны своя память.

## Структура

```
character/
├── persona.py   # Persona: загрузка JSON и системный промпт
├── memory.py    # Memory: факты о собеседнике и история (JSON-файл)
├── agent.py     # Character: запрос к Claude, цикл инструментов, откат при ошибках
└── cli.py       # чат в терминале
personas/asya.json
tests/           # тесты на фейковом клиенте, без обращения к API
```

## Тесты

```bash
pip install pytest
python -m pytest -q
```
