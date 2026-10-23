# Language Lab — сервер

Аккаунты и доступ, репетитор и тренировки для всех учеников. Ядро обучения — пакет `lp` из
`../learning-lang-platform` (подключён как редактируемая зависимость, код не копируется).

## Запуск для разработки
```
uv sync
uv run alembic upgrade head
uv run uvicorn app.main:app --reload
```
Ничего ставить в систему не нужно: без `LANGLAB_DATABASE_URL` поднимается свой Postgres из `var/pg`
(пакет `pgserver`) и останавливается вместе с сервером. Вручную: `uv run python -m app.devdb status|stop`.
Всё лишнее удаляется вместе с папкой `var/`.

## Коды доступа (закрытая бета)
```
uv run python -m app.codes new --note "Аня, коллега" --days 90 --expires 14
uv run python -m app.codes list
```
`invite` — личный код: срабатывает на одну регистрацию и сгорает. `promo` — на много регистраций,
для маркетинга (`--kind promo --uses 100`). Регистрация закрывается на `LANGLAB_MAX_LEARNERS` (10).

## Тесты
`uv run pytest` — у прогона свой временный Postgres и папки данных.
