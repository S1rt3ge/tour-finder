# Разведка: Waavo API (agregator, `joinastra.waavo.com`) — ДЖЕКПОТ

> Дата: 2026-07-18. Источник найден через astrature.lv (агрегатор всех балтийских
> операторов). Astrature = обёртка вокруг **Waavo** — движка поиска туров, который
> отдаёт ВСЕХ операторов одним чистым JSON API. Без авторизации, без бот-блока.

## TL;DR — это лучший источник из всех

Один REST JSON API возвращает офферы **всех операторов сразу** (в одной выдаче видел
teztour, novaturas, joinup, coral, anextour), плюс встроенные **TripAdvisor-рейтинги**,
**цену было/стало** и **дип-линки**. Превосходит наш нынешний Join Up (один оператор)
по всем статьям и делает ненужными отдельные модули Novatours/Tez/Anex/Coral.

## Доступ

- Хост инстанса astrature: **`https://joinastra.waavo.com`** (из `data-host` виджета).
  У каждого партнёра Waavo свой сабдомен; astrature = `joinastra`.
- **Без авторизации, без куки, без бот-защиты.** Обычный GET + `Referer`, отдаёт JSON.
- Astrature встраивает Waavo через iframe (`waavo_loader.min.js` → `iframe6.min.js`),
  но API дёргается напрямую.

## Эндпоинты (проверены живьём)

| Endpoint | Назначение |
|---|---|
| `GET /api/v1/travels/search` | Полный поиск. `data.offers[]` (вложенная структура) |
| `GET /api/v1/cheap_travels_search/` | **Горящие / last-minute** — наш кейс. `results[]` (плоская) |
| `GET /api/v1/travels/offer/details` | Детали оффера (по offerKey) |
| `GET /api/v1/hotel/details` | Детали отеля |
| `GET /travels_search/cheap_travels_filters?language=%language%` | Значения фильтров |

## Параметры поиска (из бандла `cdn/travels-search/index.js`)

`departureAirport` (код, напр. `RIX`; можно список через запятую), `dateFrom`,
`dateTo`, `adults`, `children`, `childrenAge`, `durationFrom`, `durationTo`,
`mealGroupFrom` (BB/HB/AI/RO), `operator` (список), `country`/`countries`, `stars`,
`tripAdvisorRating`, `segments`, `outboundTime`, пагинация `page`/`offset`/`limit`.
Одна страница = 100 офферов.

## Структура оффера (`cheap_travels_search` results[])

```
offerKey, operatorCode (novaturas|joinup|teztour|coral|anextour|itaka|...)
hotelId, hotelName, hotelRating (звёзды), hotelLatitude, hotelLongitude
tripadvisorRating, tripadvisorRatingsCount   ← ОТЗЫВЫ ВСТРОЕНЫ (наш v3 даром)
roomName, mealGroupCode (BB/HB/AI/RO), mealTranslation
countryName, countryId, cityName, arrivalCityId
departureAirportCode, departureAirport
adults, children, childrenAge[], date (вылет), duration, tripDuration
price, priceBefore  ← БЫЛО/СТАЛО ВСТРОЕНО, pricePerPerson, currency
transferIncluded, totalOrders, images[]
link (дип-линк на astrature→оператор с проброшенными параметрами)
```

`/api/v1/travels/search` — та же инфа, но вложенно: `offer.hotel{...tripadvisor...}`,
`offer.room.meal.group.code`, `offer.region.country`, `offer.operator.code`,
`offer.pricing{price, priceBefore, currency}`, `offer.hotelUrl`, `offer.reservationUrl`.

## Почему это меняет всё

| | Join Up (текущий) | **Waavo** |
|---|---|---|
| Операторов | 1 | **все сразу** (5+) |
| Страны | 8 | все, что летают операторы (шире) |
| Отзывы | нет (v3 не сделан) | **TripAdvisor встроен** |
| Было/стало | считаем сами | **`priceBefore` встроен** |
| Дип-линк | сами собирали | **`link` готов** |
| Доступ | открытый REST | открытый REST (так же легко) |
| Матчинг отелей между операторами | — | `hotelId` от Waavo единый → **сравнение цен на один отель у разных операторов из коробки** |

## ⚠️ ВАЖНО: Waavo НЕ полный по каждому оператору

Проверено на практике (наблюдение пользователя 2026-07-18): конкретный тур, который
есть **напрямую у Join Up**, в выдаче astrature/Waavo **НЕ показывался**. Агрегатор даёт
широту (все операторы), но жертвует полнотой по каждому: кеширует реже источника,
роняет часть офферов (динамические пакеты, отдельные комбинации номер/питание/отель),
возможны задержки обновления. То есть Waavo — **не строгое надмножество** Join Up.

## Риск

Тот же класс, что и Join Up: неофициальный API, может смениться/закрыться. Плюс
зависимость от посредника (Waavo/astrature), а не от самих операторов, И неполнота
выдачи (см. выше). Смягчение: вежливый троттл, бережный сбор.

## Вывод / рекомендация (пересмотрено)

**Не заменять Join Up, а добавить Waavo ВТОРЫМ источником.** Раз Waavo неполон:

- **Join Up direct остаётся** — полное покрытие этого оператора (включая туры, которых
  на агрегаторе нет).
- **Waavo добавляем ради ОСТАЛЬНЫХ операторов** (Novatours, Tez, Anex, Coral) — их
  никак иначе дёшево не взять, плюс TripAdvisor-отзывы и было/стало.
- Чтобы не задваивать Join Up: тянуть Waavo со ВСЕМИ операторами КРОМЕ joinup
  (joinup берём напрямую, он полнее). Либо при задвоении оффера предпочитать
  Join-Up-direct как авторитетный.

Итог: полный Join Up + широкое покрытие остальных. Пайплайн переиспользуется как есть;
новый модуль `sources/waavo.py`, поле `source` в схеме уже поддерживает несколько
источников. Отзывы Waavo (TripAdvisor) можно лить в нашу `hotel_reviews` — закрывает v3
для не-joinup отелей даром.
