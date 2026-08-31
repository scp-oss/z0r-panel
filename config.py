"""z0r-panel -- отдельный репозиторий/процесс от Zenith (был вложенной
Zenith/panel/, выделен по прямому запросу). Своя .env в корне этого репо
-- MYSQL_*/ZENITH_ENVIRONMENT_* тут ПРОДУБЛИРОВАНЫ с Zenith/.env
намеренно (тот же паттерн, что z2r_test-voice-bot::VOICE_FILTER_LINES --
две независимо деплоящиеся кодовые базы, общего python-модуля между ними
нет, и не должно быть). БД (`z2r_genome`) от этого не меняется -- панель
как был, так и остался обычным клиентом той же MySQL, просто конфиг
подключения теперь свой, не общий с orchestrator/."""
import os
import secrets


def _load_env(path):
    env = {}
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env


PANEL_DIR = os.path.dirname(os.path.abspath(__file__))
_ENV = _load_env(os.path.join(PANEL_DIR, ".env"))


def _get(key, default=""):
    return os.environ.get(key, _ENV.get(key, default))


MYSQL_HOST = _get("MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = int(_get("MYSQL_PORT", "3306"))
MYSQL_USER = _get("MYSQL_USER", "zenith")
MYSQL_PASSWORD = _get("MYSQL_PASSWORD", "")
MYSQL_DATABASE = _get("MYSQL_DATABASE", "z2r_genome")

# Единственный админ-логин панели -- см. README.md про
# gen_password_hash.py для генерации PANEL_ADMIN_PASSWORD_HASH.
# Многопользовательская модель не запрошена, заводить users-таблицу ради
# одного владельца смысла нет.
PANEL_ADMIN_USER = _get("PANEL_ADMIN_USER", "admin")
PANEL_ADMIN_PASSWORD_HASH = _get("PANEL_ADMIN_PASSWORD_HASH", "")

# Раньше при пустом PANEL_SESSION_SECRET main.py тихо подставлял
# захардкоженную ПУБЛИЧНУЮ строку ("dev-insecure-change-me", буквально в
# этом репо) как ключ подписи сессионной куки -- живая уязвимость, найдена
# при аудите перед деплоем на Provider B 2026-08-17 (любой, кто читал репо, мог
# подделать куку {"user":"admin"} и получить полный /controls). Никакого
# публичного fallback'а больше нет: если секрет не задан явно в .env,
# генерируем ОДИН раз и сохраняем в локальный файл (chmod 600, не в git,
# см. .gitignore) -- тот же принцип "просто работает без ручных шагов",
# что и у остального инсталлятора, но без публичного секрета. Файл
# специфичен для этого сервера -- секрет НЕ пытается быть стабильным
# между разными установками.
_SESSION_SECRET_FILE = os.path.join(PANEL_DIR, ".session_secret")


def _load_or_create_session_secret() -> str:
    explicit = _get("PANEL_SESSION_SECRET", "")
    if explicit:
        return explicit
    try:
        with open(_SESSION_SECRET_FILE) as f:
            existing = f.read().strip()
        if existing:
            return existing
    except OSError:
        pass
    generated = secrets.token_hex(32)
    try:
        fd = os.open(_SESSION_SECRET_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(generated)
    except OSError:
        pass  # не смогли сохранить на диск -- секрет будет новым при каждом рестарте (хуже для сессий, но не публичный)
    return generated


PANEL_SESSION_SECRET = _load_or_create_session_secret()


# TLS -- панель терминирует HTTPS САМА (uvicorn ssl_certfile/ssl_keyfile),
# без отдельного Caddy/nginx/Docker: для одного админ-логина за Cloudflare
# отдельный reverse-proxy процесс не даёт ничего, чего не даёт сам
# uvicorn, только лишняя точка отказа (см. README "Публикация панели
# через Cloudflare" -- живой пример, где это аукнулось: apt purge снёс
# сертификаты вместе с пакетом, Docker bind-mount тихо подменил
# отсутствующий файл директорией). Заданы PANEL_TLS_CERT/PANEL_TLS_KEY --
# слушаем 0.0.0.0:PANEL_PORT с TLS (PANEL_PORT тогда = тот самый
# альт-порт Cloudflare, напр. 2087). Не заданы -- голый HTTP на
# PANEL_HOST (дефолт 127.0.0.1) -- для локальной разработки или если
# TLS-терминацию всё же решили вынести во внешний reverse-proxy.
#
# Дефолт пути -- ВНУТРИ репозитория (tls/), не /etc/куда-то -- по
# прямому запросу не разбрасывать файлы по системе. tls/ в
# .gitignore -- сами PEM'ы никогда не коммитятся, только эта директория
# как место для них на конкретном сервере. Дефолт применяется, только
# если файл там реально есть -- иначе (TLS ещё не настроен) откатываемся
# к HTTP-режиму, а не падаем на открытии несуществующего файла в
# uvicorn.run().
PANEL_TLS_DIR = os.path.join(PANEL_DIR, "tls")
_default_cert = os.path.join(PANEL_TLS_DIR, "cf-origin.pem")
_default_key = os.path.join(PANEL_TLS_DIR, "cf-origin-key.pem")
PANEL_TLS_CERT = _get("PANEL_TLS_CERT", _default_cert if os.path.exists(_default_cert) else "")
PANEL_TLS_KEY = _get("PANEL_TLS_KEY", _default_key if os.path.exists(_default_key) else "")
PANEL_HOST = _get("PANEL_HOST", "127.0.0.1")
PANEL_PORT = int(_get("PANEL_PORT", "8766"))
# Secure-флаг на сессионной cookie -- отключай только для локальной
# разработки без TLS (иначе браузер попросту не отдаст cookie обратно по
# http и логин будет молча "не держаться").
PANEL_COOKIE_HTTPS_ONLY = _get("PANEL_COOKIE_HTTPS_ONLY", "true").lower() == "true"

# Внешний адрес самой панели (для готового блока KEY=value при создании
# токена ноды, см. main.py::create_node -- PANEL_HOST/PANEL_PORT это
# адрес БИНДА, не обязательно то же самое, что видно снаружи через
# Cloudflare). Не задан -- просто не показываем PANEL_URL в блоке,
# оператор впишет его сам вручную (не критично, лучшее усилие).
PANEL_PUBLIC_URL = _get("PANEL_PUBLIC_URL", "")

# Локальное окружение -- то, за которое панель показывает control-статус
# через set_strategy_cli.sh (только `get`/`max`, никогда `set` -- панель
# ничего не применяет сама, см. README "Границы ответственности панели").
LOCAL_ENVIRONMENT_NAME = _get("ZENITH_ENVIRONMENT_NAME", "prod-domru")
LOCAL_ENVIRONMENT_PROVIDER = _get("ZENITH_ENVIRONMENT_PROVIDER", "domru")

# z0r-panel живёт как СОСЕД Zenith в z2r_autobench (/opt/z2r_autobench/
# z0r-panel/, не вложена в Zenith/ как раньше) -- один dirname() до
# autobench, не два.
Z2R_AUTOBENCH_DIR = _get("Z2R_AUTOBENCH_DIR", os.path.dirname(PANEL_DIR))
SET_STRATEGY_CLI = os.path.join(Z2R_AUTOBENCH_DIR, "set_strategy_cli.sh")

# /rkn — read-only просмотр + добавление в боевой хостлист RKN_TLS
# (TCP_RKN_list.txt/TCP_Custom.txt на самом сервере, не БД) -- см.
# rkn_list_cli.sh докстринг для разделения официального/ручного списков.
RKN_LIST_CLI = os.path.join(Z2R_AUTOBENCH_DIR, "rkn_list_cli.sh")

# "Воронка" для кастомного домена на /controls (см. funnel_runner.py) --
# rank_strategies.sh --domain, ОТДЕЛЬНЫЙ инструмент от runner.py/main.py
# ниже: напрямую крутит БОЕВУЮ стратегию профиля через реальный трафик,
# а не Zenith-геномы в изолированной песочнице.
RANK_STRATEGIES_CLI = os.path.join(Z2R_AUTOBENCH_DIR, "rank_strategies.sh")

# /custom-domains -- "экзотические" домены со СВОЕЙ независимой
# стратегией каждый (в отличие от /rkn, где у ВСЕХ доменов под RKN_TLS
# одна общая стратегия), см. custom_domain_cli.sh докстринг.
CUSTOM_DOMAIN_CLI = os.path.join(Z2R_AUTOBENCH_DIR, "custom_domain_cli.sh")

# /domains "Синхронизировать с боевым списком" -- читает официальные
# курированные списки (напр. /opt/zator/lists/russia-youtube.txt) вместо
# ручной вставки текста, см. domain_list_sync.sh докстринг.
DOMAIN_LIST_SYNC_CLI = os.path.join(Z2R_AUTOBENCH_DIR, "domain_list_sync.sh")

# Zenith orchestrator -- сосед по INSTALL_DIR (см. z2r_autobench/z0r::
# ZENITH_DIR="$INSTALL_DIR/Zenith"). Нужен runner.py -- кнопке "запустить
# подбор" в /controls, чтобы дёрнуть тот же main.py тем же venv, что при
# ручном запуске из терминала (см. README Zenith "sudo venv/bin/python3
# main.py --profile ...").
ZENITH_ORCHESTRATOR_DIR = _get("ZENITH_ORCHESTRATOR_DIR", os.path.join(Z2R_AUTOBENCH_DIR, "Zenith", "orchestrator"))
ZENITH_VENV_PYTHON = _get("ZENITH_VENV_PYTHON", os.path.join(ZENITH_ORCHESTRATOR_DIR, "venv", "bin", "python3"))
# Корень git-репозитория Zenith (не orchestrator/ подкаталог) -- нужен
# только для "проверить обновления" (daemon_ctl.check_git_updates),
# zenith-autorun/zenith-promoter оба живут в этом репозитории.
ZENITH_DIR = os.path.dirname(ZENITH_ORCHESTRATOR_DIR)

# Периодический автозапуск и автопродвижение живут как НЕЗАВИСИМЫЕ от
# панели systemd-юниты (zenith-autorun.service/zenith-promoter.service,
# см. Zenith/README.md "Автономный режим") -- их интервал/пороги
# настраиваются в Zenith/.env, не здесь. Панель на /controls -- только
# start/stop/log поверх этих же юнитов (см. daemon_ctl.py).
