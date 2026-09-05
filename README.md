# z0r-panel

Веб-панель для [Zenith](https://github.com/scp-oss/Zenith) (генератор
стратегий DPI-обхода для [z2r](https://github.com/AloofLibra/z4r)):
обзор лучших геномов по профилям и провайдерам, история мутаций
конкретной стратегии (родословная: от какого генома и каким оператором
получен), read-only статус текущих боевых стратегий, и sync-хаб для
нод на разных провайдерах (см. ниже).

Отдельный репозиторий от Zenith (раньше была вложенная `Zenith/panel/`)
-- независимый деплой, свой `.env`, свой venv. Устанавливается на сервер
рядом с Zenith, тем же on-demand-clone паттерном через меню `z0r`
(`/opt/z2r_autobench/z0r-panel`).

## Как это устроено

Панель работает НА той же машине, что и локальная нода Zenith
(`orchestrator/`), и подключается к той же MySQL (`z2r_genome`) как
обычный клиент -- отдельной БД для панели нет, `MYSQL_*` в `.env`
указывают на ту же базу, что и `Zenith/.env` этого сервера (значения
дублируются между двумя `.env`, т.к. это независимо деплоящиеся кодовые
базы -- см. `config.py`).

**Границы ответственности** прошли два осознанных изменения от исходного
"только через существующие скрипты, панель НИКОГДА не пишет в боевой
конфиг" (было: `set_strategy_cli.sh get`/`max`, никогда `set`):

- 2026-08-15: `/controls` научилась и `set_strategy_cli.sh set` (форма
  "Ручное переключение стратегии"), и `systemctl restart zapret2` -- то
  же самое действие, что человек делает через `z0r` пункт 12 руками из
  терминала, просто кнопкой вместо SSH. Кнопка исполняет только то, что
  человек явно указал в форме (номер профиля + номер стратегии) -- сама
  ничего не выбирает.
- 2026-08-16 (по прямому запросу "цель: автономная работа без
  человеческого вмешательства, но панель это модуль -- её может и не
  быть, функционал должен быть в CLI и без панели"): добавлены
  **периодическая генерация** и **автопродвижение** -- см. раздел ниже.
  Оба живут КАК НЕЗАВИСИМЫЕ ОТ ПАНЕЛИ systemd-юниты в самом Zenith
  (`zenith-autorun.service`/`zenith-promoter.service`), не как код
  панели -- если панель не установлена или выключена, оба продолжают
  работать сами по себе. Панель -- ТОЛЬКО пульт (start/stop/log) поверх
  них, тем же способом, что уже было для `autotune-daemon`.

## Периодическая генерация и автопродвижение

Логика (см. `Zenith/README.md` "Автономный режим") живёт в самом
Zenith'е -- `zenith_autorun.sh` (генерация кандидатов по кругу) и
`orchestrator/auto_promoter.py --loop` (автопродвижение, критерий тот
же, что `promote.py::pick_best()` -- min_pulls + 100% успехов; пишет
новый `strategy=N` через `z2r_autobench/promote_apply_cli.sh`,
переключает, рестартует `zapret2`, проверяет ЖИВЫМ curl по доменам
профиля из `domain_pool` (не только `systemctl is-active`/`get` --
живой инцидент 2026-08-16: геном прошёл sandbox 6/6, но не работал в
проде, `is-active`/`get` этого не ловят) и сам откатывает через backup,
если проверка не прошла. Обе карточки на `/controls`
(`zenith-autorun`/`zenith-promoter`) -- это `daemon_ctl.py`, тот же
узкий `systemctl start/stop/is-active/journalctl`, что уже используется
для `autotune-daemon` -- панель НИЧЕГО не решает и не исполняет из этой
пары сама, только показывает статус/лог и жмёт кнопку.

Установка/включение -- через `z0r` пункт 14 → 2 → подменю Zenith →
"Автономный режим" (копирует `.service`-юниты из репозитория Zenith,
`systemctl enable --now`). **`zenith-promoter` по умолчанию требует
ручного подтверждения при первом включении** -- это самая рискованная
автоматика в проекте (пишет боевой файл и рестартует `zapret2` без
подтверждения человека на каждом последующем срабатывании), меню
явно рекомендует сначала прогнать `auto_promoter.py --profile <X>`
вручную на конкретном сервере, прежде чем включать `--loop`.

## Мульти-провайдерный sync (hub-and-spoke)

Топология -- **не** общая MySQL наружу и не mesh-репликация между
нодами. Панель (эта, на прод-сервере) -- единственная точка, у которой
есть прямой доступ к БД. Удалённые ноды (Zenith на других
провайдерах/ВМ) MySQL-доступа НЕ получают вообще -- порт наружу не
открывается. Вместо этого каждая удалённая нода:

- после `main.py` шлёт `sync_client.py push` (в репозитории Zenith) --
  HTTP POST снапшота своих `genomes`+`genome_scores` сюда, с собственным
  Bearer-токеном (выдаётся на странице `/nodes`, показывается один раз,
  в БД хранится только его sha256);
- может `sync_client.py pull`/`bootstrap.py` -- забрать глобальный топ
  геномов со всех нод (или provider-специфичные/universal-паттерны для
  своего провайдера) как кандидатов, чтобы затравить свой локальный UCB.

Если панель временно недоступна -- локальный `main.py` каждой ноды
продолжает работать как ни в чём не бывало (своя БД, своя песочница),
зависимость только на момент sync, не на каждый раунд генерации.

Страница `/knowledge` -- та же идея на уровне паттернов, не конкретных
геномов: group-by по `family`+`fooling`+`ttl_mode` (конкретные параметры
блобов/TTL у каждого провайдера свои, но это уже переносимый уровень)
с колонкой "сколько разных провайдеров" -- чем больше, тем более общий,
не завязанный на DPI одного оператора приём.

## Установка

```bash
# 1. Миграция БД Zenith, если ставится на УЖЕ работающую (не свежую) БД
#    (в репозитории Zenith): mysql -u zenith -p z2r_genome <
#    db/migrations/001_panel_sync.sql

# 2. Системный юзер + sudoers -- через z0r (пункт 14 → 5) это делается
#    автоматически и идемпотентно на каждый заход в пункт (см.
#    z2r_autobench/z0r::ensure_panel_runtime_grants) -- вручную нужно
#    только при установке в обход z0r (ЭТА строка -- fallback, держи её
#    синхронной с ensure_panel_runtime_grants в z0r, иначе после
#    обновления z0r кнопки start/stop для zenith-autorun/zenith-promoter
#    или "запустить подбор" начнут падать с молчаливым запросом пароля --
#    живой случай рассинхрона найден при аудите перед деплоем на Provider B
#    2026-08-17). Права: set_strategy_cli.sh get/max/set, restart+show
#    (ДВА отдельных --property=, не через запятую -- sudoers режет список
#    команд по запятой даже внутри аргумента) для zapret2.service,
#    start/stop/is-active/journalctl для autotune-daemon.service И для
#    zenith-autorun.service/zenith-promoter.service (см. Zenith/README.md
#    "Автономный режим"), и flock-обёрнутый запуск Zenith'овского
#    orchestrator/main.py (для кнопки "запустить подбор" -- flock держит
#    лок на дочернем процессе, не в памяти панели, см. runner.py).
sudo useradd --system --no-create-home --shell /usr/sbin/nologin zenith-panel
echo 'zenith-panel ALL=(root) NOPASSWD: /usr/bin/bash /opt/z2r_autobench/set_strategy_cli.sh get *, /usr/bin/bash /opt/z2r_autobench/set_strategy_cli.sh max *, /usr/bin/bash /opt/z2r_autobench/set_strategy_cli.sh set *, /usr/bin/systemctl restart zapret2, /usr/bin/systemctl show zapret2 --property=ActiveEnterTimestamp --property=SubState, /usr/bin/flock -n /opt/z2r_autobench/Zenith/orchestrator/.run.lock /opt/z2r_autobench/Zenith/orchestrator/venv/bin/python3 /opt/z2r_autobench/Zenith/orchestrator/main.py --profile * --rounds *, /usr/bin/systemctl start autotune-daemon, /usr/bin/systemctl stop autotune-daemon, /usr/bin/systemctl is-active autotune-daemon, /usr/bin/journalctl -u autotune-daemon -n 200 --no-pager, /usr/bin/systemctl start zenith-autorun, /usr/bin/systemctl stop zenith-autorun, /usr/bin/systemctl is-active zenith-autorun, /usr/bin/journalctl -u zenith-autorun -n 200 --no-pager, /usr/bin/systemctl start zenith-promoter, /usr/bin/systemctl stop zenith-promoter, /usr/bin/systemctl is-active zenith-promoter, /usr/bin/journalctl -u zenith-promoter -n 200 --no-pager' \
  | sudo tee /etc/sudoers.d/zenith-panel
sudo chmod 440 /etc/sudoers.d/zenith-panel

cp .env.example .env   # заполнить MYSQL_*/PANEL_* (см. комментарии в файле)
python3 -m venv venv && venv/bin/pip install -r requirements.txt
venv/bin/python3 gen_password_hash.py   # -> PANEL_ADMIN_PASSWORD_HASH в .env
# PANEL_SESSION_SECRET можно не задавать вручную -- если пусто, панель сама
# сгенерирует и сохранит случайный секрет в .session_secret при первом
# запуске (см. config.py) -- задавай явно только если нужен ФИКСИРОВАННЫЙ
# секрет (напр. переносишь .session_secret между серверами вручную).

sudo cp zenith-panel.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now zenith-panel
sudo systemctl status zenith-panel --no-pager
```

Из коробки (без `PANEL_TLS_CERT`/`PANEL_TLS_KEY`) панель слушает голый
HTTP на `127.0.0.1` -- этого достаточно для локальной проверки, но
**наружу в таком виде не публикуй**: пароль и сессионная cookie пойдут в
открытом виде. Для боевого доступа снаружи см. следующий раздел.

Добавить удалённую ноду: `/nodes` в UI -> имя+провайдер -> токен
показывается один раз -> на удалённой ноде в `Zenith/.env`:
```
PANEL_URL=https://<хост-панели>:<альт-порт Cloudflare>
PANEL_NODE_TOKEN=<токен>
```

## Публикация через Cloudflare (TLS прямо в uvicorn, без Caddy/Docker)

443 на хосте обычно уже занят чем-то другим -- используем один из
альтернативных HTTPS-портов, которые Cloudflare вообще проксирует на
orange-cloud записях: **443, 2053, 2083, 2087, 2096, 8443** (и только
их -- на любой другой порт проксируемый трафик Cloudflare не дойдёт, это
жёсткое ограничение на стороне Cloudflare, не завязано на блокировки
провайдера). Let's Encrypt тут не подходит по той же причине, что и порт
443 -- HTTP-01/TLS-ALPN-01 челленджам тоже нужны 80/443. Вместо этого --
**Cloudflare Origin CA**: сертификат на 15 лет, выпускается вручную в
дашборде Cloudflare без единого челленджа, доверен только самим
Cloudflare.

Отдельного Caddy/nginx перед панелью нет -- `uvicorn` (на котором и так
работает FastAPI-панель) терминирует TLS сам
(`ssl_certfile`/`ssl_keyfile`). Для одного админ-логина за Cloudflare
отдельный reverse-proxy процесс не даёт ничего сверху, только лишнюю
точку отказа.

```bash
# 1. В дашборде Cloudflare для зоны домена:
#    DNS -> Add record -> A, имя PANEL_HOSTNAME, значение = публичный IP
#    сервера, Proxy status: Proxied (оранжевое облако).
#    SSL/TLS -> Overview -> Encryption mode: Full (strict).
#    SSL/TLS -> Origin Server -> Create Certificate -> RSA, hostnames =
#    PANEL_HOSTNAME, 15 years -> сохрани Origin Certificate и Private Key
#    (private key больше нигде не показывается повторно -- сохрани его
#    себе отдельно, напр. в менеджере паролей, ДО того как закрыть
#    страницу Cloudflare).

# 2. На сервере -- вставь скопированные из дашборда PEM'ы (набери команду
#    сам, не вставляй приватный ключ в чат/куда-либо ещё). Живут ВНУТРИ
#    репозитория (tls/, в .gitignore -- никогда не коммитятся). Владелец
#    -- юзер, от которого работает панель (zenith-panel), не root:
mkdir -p tls
sudo nano tls/cf-origin.pem       # вставить Origin Certificate
sudo nano tls/cf-origin-key.pem   # вставить Private Key
sudo chown zenith-panel:zenith-panel tls/cf-origin.pem tls/cf-origin-key.pem
sudo chmod 600 tls/cf-origin-key.pem

# 3. Дефолтные пути (config.py) уже указывают сюда -- явно прописывать
#    PANEL_TLS_CERT/PANEL_TLS_KEY в .env не обязательно, если файлы лежат
#    ровно как в п.2. В .env остаётся выставить только порт (= сам этот
#    альт-порт Cloudflare, не внутренний 8766 -- панель слушает его
#    напрямую, TLS уже внутри):
PANEL_PORT=<порт из списка выше, напр. 2087>

sudo systemctl restart zenith-panel
sudo systemctl status zenith-panel --no-pager

# 4. Порт снаружи должен светить ТОЛЬКО Cloudflare, не всему интернету --
#    cloudflare_iptables.sh ставит ipset+iptables allowlist по
#    официальным диапазонам Cloudflare (обновляется по крону, диапазоны
#    изредка меняются):
sudo apt-get install -y ipset
sudo /opt/z2r_autobench/z0r-panel/cloudflare_iptables.sh <порт>
# в cron, раз в сутки:
echo "0 4 * * * root /opt/z2r_autobench/z0r-panel/cloudflare_iptables.sh <порт> >> /var/log/cf-iptables.log 2>&1" | sudo tee /etc/cron.d/cf-iptables
```

Панель после этого слушает `0.0.0.0:<порт>` НАПРЯМУЮ (см. `main.py` --
как только заданы/найдены `PANEL_TLS_CERT`/`PANEL_TLS_KEY`, `PANEL_HOST`
игнорируется, это уже не loopback-режим). Порт >1024 -- отдельных
capabilities/root для биндинга не нужно, `zenith-panel` как был
непривилегированным, так и остаётся. Сессионная cookie ставится с флагом
`Secure` (`PANEL_COOKIE_HTTPS_ONLY=true`, дефолт), т.к. до браузера теперь
всегда доходит по HTTPS.

## Требования

- Python 3.10+ (используется `str | None` в аннотациях типов).
- MySQL 8+ той же `z2r_genome` схемы, что и Zenith (`Zenith/db/schema.sql`).
- `z2r_autobench` рядом на диске (для `set_strategy_cli.sh`, только
  read-only чтение статуса на `/controls`).
