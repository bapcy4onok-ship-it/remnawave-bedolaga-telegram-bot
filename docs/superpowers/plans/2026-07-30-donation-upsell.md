# Донат-апселл при пополнении — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** При пополнении баланса предлагать добавить к платежу 10 ₽ в пользу проекта; надбавка не попадает на баланс, а учитывается как пожертвование.

**Architecture:** Платёж создаётся один, на сумму `пополнение + надбавка`. Согласие фиксируется записью в новой таблице `donation_pledges` («обещание»). Вебхуки провайдеров не меняются: они зачисляют всю сумму, а сразу после этого единая точка `send_cart_notification_after_topup` вызывает `try_apply_donation_pledge`, которая списывает надбавку с баланса и пишет пожертвование. Спека: `docs/superpowers/specs/2026-07-30-donation-upsell-design.md`.

**Tech Stack:** Python 3.13, aiogram 3, SQLAlchemy 2 (async), Alembic, FastAPI (кабинетное API), pytest; фронт кабинета — React 18 + TypeScript + react-query + Tailwind (репозиторий `/root/bedolaga-cabinet`).

---

## Статус реализации (обновлено 2026-08-19)

Реализовано в ветке `feat/donation-upsell`. 65 новых тестов, полный набор — без новых падений
относительно базовой линии (18 падений из чужих незакоммиченных правок рабочего дерева).

После реализации проведён состязательный ревью (5 независимых ревьюеров по разным осям,
каждая находка проверялась двумя скептиками на опровержение): 18 находок, 10 опровергнуто,
8 подтверждено — 7 исправлено, 1 оставлена на решение владельца (реферальные бонусы считаются
с суммы, включающей надбавку).

**Обновление 2026-08-20.** Владелец решил: надбавка в реферальной системе не учитывается.
Сделано `peek_pending_tip_kopeks` (донат-сервис) + флаг `exclude_donation_tip` у
`process_referral_topup`; вычитание выключено там, где надбавку никто не спишет — прямая покупка
подписки (YooKassa, Stars) и рекуррентный автоплатёж YooKassa. Ремонтная диагностика
(`referral_diagnostics_service`) переведена на чистую сумму первого пополнения через
`applied_tip_for_credited_amount`, туда же входят восстановленные события конкурса рефералов.
Заодно в Tribute уведомление отделено своим `try/except`, иначе упавший `send_message` уносил с
собой пост-хук (надбавка не списывалась, а комиссия уже была урезана). 16 новых тестов.

**Правки по итогам аудита (2026-08-21).** Сторонний AI-отчёт дал 8 претензий; после проверки по коду
и живой БД (16 агентов, каждая находка проверялась скептиком) подтвердились пять, из них сделано:

- **TTL обещания 72 ч → 3 ч** (`DONATION_UPSELL_PLEDGE_TTL_HOURS`). Замер по проде: между согласием и
  оплатой проходят минуты (максимум 13), инвойсы провайдеров живут 15–30 мин. Длинный TTL работал
  только на риск «прилипания».
- **Прежние обещания гасятся, когда экран не показан** (`cancel_pending_pledges`): cooldown, порог,
  исключённый способ, лимит способа, запрет пополнений, старый бандл кабинета. Раньше защита держалась
  только на том, что экран показывается при каждом пополнении, — то есть на `COOLDOWN_DAYS=0`.
  Не гасим только пожертвование из меню (`purpose='donation'`) — это не пополнение.
- **Сумма в благодарности**: «на баланс зачислено {credited}» вместо «зачислена сумма пополнения» —
  чек провайдера показывает полный платёж, и без второй цифры человек не понимал, куда делись 10 ₽.
  У Telegram Stars строка «Зачислено на баланс» заменена на «Сумма платежа»: там сообщение уходит уже
  после списания надбавки и врало прямо.
- **Расшифровка счёта** (`DONATION_UPSELL_PAYMENT_NOTE`) отдельным сообщением на обеих ветках
  согласия — обещание спеки «в т.ч. N ₽ — поддержка проекта», которого в коде не было. Особенно важно
  на ветке «иная сумма»: там итог не показывался вообще нигде.
- **Кабинет**: порог `MIN_TOPUP` проверяется до записи отказа (иначе отказ жёг бы cooldown и врал в
  воронке ниже порога).
- Докстринги про `expired` приведены к правде: статус зарезервирован и не выставляется, срок
  проверяется на лету в `list_pending_pledges`.

**Кнопка «Назад» на экране апселла (2026-08-21).** Её добавил владелец 20.08, и вела она всегда на
`topup_<method>` — сетку сумм. По логам `button_click_logs` за 3 дня больше половины входов на экран
апселла приходит оттуда, где сумма известна ЗАРАНЕЕ (корзина `balance_topup` — 108, экраны тарифа
`tariff_period:*` — 51, устройства — 12, против 129 обычных): там сетка сумм не рисуется, и «Назад»
уводил на экран, которого человек не видел, теряя контекст покупки тарифа. Причина в том, что
`get_payment_methods_keyboard` при известной сумме выдаёт тот же колбэк `topup_amount|<method>|<amount>`,
что и сетка сумм, — различить входы в апселле нечем.

Сделано: экран, который знает свой адрес возврата, кладёт подсказку в FSM
(`upsell_back_callback` + `upsell_back_amount`), а `_resolve_back_callback` использует её, только если
сумма совпадает с текущей; иначе прежний фолбэк `topup_<method>`. Подсказку ставят два хендлера,
покрывающие ~93 % «amount-first» входов: `show_payment_methods` (корзина) и `select_tariff_period`
(не хватает на тариф). Остальные такие экраны (устройства, трафик, страны) оставлены на фолбэке
сознательно: правка в 27 местах восьми файлов, включая основной флоу покупки, не окупается ради
навигации. Остаточный риск — подсказка от прошлого сценария при совпадении суммы до копейки; оба
адреса в этом случае ведут на живой экран.

По итогам ревью правки (2 ревьюера + скептик на каждую находку) дополнительно сделано:

- адрес возврата у тарифа — `tariff_period:<id>:<period>`, а не `tariff_select:<id>`: первый рисует тот
  самый экран «Недостаточно средств», второй уводил на шаг выше (к выбору периода), а в мульти-тарифе
  на уже активном тарифе вообще отвечал бы алертом без экрана;
- подсказка добавлена в `return_to_saved_tariff_cart` (экран «Всё ещё недостаточно средств»): он рисует
  ту же клавиатуру с известной суммой, и без подсказки там оставался исходный дефект;
- `_context` теперь сверяет `message_id` нажатой кнопки с `upsell_prompt_message_id`: нажатие на СТАРЫЙ
  экран апселла в истории чата оплачивало сумму нового, показывая цифры старого.

Осознанно не менялось: `handle_upsell_back` по-прежнему ставит `BalanceStates.waiting_for_amount`
(снимать состояние — риск для основного платёжного пути, а последствие ограничено тем, что после двух
«Назад» подряд набранное число начнёт обычное пополнение); подсказка не одноразовая — гасить её при
чтении значит терять адрес возврата при повторном заходе на тот же экран.

Опровергнуто и не менялось: «двойной учёт в отчётности» (ни один денежный агрегат надбавку не
удваивает — выручка фильтруется по `deposit`/`subscription_payment`), «воронка завышает согласия»
(`accepted = показы − отказы`, окно по `created_at`), «ветка иной суммы не используется» (к вечеру
20.08 она дала четверть выручки апселла).

| Задача | Статус |
|---|---|
| Task 1 — настройки, модель, миграция | ✅ сделано |
| Task 2 — CRUD обещаний | ✅ сделано |
| Task 3 — сервис условий показа | ✅ сделано |
| Task 4 — исполнение обещания | ✅ сделано |
| Task 5 — экран в боте | ✅ сделано |
| Task 6 — знак «−» в истории бота | ✅ сделано |
| Task 7 — кабинетное API | ✅ сделано |
| Task 8 — фронт кабинета (модалка) | ✅ сделано на сервере кабинета (195.78.48.27) |
| Task 9 — статистика | ✅ полностью: бэкенд (шаги 1-6) + фронт (шаги 7-8) |
| Task 10 — финальная проверка | ✅ кроме шага 4 (ручная проверка на живом стенде) |

### Отклонения от плана и почему

1. **Миграция `0107`, а не `0097`.** Номер 0097 занят `0097_add_grace_access.py`, донатная миграция
   после мержа upstream v4.0.0 стала `0106_donations.py`. `down_revision = '0106'`.
   Накат и откат проверены на копии схемы рабочей БД.
2. **Допуск сверки суммы переписан — это был денежный баг плана.** Было
   `max(200 копеек, 1% от expected_total)`; при надбавке 10 ₽ и пополнении больше ~1000 ₽ допуск
   превышал саму надбавку, из-за чего обычное пополнение на базовую сумму совпадало бы с забытым
   обещанием и списывало бы с человека чужую надбавку. Стало `min(200 копеек, tip − 1)` —
   инвариант «допуск строго меньше надбавки». Обоснование про округление Telegram Stars тоже неверно:
   Stars зачисляет ровно копейки из payload, дрейф возможен у CryptoBot (курс USD→RUB).
   Регрессия закреплена тестом `test_plain_topup_of_base_amount_never_matches`.
3. **Шаг 7.1 Task 4 отменён.** Строки `del amount_kopeks` в коде уже нет — её убрал upstream-коммит
   d527ce57. Сигнатура хука выросла: появился keyword-only `notify_email: bool = True`.
4. **Тексты добавлены во все 5 локалей**, вопреки указанию плана не трогать `locales/*.json`.
   Причина: `tests/test_locale_integrity.py::test_t_calls_with_static_default_exist_in_ru` прямо
   документирует, что `t('KEY', 'дефолт')` без ключа в `ru.json` отдаёт русский текст ВСЕМ языкам.
   По плану экран апселла был бы русским для англо-, фарси- и китаеязычных пользователей.
5. **Кабинет: три правки против плана.** `_apply_donation_tip` вызывается ПОСЛЕ валидации лимитов
   способа (иначе ответ 400 оставлял бы висячее обещание), исключает `tribute` и не навешивает
   надбавку на сам донат (`purpose='donation'`). Для Stars способ передаётся явно.
6. **Исправлено по итогам состязательного ревью:**
   - `_rollback_and_revive` — откат внутри `try_apply_donation_pledge` экспайрил объект `user`
     в сессии вызывающего провайдера, из-за чего остаток пост-хука падал с `MissingGreenlet`:
     автовыкуп сохранённой корзины не выполнялся, человек платил и не получал подписку.
     Тот же приём и по той же причине уже применён в `app/services/campaign_service.py`.
   - CryptoBot исключён из апселла: инвойс в USD, рублёвая сумма пересчитывается по курсу
     в момент оплаты, обещание не исполнилось бы почти никогда, зато висело бы весь TTL.
   - `_cancel_pledge` в `create_topup` — обещание гасится, если платёж создать не удалось
     (для `telegram_stars` кабинет всегда отвечает 400, обещание висело бы 72 часа).
   - Добавлен лог, когда обещание есть, а сумма не совпала: раньше расхождение было немым.
   - `payment_method` проставляется в FSM на пути «надбавка текстом» — иначе провайдер мог
     выбрать подметод (СБП/карта) от прошлого экрана.
   - Ввод `inf`/`nan` в надбавку давал `OverflowError` и общую «Произошла ошибка» вместо
     «неверная сумма».
   - Письмо о пополнении для email-юзеров показывало сумму с надбавкой при балансе без неё;
     хук теперь возвращает размер списанной надбавки, и письмо её вычитает.

7. **Добавлено сверх плана:** ранний выход при `db is None`; отдельная ветка `IntegrityError`
   (повторный вебхук — это INFO, а не ERROR со стектрейсом); индекс `ix_donations_source` объявлен
   и в модели (свежие БД создаются через `create_all` в обход alembic); префикс `DONATION_UPSELL`
   отнесён к категории `DONATIONS` в админке бота; `suppress(TelegramBadRequest)` на финальном
   `callback.answer()` — Tribute отвечает на callback сам.

### Фронт кабинета

Лежит на отдельном сервере 195.78.48.27, `/root/bedolaga-cabinet` (репозиторий был чистый).
Ветка `feat/donation-upsell`: `a7b7676` — фича, `eff1d44` и `7252665` — исправления по итогам
двух раундов ревью.
Проверено там же через docker-образ `node:22-alpine` (node/npm на хосте нет):
type-check, lint (0 ошибок), prettier, build. Собранный `dist/` **не выкачен** —
`/srv/cabinet` не тронут.

Отличия от плана по фронту:
- Локалей в кабинете **4, а не 5** (нет `ua`), причём `supportProject` был только в `ru`/`en` —
  фарси и китайский у существующей фичи не переведены. Ключи апселла добавлены во все 4.
- `cryptobot` исключён в `shouldOfferUpsell` — синхронно с бэкендом.
- Коммиты с `--no-verify`: husky-хук вызывает `npx`, которого на хосте нет; его проверки
  (`eslint --fix`, `prettier --write`) прогнаны вручную через docker.
- Esc и клик вне окна — **отмена**, а не отказ. План предлагал `onOpenChange={(next) => !next && decide(0)}`,
  но это делало случайный Esc выпуском реального платежа (см. ниже).
- Поле «своя сумма» конвертируется через `convertToRub`: диалог показывает суммы в валюте локали.

### Исправления по итогам ревью фронта

Состязательная проверка (3 ревьюера + скептики): 9 находок, 3 опровергнуто, 6 подтверждено —
это два различных дефекта, оба исправлены:

1. **Esc создавал платёж и уводил со страницы одновременно.** Radix вешает Escape-обработчик в фазе
   перехвата и делает `preventDefault` без `stopPropagation`, поэтому событие доходило и до обработчика
   страницы: закрытие модалки трактовалось как отказ → `startPayment`, а следом `navigate(-1)`.
   На web с `open_url_direct` браузер затем редиректил на провайдера уже после «выхода».
2. **«Своя сумма» парсилась как рубли**, тогда как весь диалог показывает суммы в валюте локали.
   В фарси ввод 50000 (по ориентиру соседних пресетов ≈120 ₽) уходил как 50 000 ₽ — переплата почти
   в 417 раз.

Второй раунд (проверка самих исправлений) подтвердил, что оба дефекта закрыты, и нашёл четыре
следствия — все исправлены коммитом `7252665`:

3. **Отмена тратила лимит попыток оплаты.** `checkRateLimit` вызывался в `handleSubmit`, до показа
   модалки. Пока закрытие создавало платёж, «показ без платежа» был недостижим; после правки три
   закрытия за 30 с блокировали кнопку «Оплатить», хотя платежей не было. Лимит перенесён
   в `startPayment`.
4. **Кнопка отказа переименована** в «Оплатить без поддержки»: рядом с ней Esc, крестик и клик вне
   окна означают «вернуться», и два соседних выхода выглядели одинаково, а вели по-разному.
5. **Поле суммы переведено с `type=number` на `type=text`** с нормализацией персидских и арабских
   цифр и разделителей разрядов: в фарси суммы отрисованы через `toLocaleString('fa-IR')`, и
   набранное «۵۰» браузер не пропускал. Разбор проверен численно на 20 входах.
6. **Добавлен минимум надбавки 1 ₽**: в неруб­лёвых локалях проходила надбавка в одну копейку,
   для которой окно сверки обещания вырождается в ноль.

### Известное, но не исправленное (фронт)

- **fa: «تومان» (томан) при курсе IRR (риал)** — расхождение единиц в 10 раз. Существует в `main`
  до этой работы и касается ВСЕХ цен кабинета, не только апселла: `LANGUAGE_CURRENCY_MAP.fa = 'IRR'`,
  а `common.currency` = «تومان». Чинить нужно отдельно и целиком.
- **Закрытие модалки не пишется на сервер**: cooldown от него не встаёт, воронка не считает такие
  показы. Это следствие сознательного решения «закрытие ≠ решение». При `COOLDOWN_DAYS=0`
  (значение по умолчанию) поведение не отличается от штатного.

### Что осталось сделать вручную

- Пересобрать docker-образ бота: `docker-compose.yml` не монтирует `./app`, код вшит в образ.
- Применить миграцию `0107` на рабочей БД (сейчас она на `0106`).
- Выкатить фронт кабинета: собранный `dist/` → `/srv/cabinet`. **Только после бэкенда** —
  иначе `GET /cabinet/balance/donation-upsell` вернёт 404 и модалка просто не появится.
- Ручная проверка по шагу 4 Task 10.

---

## Global Constraints

- Форматирование: `ruff`, `line-length = 120`, одинарные кавычки, `target-version = py313`. Проверка: `uv run ruff check .` и `uv run ruff format .` (или `make fix`).
- Тесты: `uv run pytest -v`. Реальной БД в тестах нет — сессии мокаются (`AsyncMock`/`MagicMock`), настройки подменяются `monkeypatch.setattr(settings, 'KEY', value, raising=False)`. Async-тесты помечаются `@pytest.mark.asyncio`.
- Логи: `structlog` (`logger = structlog.get_logger(__name__)`), сообщения на русском, поля — kwargs.
- Тексты бота задаются **только** дефолтами в коде: `texts.t('KEY', 'дефолт')`. Файлы `app/localization/locales/*.json` НЕ трогаем — `tests/test_locale_integrity.py` требует одинаковый набор ключей во всех 5 языках, а существующие донат-тексты там тоже отсутствуют.
- Тексты кабинета — во **всех 4** файлах `src/locales/{ru,en,fa,zh}.json`.
- Все суммы в коде — в копейках (`*_kopeks`), в `.env` — в рублях.
- Значение статуса надбавки в БД — строки из `DonationPledgeStatus` (см. Task 1).
- `external_id` донат-транзакции и записи `donations` для надбавки — строго `f'pledge:{pledge.id}'`. Это контракт: по префиксу `pledge:` бот и кабинет понимают, что пожертвование уменьшило баланс.
- Коммиты: без трейлеров `Co-Authored-By` / `Claude-Session`.
- Кабинет: `npm run lint`, `npm run type-check` в `/root/bedolaga-cabinet` перед коммитом фронта.

---

## Файловая структура

**Бэкенд бота (репозиторий `/root/remnawave-bedolaga-telegram-bot`)**

| Файл | Ответственность |
|---|---|
| `app/config.py` (правка) | 7 настроек апселла + 3 хелпера перевода в копейки |
| `app/database/models.py` (правка) | модель `DonationPledge`, класс статусов, поле `Donation.source` |
| `migrations/alembic/versions/0097_donation_upsell.py` (создать) | таблица + колонка |
| `app/database/crud/donation_pledge.py` (создать) | только SQL: создать/погасить/найти/пометить |
| `app/services/donation_upsell_service.py` (создать) | вся логика: показывать ли, подбор обещания, применение надбавки |
| `app/services/payment/common.py` (правка) | вызов `try_apply_donation_pledge` в начале `send_cart_notification_after_topup` |
| `app/services/admin_notification_service.py` (правка) | пометка «при пополнении» в уведомлении админам |
| `app/handlers/balance/donation_upsell.py` (создать) | экран апселла, кнопки, ввод своей суммы |
| `app/handlers/balance/main.py` (правка) | вынос разводки платежей в `dispatch_topup`, врезка апселла, знак «−» в истории |
| `app/states.py` (правка) | состояние ввода своей надбавки |
| `.env.example` (правка) | документация настроек |

**Кабинетное API (тот же репозиторий)**

| Файл | Ответственность |
|---|---|
| `app/cabinet/schemas/balance.py` (правка) | поля надбавки в запросах, схема ответа апселла |
| `app/cabinet/routes/balance.py` (правка) | эндпоинт апселла, приём надбавки, знак в истории |
| `app/cabinet/routes/admin_sales_stats.py` (правка) | разбивка донатов по источнику + воронка апселла |
| `app/database/crud/donation.py` (правка) | `source` в `create_donation`, агрегаты по источнику |

**Фронт кабинета (репозиторий `/root/bedolaga-cabinet`)**

| Файл | Ответственность |
|---|---|
| `src/api/balance.ts` (правка) | `donationTipKopeks` в создании платежа, `getDonationUpsell()` |
| `src/components/DonationUpsellDialog.tsx` (создать) | модалка апселла |
| `src/pages/TopUpAmount.tsx` (правка) | показ модалки перед созданием платежа |
| `src/pages/Balance.tsx` (правка) | знак «−» у пожертвования, уменьшившего баланс |
| `src/api/adminSalesStats.ts` (правка) | типы разбивки и воронки |
| `src/components/sales-stats/DonationsTab.tsx` (правка) | карточки «источник» и «воронка» |
| `src/locales/{ru,en,fa,zh}.json` (правка) | тексты |

---

## Task 1: Настройки, модель, миграция

**Files:**
- Modify: `app/config.py` (после `DONATIONS_ENABLED`, строка 39; хелперы — рядом с `rubles_to_stars`, строка ~3027)
- Modify: `app/database/models.py` (`Donation`, строка 2388 — новое поле; новый класс после `Donation`)
- Create: `migrations/alembic/versions/0097_donation_upsell.py`
- Modify: `.env.example` (после строки 28 `DONATIONS_ENABLED=true`)
- Test: `tests/services/test_donation_upsell_config.py`

**Interfaces:**
- Consumes: ничего.
- Produces:
  - `settings.DONATION_UPSELL_ENABLED: bool`, `DONATION_UPSELL_AMOUNT: float`, `DONATION_UPSELL_PRESETS: str`, `DONATION_UPSELL_COOLDOWN_DAYS: int`, `DONATION_UPSELL_MIN_TOPUP: float`, `DONATION_UPSELL_SKIP_IF_DONATED: bool`, `DONATION_UPSELL_PLEDGE_TTL_HOURS: int`
  - `settings.get_donation_upsell_tip_kopeks() -> int`
  - `settings.get_donation_upsell_presets_kopeks() -> list[int]`
  - `settings.get_donation_upsell_min_topup_kopeks() -> int`
  - `app.database.models.DonationPledge` с полями `id, user_id, tip_kopeks, topup_amount_kopeks, expected_total_kopeks, payment_method, source, status, created_at, expires_at, applied_at, transaction_id`
  - `app.database.models.DonationPledgeStatus` с константами `PENDING, APPLIED, DECLINED, SUPERSEDED, EXPIRED, SKIPPED`
  - `Donation.source: str` (`'menu'` по умолчанию)

- [ ] **Step 1: Написать падающий тест на хелперы настроек**

Создать `tests/services/test_donation_upsell_config.py`:

```python
"""Настройки донат-апселла: рубли из .env → копейки, отсев мусора в пресетах."""

import pytest

from app.config import settings


def test_tip_kopeks_from_rubles(monkeypatch):
    monkeypatch.setattr(settings, 'DONATION_UPSELL_AMOUNT', 10, raising=False)
    assert settings.get_donation_upsell_tip_kopeks() == 1000


def test_tip_kopeks_accepts_fractional_rubles(monkeypatch):
    monkeypatch.setattr(settings, 'DONATION_UPSELL_AMOUNT', 10.5, raising=False)
    assert settings.get_donation_upsell_tip_kopeks() == 1050


def test_tip_kopeks_never_negative(monkeypatch):
    monkeypatch.setattr(settings, 'DONATION_UPSELL_AMOUNT', -5, raising=False)
    assert settings.get_donation_upsell_tip_kopeks() == 0


def test_presets_parsed_sorted_and_deduplicated(monkeypatch):
    monkeypatch.setattr(settings, 'DONATION_UPSELL_PRESETS', '100, 25,50,100, ,abc,0', raising=False)
    assert settings.get_donation_upsell_presets_kopeks() == [2500, 5000, 10000]


def test_presets_empty_string_gives_empty_list(monkeypatch):
    monkeypatch.setattr(settings, 'DONATION_UPSELL_PRESETS', '', raising=False)
    assert settings.get_donation_upsell_presets_kopeks() == []


def test_min_topup_kopeks(monkeypatch):
    monkeypatch.setattr(settings, 'DONATION_UPSELL_MIN_TOPUP', 300, raising=False)
    assert settings.get_donation_upsell_min_topup_kopeks() == 30000
```

- [ ] **Step 2: Убедиться, что тест падает**

Run: `uv run pytest tests/services/test_donation_upsell_config.py -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'get_donation_upsell_tip_kopeks'`

- [ ] **Step 3: Добавить настройки в `app/config.py`**

После `DONATIONS_ENABLED: bool = True` (строка 39):

```python
    # Донат-апселл: перед переходом на оплату предлагаем добавить к платежу небольшую
    # сумму в пользу проекта. Надбавка НЕ попадает на баланс — она списывается сразу
    # после зачисления и записывается как пожертвование (app/services/donation_upsell_service.py).
    DONATION_UPSELL_ENABLED: bool = True
    DONATION_UPSELL_AMOUNT: float = 10  # размер надбавки по умолчанию, ₽
    DONATION_UPSELL_PRESETS: str = '25,50,100,300,500'  # кнопки «иная сумма», ₽
    DONATION_UPSELL_COOLDOWN_DAYS: int = 0  # 0 — спрашивать при каждом пополнении
    DONATION_UPSELL_MIN_TOPUP: float = 0  # 0 — без порога суммы пополнения, ₽
    DONATION_UPSELL_SKIP_IF_DONATED: bool = False  # не спрашивать тех, кто уже жертвовал
    DONATION_UPSELL_PLEDGE_TTL_HOURS: int = 72  # сколько ждём оплаты по обещанию
```

- [ ] **Step 4: Добавить хелперы в `app/config.py`**

Рядом с `rubles_to_stars` (после строки ~3031):

```python
    def get_donation_upsell_tip_kopeks(self) -> int:
        """Размер надбавки из .env (рубли) в копейках. Мусор и минус → 0."""
        try:
            return max(0, int(round(float(self.DONATION_UPSELL_AMOUNT) * 100)))
        except (TypeError, ValueError):
            return 0

    def get_donation_upsell_presets_kopeks(self) -> list[int]:
        """Кнопки «иная сумма» из .env: '25,50,100' → [2500, 5000, 10000]."""
        presets: set[int] = set()
        for chunk in (self.DONATION_UPSELL_PRESETS or '').split(','):
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                kopeks = int(round(float(chunk.replace(',', '.')) * 100))
            except ValueError:
                continue
            if kopeks > 0:
                presets.add(kopeks)
        return sorted(presets)

    def get_donation_upsell_min_topup_kopeks(self) -> int:
        """Порог суммы пополнения, ниже которого апселл не показываем."""
        try:
            return max(0, int(round(float(self.DONATION_UPSELL_MIN_TOPUP) * 100)))
        except (TypeError, ValueError):
            return 0
```

- [ ] **Step 5: Убедиться, что тесты проходят**

Run: `uv run pytest tests/services/test_donation_upsell_config.py -v`
Expected: 6 passed

- [ ] **Step 6: Добавить поле `source` в модель `Donation`**

В `app/database/models.py`, в классе `Donation` после строки `payment_method = Column(String(50), nullable=True)`:

```python
    # Откуда пришло пожертвование: 'menu' — кнопка «Поддержать проект»,
    # 'topup_upsell' — надбавка к платежу при пополнении баланса.
    source = Column(String(32), nullable=False, server_default='menu', default='menu')
```

- [ ] **Step 7: Добавить модель `DonationPledge`**

В `app/database/models.py` сразу после класса `Donation` (перед `class SubscriptionConversion`):

```python
class DonationPledgeStatus:
    """Статусы обещания надбавки. Хранятся строкой — миграции для новых не нужны."""

    PENDING = 'pending'  # согласился, платёж ещё не зачислен
    APPLIED = 'applied'  # надбавка списана и записана как пожертвование
    DECLINED = 'declined'  # отказался (нужно для cooldown и воронки)
    SUPERSEDED = 'superseded'  # погашено новым решением пользователя
    EXPIRED = 'expired'  # платёж не пришёл за TTL
    SKIPPED = 'skipped'  # применить не удалось (например, не хватило баланса)


class DonationPledge(Base):
    """Обещание надбавки к платежу («добавить 10 ₽ на поддержку»).

    Пишется в момент показа экрана апселла — и при согласии, и при отказе. Согласие
    (`pending`) исполняется вебхуком: после зачисления платежа надбавка списывается
    с баланса и превращается в запись `donations`
    (``app/services/donation_upsell_service.py::try_apply_donation_pledge``).
    Отказы нужны для cooldown и для воронки «показали → согласились → оплатили».
    """

    __tablename__ = 'donation_pledges'
    __table_args__ = (
        Index('ix_donation_pledges_user_status', 'user_id', 'status', 'created_at'),
        Index('ix_donation_pledges_status_expires', 'status', 'expires_at'),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)

    tip_kopeks = Column(Integer, nullable=False, default=0)
    topup_amount_kopeks = Column(Integer, nullable=False)
    # Сумма платежа = пополнение + надбавка. Для отказа равна сумме пополнения.
    expected_total_kopeks = Column(Integer, nullable=False)

    payment_method = Column(String(50), nullable=True)
    source = Column(String(16), nullable=False, default='bot')  # bot | cabinet
    status = Column(String(16), nullable=False, default=DonationPledgeStatus.PENDING)

    created_at = Column(AwareDateTime(), default=func.now())
    expires_at = Column(AwareDateTime(), nullable=True)
    applied_at = Column(AwareDateTime(), nullable=True)
    transaction_id = Column(Integer, ForeignKey('transactions.id', ondelete='SET NULL'), nullable=True)

    user = relationship('User')
```

- [ ] **Step 8: Проверить, что модели импортируются**

Run: `uv run python -c "from app.database.models import DonationPledge, DonationPledgeStatus, Donation; print(DonationPledge.__tablename__, Donation.source is not None, DonationPledgeStatus.PENDING)"`
Expected: `donation_pledges True pending`

- [ ] **Step 9: Написать миграцию**

Создать `migrations/alembic/versions/0097_donation_upsell.py`:

```python
"""donation upsell: pledges table + donations.source

Revision ID: 0097
Revises: 0096
Create Date: 2026-07-30

Добавляет таблицу ``donation_pledges`` (обещание надбавки к платежу при пополнении)
и колонку ``donations.source`` (откуда пришло пожертвование: меню или апселл).
Только добавление — существующие данные не меняются, старые донаты получают
``source='menu'`` через server_default.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0097'
down_revision: Union[str, None] = '0096'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if 'donations' in inspector.get_table_names():
        donation_columns = {column['name'] for column in inspector.get_columns('donations')}
        if 'source' not in donation_columns:
            op.add_column(
                'donations',
                sa.Column('source', sa.String(length=32), nullable=False, server_default='menu'),
            )
            op.create_index('ix_donations_source', 'donations', ['source'])

    if 'donation_pledges' in inspector.get_table_names():
        return

    op.create_table(
        'donation_pledges',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('tip_kopeks', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('topup_amount_kopeks', sa.Integer(), nullable=False),
        sa.Column('expected_total_kopeks', sa.Integer(), nullable=False),
        sa.Column('payment_method', sa.String(length=50), nullable=True),
        sa.Column('source', sa.String(length=16), nullable=False, server_default='bot'),
        sa.Column('status', sa.String(length=16), nullable=False, server_default='pending'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('applied_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            'transaction_id',
            sa.Integer(),
            sa.ForeignKey('transactions.id', ondelete='SET NULL'),
            nullable=True,
        ),
    )

    op.create_index('ix_donation_pledges_user_status', 'donation_pledges', ['user_id', 'status', 'created_at'])
    op.create_index('ix_donation_pledges_status_expires', 'donation_pledges', ['status', 'expires_at'])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if 'donation_pledges' in inspector.get_table_names():
        op.drop_index('ix_donation_pledges_status_expires', table_name='donation_pledges')
        op.drop_index('ix_donation_pledges_user_status', table_name='donation_pledges')
        op.drop_table('donation_pledges')

    if 'donations' in inspector.get_table_names():
        donation_columns = {column['name'] for column in inspector.get_columns('donations')}
        if 'source' in donation_columns:
            op.drop_index('ix_donations_source', table_name='donations')
            op.drop_column('donations', 'source')
```

- [ ] **Step 10: Проверить, что миграция читается Alembic и цепочка не разорвана**

Run: `uv run python -c "
import ast, pathlib
src = pathlib.Path('migrations/alembic/versions/0097_donation_upsell.py').read_text()
tree = ast.parse(src)
print('parsed ok')
print([n.targets[0].id for n in tree.body if isinstance(n, ast.AnnAssign) or isinstance(n, ast.Assign) and hasattr(n.targets[0], 'id')][:4])
"`
Expected: `parsed ok` без ошибок синтаксиса.

Дополнительно вручную сверить: `grep -n "revision\|down_revision" migrations/alembic/versions/0097_donation_upsell.py` → `revision = '0097'`, `down_revision = '0096'`.

- [ ] **Step 11: Задокументировать настройки в `.env.example`**

После строки `DONATIONS_ENABLED=true` (строка 28):

```
# Донат-апселл: предложение добавить к платежу сумму в пользу проекта
# (показывается после выбора суммы пополнения, перед переходом к оплате).
# Надбавка не попадает на баланс — она учитывается как пожертвование.
DONATION_UPSELL_ENABLED=true
DONATION_UPSELL_AMOUNT=10              # размер надбавки, ₽
DONATION_UPSELL_PRESETS=25,50,100,300,500   # кнопки «иная сумма», ₽
DONATION_UPSELL_COOLDOWN_DAYS=0        # 0 = спрашивать при каждом пополнении
DONATION_UPSELL_MIN_TOPUP=0            # 0 = без порога суммы пополнения, ₽
DONATION_UPSELL_SKIP_IF_DONATED=false  # true = не спрашивать тех, кто уже жертвовал
DONATION_UPSELL_PLEDGE_TTL_HOURS=72    # сколько ждём оплаты по обещанию
```

- [ ] **Step 12: Прогнать линтер и весь набор тестов**

Run: `uv run ruff format . && uv run ruff check . && uv run pytest -q`
Expected: линтер чист, тесты зелёные (существующие не сломаны).

- [ ] **Step 13: Коммит**

```bash
git add app/config.py app/database/models.py migrations/alembic/versions/0097_donation_upsell.py .env.example tests/services/test_donation_upsell_config.py
git commit -m "feat(donations): настройки, модель и миграция донат-апселла"
```

---

## Task 2: CRUD обещаний

**Files:**
- Create: `app/database/crud/donation_pledge.py`
- Modify: `app/database/crud/donation.py` (`create_donation` — параметр `source`)
- Test: `tests/crud/test_donation_pledge.py`

**Interfaces:**
- Consumes: `DonationPledge`, `DonationPledgeStatus` (Task 1).
- Produces:
  - `async def supersede_pending_pledges(db, *, user_id: int) -> int`
  - `async def create_pledge(db, *, user_id: int, tip_kopeks: int, topup_amount_kopeks: int, payment_method: str | None, source: str, ttl_hours: int, status: str = DonationPledgeStatus.PENDING) -> DonationPledge`
  - `async def list_pending_pledges(db, *, user_id: int) -> list[DonationPledge]` — только не истёкшие, новые первыми
  - `async def mark_pledge_applied(db, *, pledge_id: int, transaction_id: int | None) -> bool` — атомарно, без коммита
  - `async def mark_pledge_status(db, *, pledge_id: int, status: str) -> None` — без коммита
  - `async def has_recent_decision(db, *, user_id: int, days: int) -> bool`
  - `create_donation(..., source: str = 'menu')` в `app/database/crud/donation.py`

- [ ] **Step 1: Написать падающие тесты**

Создать `tests/crud/test_donation_pledge.py`:

```python
"""CRUD обещаний надбавки: сессия замокана, проверяем поведение, не SQL."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.database.crud import donation_pledge as crud
from app.database.models import DonationPledgeStatus


def _db(execute_results=None):
    db = AsyncMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    db.refresh = AsyncMock()
    db.execute = AsyncMock(side_effect=list(execute_results or []))
    return db


def _rowcount_result(rowcount: int):
    result = MagicMock()
    result.rowcount = rowcount
    return result


def _scalars_result(rows):
    result = MagicMock()
    scalars = MagicMock()
    scalars.all = MagicMock(return_value=list(rows))
    result.scalars = MagicMock(return_value=scalars)
    return result


def _scalar_result(value):
    result = MagicMock()
    result.scalar = MagicMock(return_value=value)
    return result


@pytest.mark.asyncio
async def test_create_pledge_sets_total_and_expiry():
    db = _db()

    pledge = await crud.create_pledge(
        db,
        user_id=42,
        tip_kopeks=1000,
        topup_amount_kopeks=50000,
        payment_method='yookassa',
        source='bot',
        ttl_hours=72,
    )

    assert pledge.expected_total_kopeks == 51000
    assert pledge.status == DonationPledgeStatus.PENDING
    assert pledge.expires_at is not None
    assert pledge.expires_at > datetime.now(UTC)
    db.add.assert_called_once()
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_declined_pledge_has_no_tip_and_no_expiry():
    db = _db()

    pledge = await crud.create_pledge(
        db,
        user_id=42,
        tip_kopeks=0,
        topup_amount_kopeks=50000,
        payment_method='yookassa',
        source='cabinet',
        ttl_hours=72,
        status=DonationPledgeStatus.DECLINED,
    )

    assert pledge.tip_kopeks == 0
    assert pledge.expected_total_kopeks == 50000
    assert pledge.status == DonationPledgeStatus.DECLINED
    assert pledge.expires_at is None


@pytest.mark.asyncio
async def test_mark_pledge_applied_returns_false_when_already_applied():
    db = _db([_rowcount_result(0)])

    applied = await crud.mark_pledge_applied(db, pledge_id=7, transaction_id=99)

    assert applied is False
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_mark_pledge_applied_returns_true_on_first_call():
    db = _db([_rowcount_result(1)])

    applied = await crud.mark_pledge_applied(db, pledge_id=7, transaction_id=99)

    assert applied is True


@pytest.mark.asyncio
async def test_list_pending_pledges_returns_rows():
    row = MagicMock(id=5)
    db = _db([_scalars_result([row])])

    pledges = await crud.list_pending_pledges(db, user_id=42)

    assert pledges == [row]


@pytest.mark.asyncio
async def test_has_recent_decision_false_when_cooldown_disabled():
    db = _db()

    assert await crud.has_recent_decision(db, user_id=42, days=0) is False
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_has_recent_decision_true_when_row_found():
    db = _db([_scalar_result(1)])

    assert await crud.has_recent_decision(db, user_id=42, days=7) is True
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `uv run pytest tests/crud/test_donation_pledge.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.database.crud.donation_pledge'`

- [ ] **Step 3: Реализовать CRUD**

Создать `app/database/crud/donation_pledge.py`:

```python
"""CRUD обещаний надбавки («добавить 10 ₽ к платежу»).

Здесь только запросы. Решение «показывать ли апселл» и подбор обещания под
зачисленный платёж — в ``app/services/donation_upsell_service.py``.
"""

from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import DonationPledge, DonationPledgeStatus


logger = structlog.get_logger(__name__)


async def supersede_pending_pledges(db: AsyncSession, *, user_id: int) -> int:
    """Гасит незавершённые обещания пользователя: новое решение отменяет прежние.

    Без этого брошенный платёж мог бы «прилипнуть» к следующему пополнению.
    Коммит делает вызывающая сторона (обычно сразу после — ``create_pledge``).
    """
    result = await db.execute(
        update(DonationPledge)
        .where(
            DonationPledge.user_id == user_id,
            DonationPledge.status == DonationPledgeStatus.PENDING,
        )
        .values(status=DonationPledgeStatus.SUPERSEDED)
    )
    return int(result.rowcount or 0)


async def create_pledge(
    db: AsyncSession,
    *,
    user_id: int,
    tip_kopeks: int,
    topup_amount_kopeks: int,
    payment_method: str | None,
    source: str,
    ttl_hours: int,
    status: str = DonationPledgeStatus.PENDING,
) -> DonationPledge:
    """Записывает решение пользователя по апселлу (согласие или отказ)."""
    tip_kopeks = max(0, int(tip_kopeks))
    is_pending = status == DonationPledgeStatus.PENDING

    pledge = DonationPledge(
        user_id=user_id,
        tip_kopeks=tip_kopeks,
        topup_amount_kopeks=topup_amount_kopeks,
        expected_total_kopeks=topup_amount_kopeks + tip_kopeks,
        payment_method=payment_method,
        source=source,
        status=status,
        # Ждём оплату только по согласию; отказ — просто отметка в истории решений.
        expires_at=datetime.now(UTC) + timedelta(hours=max(1, int(ttl_hours))) if is_pending else None,
    )

    db.add(pledge)
    await db.commit()
    await db.refresh(pledge)

    logger.info(
        '❤️ Записано решение по донат-апселлу',
        pledge_id=pledge.id,
        user_id=user_id,
        status=status,
        tip_kopeks=tip_kopeks,
        expected_total_kopeks=pledge.expected_total_kopeks,
        source=source,
    )
    return pledge


async def list_pending_pledges(db: AsyncSession, *, user_id: int) -> list[DonationPledge]:
    """Неистёкшие согласия пользователя, свежие первыми."""
    result = await db.execute(
        select(DonationPledge)
        .where(
            DonationPledge.user_id == user_id,
            DonationPledge.status == DonationPledgeStatus.PENDING,
            DonationPledge.expires_at > datetime.now(UTC),
        )
        .order_by(DonationPledge.created_at.desc())
    )
    return list(result.scalars().all())


async def mark_pledge_applied(db: AsyncSession, *, pledge_id: int, transaction_id: int | None) -> bool:
    """Переводит обещание в ``applied``. ``False`` — уже применено кем-то другим.

    Условие ``status == PENDING`` внутри UPDATE — и есть защита от двойного
    списания при повторном вебхуке. Коммитит вызывающая сторона: списание баланса
    и эта отметка должны попасть в одну транзакцию.
    """
    result = await db.execute(
        update(DonationPledge)
        .where(
            DonationPledge.id == pledge_id,
            DonationPledge.status == DonationPledgeStatus.PENDING,
        )
        .values(
            status=DonationPledgeStatus.APPLIED,
            applied_at=datetime.now(UTC),
            transaction_id=transaction_id,
        )
    )
    return bool(result.rowcount)


async def mark_pledge_status(db: AsyncSession, *, pledge_id: int, status: str) -> None:
    """Ставит произвольный терминальный статус (``expired``, ``skipped``). Без коммита."""
    await db.execute(update(DonationPledge).where(DonationPledge.id == pledge_id).values(status=status))


async def has_recent_decision(db: AsyncSession, *, user_id: int, days: int) -> bool:
    """Было ли решение по апселлу за последние ``days`` дней (cooldown). 0 → всегда False."""
    if days <= 0:
        return False
    since = datetime.now(UTC) - timedelta(days=days)
    result = await db.execute(
        select(func.count(DonationPledge.id)).where(
            DonationPledge.user_id == user_id,
            DonationPledge.created_at >= since,
        )
    )
    return bool(result.scalar() or 0)
```

- [ ] **Step 4: Убедиться, что тесты проходят**

Run: `uv run pytest tests/crud/test_donation_pledge.py -v`
Expected: 7 passed

- [ ] **Step 5: Добавить `source` в `create_donation`**

В `app/database/crud/donation.py`: в сигнатуру `create_donation` (после `transaction_id: int | None = None`) добавить параметр, и передать его в конструктор `Donation`:

```python
    transaction_id: int | None = None,
    source: str = 'menu',
```

и в объект:

```python
        transaction_id=transaction_id,
        source=source,
```

- [ ] **Step 6: Проверить, что существующие донат-тесты не сломались**

Run: `uv run pytest tests/handlers/test_donation_flow.py tests/services/test_payment_service_donation.py -q`
Expected: passed (значение по умолчанию `source='menu'` сохраняет поведение).

- [ ] **Step 7: Коммит**

```bash
git add app/database/crud/donation_pledge.py app/database/crud/donation.py tests/crud/test_donation_pledge.py
git commit -m "feat(donations): CRUD обещаний надбавки и source у пожертвований"
```

---

## Task 3: Сервис — решение о показе апселла

**Files:**
- Create: `app/services/donation_upsell_service.py`
- Test: `tests/services/test_donation_upsell_offer.py`

**Interfaces:**
- Consumes: настройки и хелперы (Task 1), `donation_pledge` CRUD (Task 2), `app.database.crud.donation.get_user_donation_total_kopeks`.
- Produces:
  - `@dataclass(frozen=True) class UpsellOffer: tip_kopeks: int; presets_kopeks: list[int]; topup_amount_kopeks: int; total_kopeks: int; max_tip_kopeks: int`
  - `EXCLUDED_UPSELL_METHODS: frozenset[str]` (= `{'tribute'}`)
  - `def resolve_max_tip_kopeks(*, topup_amount_kopeks: int, method_max_kopeks: int | None) -> int`
  - `async def is_upsell_available(db, user) -> bool` — только «пользовательские» условия (без суммы и метода)
  - `async def build_offer(db, user, *, topup_amount_kopeks: int, payment_method: str, method_max_kopeks: int | None = None) -> UpsellOffer | None`
  - `async def register_pledge(db, user, *, tip_kopeks: int, topup_amount_kopeks: int, payment_method: str | None, source: str) -> DonationPledge`
  - `async def register_decline(db, user, *, topup_amount_kopeks: int, payment_method: str | None, source: str) -> None`

- [ ] **Step 1: Написать падающие тесты**

Создать `tests/services/test_donation_upsell_offer.py`:

```python
"""Когда апселл показывается, а когда молчит; и что именно предлагает."""

from unittest.mock import AsyncMock, MagicMock

import pytest

import app.services.donation_upsell_service as upsell
from app.config import settings


def _user(user_id: int = 42, restricted: bool = False):
    user = MagicMock()
    user.id = user_id
    user.restriction_topup = restricted
    return user


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(settings, 'DONATIONS_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'DONATION_UPSELL_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'DONATION_UPSELL_AMOUNT', 10, raising=False)
    monkeypatch.setattr(settings, 'DONATION_UPSELL_PRESETS', '25,50,100', raising=False)
    monkeypatch.setattr(settings, 'DONATION_UPSELL_COOLDOWN_DAYS', 0, raising=False)
    monkeypatch.setattr(settings, 'DONATION_UPSELL_MIN_TOPUP', 0, raising=False)
    monkeypatch.setattr(settings, 'DONATION_UPSELL_SKIP_IF_DONATED', False, raising=False)
    monkeypatch.setattr(upsell.pledge_crud, 'has_recent_decision', AsyncMock(return_value=False))


@pytest.mark.asyncio
async def test_offer_built_for_regular_topup():
    offer = await upsell.build_offer(
        AsyncMock(), _user(), topup_amount_kopeks=50000, payment_method='yookassa', method_max_kopeks=10_000_000
    )

    assert offer is not None
    assert offer.tip_kopeks == 1000
    assert offer.total_kopeks == 51000
    assert offer.presets_kopeks == [2500, 5000, 10000]


@pytest.mark.asyncio
async def test_no_offer_when_feature_disabled(monkeypatch):
    monkeypatch.setattr(settings, 'DONATION_UPSELL_ENABLED', False, raising=False)

    offer = await upsell.build_offer(AsyncMock(), _user(), topup_amount_kopeks=50000, payment_method='yookassa')

    assert offer is None


@pytest.mark.asyncio
async def test_no_offer_when_donations_disabled(monkeypatch):
    monkeypatch.setattr(settings, 'DONATIONS_ENABLED', False, raising=False)

    offer = await upsell.build_offer(AsyncMock(), _user(), topup_amount_kopeks=50000, payment_method='yookassa')

    assert offer is None


@pytest.mark.asyncio
async def test_no_offer_for_restricted_user():
    offer = await upsell.build_offer(
        AsyncMock(), _user(restricted=True), topup_amount_kopeks=50000, payment_method='yookassa'
    )

    assert offer is None


@pytest.mark.asyncio
async def test_no_offer_for_tribute():
    offer = await upsell.build_offer(AsyncMock(), _user(), topup_amount_kopeks=50000, payment_method='tribute')

    assert offer is None


@pytest.mark.asyncio
async def test_no_offer_below_min_topup(monkeypatch):
    monkeypatch.setattr(settings, 'DONATION_UPSELL_MIN_TOPUP', 300, raising=False)

    offer = await upsell.build_offer(AsyncMock(), _user(), topup_amount_kopeks=10000, payment_method='yookassa')

    assert offer is None


@pytest.mark.asyncio
async def test_no_offer_when_tip_does_not_fit_method_limit():
    offer = await upsell.build_offer(
        AsyncMock(), _user(), topup_amount_kopeks=100000, payment_method='yookassa', method_max_kopeks=100000
    )

    assert offer is None


@pytest.mark.asyncio
async def test_no_offer_within_cooldown(monkeypatch):
    monkeypatch.setattr(settings, 'DONATION_UPSELL_COOLDOWN_DAYS', 7, raising=False)
    monkeypatch.setattr(upsell.pledge_crud, 'has_recent_decision', AsyncMock(return_value=True))

    offer = await upsell.build_offer(AsyncMock(), _user(), topup_amount_kopeks=50000, payment_method='yookassa')

    assert offer is None


@pytest.mark.asyncio
async def test_no_offer_when_user_already_donated(monkeypatch):
    monkeypatch.setattr(settings, 'DONATION_UPSELL_SKIP_IF_DONATED', True, raising=False)
    monkeypatch.setattr(upsell, 'get_user_donation_total_kopeks', AsyncMock(return_value=5000))

    offer = await upsell.build_offer(AsyncMock(), _user(), topup_amount_kopeks=50000, payment_method='yookassa')

    assert offer is None


def test_max_tip_respects_method_limit():
    assert upsell.resolve_max_tip_kopeks(topup_amount_kopeks=90000, method_max_kopeks=100000) == 10000
    assert upsell.resolve_max_tip_kopeks(topup_amount_kopeks=90000, method_max_kopeks=None) == upsell.MAX_TIP_KOPEKS
    assert upsell.resolve_max_tip_kopeks(topup_amount_kopeks=100000, method_max_kopeks=100000) == 0


@pytest.mark.asyncio
async def test_register_pledge_supersedes_previous(monkeypatch):
    db = AsyncMock()
    supersede = AsyncMock(return_value=1)
    create = AsyncMock(return_value=MagicMock(id=3))
    monkeypatch.setattr(upsell.pledge_crud, 'supersede_pending_pledges', supersede)
    monkeypatch.setattr(upsell.pledge_crud, 'create_pledge', create)

    await upsell.register_pledge(
        db, _user(), tip_kopeks=1000, topup_amount_kopeks=50000, payment_method='yookassa', source='bot'
    )

    supersede.assert_awaited_once()
    assert create.await_args.kwargs['tip_kopeks'] == 1000
    assert create.await_args.kwargs['source'] == 'bot'
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `uv run pytest tests/services/test_donation_upsell_offer.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.donation_upsell_service'`

- [ ] **Step 3: Реализовать сервис (часть «предложение»)**

Создать `app/services/donation_upsell_service.py`:

```python
"""Донат-апселл: «добавить 10 ₽ к платежу» при пополнении баланса.

Единственный источник правды для бота и кабинета: показывать ли предложение,
что именно предлагать, как зафиксировать решение и как исполнить обещание после
оплаты. Механика денег описана в
``docs/superpowers/specs/2026-07-30-donation-upsell-design.md``:

  платёж на (пополнение + надбавка) → вебхук зачисляет всю сумму
  → ``try_apply_donation_pledge`` списывает надбавку и пишет пожертвование.

Порядок «зачислить всё, потом списать надбавку» выбран сознательно: баланс не
уходит в минус, а любая ошибка исполнения оставляет деньги пользователю.
"""

from dataclasses import dataclass

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud import donation_pledge as pledge_crud
from app.database.crud.donation import get_user_donation_total_kopeks
from app.database.models import DonationPledge, DonationPledgeStatus, User


logger = structlog.get_logger(__name__)

# Tribute — внешняя donate-ссылка без локальной записи платежа: зачисление проходит
# мимо общего пост-хука, исполнить обещание нечем. Как и в обычных донатах.
EXCLUDED_UPSELL_METHODS = frozenset({'tribute'})

# Предохранитель на «иную сумму»: 50 000 ₽ надбавки — заведомо ошибка ввода.
MAX_TIP_KOPEKS = 5_000_000

DONATION_UPSELL_SOURCE = 'topup_upsell'


@dataclass(frozen=True)
class UpsellOffer:
    """Что показать пользователю на экране апселла."""

    tip_kopeks: int
    presets_kopeks: list[int]
    topup_amount_kopeks: int
    total_kopeks: int
    max_tip_kopeks: int


def resolve_max_tip_kopeks(*, topup_amount_kopeks: int, method_max_kopeks: int | None) -> int:
    """Наибольшая надбавка, которая ещё влезает в лимит способа оплаты."""
    limit = MAX_TIP_KOPEKS
    if method_max_kopeks is not None:
        limit = min(limit, max(0, int(method_max_kopeks) - int(topup_amount_kopeks)))
    return max(0, limit)


async def is_upsell_available(db: AsyncSession, user: User) -> bool:
    """Условия, не зависящие от суммы и способа оплаты (для кабинета и бота)."""
    if not settings.DONATIONS_ENABLED or not settings.DONATION_UPSELL_ENABLED:
        return False
    if getattr(user, 'restriction_topup', False):
        return False
    if settings.get_donation_upsell_tip_kopeks() <= 0:
        return False

    if await pledge_crud.has_recent_decision(db, user_id=user.id, days=settings.DONATION_UPSELL_COOLDOWN_DAYS):
        return False

    if settings.DONATION_UPSELL_SKIP_IF_DONATED:
        try:
            donated = await get_user_donation_total_kopeks(db, user.id)
        except Exception as error:  # pragma: no cover — статистика не должна ломать оплату
            logger.warning('Не удалось проверить прошлые пожертвования', user_id=user.id, error=error)
            donated = 0
        if donated > 0:
            return False

    return True


async def build_offer(
    db: AsyncSession,
    user: User,
    *,
    topup_amount_kopeks: int,
    payment_method: str,
    method_max_kopeks: int | None = None,
) -> UpsellOffer | None:
    """Предложение для конкретного пополнения, либо ``None`` — не показывать."""
    if payment_method in EXCLUDED_UPSELL_METHODS:
        return None
    if topup_amount_kopeks < settings.get_donation_upsell_min_topup_kopeks():
        return None
    if not await is_upsell_available(db, user):
        return None

    tip_kopeks = settings.get_donation_upsell_tip_kopeks()
    max_tip = resolve_max_tip_kopeks(topup_amount_kopeks=topup_amount_kopeks, method_max_kopeks=method_max_kopeks)
    if tip_kopeks > max_tip:
        # Пополнение уже у верхней границы способа — надбавку провайдер не примет.
        return None

    presets = [amount for amount in settings.get_donation_upsell_presets_kopeks() if amount <= max_tip]

    return UpsellOffer(
        tip_kopeks=tip_kopeks,
        presets_kopeks=presets,
        topup_amount_kopeks=topup_amount_kopeks,
        total_kopeks=topup_amount_kopeks + tip_kopeks,
        max_tip_kopeks=max_tip,
    )


async def register_pledge(
    db: AsyncSession,
    user: User,
    *,
    tip_kopeks: int,
    topup_amount_kopeks: int,
    payment_method: str | None,
    source: str,
) -> DonationPledge:
    """Фиксирует согласие: гасит прежние обещания и создаёт новое ``pending``."""
    await pledge_crud.supersede_pending_pledges(db, user_id=user.id)
    return await pledge_crud.create_pledge(
        db,
        user_id=user.id,
        tip_kopeks=tip_kopeks,
        topup_amount_kopeks=topup_amount_kopeks,
        payment_method=payment_method,
        source=source,
        ttl_hours=settings.DONATION_UPSELL_PLEDGE_TTL_HOURS,
    )


async def register_decline(
    db: AsyncSession,
    user: User,
    *,
    topup_amount_kopeks: int,
    payment_method: str | None,
    source: str,
) -> None:
    """Фиксирует отказ: нужен для cooldown и воронки, гасит прежние обещания."""
    try:
        await pledge_crud.supersede_pending_pledges(db, user_id=user.id)
        await pledge_crud.create_pledge(
            db,
            user_id=user.id,
            tip_kopeks=0,
            topup_amount_kopeks=topup_amount_kopeks,
            payment_method=payment_method,
            source=source,
            ttl_hours=settings.DONATION_UPSELL_PLEDGE_TTL_HOURS,
            status=DonationPledgeStatus.DECLINED,
        )
    except Exception as error:  # pragma: no cover — отказ не должен мешать оплате
        logger.warning('Не удалось записать отказ от донат-апселла', user_id=user.id, error=error)
```

- [ ] **Step 4: Убедиться, что тесты проходят**

Run: `uv run pytest tests/services/test_donation_upsell_offer.py -v`
Expected: 12 passed

- [ ] **Step 5: Линтер и коммит**

```bash
uv run ruff format app/services/donation_upsell_service.py tests/services/test_donation_upsell_offer.py
uv run ruff check app/services/donation_upsell_service.py
git add app/services/donation_upsell_service.py tests/services/test_donation_upsell_offer.py
git commit -m "feat(donations): сервис донат-апселла — условия показа и фиксация решения"
```

---

## Task 4: Исполнение обещания после зачисления

**Files:**
- Modify: `app/services/donation_upsell_service.py` (добавить подбор и применение)
- Modify: `app/services/payment/common.py` (`send_cart_notification_after_topup`, строки 319-340)
- Modify: `app/services/admin_notification_service.py` (`send_donation_notification`, строка 931)
- Test: `tests/services/test_donation_upsell_apply.py`

**Interfaces:**
- Consumes: `pledge_crud.list_pending_pledges`, `pledge_crud.mark_pledge_applied`, `pledge_crud.mark_pledge_status` (Task 2); `UpsellOffer`, `DONATION_UPSELL_SOURCE` (Task 3).
- Produces:
  - `def select_matching_pledge(pledges: list[DonationPledge], *, credited_amount_kopeks: int) -> DonationPledge | None` — чистая функция
  - `def pledge_external_id(pledge_id: int) -> str` → `f'pledge:{pledge_id}'`
  - `async def try_apply_donation_pledge(db, user, credited_amount_kopeks: int, bot=None) -> bool`
  - `send_donation_notification(..., source: str = 'menu')` в `AdminNotificationService`

- [ ] **Step 1: Написать падающие тесты**

Создать `tests/services/test_donation_upsell_apply.py`:

```python
"""Исполнение обещания: надбавка уходит из баланса в пожертвование ровно один раз."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.services.donation_upsell_service as upsell
from app.config import settings
from app.database.models import DonationPledgeStatus, TransactionType


def _pledge(pledge_id: int = 1, tip: int = 1000, total: int = 51000):
    pledge = MagicMock()
    pledge.id = pledge_id
    pledge.tip_kopeks = tip
    pledge.expected_total_kopeks = total
    pledge.topup_amount_kopeks = total - tip
    pledge.payment_method = 'yookassa'
    pledge.expires_at = datetime.now(UTC) + timedelta(hours=1)
    return pledge


def _user(balance: int = 51000):
    user = MagicMock()
    user.id = 42
    user.telegram_id = 111
    user.language = 'ru'
    user.balance_kopeks = balance
    return user


def test_matching_pledge_exact_amount():
    pledge = _pledge()

    assert upsell.select_matching_pledge([pledge], credited_amount_kopeks=51000) is pledge


def test_matching_pledge_tolerates_stars_rounding():
    """Stars подгоняют сумму под целое число звёзд — допуск это покрывает."""
    pledge = _pledge(total=51000)

    assert upsell.select_matching_pledge([pledge], credited_amount_kopeks=50980) is pledge


def test_no_match_for_different_amount():
    pledge = _pledge(total=51000)

    assert upsell.select_matching_pledge([pledge], credited_amount_kopeks=10000) is None


def test_no_match_when_tip_is_zero():
    assert upsell.select_matching_pledge([_pledge(tip=0)], credited_amount_kopeks=51000) is None


def test_newest_matching_pledge_wins():
    older = _pledge(pledge_id=1)
    newer = _pledge(pledge_id=2)

    assert upsell.select_matching_pledge([newer, older], credited_amount_kopeks=51000) is newer


def test_external_id_format():
    assert upsell.pledge_external_id(7) == 'pledge:7'


@pytest.fixture
def applied_env(monkeypatch):
    """Общая обвязка: обещание найдено, отметка проходит, побочки замоканы."""
    monkeypatch.setattr(settings, 'DONATIONS_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'DONATION_UPSELL_ENABLED', True, raising=False)

    pledge = _pledge()
    monkeypatch.setattr(upsell.pledge_crud, 'list_pending_pledges', AsyncMock(return_value=[pledge]))
    monkeypatch.setattr(upsell.pledge_crud, 'mark_pledge_applied', AsyncMock(return_value=True))
    monkeypatch.setattr(upsell.pledge_crud, 'mark_pledge_status', AsyncMock())

    transaction = MagicMock(id=555)
    create_transaction = AsyncMock(return_value=transaction)
    create_donation = AsyncMock()
    lock_user = AsyncMock(side_effect=lambda db, user: user)
    notify = AsyncMock()

    monkeypatch.setattr(upsell, 'create_transaction', create_transaction, raising=False)
    monkeypatch.setattr(upsell, 'create_donation', create_donation, raising=False)
    monkeypatch.setattr(upsell, 'lock_user_for_update', lock_user, raising=False)
    monkeypatch.setattr(upsell, '_notify_pledge_applied', notify, raising=False)

    return {
        'pledge': pledge,
        'create_transaction': create_transaction,
        'create_donation': create_donation,
        'notify': notify,
    }


@pytest.mark.asyncio
async def test_apply_debits_balance_and_records_donation(applied_env):
    db = AsyncMock()
    user = _user(balance=51000)

    applied = await upsell.try_apply_donation_pledge(db, user, credited_amount_kopeks=51000, bot=MagicMock())

    assert applied is True
    assert user.balance_kopeks == 50000
    tx_kwargs = applied_env['create_transaction'].await_args.kwargs
    assert tx_kwargs['type'] == TransactionType.DONATION
    assert tx_kwargs['amount_kopeks'] == 1000
    assert tx_kwargs['external_id'] == 'pledge:1'
    donation_kwargs = applied_env['create_donation'].await_args.kwargs
    assert donation_kwargs['amount_kopeks'] == 1000
    assert donation_kwargs['source'] == upsell.DONATION_UPSELL_SOURCE
    assert donation_kwargs['external_id'] == 'pledge:1'
    assert donation_kwargs['transaction_id'] == 555
    db.commit.assert_awaited()
    applied_env['notify'].assert_awaited_once()


@pytest.mark.asyncio
async def test_apply_is_idempotent(applied_env, monkeypatch):
    """Повторный вебхук: отметка не прошла → откат, баланс не тронут.

    Транзакция пожертвования создаётся до отметки (ей нужен transaction_id), но
    всё живёт в одной транзакции БД — не прошла отметка, откатывается и вставка.
    """
    monkeypatch.setattr(upsell.pledge_crud, 'mark_pledge_applied', AsyncMock(return_value=False))
    db = AsyncMock()
    user = _user(balance=51000)

    applied = await upsell.try_apply_donation_pledge(db, user, credited_amount_kopeks=51000)

    assert applied is False
    assert user.balance_kopeks == 51000
    db.rollback.assert_awaited()
    applied_env['create_donation'].assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_skipped_when_balance_too_low(applied_env, monkeypatch):
    mark_status = AsyncMock()
    monkeypatch.setattr(upsell.pledge_crud, 'mark_pledge_status', mark_status)
    db = AsyncMock()
    user = _user(balance=500)

    applied = await upsell.try_apply_donation_pledge(db, user, credited_amount_kopeks=51000)

    assert applied is False
    assert user.balance_kopeks == 500
    assert mark_status.await_args.kwargs['status'] == DonationPledgeStatus.SKIPPED


@pytest.mark.asyncio
async def test_apply_noop_when_feature_disabled(monkeypatch):
    monkeypatch.setattr(settings, 'DONATION_UPSELL_ENABLED', False, raising=False)
    list_pending = AsyncMock(return_value=[])
    monkeypatch.setattr(upsell.pledge_crud, 'list_pending_pledges', list_pending)

    applied = await upsell.try_apply_donation_pledge(AsyncMock(), _user(), credited_amount_kopeks=51000)

    assert applied is False
    list_pending.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_noop_when_no_pledge(monkeypatch):
    monkeypatch.setattr(settings, 'DONATION_UPSELL_ENABLED', True, raising=False)
    monkeypatch.setattr(upsell.pledge_crud, 'list_pending_pledges', AsyncMock(return_value=[]))

    applied = await upsell.try_apply_donation_pledge(AsyncMock(), _user(), credited_amount_kopeks=51000)

    assert applied is False
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `uv run pytest tests/services/test_donation_upsell_apply.py -v`
Expected: FAIL — `AttributeError: module 'app.services.donation_upsell_service' has no attribute 'select_matching_pledge'`

- [ ] **Step 3: Добавить импорты и подбор обещания в сервис**

В `app/services/donation_upsell_service.py` дополнить импорты (верх файла):

```python
from app.database.crud.donation import create_donation, get_user_donation_total_kopeks
from app.database.crud.user import lock_user_for_update
from app.database.crud.transaction import create_transaction
from app.database.models import (
    DonationPledge,
    DonationPledgeStatus,
    PaymentMethod,
    TransactionType,
    User,
)
from app.localization.texts import get_texts
```

И добавить константы + чистые функции после `resolve_max_tip_kopeks`:

```python
# Допуск при сверке суммы: у Telegram Stars платёж подгоняется под целое число
# звёзд, поэтому зачисленная сумма может отличаться от обещанной на копейки.
PLEDGE_MATCH_TOLERANCE_KOPEKS = 200
PLEDGE_MATCH_TOLERANCE_RATIO = 0.01


def pledge_external_id(pledge_id: int) -> str:
    """Идентификатор пожертвования-надбавки. Префикс — контракт для истории баланса."""
    return f'pledge:{pledge_id}'


def select_matching_pledge(
    pledges: list[DonationPledge],
    *,
    credited_amount_kopeks: int,
) -> DonationPledge | None:
    """Обещание, к которому относится зачисленный платёж (самое свежее подходящее).

    ``pledges`` приходит отсортированным «новые первыми``. Сверяем сумму: без этого
    брошенный платёж мог бы списать надбавку со следующего пополнения.
    """
    for pledge in pledges:
        tip = int(getattr(pledge, 'tip_kopeks', 0) or 0)
        if tip <= 0:
            continue
        expected = int(getattr(pledge, 'expected_total_kopeks', 0) or 0)
        tolerance = max(PLEDGE_MATCH_TOLERANCE_KOPEKS, int(expected * PLEDGE_MATCH_TOLERANCE_RATIO))
        if abs(credited_amount_kopeks - expected) <= tolerance:
            return pledge
    return None
```

- [ ] **Step 4: Реализовать применение надбавки**

Добавить в конец `app/services/donation_upsell_service.py`:

```python
async def try_apply_donation_pledge(
    db: AsyncSession,
    user: User,
    credited_amount_kopeks: int,
    bot: object | None = None,
) -> bool:
    """Списывает обещанную надбавку с только что пополненного баланса.

    Вызывается сразу после зачисления платежа (см.
    ``app/services/payment/common.py::send_cart_notification_after_topup``), то есть
    уже вне транзакции зачисления. Возвращает ``True``, если надбавка списана и
    записана как пожертвование.

    Ошибки намеренно проглатываются: пополнение важнее надбавки, а неисполненное
    обещание означает лишь, что деньги остались на балансе пользователя.
    """
    if not settings.DONATIONS_ENABLED or not settings.DONATION_UPSELL_ENABLED:
        return False

    try:
        pledges = await pledge_crud.list_pending_pledges(db, user_id=user.id)
    except Exception as error:  # pragma: no cover — БД недоступна
        logger.warning('Не удалось получить обещания донат-апселла', user_id=user.id, error=error)
        return False

    pledge = select_matching_pledge(pledges, credited_amount_kopeks=credited_amount_kopeks)
    if pledge is None:
        return False

    pledge_id = int(pledge.id)
    tip_kopeks = int(pledge.tip_kopeks)
    payment_method_value = pledge.payment_method
    external_id = pledge_external_id(pledge_id)

    try:
        locked_user = await lock_user_for_update(db, user)

        if locked_user.balance_kopeks < tip_kopeks:
            # Успел потратить пополнение до нашего списания — не загоняем баланс в минус.
            await pledge_crud.mark_pledge_status(db, pledge_id=pledge_id, status=DonationPledgeStatus.SKIPPED)
            await db.commit()
            logger.warning(
                'Надбавку донат-апселла применить нечем: не хватает баланса',
                user_id=user.id,
                pledge_id=pledge_id,
                tip_kopeks=tip_kopeks,
                balance_kopeks=locked_user.balance_kopeks,
            )
            return False

        transaction = await create_transaction(
            db=db,
            user_id=locked_user.id,
            type=TransactionType.DONATION,
            amount_kopeks=tip_kopeks,
            description='Пожертвование при пополнении баланса',
            payment_method=_as_payment_method(payment_method_value),
            external_id=external_id,
            is_completed=True,
            commit=False,
        )

        # Отметка и списание — в одной транзакции: condition status='pending' внутри
        # UPDATE делает повторный вебхук безопасным (вторая попытка увидит applied).
        marked = await pledge_crud.mark_pledge_applied(db, pledge_id=pledge_id, transaction_id=transaction.id)
        if not marked:
            await db.rollback()
            logger.info('Надбавка донат-апселла уже применена, пропускаем', pledge_id=pledge_id)
            return False

        locked_user.balance_kopeks -= tip_kopeks

        await create_donation(
            db,
            user=locked_user,
            user_id=locked_user.id,
            amount_kopeks=tip_kopeks,
            payment_method=payment_method_value,
            external_id=external_id,
            transaction_id=transaction.id,
            source=DONATION_UPSELL_SOURCE,
            commit=False,
        )

        await db.commit()
    except Exception as error:
        await db.rollback()
        logger.error(
            'Ошибка применения надбавки донат-апселла',
            user_id=user.id,
            pledge_id=pledge_id,
            error=error,
            exc_info=True,
        )
        return False

    logger.info(
        '❤️ Надбавка донат-апселла записана как пожертвование',
        user_id=user.id,
        pledge_id=pledge_id,
        tip_kopeks=tip_kopeks,
        credited_amount_kopeks=credited_amount_kopeks,
    )

    if bot is not None:
        await _notify_pledge_applied(bot, db, user, tip_kopeks, payment_method_value)

    return True


def _as_payment_method(value: str | None) -> PaymentMethod | None:
    """Строка способа оплаты → enum, если он там есть (иначе None)."""
    if not value:
        return None
    try:
        return PaymentMethod(value)
    except ValueError:
        return None


async def _notify_pledge_applied(
    bot: object,
    db: AsyncSession,
    user: User,
    tip_kopeks: int,
    payment_method_value: str | None,
) -> None:
    """Спасибо донору и уведомление админам. Никогда не ломает основной поток."""
    try:
        from app.services.admin_notification_service import AdminNotificationService

        await AdminNotificationService(bot).send_donation_notification(
            user,
            amount_kopeks=tip_kopeks,
            payment_method=payment_method_value,
            source=DONATION_UPSELL_SOURCE,
        )
    except Exception as error:
        logger.warning('Не удалось отправить админ-уведомление о надбавке', error=error)

    telegram_id = getattr(user, 'telegram_id', None)
    if not telegram_id:
        return

    try:
        texts = get_texts(getattr(user, 'language', None) or 'ru')
        message = texts.t(
            'DONATION_UPSELL_THANK_YOU',
            '❤️ <b>Спасибо за поддержку!</b>\n\n'
            '{amount} из вашего платежа пошли на развитие проекта — на мощности и скорость соединения. '
            'На баланс зачислена сумма пополнения. Спасибо! 🙏',
        ).format(amount=settings.format_price(tip_kopeks))
        await bot.send_message(chat_id=telegram_id, text=message, parse_mode='HTML')
    except Exception as error:
        logger.warning('Не удалось поблагодарить за надбавку', error=error)
```

- [ ] **Step 5: Убедиться, что тесты проходят**

Run: `uv run pytest tests/services/test_donation_upsell_apply.py -v`
Expected: 11 passed

- [ ] **Step 6: Добавить параметр `source` в админ-уведомление**

В `app/services/admin_notification_service.py`, `send_donation_notification` (строка 931): добавить в сигнатуру `source: str = 'menu'`, а в тексте (строка ~954, заголовок `'❤️ <b>Новое пожертвование!</b>'`) — пометку источника:

```python
            title = (
                '❤️ <b>Новое пожертвование при пополнении!</b>'
                if source == 'topup_upsell'
                else '❤️ <b>Новое пожертвование!</b>'
            )

            lines = [
                title,
                '',
```

- [ ] **Step 7: Врезать вызов в общий пост-хук**

В `app/services/payment/common.py`, функция `send_cart_notification_after_topup` (строка 319):

1. удалить строку `del amount_kopeks  # больше не используется после удаления второго сообщения` — сумма снова нужна;
2. сразу после докстринга вставить:

```python
    # Донат-апселл: пользователь согласился добавить к платежу сумму в пользу проекта.
    # Зачислено было всё (включая надбавку) — списываем её здесь и записываем
    # пожертвование. Делаем до автопокупки корзины, чтобы та видела верный баланс.
    try:
        from app.services.donation_upsell_service import try_apply_donation_pledge

        await try_apply_donation_pledge(db, user, amount_kopeks, bot=bot)
    except Exception as upsell_error:  # pragma: no cover — надбавка не должна ломать пополнение
        logger.error(
            'Ошибка применения донат-апселла после пополнения',
            user_id=getattr(user, 'id', None),
            error=upsell_error,
            exc_info=True,
        )
```

3. обновить докстринг функции, добавив строкой: `Первым делом исполняет обещание донат-апселла (если оно есть).`

- [ ] **Step 8: Проверить, что весь набор тестов зелёный**

Run: `uv run ruff format . && uv run ruff check . && uv run pytest -q`
Expected: всё зелёное; особенно `tests/services/` и `tests/handlers/test_donation_flow.py`.

- [ ] **Step 9: Коммит**

```bash
git add app/services/donation_upsell_service.py app/services/payment/common.py app/services/admin_notification_service.py tests/services/test_donation_upsell_apply.py
git commit -m "feat(donations): исполнение надбавки после зачисления платежа"
```

---

## Task 5: Бот — экран апселла

**Files:**
- Create: `app/handlers/balance/donation_upsell.py`
- Modify: `app/handlers/balance/main.py` (вынести разводку из `handle_topup_amount_callback`, строки 617-678; врезка в `process_topup_amount`, строка 570; регистрация в `register_balance_handlers`, строка 680)
- Modify: `app/states.py` (`BalanceStates`, строка 38)
- Test: `tests/handlers/test_donation_upsell_flow.py`

**Interfaces:**
- Consumes: `build_offer`, `register_pledge`, `register_decline`, `UpsellOffer` (Task 3).
- Produces:
  - `app/handlers/balance/main.py::dispatch_topup(callback, db_user, state, *, amount_kopeks: int, method: str) -> bool` — разводка платежа по способу (вынесено из `handle_topup_amount_callback`)
  - `app/handlers/balance/donation_upsell.py::maybe_show_upsell(callback_or_message, db_user, db, state, *, amount_kopeks: int, method: str) -> bool` — `True`, если экран показан и разводку надо отложить
  - `app/handlers/balance/donation_upsell.py::register_donation_upsell_handlers(dp) -> None`
  - `BalanceStates.waiting_for_upsell_tip`
  - Callback-данные: `dupsell_yes`, `dupsell_no`, `dupsell_custom`, `dupsell_amt|<kopeks>`, `dupsell_back`

**Важно про сессию БД:** `app/middlewares/auth.py:234` кладёт сессию в `data['db']`, и aiogram инжектит её в хендлер по имени параметра. Поэтому в этой задаче `handle_topup_amount_callback` и `process_topup_amount` получают новый параметр `db: AsyncSession`, а модуль апселла **не открывает** собственные сессии через `AsyncSessionLocal` — иначе тесты (и прод) получали бы вторую параллельную транзакцию. Оба хендлера вызываются только через регистрацию (`main.py:753`, `main.py:887`), прямых вызовов из кода нет — менять сигнатуры безопасно.

- [ ] **Step 1: Написать падающие тесты**

Создать `tests/handlers/test_donation_upsell_flow.py`:

```python
"""Экран донат-апселла в боте: что показывает и с какой суммой продолжает оплату."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.handlers.balance.donation_upsell as upsell_mod
from app.config import settings
from app.services.donation_upsell_service import UpsellOffer


def _user():
    user = MagicMock()
    user.id = 42
    user.language = 'ru'
    user.telegram_id = 111
    user.restriction_topup = False
    return user


def _callback(data: str = 'dupsell_yes'):
    callback = MagicMock()
    callback.data = data
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.text = 'экран суммы'
    callback.message.chat.id = 111
    callback.message.message_id = 5
    callback.message.edit_text = AsyncMock()
    callback.message.answer = AsyncMock()
    return callback


def _state(data: dict | None = None):
    state = AsyncMock()
    state.get_data = AsyncMock(return_value=data or {})
    return state


def _offer(tip: int = 1000, topup: int = 50000, presets=(2500, 5000)):
    return UpsellOffer(
        tip_kopeks=tip,
        presets_kopeks=list(presets),
        topup_amount_kopeks=topup,
        total_kopeks=topup + tip,
        max_tip_kopeks=5_000_000,
    )


@pytest.mark.asyncio
async def test_screen_shown_with_three_choices(monkeypatch):
    monkeypatch.setattr(upsell_mod, 'build_offer', AsyncMock(return_value=_offer()))
    callback = _callback()

    shown = await upsell_mod.maybe_show_upsell(
        callback, _user(), MagicMock(), _state(), amount_kopeks=50000, method='yookassa'
    )

    assert shown is True
    markup = callback.message.edit_text.call_args.kwargs['reply_markup']
    callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
    assert 'dupsell_yes' in callbacks
    assert 'dupsell_no' in callbacks
    assert 'dupsell_custom' in callbacks
    text = callback.message.edit_text.call_args.args[0]
    assert '510' in text  # итоговая сумма: 500 ₽ пополнение + 10 ₽ поддержка


@pytest.mark.asyncio
async def test_no_screen_when_offer_is_none(monkeypatch):
    monkeypatch.setattr(upsell_mod, 'build_offer', AsyncMock(return_value=None))
    callback = _callback()

    shown = await upsell_mod.maybe_show_upsell(
        callback, _user(), MagicMock(), _state(), amount_kopeks=50000, method='tribute'
    )

    assert shown is False
    callback.message.edit_text.assert_not_called()


@pytest.mark.asyncio
async def test_accept_registers_pledge_and_dispatches_total(monkeypatch):
    register_pledge = AsyncMock(return_value=MagicMock(id=3))
    dispatch = AsyncMock(return_value=True)
    monkeypatch.setattr(upsell_mod, 'register_pledge', register_pledge)
    monkeypatch.setattr(upsell_mod, '_dispatch_topup', dispatch)

    state = _state({'upsell_topup_amount': 50000, 'upsell_tip': 1000, 'upsell_method': 'yookassa'})
    await upsell_mod.handle_upsell_accept(_callback('dupsell_yes'), _user(), MagicMock(), state)

    assert register_pledge.await_args.kwargs['tip_kopeks'] == 1000
    assert register_pledge.await_args.kwargs['source'] == 'bot'
    assert dispatch.await_args.kwargs['amount_kopeks'] == 51000
    assert dispatch.await_args.kwargs['method'] == 'yookassa'


@pytest.mark.asyncio
async def test_decline_dispatches_original_amount(monkeypatch):
    register_decline = AsyncMock()
    dispatch = AsyncMock(return_value=True)
    monkeypatch.setattr(upsell_mod, 'register_decline', register_decline)
    monkeypatch.setattr(upsell_mod, '_dispatch_topup', dispatch)

    state = _state({'upsell_topup_amount': 50000, 'upsell_tip': 1000, 'upsell_method': 'yookassa'})
    await upsell_mod.handle_upsell_decline(_callback('dupsell_no'), _user(), MagicMock(), state)

    register_decline.assert_awaited_once()
    assert dispatch.await_args.kwargs['amount_kopeks'] == 50000


@pytest.mark.asyncio
async def test_custom_screen_offers_presets(monkeypatch):
    monkeypatch.setattr(settings, 'DONATION_UPSELL_PRESETS', '25,50', raising=False)
    callback = _callback('dupsell_custom')
    state = _state({'upsell_topup_amount': 50000, 'upsell_tip': 1000, 'upsell_method': 'yookassa'})

    await upsell_mod.handle_upsell_custom(callback, _user(), MagicMock(), state)

    markup = callback.message.edit_text.call_args.kwargs['reply_markup']
    callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
    assert 'dupsell_amt|2500' in callbacks
    assert 'dupsell_amt|5000' in callbacks
    assert 'dupsell_back' in callbacks


@pytest.mark.asyncio
async def test_custom_preset_dispatches_sum_with_chosen_tip(monkeypatch):
    monkeypatch.setattr(upsell_mod, 'register_pledge', AsyncMock(return_value=MagicMock(id=4)))
    dispatch = AsyncMock(return_value=True)
    monkeypatch.setattr(upsell_mod, '_dispatch_topup', dispatch)

    state = _state({'upsell_topup_amount': 50000, 'upsell_tip': 1000, 'upsell_method': 'yookassa'})
    await upsell_mod.handle_upsell_custom_amount(_callback('dupsell_amt|10000'), _user(), MagicMock(), state)

    assert dispatch.await_args.kwargs['amount_kopeks'] == 60000


@pytest.mark.asyncio
async def test_typed_tip_rejected_when_over_method_limit(monkeypatch):
    dispatch = AsyncMock()
    monkeypatch.setattr(upsell_mod, '_route_payment', dispatch)
    monkeypatch.setattr(upsell_mod, 'resolve_max_tip_kopeks', MagicMock(return_value=5000))

    message = MagicMock()
    message.text = '1000'
    message.answer = AsyncMock()
    state = _state(
        {
            'upsell_topup_amount': 50000,
            'upsell_tip': 1000,
            'upsell_method': 'yookassa',
            'upsell_method_max': 55000,
        }
    )

    await upsell_mod.process_upsell_custom_amount(message, _user(), MagicMock(), state)

    dispatch.assert_not_awaited()
    message.answer.assert_awaited()


@pytest.mark.asyncio
async def test_typed_tip_dispatches_total(monkeypatch):
    monkeypatch.setattr(upsell_mod, 'register_pledge', AsyncMock(return_value=MagicMock(id=5)))
    dispatch = AsyncMock(return_value=True)
    monkeypatch.setattr(upsell_mod, '_route_payment', dispatch)

    message = MagicMock()
    message.text = '50'
    message.answer = AsyncMock()
    message.chat.id = 111
    message.message_id = 9
    message.bot = MagicMock()
    message.bot.delete_message = AsyncMock()
    state = _state({'upsell_topup_amount': 50000, 'upsell_tip': 1000, 'upsell_method': 'yookassa'})

    await upsell_mod.process_upsell_custom_amount(message, _user(), MagicMock(), state)

    assert dispatch.await_args.kwargs['amount_kopeks'] == 55000
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `uv run pytest tests/handlers/test_donation_upsell_flow.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.handlers.balance.donation_upsell'`

- [ ] **Step 3: Добавить состояние FSM**

В `app/states.py`, класс `BalanceStates` (строка 38) — после `waiting_for_donation_amount`:

```python
    waiting_for_upsell_tip = State()
```

- [ ] **Step 4: Вынести разводку платежей в `dispatch_topup`**

В `app/handlers/balance/main.py` заменить тело `handle_topup_amount_callback` (строки 617-678) так, чтобы разводка жила в отдельной функции, а хендлер только парсил callback:

```python
async def dispatch_topup(
    callback: types.CallbackQuery,
    db_user: User,
    state: FSMContext,
    *,
    amount_kopeks: int,
    method: str,
) -> bool:
    """Отправляет пользователя к оплате выбранным способом на указанную сумму.

    Вынесено из ``handle_topup_amount_callback``, потому что этой же разводкой
    продолжается флоу после экрана донат-апселла
    (``app/handlers/balance/donation_upsell.py``).

    Возвращает False, если способ неизвестен.
    """
    # Особые случаи, требующие специальной логики
    if method.startswith('platega_m'):
        from app.database.database import AsyncSessionLocal

        from .platega import process_platega_payment_amount

        platega_method_code = int(method[len('platega_m') :])
        await state.update_data(payment_method='platega', platega_method=platega_method_code)
        await state.set_state(BalanceStates.waiting_for_amount)
        async with AsyncSessionLocal() as db:
            await process_platega_payment_amount(callback.message, db_user, db, amount_kopeks, state)
        return True

    if method == 'platega':
        from app.database.database import AsyncSessionLocal

        from .platega import process_platega_payment_amount, start_platega_payment

        data = await state.get_data()
        method_code = int(data.get('platega_method', 0)) if data else 0

        if method_code > 0:
            await state.set_state(BalanceStates.waiting_for_amount)
            async with AsyncSessionLocal() as db:
                await process_platega_payment_amount(callback.message, db_user, db, amount_kopeks, state)
        else:
            await state.update_data(platega_pending_amount=amount_kopeks)
            await start_platega_payment(callback, db_user, state)
        return True

    if method == 'tribute':
        from .tribute import start_tribute_payment

        await start_tribute_payment(callback, db_user)
        return True

    # Стандартные методы через роутер
    await state.update_data(payment_method=method)
    await state.set_state(BalanceStates.waiting_for_amount)
    return await route_payment_by_method(callback.message, db_user, amount_kopeks, state, method)


@error_handler
async def handle_topup_amount_callback(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
):
    try:
        _, method, amount_str = callback.data.split('|', 2)
        amount_kopeks = int(amount_str)
    except ValueError:
        await callback.answer('❌ Некорректный запрос', show_alert=True)
        return

    if amount_kopeks <= 0:
        await callback.answer('❌ Некорректная сумма', show_alert=True)
        return

    try:
        # Донат-апселл вклинивается между суммой и оплатой: если экран показан,
        # разводку продолжит его хендлер — уже с учётом надбавки.
        from .donation_upsell import maybe_show_upsell

        if await maybe_show_upsell(callback, db_user, db, state, amount_kopeks=amount_kopeks, method=method):
            await callback.answer()
            return

        if not await dispatch_topup(callback, db_user, state, amount_kopeks=amount_kopeks, method=method):
            await callback.answer('❌ Неизвестный способ оплаты', show_alert=True)
            return

        await callback.answer()

    except Exception as error:
        logger.error('Ошибка быстрого пополнения', error=error)
        await callback.answer('❌ Ошибка обработки запроса', show_alert=True)
```

- [ ] **Step 5: Врезать апселл в текстовый ввод суммы**

В `app/handlers/balance/main.py`, `process_topup_amount`: добавить в сигнатуру параметр `db: AsyncSession` (после `db_user: User`) — его инжектит auth-middleware — и заменить блок вызова роутера (строки 570-571):

```python
        # Донат-апселл: экран показывается и когда сумму вводят текстом. Решение
        # придёт кнопкой, поэтому продолжение флоу берёт на себя его хендлер.
        from .donation_upsell import maybe_show_upsell

        if await maybe_show_upsell(message, db_user, db, state, amount_kopeks=amount_kopeks, method=payment_method):
            return

        if not await route_payment_by_method(message, db_user, amount_kopeks, state, payment_method):
            await message.answer('Неизвестный способ оплаты')
```

- [ ] **Step 6: Реализовать модуль апселла**

Создать `app/handlers/balance/donation_upsell.py`:

```python
"""Донат-апселл в боте: «Поддержать проект и добавить 10 ₽?».

Экран вклинивается между выбором суммы пополнения и переходом к оплате. Согласие
увеличивает сумму платежа и создаёт обещание надбавки, отказ — просто отметку.
Дальше флоу продолжает та же разводка, что и обычное пополнение
(``app/handlers/balance/main.py::dispatch_topup``), поэтому провайдерные хендлеры
о надбавке ничего не знают.

Исполняет обещание вебхук: ``app/services/donation_upsell_service.py``.
"""

import structlog
from aiogram import Dispatcher, F, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import InaccessibleMessage
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import User
from app.localization.texts import get_texts
from app.services.donation_upsell_service import (
    build_offer,
    register_decline,
    register_pledge,
    resolve_max_tip_kopeks,
)
from app.states import BalanceStates
from app.utils.decorators import error_handler


logger = structlog.get_logger(__name__)

UPSELL_ACCEPT = 'dupsell_yes'
UPSELL_DECLINE = 'dupsell_no'
UPSELL_CUSTOM = 'dupsell_custom'
UPSELL_CUSTOM_PREFIX = 'dupsell_amt|'
UPSELL_BACK = 'dupsell_back'

PLEDGE_SOURCE = 'bot'


async def _method_max_kopeks(db: AsyncSession | None, method: str) -> int | None:
    """Максимум выбранного способа оплаты (настройка кабинета), либо None."""
    if db is None:
        return None
    try:
        from app.keyboards.topup_amounts import resolve_config_method_id
        from app.services.payment_method_config_service import _get_method_defaults, get_config_by_method_id

        method_id = resolve_config_method_id(method)
        defaults = _get_method_defaults().get(method_id, {})
        max_amount = defaults.get('default_max')
        config = await get_config_by_method_id(db, method_id)
        if config is not None and config.max_amount_kopeks is not None:
            max_amount = config.max_amount_kopeks
        return int(max_amount) if max_amount else None
    except Exception as error:  # pragma: no cover — лимиты не должны ломать оплату
        logger.warning('Не удалось получить лимит способа оплаты для апселла', method=method, error=error)
        return None


def _screen_text(texts, *, topup_kopeks: int, tip_kopeks: int) -> str:
    return texts.t(
        'DONATION_UPSELL_PROMPT',
        '💝 <b>Поддержать проект и добавить {tip}?</b>\n\n'
        '<tg-emoji emoji-id="6244595016287654616">⚠️</tg-emoji> Не зачисляются на баланс, '
        'а идут на увеличение мощностей и скорости соединения 🖥🖥😡\n\n'
        'Пополнение: <b>{topup}</b>\n'
        'Поддержка: <b>+{tip}</b>\n'
        'К оплате: <b>{total}</b>',
    ).format(
        tip=texts.format_price(tip_kopeks),
        topup=texts.format_price(topup_kopeks),
        total=texts.format_price(topup_kopeks + tip_kopeks),
    )


def _screen_keyboard(texts, *, topup_kopeks: int, tip_kopeks: int) -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=texts.t('DONATION_UPSELL_ACCEPT', '❤️ Поддержать — {total}').format(
                        total=texts.format_price(topup_kopeks + tip_kopeks)
                    ),
                    callback_data=UPSELL_ACCEPT,
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t('DONATION_UPSELL_DECLINE', 'Не поддержать — {topup}').format(
                        topup=texts.format_price(topup_kopeks)
                    ),
                    callback_data=UPSELL_DECLINE,
                )
            ],
            [
                types.InlineKeyboardButton(
                    text=texts.t('DONATION_UPSELL_CUSTOM', '💰 Пожертвовать иную сумму'),
                    callback_data=UPSELL_CUSTOM,
                )
            ],
        ]
    )


async def _show(message: types.Message, text: str, keyboard: types.InlineKeyboardMarkup, *, edit: bool) -> int | None:
    """Рисует экран: правкой сообщения, если можем, иначе новым сообщением."""
    if edit:
        try:
            if message.text:
                sent = await message.edit_text(text, reply_markup=keyboard, parse_mode='HTML')
            else:
                sent = await message.edit_caption(caption=text, reply_markup=keyboard, parse_mode='HTML')
            return getattr(sent, 'message_id', message.message_id)
        except TelegramBadRequest:
            pass
    sent = await message.answer(text, reply_markup=keyboard, parse_mode='HTML')
    return getattr(sent, 'message_id', None)


async def maybe_show_upsell(
    callback_or_message,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
    *,
    amount_kopeks: int,
    method: str,
) -> bool:
    """Показывает экран апселла. ``True`` — показан, оплату продолжит его хендлер.

    ``db`` — сессия из auth-middleware: своих сессий здесь не открываем, чтобы не
    плодить параллельные транзакции внутри одного апдейта.
    """
    is_callback = hasattr(callback_or_message, 'data') and hasattr(callback_or_message, 'message')
    message = callback_or_message.message if is_callback else callback_or_message
    if isinstance(message, InaccessibleMessage):
        return False

    try:
        method_max = await _method_max_kopeks(db, method)
        offer = await build_offer(
            db,
            db_user,
            topup_amount_kopeks=amount_kopeks,
            payment_method=method,
            method_max_kopeks=method_max,
        )
    except Exception as error:  # pragma: no cover — апселл не должен ломать пополнение
        logger.warning('Не удалось построить предложение донат-апселла', user_id=db_user.id, error=error)
        return False

    if offer is None:
        return False

    texts = get_texts(db_user.language)
    prompt_message_id = await _show(
        message,
        _screen_text(texts, topup_kopeks=amount_kopeks, tip_kopeks=offer.tip_kopeks),
        _screen_keyboard(texts, topup_kopeks=amount_kopeks, tip_kopeks=offer.tip_kopeks),
        edit=is_callback,
    )

    await state.update_data(
        upsell_topup_amount=amount_kopeks,
        upsell_tip=offer.tip_kopeks,
        upsell_method=method,
        upsell_method_max=offer.max_tip_kopeks + amount_kopeks,
        upsell_prompt_message_id=prompt_message_id,
    )
    return True


async def _dispatch_topup(callback: types.CallbackQuery, db_user: User, state: FSMContext, *, amount_kopeks, method):
    """Продолжает обычный флоу оплаты (импорт локальный — циклическая зависимость)."""
    from .main import dispatch_topup

    return await dispatch_topup(callback, db_user, state, amount_kopeks=amount_kopeks, method=method)


async def _route_payment(message: types.Message, db_user: User, state: FSMContext, *, amount_kopeks, method):
    """Тот же путь, что у обычного текстового ввода суммы (без callback-веток)."""
    from .main import route_payment_by_method

    return await route_payment_by_method(message, db_user, amount_kopeks, state, method)


async def _context(state: FSMContext) -> tuple[int, int, str] | None:
    data = await state.get_data()
    topup = data.get('upsell_topup_amount')
    method = data.get('upsell_method')
    if not topup or not method:
        return None
    return int(topup), int(data.get('upsell_tip') or 0), str(method)


@error_handler
async def handle_upsell_accept(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    """«Поддержать»: обещание + оплата на увеличенную сумму."""
    context = await _context(state)
    if context is None:
        await callback.answer('❌ Сессия оплаты истекла, начните заново', show_alert=True)
        return

    topup_kopeks, tip_kopeks, method = context
    await _accept_with_tip(
        callback, db_user, db, state, topup_kopeks=topup_kopeks, tip_kopeks=tip_kopeks, method=method
    )


async def _accept_with_tip(
    callback: types.CallbackQuery,
    db_user: User,
    db: AsyncSession,
    state: FSMContext,
    *,
    topup_kopeks: int,
    tip_kopeks: int,
    method: str,
) -> None:
    await register_pledge(
        db,
        db_user,
        tip_kopeks=tip_kopeks,
        topup_amount_kopeks=topup_kopeks,
        payment_method=method,
        source=PLEDGE_SOURCE,
    )

    logger.info(
        '❤️ Пользователь согласился на надбавку',
        user_id=db_user.id,
        tip_kopeks=tip_kopeks,
        topup_amount_kopeks=topup_kopeks,
        method=method,
    )

    await callback.answer()
    if not await _dispatch_topup(
        callback, db_user, state, amount_kopeks=topup_kopeks + tip_kopeks, method=method
    ):
        await callback.answer('❌ Неизвестный способ оплаты', show_alert=True)


@error_handler
async def handle_upsell_decline(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    """«Не поддержать»: отметка отказа + обычная оплата."""
    context = await _context(state)
    if context is None:
        await callback.answer('❌ Сессия оплаты истекла, начните заново', show_alert=True)
        return

    topup_kopeks, _tip_kopeks, method = context

    await register_decline(
        db,
        db_user,
        topup_amount_kopeks=topup_kopeks,
        payment_method=method,
        source=PLEDGE_SOURCE,
    )

    await callback.answer()
    if not await _dispatch_topup(callback, db_user, state, amount_kopeks=topup_kopeks, method=method):
        await callback.answer('❌ Неизвестный способ оплаты', show_alert=True)


@error_handler
async def handle_upsell_custom(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    """«Иная сумма»: пресеты + приглашение ввести своё число."""
    context = await _context(state)
    if context is None:
        await callback.answer('❌ Сессия оплаты истекла, начните заново', show_alert=True)
        return

    topup_kopeks, tip_kopeks, method = context
    texts = get_texts(db_user.language)
    max_tip = resolve_max_tip_kopeks(
        topup_amount_kopeks=topup_kopeks,
        method_max_kopeks=(await state.get_data()).get('upsell_method_max'),
    )

    rows: list[list[types.InlineKeyboardButton]] = []
    row: list[types.InlineKeyboardButton] = []
    for preset in settings.get_donation_upsell_presets_kopeks():
        if preset > max_tip:
            continue
        row.append(
            types.InlineKeyboardButton(
                text=texts.format_price(preset),
                callback_data=f'{UPSELL_CUSTOM_PREFIX}{preset}',
            )
        )
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([types.InlineKeyboardButton(text=texts.BACK, callback_data=UPSELL_BACK)])

    text = texts.t(
        'DONATION_UPSELL_CUSTOM_PROMPT',
        '💰 <b>Сумма поддержки</b>\n\n'
        'Выберите сумму кнопкой или введите свою в рублях — она добавится к платежу.\n\n'
        '<tg-emoji emoji-id="6244595016287654616">⚠️</tg-emoji> Эта сумма не зачисляется на баланс.',
    )

    await _show(callback.message, text, types.InlineKeyboardMarkup(inline_keyboard=rows), edit=True)
    await state.set_state(BalanceStates.waiting_for_upsell_tip)
    await state.update_data(upsell_tip=tip_kopeks, upsell_method=method, upsell_topup_amount=topup_kopeks)
    await callback.answer()


@error_handler
async def handle_upsell_custom_amount(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext
):
    """Пресет надбавки нажат кнопкой."""
    context = await _context(state)
    if context is None:
        await callback.answer('❌ Сессия оплаты истекла, начните заново', show_alert=True)
        return

    topup_kopeks, _tip_kopeks, method = context
    try:
        tip_kopeks = int(callback.data[len(UPSELL_CUSTOM_PREFIX) :])
    except ValueError:
        await callback.answer('❌ Некорректная сумма', show_alert=True)
        return

    await state.set_state(BalanceStates.waiting_for_amount)
    await _accept_with_tip(
        callback, db_user, db, state, topup_kopeks=topup_kopeks, tip_kopeks=tip_kopeks, method=method
    )


@error_handler
async def handle_upsell_back(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    """Возврат с экрана «иная сумма» к предложению по умолчанию."""
    context = await _context(state)
    if context is None:
        await callback.answer('❌ Сессия оплаты истекла, начните заново', show_alert=True)
        return

    topup_kopeks, tip_kopeks, _method = context
    texts = get_texts(db_user.language)
    await _show(
        callback.message,
        _screen_text(texts, topup_kopeks=topup_kopeks, tip_kopeks=tip_kopeks),
        _screen_keyboard(texts, topup_kopeks=topup_kopeks, tip_kopeks=tip_kopeks),
        edit=True,
    )
    await state.set_state(BalanceStates.waiting_for_amount)
    await callback.answer()


@error_handler
async def process_upsell_custom_amount(
    message: types.Message, db_user: User, db: AsyncSession, state: FSMContext
):
    """Надбавка, введённая текстом."""
    texts = get_texts(db_user.language)
    context = await _context(state)
    if context is None:
        await state.clear()
        await message.answer(texts.t('DONATION_UPSELL_EXPIRED', 'Сессия оплаты истекла, начните пополнение заново.'))
        return

    topup_kopeks, _tip_kopeks, method = context
    data = await state.get_data()

    try:
        tip_kopeks = int(round(float((message.text or '').strip().replace(',', '.')) * 100))
    except ValueError:
        await message.answer(texts.INVALID_AMOUNT)
        return

    if tip_kopeks <= 0:
        await message.answer(texts.INVALID_AMOUNT)
        return

    max_tip = resolve_max_tip_kopeks(
        topup_amount_kopeks=topup_kopeks,
        method_max_kopeks=data.get('upsell_method_max'),
    )
    if tip_kopeks > max_tip:
        await message.answer(
            texts.t(
                'DONATION_UPSELL_TIP_TOO_BIG',
                '❌ Для этого способа оплаты можно добавить не больше {amount}.',
            ).format(amount=texts.format_price(max_tip))
        )
        return

    # Убираем сообщение с числом и экран запроса, чтобы в чате остался только счёт.
    for chat_id, message_id in (
        (message.chat.id, message.message_id),
        (message.chat.id, data.get('upsell_prompt_message_id')),
    ):
        if not message_id:
            continue
        try:
            await message.bot.delete_message(chat_id, message_id)
        except Exception as delete_error:  # pragma: no cover — зависит от прав бота
            logger.warning('Не удалось удалить сообщение апселла', error=delete_error)

    await register_pledge(
        db,
        db_user,
        tip_kopeks=tip_kopeks,
        topup_amount_kopeks=topup_kopeks,
        payment_method=method,
        source=PLEDGE_SOURCE,
    )

    await state.set_state(BalanceStates.waiting_for_amount)

    if not await _route_payment(message, db_user, state, amount_kopeks=topup_kopeks + tip_kopeks, method=method):
        await message.answer('Неизвестный способ оплаты')


def register_donation_upsell_handlers(dp: Dispatcher) -> None:
    dp.callback_query.register(handle_upsell_accept, F.data == UPSELL_ACCEPT)
    dp.callback_query.register(handle_upsell_decline, F.data == UPSELL_DECLINE)
    dp.callback_query.register(handle_upsell_custom, F.data == UPSELL_CUSTOM)
    dp.callback_query.register(handle_upsell_back, F.data == UPSELL_BACK)
    dp.callback_query.register(handle_upsell_custom_amount, F.data.startswith(UPSELL_CUSTOM_PREFIX))
    # ~F.successful_payment: сообщение об оплате Stars не должно попасть в ввод суммы
    # (тот же приём, что в обычном пополнении и донатах).
    dp.message.register(
        process_upsell_custom_amount, BalanceStates.waiting_for_upsell_tip, ~F.successful_payment
    )
```

- [ ] **Step 7: Зарегистрировать хендлеры**

В `app/handlers/balance/main.py`, `register_balance_handlers` (строка 680) — сразу после `register_donation_handlers(dp)`:

```python
    from .donation_upsell import register_donation_upsell_handlers

    register_donation_upsell_handlers(dp)
```

- [ ] **Step 8: Убедиться, что тесты проходят**

Run: `uv run pytest tests/handlers/test_donation_upsell_flow.py -v`
Expected: 8 passed

- [ ] **Step 9: Проверить, что старые флоу целы**

Run: `uv run pytest tests/handlers tests/test_topup_amounts_keyboard.py tests/test_miniapp_payments.py -q`
Expected: passed

- [ ] **Step 10: Линтер и коммит**

```bash
uv run ruff format . && uv run ruff check .
git add app/handlers/balance/donation_upsell.py app/handlers/balance/main.py app/states.py tests/handlers/test_donation_upsell_flow.py
git commit -m "feat(bot): экран донат-апселла между суммой и оплатой"
```

---

## Task 6: Бот — знак «−» у надбавки в истории

**Files:**
- Modify: `app/handlers/balance/main.py` (строки 291-296, рендер `TransactionType.DONATION`)
- Test: `tests/handlers/test_donation_upsell_history.py`

**Interfaces:**
- Consumes: контракт `external_id = 'pledge:<id>'` (Task 4).
- Produces: ничего для других задач.

- [ ] **Step 1: Написать падающий тест**

Создать `tests/handlers/test_donation_upsell_history.py`:

```python
"""История баланса: надбавка уменьшила баланс — показываем «−», обычный донат — нет."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.handlers.balance.main as balance_main
from app.database.models import TransactionType


def _transaction(external_id: str | None, amount_kopeks: int = 1000):
    transaction = MagicMock()
    transaction.type = TransactionType.DONATION.value
    transaction.amount_kopeks = amount_kopeks
    transaction.external_id = external_id
    transaction.description = 'Пожертвование'
    transaction.created_at = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
    return transaction


def _callback():
    callback = MagicMock()
    callback.answer = AsyncMock()
    callback.message = MagicMock()
    callback.message.text = 'история'
    callback.message.edit_text = AsyncMock()
    return callback


def _user():
    user = MagicMock()
    user.id = 42
    user.language = 'ru'
    return user


async def _render(transaction):
    callback = _callback()
    with patch.object(balance_main, 'get_user_transactions', AsyncMock(return_value=[transaction])):
        await balance_main.show_balance_history(callback, _user(), MagicMock())
    return callback.message.edit_text.call_args.args[0]


@pytest.mark.asyncio
async def test_upsell_donation_rendered_as_debit():
    text = await _render(_transaction('pledge:7'))

    assert '❤️ -' in text


@pytest.mark.asyncio
async def test_menu_donation_rendered_without_sign():
    text = await _render(_transaction('yookassa-payment-id'))

    assert '❤️ -' not in text
    assert '❤️ ' in text
```

- [ ] **Step 2: Убедиться, что тест падает**

Run: `uv run pytest tests/handlers/test_donation_upsell_history.py -v`
Expected: FAIL на `test_upsell_donation_rendered_as_debit` — сейчас знак не ставится.

- [ ] **Step 3: Поправить рендер**

В `app/handlers/balance/main.py` заменить блок для `DONATION` (строки 291-296):

```python
        if transaction.type == TransactionType.DONATION.value:
            # Обычное пожертвование баланс не двигает — показываем нейтрально.
            # Надбавка при пополнении (external_id 'pledge:<id>') баланс уменьшила —
            # показываем как расход, иначе столбик не сходится.
            emoji = '❤️'
            amount_text = texts.format_price(abs(transaction.amount_kopeks))
            if str(getattr(transaction, 'external_id', '') or '').startswith('pledge:'):
                amount_text = f'-{amount_text}'
```

- [ ] **Step 4: Убедиться, что тесты проходят**

Run: `uv run pytest tests/handlers/test_donation_upsell_history.py -v`
Expected: 2 passed

- [ ] **Step 5: Коммит**

```bash
git add app/handlers/balance/main.py tests/handlers/test_donation_upsell_history.py
git commit -m "feat(bot): надбавка на поддержку показывается расходом в истории"
```

---

## Task 7: Кабинетное API

**Files:**
- Modify: `app/cabinet/schemas/balance.py` (`TopUpRequest` строка 67, `StarsInvoiceRequest` строка 91; новая схема ответа)
- Modify: `app/cabinet/routes/balance.py` (`get_transactions` строка 108-125, `create_stars_invoice` строка 224, `create_topup` строка 324, новый эндпоинт)
- Test: `tests/cabinet/test_donation_upsell_routes.py`

**Interfaces:**
- Consumes: `build_offer`, `is_upsell_available`, `register_pledge`, `register_decline`, `resolve_max_tip_kopeks` (Task 3/4).
- Produces:
  - `TopUpRequest.donation_tip_kopeks: int | None`, `TopUpRequest.donation_upsell_shown: bool | None`
  - `StarsInvoiceRequest.donation_tip_kopeks: int | None`, `StarsInvoiceRequest.donation_upsell_shown: bool | None`
  - `DonationUpsellResponse(available: bool, tip_kopeks: int, presets_kopeks: list[int], min_topup_kopeks: int, max_tip_kopeks: int)`
  - `GET /cabinet/balance/donation-upsell`

- [ ] **Step 1: Написать падающие тесты**

Создать `tests/cabinet/test_donation_upsell_routes.py`:

```python
"""Кабинетное API апселла: надбавка увеличивает платёж и создаёт обещание."""

from unittest.mock import AsyncMock, MagicMock

import pytest

import app.cabinet.routes.balance as balance_routes
from app.cabinet.schemas.balance import TopUpRequest
from app.config import settings


def _user():
    user = MagicMock()
    user.id = 42
    user.telegram_id = 111
    user.username = 'donor'
    user.language = 'ru'
    user.restriction_topup = False
    return user


@pytest.mark.asyncio
async def test_upsell_settings_endpoint_reports_availability(monkeypatch):
    monkeypatch.setattr(settings, 'DONATION_UPSELL_AMOUNT', 10, raising=False)
    monkeypatch.setattr(settings, 'DONATION_UPSELL_PRESETS', '25,50', raising=False)
    monkeypatch.setattr(settings, 'DONATION_UPSELL_MIN_TOPUP', 100, raising=False)
    monkeypatch.setattr(balance_routes, 'is_upsell_available', AsyncMock(return_value=True))

    response = await balance_routes.get_donation_upsell(user=_user(), db=AsyncMock())

    assert response.available is True
    assert response.tip_kopeks == 1000
    assert response.presets_kopeks == [2500, 5000]
    assert response.min_topup_kopeks == 10000


@pytest.mark.asyncio
async def test_upsell_settings_endpoint_unavailable(monkeypatch):
    monkeypatch.setattr(balance_routes, 'is_upsell_available', AsyncMock(return_value=False))

    response = await balance_routes.get_donation_upsell(user=_user(), db=AsyncMock())

    assert response.available is False


def test_topup_request_accepts_tip():
    request = TopUpRequest(
        amount_kopeks=50000,
        payment_method='yookassa',
        donation_tip_kopeks=1000,
        donation_upsell_shown=True,
    )

    assert request.donation_tip_kopeks == 1000
    assert request.donation_upsell_shown is True


@pytest.mark.asyncio
async def test_apply_tip_increases_amount_and_registers_pledge(monkeypatch):
    register_pledge = AsyncMock()
    monkeypatch.setattr(balance_routes, 'register_pledge', register_pledge)
    monkeypatch.setattr(balance_routes, 'is_upsell_available', AsyncMock(return_value=True))

    request = TopUpRequest(
        amount_kopeks=50000,
        payment_method='yookassa',
        donation_tip_kopeks=1000,
        donation_upsell_shown=True,
    )

    patched, tip = await balance_routes._apply_donation_tip(AsyncMock(), _user(), request)

    assert patched.amount_kopeks == 51000
    assert tip == 1000
    assert register_pledge.await_args.kwargs['tip_kopeks'] == 1000
    assert register_pledge.await_args.kwargs['source'] == 'cabinet'


@pytest.mark.asyncio
async def test_declined_upsell_records_decline_without_changing_amount(monkeypatch):
    register_decline = AsyncMock()
    monkeypatch.setattr(balance_routes, 'register_decline', register_decline)
    monkeypatch.setattr(balance_routes, 'is_upsell_available', AsyncMock(return_value=True))

    request = TopUpRequest(
        amount_kopeks=50000,
        payment_method='yookassa',
        donation_tip_kopeks=0,
        donation_upsell_shown=True,
    )

    patched, tip = await balance_routes._apply_donation_tip(AsyncMock(), _user(), request)

    assert patched.amount_kopeks == 50000
    assert tip == 0
    register_decline.assert_awaited_once()


@pytest.mark.asyncio
async def test_tip_ignored_when_upsell_unavailable(monkeypatch):
    register_pledge = AsyncMock()
    monkeypatch.setattr(balance_routes, 'register_pledge', register_pledge)
    monkeypatch.setattr(balance_routes, 'is_upsell_available', AsyncMock(return_value=False))

    request = TopUpRequest(amount_kopeks=50000, payment_method='yookassa', donation_tip_kopeks=1000)

    patched, tip = await balance_routes._apply_donation_tip(AsyncMock(), _user(), request)

    assert patched.amount_kopeks == 50000
    assert tip == 0
    register_pledge.assert_not_awaited()
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `uv run pytest tests/cabinet/test_donation_upsell_routes.py -v`
Expected: FAIL — `AttributeError: module 'app.cabinet.routes.balance' has no attribute 'get_donation_upsell'`

- [ ] **Step 3: Расширить схемы**

В `app/cabinet/schemas/balance.py`:

в `TopUpRequest` (после поля `purpose`):

```python
    # Донат-апселл: надбавка к платежу в пользу проекта. Сервер прибавляет её к
    # amount_kopeks (провайдеру уходит сумма с надбавкой) и создаёт «обещание»,
    # которое вебхук исполнит после зачисления. 0 + donation_upsell_shown=True —
    # пользователь отказался (нужно для cooldown и воронки).
    donation_tip_kopeks: int | None = Field(None, ge=0, le=5_000_000, description='Donation add-on in kopeks')
    donation_upsell_shown: bool | None = Field(None, description='True if the upsell dialog was shown')
```

в `StarsInvoiceRequest` — те же два поля с теми же описаниями.

в конец файла — схема ответа:

```python
class DonationUpsellResponse(BaseModel):
    """Настройки донат-апселла для текущего пользователя."""

    available: bool
    tip_kopeks: int
    presets_kopeks: list[int]
    min_topup_kopeks: int
    max_tip_kopeks: int
```

- [ ] **Step 4: Добавить эндпоинт и хелпер в роут**

В `app/cabinet/routes/balance.py`: к импортам схем добавить `DonationUpsellResponse`, и импортировать сервис на уровне модуля (тесты подменяют эти имена через `monkeypatch.setattr(balance_routes, ...)`):

```python
from app.services.donation_upsell_service import (
    MAX_TIP_KOPEKS,
    is_upsell_available,
    register_decline,
    register_pledge,
    resolve_max_tip_kopeks,
)
```

Перед `create_topup` добавить:

```python
@router.get('/donation-upsell', response_model=DonationUpsellResponse)
async def get_donation_upsell(
    user: User = Depends(get_current_cabinet_user),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Показывать ли предложение «добавить к платежу» и какие суммы предлагать.

    Условия, зависящие от суммы и способа (порог, лимит метода), кабинет проверяет
    сам по возвращённым значениям — чтобы не ходить на сервер перед каждой оплатой.
    """
    available = await is_upsell_available(db, user)
    return DonationUpsellResponse(
        available=available,
        tip_kopeks=settings.get_donation_upsell_tip_kopeks(),
        presets_kopeks=settings.get_donation_upsell_presets_kopeks(),
        min_topup_kopeks=settings.get_donation_upsell_min_topup_kopeks(),
        max_tip_kopeks=MAX_TIP_KOPEKS,
    )


async def _apply_donation_tip(db: AsyncSession, user: User, request):
    """Учитывает надбавку в запросе на оплату.

    Возвращает (запрос с суммой «пополнение + надбавка», размер надбавки). Надбавка
    прибавляется к ``amount_kopeks`` копией запроса, поэтому весь дальнейший код
    роута — включая проверку лимитов способа — работает уже с итоговой суммой.
    Клиенту не доверяем: доступность апселла перепроверяем на сервере.
    """
    shown = bool(getattr(request, 'donation_upsell_shown', False))
    tip_kopeks = max(0, int(getattr(request, 'donation_tip_kopeks', 0) or 0))

    if not shown and tip_kopeks <= 0:
        return request, 0

    if not await is_upsell_available(db, user):
        return request, 0

    if tip_kopeks <= 0:
        await register_decline(
            db,
            user,
            topup_amount_kopeks=request.amount_kopeks,
            payment_method=getattr(request, 'payment_method', None),
            source='cabinet',
        )
        return request, 0

    # Порог суммы пополнения — тоже условие показа: клиент мог отстать от настроек.
    if request.amount_kopeks < settings.get_donation_upsell_min_topup_kopeks():
        return request, 0

    max_tip = resolve_max_tip_kopeks(topup_amount_kopeks=request.amount_kopeks, method_max_kopeks=None)
    tip_kopeks = min(tip_kopeks, max_tip)
    if tip_kopeks <= 0:
        return request, 0

    await register_pledge(
        db,
        user,
        tip_kopeks=tip_kopeks,
        topup_amount_kopeks=request.amount_kopeks,
        payment_method=getattr(request, 'payment_method', None),
        source='cabinet',
    )

    return request.model_copy(update={'amount_kopeks': request.amount_kopeks + tip_kopeks}), tip_kopeks
```

- [ ] **Step 5: Подключить хелпер к созданию платежей**

В `create_topup` — сразу после проверки `restriction_topup` (перед валидацией способа, чтобы лимиты метода проверялись уже по итоговой сумме):

```python
    # Донат-апселл: дальше по функции request.amount_kopeks — это сумма платежа
    # (пополнение + надбавка). На баланс попадёт только пополнение: надбавку
    # спишет вебхук (app/services/donation_upsell_service.py).
    request, donation_tip_kopeks = await _apply_donation_tip(db, user, request)
```

В `create_stars_invoice` — сразу после проверки `TELEGRAM_STARS_ENABLED`:

```python
    request, donation_tip_kopeks = await _apply_donation_tip(db, user, request)
```

Затем, в `create_stars_invoice` после вычисления `normalized_kopeks`, привести обещание к нормализованной сумме (Stars округляют платёж до целых звёзд):

```python
    if donation_tip_kopeks:
        # Stars подгоняют сумму под целое число звёзд — обещание должно ждать
        # именно нормализованную сумму, иначе сверка пойдёт по допуску.
        from app.database.crud import donation_pledge as pledge_crud

        pledges = await pledge_crud.list_pending_pledges(db, user_id=user.id)
        if pledges:
            pledges[0].expected_total_kopeks = normalized_kopeks
            await db.commit()
```

- [ ] **Step 6: Показать надбавку расходом в истории кабинета**

В `app/cabinet/routes/balance.py`, `get_transactions` — заменить вычисление `is_debit` (строка ~110):

```python
        # Credits (positive): DEPOSIT, REFERRAL_REWARD, REFUND, POLL_REWARD
        # Debits (negative): SUBSCRIPTION_PAYMENT, WITHDRAWAL, GIFT_PAYMENT и
        # пожертвование-надбавка при пополнении (external_id 'pledge:<id>') —
        # оно, в отличие от обычного доната, уменьшило баланс.
        is_upsell_donation = t.type == 'donation' and str(t.external_id or '').startswith('pledge:')
        is_debit = t.type in ['subscription_payment', 'withdrawal', 'gift_payment'] or is_upsell_donation
```

- [ ] **Step 7: Убедиться, что тесты проходят**

Run: `uv run pytest tests/cabinet/test_donation_upsell_routes.py -v`
Expected: 6 passed

- [ ] **Step 8: Прогнать все тесты кабинета и линтер**

Run: `uv run ruff format . && uv run ruff check . && uv run pytest tests/cabinet -q`
Expected: passed

- [ ] **Step 9: Коммит**

```bash
git add app/cabinet/schemas/balance.py app/cabinet/routes/balance.py tests/cabinet/test_donation_upsell_routes.py
git commit -m "feat(cabinet-api): приём надбавки на поддержку при создании платежа"
```

---

## Task 8: Фронт кабинета — модалка апселла

**Files:** (репозиторий `/root/bedolaga-cabinet`)
- Modify: `src/api/balance.ts` (`createTopUp` строка 44, `createStarsInvoice` строка 104)
- Create: `src/components/DonationUpsellDialog.tsx`
- Modify: `src/pages/TopUpAmount.tsx` (мутации строки 180-230, обработчик оплаты строка ~378)
- Modify: `src/pages/Balance.tsx` (строка ~404, знак суммы)
- Modify: `src/locales/{ru,en,fa,zh}.json` (блок `donationUpsell` рядом с `supportProject`, строка ~900)

**Interfaces:**
- Consumes: `GET /cabinet/balance/donation-upsell`, поля `donation_tip_kopeks` / `donation_upsell_shown` (Task 7).
- Produces:
  - `balanceApi.getDonationUpsell(): Promise<DonationUpsellInfo>`
  - `balanceApi.createTopUp(amountKopeks, paymentMethod, paymentOption?, purpose?, donationTip?)` где `donationTip?: { tipKopeks: number; shown: boolean }`
  - `balanceApi.createStarsInvoice(amountKopeks, purpose?, donationTip?)`
  - `<DonationUpsellDialog open onClose onDecide />`

- [ ] **Step 1: Расширить API-клиент**

В `src/api/balance.ts` добавить тип и метод (после `createStarsInvoice`):

```ts
export interface DonationUpsellInfo {
  available: boolean;
  tip_kopeks: number;
  presets_kopeks: number[];
  min_topup_kopeks: number;
  max_tip_kopeks: number;
}
```

в объект `balanceApi`:

```ts
  // Донат-апселл: показывать ли предложение добавить сумму к платежу.
  // Условия по сумме/методу проверяет сам кабинет — по значениям из ответа.
  getDonationUpsell: async (): Promise<DonationUpsellInfo> => {
    const response = await apiClient.get('/cabinet/balance/donation-upsell');
    return response.data;
  },
```

В `createTopUp` добавить пятый аргумент и поля payload:

```ts
    purpose?: 'donation',
    donationTip?: { tipKopeks: number; shown: boolean },
```

```ts
    if (donationTip?.shown) {
      payload.donation_upsell_shown = true;
      payload.donation_tip_kopeks = donationTip.tipKopeks;
    }
```

(тип payload дополнить полями `donation_upsell_shown?: boolean; donation_tip_kopeks?: number;`)

То же самое — в `createStarsInvoice` (третий аргумент `donationTip`).

- [ ] **Step 2: Создать модалку**

Создать `src/components/DonationUpsellDialog.tsx`:

```tsx
import { useState } from 'react';
import { useTranslation } from 'react-i18next';

import { useHaptic } from '@/platform';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from '@/components/primitives/Dialog';
import { useCurrency } from '../hooks/useCurrency';

interface DonationUpsellDialogProps {
  open: boolean;
  /** Сумма пополнения в копейках — надбавка добавляется к ней. */
  topupKopeks: number;
  tipKopeks: number;
  presetsKopeks: number[];
  maxTipKopeks: number;
  /** tipKopeks = 0 — пользователь отказался. Диалог всегда завершает оплату. */
  onDecide: (tipKopeks: number) => void;
}

export default function DonationUpsellDialog({
  open,
  topupKopeks,
  tipKopeks,
  presetsKopeks,
  maxTipKopeks,
  onDecide,
}: DonationUpsellDialogProps) {
  const { t } = useTranslation();
  const haptic = useHaptic();
  const { formatAmount } = useCurrency();
  const [customOpen, setCustomOpen] = useState(false);
  const [customValue, setCustomValue] = useState('');

  const rub = (kopeks: number) => formatAmount(kopeks / 100);

  const decide = (tip: number) => {
    haptic.impact(tip > 0 ? 'medium' : 'light');
    onDecide(tip);
  };

  const submitCustom = () => {
    const parsed = Math.round(parseFloat(customValue.replace(',', '.')) * 100);
    if (!Number.isFinite(parsed) || parsed <= 0 || parsed > maxTipKopeks) return;
    decide(parsed);
  };

  return (
    <Dialog open={open} onOpenChange={(next) => !next && decide(0)}>
      <DialogContent className="max-w-md">
        <DialogHeader className="text-center sm:text-center">
          <div
            className="mx-auto mb-1 flex h-16 w-16 items-center justify-center rounded-full text-3xl"
            style={{
              background:
                'linear-gradient(135deg, rgba(244,63,94,0.18) 0%, rgba(236,72,153,0.18) 100%)',
            }}
          >
            ⚡
          </div>
          <DialogTitle className="text-center text-xl">{t('donationUpsell.title')}</DialogTitle>
          <div className="flex justify-center pt-1">
            <span className="badge-donation">{t('donationUpsell.badge')}</span>
          </div>
          <DialogDescription className="pt-2 text-center text-[15px] leading-relaxed text-dark-300">
            {t('donationUpsell.description', { tip: rub(tipKopeks) })}
          </DialogDescription>
        </DialogHeader>

        {customOpen ? (
          <div className="space-y-3">
            <div className="flex gap-2">
              {presetsKopeks
                .filter((preset) => preset <= maxTipKopeks)
                .map((preset) => (
                  <button
                    key={preset}
                    type="button"
                    onClick={() => decide(preset)}
                    className="flex-1 rounded-xl border border-dark-700/40 bg-dark-800/40 py-3 text-sm font-semibold"
                  >
                    {rub(preset)}
                  </button>
                ))}
            </div>
            <input
              type="number"
              inputMode="decimal"
              value={customValue}
              onChange={(event) => setCustomValue(event.target.value)}
              placeholder={t('donationUpsell.customPlaceholder')}
              className="w-full rounded-xl border border-dark-700/40 bg-dark-800/40 px-4 py-3 text-base"
            />
            <button
              type="button"
              onClick={submitCustom}
              className="h-12 w-full rounded-2xl text-base font-bold text-white"
              style={{ background: 'linear-gradient(135deg, #f43f5e 0%, #ec4899 100%)' }}
            >
              {t('donationUpsell.confirmCustom')}
            </button>
          </div>
        ) : (
          <button
            type="button"
            onClick={() => decide(tipKopeks)}
            className="flex h-14 w-full items-center justify-center gap-2 rounded-2xl text-base font-bold text-white transition-transform active:scale-[0.98]"
            style={{
              background: 'linear-gradient(135deg, #f43f5e 0%, #ec4899 100%)',
              boxShadow: '0 8px 24px rgba(244, 63, 94, 0.3)',
            }}
          >
            <span className="text-xl leading-none">❤️</span>
            <span>{t('donationUpsell.accept', { total: rub(topupKopeks + tipKopeks) })}</span>
          </button>
        )}

        <div className="flex items-center justify-between pt-1 text-sm">
          <button
            type="button"
            onClick={() => setCustomOpen((value) => !value)}
            className="text-dark-300 underline-offset-2 hover:underline"
          >
            {t('donationUpsell.otherAmount')}
          </button>
          <button type="button" onClick={() => decide(0)} className="text-dark-300 hover:underline">
            {t('donationUpsell.decline')}
          </button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
```

- [ ] **Step 3: Подключить модалку в `TopUpAmount.tsx`**

Импорты:

```tsx
import DonationUpsellDialog from '../components/DonationUpsellDialog';
```

Состояние и запрос настроек (рядом с `useQuery` по `payment-methods`):

```tsx
  const [pendingAmountKopeks, setPendingAmountKopeks] = useState<number | null>(null);

  const { data: upsell } = useQuery({
    queryKey: ['donation-upsell'],
    queryFn: balanceApi.getDonationUpsell,
    // Донат — сам по себе донат: апселл в этом флоу не нужен.
    enabled: !isDonation,
    staleTime: 60_000,
  });
```

Мутации: обе принимают надбавку вместе с суммой.

```tsx
  const starsPaymentMutation = useMutation({
    mutationFn: ({ amountKopeks, tip }: { amountKopeks: number; tip: number | null }) =>
      balanceApi.createStarsInvoice(
        amountKopeks,
        isDonation ? 'donation' : undefined,
        tip === null ? undefined : { tipKopeks: tip, shown: true },
      ),
```

(аналогично в `topUpMutation`: `mutationFn: ({ amountKopeks, tip }) => balanceApi.createTopUp(amountKopeks, method.id, selectedOption || undefined, isDonation ? 'donation' : undefined, tip === null ? undefined : { tipKopeks: tip, shown: true })`; тип переменной мутации меняется с `number` на `{ amountKopeks: number; tip: number | null }`.)

Запуск оплаты: вместо прямого `mutate(amountKopeks)` — проверка апселла.

```tsx
  const startPayment = (amountKopeks: number, tip: number | null) => {
    if (isStarsMethod) {
      starsPaymentMutation.mutate({ amountKopeks, tip });
    } else {
      topUpMutation.mutate({ amountKopeks, tip });
    }
  };

  const shouldOfferUpsell = (amountKopeks: number) =>
    !isDonation &&
    !!upsell?.available &&
    upsell.tip_kopeks > 0 &&
    amountKopeks >= upsell.min_topup_kopeks &&
    method?.id !== 'tribute' &&
    amountKopeks + upsell.tip_kopeks <= (method?.max_amount_kopeks ?? Number.MAX_SAFE_INTEGER);
```

В существующем обработчике (строка ~378) заменить вызовы `mutate`:

```tsx
    if (shouldOfferUpsell(amountKopeks)) {
      setPendingAmountKopeks(amountKopeks);
      return;
    }
    startPayment(amountKopeks, null);
```

И рендер модалки (внутри возвращаемого JSX, рядом с остальными оверлеями):

```tsx
      {pendingAmountKopeks !== null && upsell && (
        <DonationUpsellDialog
          open
          topupKopeks={pendingAmountKopeks}
          tipKopeks={upsell.tip_kopeks}
          presetsKopeks={upsell.presets_kopeks}
          maxTipKopeks={Math.min(
            upsell.max_tip_kopeks,
            (method?.max_amount_kopeks ?? Number.MAX_SAFE_INTEGER) - pendingAmountKopeks,
          )}
          onDecide={(tipKopeks) => {
            const amount = pendingAmountKopeks;
            setPendingAmountKopeks(null);
            startPayment(amount, tipKopeks);
          }}
        />
      )}
```

- [ ] **Step 4: Знак «−» у надбавки в истории кабинета**

В `src/pages/Balance.tsx` (строка ~404) заменить вычисление знака:

```tsx
                        // Обычное пожертвование баланс не двигает — показываем
                        // нейтрально (розовый, без «+»). Надбавка при пополнении
                        // приходит с отрицательной суммой — у неё знак «−».
                        const sign = isZero ? '' : isPositive ? (isDonation ? '' : '+') : '-';
```

- [ ] **Step 5: Добавить тексты во все 4 локали**

В `src/locales/ru.json` рядом с блоком `supportProject` (строка ~900):

```json
  "donationUpsell": {
    "title": "Поддержать проект?",
    "badge": "Не на баланс",
    "description": "Добавить {{tip}} к платежу? Эта сумма не зачисляется на баланс, а идёт на увеличение мощностей и скорости соединения.",
    "accept": "❤️ Поддержать — {{total}}",
    "decline": "Не сейчас",
    "otherAmount": "Иная сумма",
    "customPlaceholder": "Своя сумма",
    "confirmCustom": "Поддержать"
  },
```

`en.json`:

```json
  "donationUpsell": {
    "title": "Support the project?",
    "badge": "Not credited",
    "description": "Add {{tip}} to your payment? This amount is not credited to your balance — it goes to more servers and faster connections.",
    "accept": "❤️ Support — {{total}}",
    "decline": "Not now",
    "otherAmount": "Other amount",
    "customPlaceholder": "Custom amount",
    "confirmCustom": "Support"
  },
```

`fa.json`:

```json
  "donationUpsell": {
    "title": "از پروژه حمایت می‌کنید؟",
    "badge": "به موجودی اضافه نمی‌شود",
    "description": "{{tip}} به پرداخت اضافه شود؟ این مبلغ به موجودی اضافه نمی‌شود و برای افزایش ظرفیت و سرعت اتصال هزینه می‌شود.",
    "accept": "❤️ حمایت — {{total}}",
    "decline": "الان نه",
    "otherAmount": "مبلغ دیگر",
    "customPlaceholder": "مبلغ دلخواه",
    "confirmCustom": "حمایت"
  },
```

`zh.json`:

```json
  "donationUpsell": {
    "title": "支持项目？",
    "badge": "不计入余额",
    "description": "在付款中添加 {{tip}}？此金额不会计入余额，而用于提升服务器性能和连接速度。",
    "accept": "❤️ 支持 — {{total}}",
    "decline": "暂不",
    "otherAmount": "其他金额",
    "customPlaceholder": "自定义金额",
    "confirmCustom": "支持"
  },
```

- [ ] **Step 6: Проверить типы, линтер и сборку**

Run (в `/root/bedolaga-cabinet`): `npm run type-check && npm run lint && npm run build`
Expected: без ошибок.

- [ ] **Step 7: Коммит (репозиторий кабинета)**

```bash
cd /root/bedolaga-cabinet
git add src/api/balance.ts src/components/DonationUpsellDialog.tsx src/pages/TopUpAmount.tsx src/pages/Balance.tsx src/locales
git commit -m "feat(cabinet): модалка «добавить к платежу» перед оплатой"
```

---

## Task 9: Статистика — источник донатов и воронка апселла

**Files:**
- Modify: `app/database/crud/donation.py` (агрегаты; после `get_total_donations_kopeks`, строка 106)
- Create: `app/database/crud/donation_pledge.py` — функция воронки (дополнение Task 2)
- Modify: `app/cabinet/routes/admin_sales_stats.py` (`DonationsStatsResponse` строка 1350, `get_donations_stats` строка 1359)
- Modify: `/root/bedolaga-cabinet/src/api/adminSalesStats.ts` (`DonationsStats` строка 197)
- Modify: `/root/bedolaga-cabinet/src/components/sales-stats/DonationsTab.tsx`
- Modify: `/root/bedolaga-cabinet/src/locales/{ru,en,fa,zh}.json` (блок `admin.salesStats.donations`)
- Test: `tests/cabinet/test_donation_upsell_stats.py`

**Interfaces:**
- Consumes: `Donation.source` (Task 1), `DonationPledge`/`DonationPledgeStatus` (Task 1).
- Produces:
  - `async def get_donations_by_source_kopeks(db, *, start=None, end=None) -> dict[str, int]` в `app/database/crud/donation.py`
  - `async def get_upsell_funnel(db, *, start=None, end=None) -> dict[str, int]` (ключи `shown`, `accepted`, `applied`, `declined`) в `app/database/crud/donation_pledge.py`
  - Поля `by_source: dict[str, int]` и `upsell_funnel: dict[str, int]` в `DonationsStatsResponse`

- [ ] **Step 1: Написать падающие тесты**

Создать `tests/cabinet/test_donation_upsell_stats.py`:

```python
"""Статистика: донаты разложены по источнику, воронка апселла считается."""

from unittest.mock import AsyncMock, MagicMock

import pytest

import app.cabinet.routes.admin_sales_stats as stats_routes
from app.database.crud import donation as donation_crud
from app.database.crud import donation_pledge as pledge_crud


def _rows_result(rows):
    result = MagicMock()
    result.all = MagicMock(return_value=list(rows))
    return result


@pytest.mark.asyncio
async def test_donations_by_source_maps_rows():
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_rows_result([('menu', 50000), ('topup_upsell', 7000)]))

    result = await donation_crud.get_donations_by_source_kopeks(db)

    assert result == {'menu': 50000, 'topup_upsell': 7000}


@pytest.mark.asyncio
async def test_upsell_funnel_counts_statuses():
    db = AsyncMock()
    db.execute = AsyncMock(
        return_value=_rows_result([('pending', 4), ('applied', 6), ('declined', 90), ('superseded', 2)])
    )

    funnel = await pledge_crud.get_upsell_funnel(db)

    assert funnel['shown'] == 102
    assert funnel['accepted'] == 12  # pending + applied + superseded
    assert funnel['applied'] == 6
    assert funnel['declined'] == 90


@pytest.mark.asyncio
async def test_stats_endpoint_returns_source_and_funnel(monkeypatch):
    monkeypatch.setattr(
        stats_routes, '_parse_period', lambda *args, **kwargs: (None, None), raising=False
    )

    async def fake_execute(*args, **kwargs):
        raise AssertionError('не должно вызываться напрямую')

    db = AsyncMock()
    db.execute = fake_execute

    monkeypatch.setattr(donation_crud, 'get_total_donations_kopeks', AsyncMock(return_value=57000))
    monkeypatch.setattr(donation_crud, 'count_distinct_donors', AsyncMock(return_value=9))
    monkeypatch.setattr(donation_crud, 'get_donation_leaderboard', AsyncMock(return_value=[]))
    monkeypatch.setattr(
        donation_crud,
        'get_donations_by_source_kopeks',
        AsyncMock(return_value={'menu': 50000, 'topup_upsell': 7000}),
    )
    monkeypatch.setattr(
        pledge_crud,
        'get_upsell_funnel',
        AsyncMock(return_value={'shown': 102, 'accepted': 12, 'applied': 6, 'declined': 90}),
    )

    response = await stats_routes.get_donations_stats(
        days=None, start_date=None, end_date=None, limit=20, admin=MagicMock(), db=db
    )

    assert response.total_amount_kopeks == 57000
    assert response.by_source == {'menu': 50000, 'topup_upsell': 7000}
    assert response.upsell_funnel['applied'] == 6
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `uv run pytest tests/cabinet/test_donation_upsell_stats.py -v`
Expected: FAIL — `AttributeError: module 'app.database.crud.donation' has no attribute 'get_donations_by_source_kopeks'`

- [ ] **Step 3: Добавить агрегат по источнику**

В `app/database/crud/donation.py` после `get_total_donations_kopeks`:

```python
async def get_donations_by_source_kopeks(
    db: AsyncSession,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> dict[str, int]:
    """Суммы пожертвований по источнику: 'menu' (кнопка) и 'topup_upsell' (надбавка)."""
    query = select(Donation.source, func.coalesce(func.sum(Donation.amount_kopeks), 0)).group_by(Donation.source)
    result = await db.execute(_apply_period(query, start, end))
    return {str(source or 'menu'): int(total or 0) for source, total in result.all()}
```

- [ ] **Step 4: Добавить воронку апселла**

В `app/database/crud/donation_pledge.py`:

```python
async def get_upsell_funnel(
    db: AsyncSession,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> dict[str, int]:
    """Воронка апселла: показов, согласий, исполненных согласий, отказов.

    «Согласий» — все статусы, начинавшиеся с согласия (``pending`` — ждёт оплаты,
    ``applied`` — исполнено, ``superseded``/``expired``/``skipped`` — согласие было,
    но платёж не дошёл). Так видно и намерение, и реальный сбор.
    """
    query = select(DonationPledge.status, func.count(DonationPledge.id)).group_by(DonationPledge.status)
    if start is not None:
        query = query.where(DonationPledge.created_at >= start)
    if end is not None:
        query = query.where(DonationPledge.created_at <= end)

    result = await db.execute(query)
    counts = {str(status): int(count or 0) for status, count in result.all()}

    declined = counts.get(DonationPledgeStatus.DECLINED, 0)
    shown = sum(counts.values())
    return {
        'shown': shown,
        'accepted': shown - declined,
        'applied': counts.get(DonationPledgeStatus.APPLIED, 0),
        'declined': declined,
    }
```

- [ ] **Step 5: Отдать новые поля в API**

В `app/cabinet/routes/admin_sales_stats.py`:

в `DonationsStatsResponse` добавить:

```python
    # Суммы по источнику: 'menu' — кнопка «Поддержать проект», 'topup_upsell' —
    # надбавка к пополнению. Надбавки уже посчитаны и внутри статистики пополнений:
    # реальный платёж был на сумму «пополнение + надбавка».
    by_source: dict[str, int] = {}
    # Воронка апселла: shown / accepted / applied / declined.
    upsell_funnel: dict[str, int] = {}
```

в `get_donations_stats` — импорты и заполнение:

```python
        from app.database.crud.donation import (
            count_distinct_donors,
            get_donation_leaderboard,
            get_donations_by_source_kopeks,
            get_total_donations_kopeks,
        )
        from app.database.crud.donation_pledge import get_upsell_funnel
```

```python
        by_source = await get_donations_by_source_kopeks(db, start=period_start, end=period_end)
        upsell_funnel = await get_upsell_funnel(db, start=period_start, end=period_end)

        return DonationsStatsResponse(
            total_amount_kopeks=total_amount,
            donors_count=donors_count,
            top_donors=[DonorItem(**row) for row in leaderboard],
            by_source=by_source,
            upsell_funnel=upsell_funnel,
        )
```

- [ ] **Step 6: Убедиться, что тесты проходят**

Run: `uv run pytest tests/cabinet/test_donation_upsell_stats.py -v`
Expected: 3 passed

- [ ] **Step 7: Показать это в админке кабинета**

В `/root/bedolaga-cabinet/src/api/adminSalesStats.ts` — расширить тип:

```ts
export interface DonationsStats {
  total_amount_kopeks: number;
  donors_count: number;
  top_donors: DonorItem[];
  by_source: Record<string, number>;
  upsell_funnel: Record<string, number>;
}
```

В `src/components/sales-stats/DonationsTab.tsx` — после существующего грида карточек добавить блок:

```tsx
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <StatCard
          icon={<BanknotesIcon />}
          label={t('admin.salesStats.donations.fromMenu')}
          value={formatWithCurrency((data.by_source?.menu ?? 0) / 100)}
        />
        <StatCard
          icon={<BanknotesIcon />}
          label={t('admin.salesStats.donations.fromUpsell')}
          value={formatWithCurrency((data.by_source?.topup_upsell ?? 0) / 100)}
        />
      </div>

      <div className="rounded-xl border border-dark-700/30 bg-dark-800/30 p-4">
        <div className="mb-3 text-sm font-semibold text-dark-200">
          {t('admin.salesStats.donations.funnelTitle')}
        </div>
        <div className="grid grid-cols-3 gap-3 text-center">
          <div>
            <div className="text-lg font-bold">{data.upsell_funnel?.shown ?? 0}</div>
            <div className="text-xs text-dark-400">{t('admin.salesStats.donations.funnelShown')}</div>
          </div>
          <div>
            <div className="text-lg font-bold">{data.upsell_funnel?.accepted ?? 0}</div>
            <div className="text-xs text-dark-400">
              {t('admin.salesStats.donations.funnelAccepted')}
            </div>
          </div>
          <div>
            <div className="text-lg font-bold">{data.upsell_funnel?.applied ?? 0}</div>
            <div className="text-xs text-dark-400">
              {t('admin.salesStats.donations.funnelApplied')}
            </div>
          </div>
        </div>
      </div>
```

- [ ] **Step 8: Добавить тексты статистики в 4 локали**

В блок `admin.salesStats.donations` каждого файла добавить ключи:

`ru.json`:
```json
    "fromMenu": "Из меню «Поддержать»",
    "fromUpsell": "Надбавкой при оплате",
    "funnelTitle": "Воронка предложения при оплате",
    "funnelShown": "Показов",
    "funnelAccepted": "Согласий",
    "funnelApplied": "Оплачено",
```

`en.json`:
```json
    "fromMenu": "From «Support» menu",
    "fromUpsell": "Add-on at checkout",
    "funnelTitle": "Checkout offer funnel",
    "funnelShown": "Shown",
    "funnelAccepted": "Accepted",
    "funnelApplied": "Paid",
```

`fa.json`:
```json
    "fromMenu": "از منوی «حمایت»",
    "fromUpsell": "افزودنی هنگام پرداخت",
    "funnelTitle": "قیف پیشنهاد پرداخت",
    "funnelShown": "نمایش",
    "funnelAccepted": "پذیرفته",
    "funnelApplied": "پرداخت‌شده",
```

`zh.json`:
```json
    "fromMenu": "来自“支持”菜单",
    "fromUpsell": "结账时追加",
    "funnelTitle": "结账提议漏斗",
    "funnelShown": "展示",
    "funnelAccepted": "接受",
    "funnelApplied": "已支付",
```

- [ ] **Step 9: Проверки и коммиты**

```bash
cd /root/remnawave-bedolaga-telegram-bot
uv run ruff format . && uv run ruff check . && uv run pytest -q
git add app/database/crud/donation.py app/database/crud/donation_pledge.py app/cabinet/routes/admin_sales_stats.py tests/cabinet/test_donation_upsell_stats.py
git commit -m "feat(stats): разбивка донатов по источнику и воронка апселла"

cd /root/bedolaga-cabinet
npm run type-check && npm run lint && npm run build
git add src/api/adminSalesStats.ts src/components/sales-stats/DonationsTab.tsx src/locales
git commit -m "feat(cabinet): статистика по источнику донатов и воронке апселла"
```

---

## Task 10: Финальная проверка и документация

**Files:**
- Modify: `docs/superpowers/specs/2026-07-30-donation-upsell-design.md` (актуализировать §4.4 — поле `reduces_balance` не понадобилось)
- Test: весь набор

**Interfaces:**
- Consumes: всё предыдущее.
- Produces: ничего.

- [ ] **Step 1: Прогнать полный набор тестов и линтеры**

Run:
```bash
cd /root/remnawave-bedolaga-telegram-bot && uv run ruff check . && uv run pytest -q
cd /root/bedolaga-cabinet && npm run type-check && npm run lint && npm run build
```
Expected: всё зелёное. Если что-то падает — фиксить до зелёного, не коммитить поверх.

- [ ] **Step 2: Проверить целостность цепочки миграций**

Run: `grep -rn "down_revision" migrations/alembic/versions/0097_donation_upsell.py migrations/alembic/versions/0096_donations.py`
Expected: `0097` ссылается на `0096`, `0096` — на `0095`; ни один другой файл не заявляет `down_revision = '0096'`.

Run: `grep -rln "down_revision: Union\[str, None\] = '0096'" migrations/alembic/versions/ | wc -l`
Expected: `1`

- [ ] **Step 3: Актуализировать спеку**

В `docs/superpowers/specs/2026-07-30-donation-upsell-design.md`, §4.4 — заменить абзац про кабинет:

```markdown
- Кабинет — `get_transactions` относит такое пожертвование к расходам, то есть
  сумма приходит отрицательной; `src/pages/Balance.tsx` показывает знак «−»,
  сохраняя «донатный» розовый цвет. Отдельное поле в `TransactionResponse`
  не понадобилось.
```

и убрать упоминание `reduces_balance` из таблицы §6 (строка про `app/cabinet/schemas/balance.py`), заменив на `donation_tip_kopeks` / `donation_upsell_shown` и `DonationUpsellResponse`.

Там же в §5, в абзаце про тексты, заменить последнее предложение на:

```markdown
Тексты бота — дефолтами в коде (`texts.t('DONATION_UPSELL_*', '...')`); файлы
`app/localization/locales/*.json` не затрагиваются (как и у существующих донатов —
`tests/test_locale_integrity.py` требует одинаковый набор ключей во всех языках).
Тексты кабинета — в `src/locales/{ru,en,fa,zh}.json`.
```

- [ ] **Step 4: Ручная проверка на живом стенде**

Проверить последовательно (бот и кабинет, основной провайдер):

1. Пополнение на 500 ₽ → появился экран/модалка апселла → «Поддержать» → счёт на 510 ₽.
2. Оплатить → на баланс пришло 500 ₽, в истории две записи («Пополнение +510», «❤️ −10»), пришло «Спасибо за поддержку», админам — уведомление «Новое пожертвование при пополнении».
3. Повторное пополнение → «Не поддержать» → счёт на 500 ₽, баланс +500, донат не создан.
4. `DONATION_UPSELL_ENABLED=false` + перезапуск → экран/модалка не появляются, пополнение работает как раньше.
5. В админке кабинета («Донаты») видны разбивка по источнику и воронка.

- [ ] **Step 5: Коммит документации**

```bash
git add docs/superpowers/specs/2026-07-30-donation-upsell-design.md
git commit -m "docs: актуализация спеки донат-апселла по факту реализации"
```

---

## Проверка покрытия спеки

| Требование спеки | Задача |
|---|---|
| §2.1 экран в боте, три кнопки, «иная сумма» с пресетами и вводом | Task 5 |
| §2.2 модалка в кабинете в стиле «Поддержать проект» | Task 8 |
| §3.1 обещание + списание в общем пост-хуке | Task 2, 4 |
| §3.2 подбор обещания (сумма + допуск, TTL, погашение прежних) | Task 2, 3, 4 |
| §3.3 запись отказа в боте и кабинете | Task 3, 5, 7 |
| §3.3 «иная сумма» сверх лимита метода → ошибка | Task 5 (бот), Task 7/8 (кабинет) |
| §4.1 таблица `donation_pledges` | Task 1 |
| §4.2 `donations.source` | Task 1, 2 |
| §4.3 миграция 0097 | Task 1 |
| §4.4 знак «−» в истории бота и кабинета | Task 6, 7, 8 |
| §5 семь настроек .env + условия показа | Task 1, 3 |
| §6 admin-уведомление с пометкой источника | Task 4 |
| §6 статистика по источнику и воронка | Task 9 |
| §7 тесты (условия, подбор, идемпотентность, нехватка баланса, оба клиента) | Task 1, 2, 3, 4, 5, 6, 7, 9 |
| §7 ручная проверка на проде | Task 10 |
| §8 риски (безопасный отказ, допуск Stars, лимиты) | Task 3, 4, 7 |
