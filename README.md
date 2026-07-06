# tour-finder

Last-minute агрегатор туров из Риги. Продукт: [SPEC.md](SPEC.md).
Источник данных: Join Up Baltic через неофициальный JSON API — [docs/joinup-api-recon.md](docs/joinup-api-recon.md).

## Запуск (Windows)

```powershell
py -3.12 -m venv .venv
.venv\Scripts\pip install -e .

# сбор данных (обычные + горящие туры, вылет из Риги, окно 30 дней)
.venv\Scripts\python -m tourfinder.cli fetch

# веб-интерфейс на http://127.0.0.1:8000
.venv\Scripts\python -m tourfinder.cli serve
```

`fetch` можно ограничить для проверки: `fetch --destinations c_8 --max-pages 2 --days 14`.
Числа по базе: `python -m tourfinder.cli stats`.

БД — SQLite в `data/tourfinder.db` (в git не попадает). Каждый запуск `fetch`
пишет снимок цены по каждому найденному офферу — история копится с первого дня.
