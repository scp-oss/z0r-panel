"""Панель читает/пишет ТУ ЖЕ БД, что и локальный orchestrator (см.
config.py) -- отдельной схемы для панели нет. Часть функций здесь --
дубликаты запросов из orchestrator/db.py в форме, удобной для страниц
(dictionary-курсоры, агрегаты для таблиц), часть -- новые: реестр нод
(токены) и sync push/pull.
"""
import hashlib
import json
import secrets
import uuid

import mysql.connector

import config


def connect():
    return mysql.connector.connect(
        host=config.MYSQL_HOST,
        port=config.MYSQL_PORT,
        user=config.MYSQL_USER,
        password=config.MYSQL_PASSWORD,
        database=config.MYSQL_DATABASE,
        autocommit=True,
    )


# ---------------------------------------------------------------- nodes ---

def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_node(conn) -> tuple:
    """Создаёт ПУСТУЮ ноду -- только токен + node_uuid, без имени/
    провайдера (см. схему: их сообщает сама нода при первом push, из
    собственных ZENITH_ENVIRONMENT_NAME/PROVIDER -- незачем вводить то
    же самое значение второй раз в веб-форме). name временно =
    'pending-<uuid8>' (UNIQUE NOT NULL требует хоть какое-то значение),
    перезаписывается на реальное при self-report. Токен показывается
    ОДИН раз в UI в момент создания -- в БД лежит только его sha256."""
    token = secrets.token_urlsafe(32)
    token_hash = hash_token(token)
    node_uuid = str(uuid.uuid4())
    placeholder_name = f"pending-{node_uuid[:8]}"
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO environments (name, provider, api_token_hash, node_uuid)
           VALUES (%s, NULL, %s, %s)""",
        (placeholder_name, token_hash, node_uuid),
    )
    return token, node_uuid


def reissue_node_token(conn, environment_id: int) -> str:
    """Новый токен для УЖЕ существующей записи (в отличие от create_node --
    та заводит новую строку) -- для нод, которые ещё не откликнулись (или
    откликались, но токен утерян/не сохранён при создании). Старый токен
    сразу перестаёт действовать (перезаписывается хэш), имя/провайдер/
    история (если уже есть) не трогаются. Возвращает новый токен в
    открытом виде -- как и create_node, показывается вызывающим кодом
    ОДИН раз, в БД остаётся только хэш."""
    token = secrets.token_urlsafe(32)
    token_hash = hash_token(token)
    cur = conn.cursor()
    cur.execute(
        "UPDATE environments SET api_token_hash=%s WHERE id=%s AND is_production=FALSE",
        (token_hash, environment_id),
    )
    return token


def self_report_node(conn, environment_id: int, name: str, provider: str) -> bool:
    """Нода сообщает своё реальное имя/провайдера при push (см.
    sync_api.py::push) -- перезаписывает placeholder ('pending-<uuid8>')
    или любое предыдущее значение, идемпотентно на каждый push (если
    оператор потом поменяет ZENITH_ENVIRONMENT_NAME локально -- панель
    сама подхватит на следующей синхронизации, без ручного вмешательства).
    Возвращает False при конфликте имени с ДРУГОЙ нодой (name UNIQUE) --
    вызывающий код должен сообщить об этом ноде понятной ошибкой, а не
    затирать чужую запись."""
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE environments SET name=%s, provider=%s WHERE id=%s",
            (name, provider, environment_id),
        )
        return True
    except mysql.connector.errors.IntegrityError:
        return False


def get_environment_by_token(conn, token: str):
    cur = conn.cursor(dictionary=True)
    cur.execute(
        "SELECT id, name, provider, node_uuid FROM environments WHERE api_token_hash=%s AND active=1",
        (hash_token(token),),
    )
    return cur.fetchone()


def known_providers_for_genome(conn, genome_id: str) -> int:
    cur = conn.cursor()
    cur.execute(
        """SELECT COUNT(DISTINCT e.provider)
           FROM genome_scores gs JOIN environments e ON e.id = gs.environment_id
           WHERE gs.genome_id=%s AND gs.pulls > 0 AND gs.successes = gs.pulls""",
        (genome_id,),
    )
    return cur.fetchone()[0]


def touch_node_sync(conn, environment_id: int):
    cur = conn.cursor()
    cur.execute("UPDATE environments SET last_sync_at=NOW() WHERE id=%s", (environment_id,))


def list_environments(conn):
    cur = conn.cursor(dictionary=True)
    cur.execute(
        """SELECT e.id, e.name, e.provider, e.node_uuid, e.is_production, e.active,
                  e.api_token_hash IS NOT NULL AS is_remote_node,
                  e.last_sync_at, e.created_at,
                  COUNT(DISTINCT gs.genome_id) AS genome_count,
                  COALESCE(SUM(gs.pulls), 0) AS total_pulls
           FROM environments e
           LEFT JOIN genome_scores gs ON gs.environment_id = e.id
           GROUP BY e.id
           ORDER BY e.is_production DESC, (e.provider IS NULL), e.name""",
    )
    return cur.fetchall()


def get_environment_genome_count(conn, environment_id: int) -> int:
    """Гейт для delete_environment -- ноду с реальной историей (хоть один
    genome_scores) удалять нельзя, только деактивировать (см. main.py
    delete_node -- клиентская проверка в шаблоне дублируется здесь на
    сервере, не доверяем только форме)."""
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM genome_scores WHERE environment_id=%s", (environment_id,))
    return cur.fetchone()[0]


def delete_environment(conn, environment_id: int) -> bool:
    """Жёсткое удаление -- только для нод БЕЗ истории (см.
    get_environment_genome_count) -- типично те самые 'pending-<uuid8>',
    которым выписали токен, но так и не подключили (или подключили не
    туда, повторный тест и т.п.). Локальную (is_production) не даёт
    удалить -- у неё нет токена, но по ошибке кликнуть ID можно."""
    cur = conn.cursor()
    cur.execute("DELETE FROM environments WHERE id=%s AND is_production=FALSE", (environment_id,))
    return cur.rowcount > 0


def force_delete_environment(conn, environment_id: int) -> bool:
    """Удаление ЛЮБОЙ ноды, вместе с её историей -- по прямому запросу
    (delete_environment выше отказывает, если genome_count > 0). Чистим
    genome_scores/experiments/operator_stats/ban_events за этим
    environment_id явно, а не полагаемся на FOREIGN KEY CASCADE -- schema.sql
    объявляет REFERENCES только как аннотацию в определении колонки, не
    полноценным table-level constraint, так что рассчитывать на автоудаление
    зависимых строк нельзя. Сами genomes НЕ трогаем -- геном не принадлежит
    одной ноде (мог прийти через sync_import с другой), делить нечего."""
    cur = conn.cursor()
    for table in ("genome_scores", "experiments", "operator_stats", "ban_events"):
        cur.execute(f"DELETE FROM {table} WHERE environment_id=%s", (environment_id,))
    cur.execute("DELETE FROM environments WHERE id=%s AND is_production=FALSE", (environment_id,))
    return cur.rowcount > 0


def set_environment_active(conn, environment_id: int, active: bool):
    cur = conn.cursor()
    cur.execute("UPDATE environments SET active=%s WHERE id=%s AND is_production=FALSE", (active, environment_id))


def get_or_create_local_environment(conn) -> int:
    cur = conn.cursor()
    cur.execute("SELECT id FROM environments WHERE name=%s", (config.LOCAL_ENVIRONMENT_NAME,))
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute(
        "INSERT INTO environments (name, provider, is_production) VALUES (%s, %s, TRUE)",
        (config.LOCAL_ENVIRONMENT_NAME, config.LOCAL_ENVIRONMENT_PROVIDER),
    )
    return cur.lastrowid


# -------------------------------------------------------------- overview --

def overview_rows(conn):
    """Профиль x окружение -- лучший геном там, среднее качество, надёжность.
    Основа страницы 'обзор по провайдерам'."""
    cur = conn.cursor(dictionary=True)
    cur.execute(
        """SELECT g.profile, e.id AS environment_id, e.name AS environment_name,
                  e.provider, e.is_production,
                  g.id AS genome_id, g.rendered_args, g.source, g.family,
                  gs.pulls, gs.successes, gs.total_reward,
                  ROUND(gs.total_reward / NULLIF(gs.pulls, 0), 3) AS avg_score
           FROM genome_scores gs
           JOIN genomes g ON g.id = gs.genome_id
           JOIN environments e ON e.id = gs.environment_id
           WHERE g.family != 'control' AND gs.pulls > 0
           ORDER BY g.profile, e.name, avg_score DESC""",
    )
    rows = cur.fetchall()
    # лучший геном на (profile, environment) -- первая строка после сортировки avg_score DESC
    best = {}
    for row in rows:
        key = (row["profile"], row["environment_id"])
        if key not in best:
            best[key] = row
    return list(best.values())


def controls_for_profile(conn, profile: str, environment_id: int, min_score_rows: int = 20):
    cur = conn.cursor(dictionary=True)
    cur.execute(
        """SELECT g.id, g.rendered_args, g.source, g.family, g.generation,
                  gs.pulls, gs.successes,
                  ROUND(gs.total_reward / NULLIF(gs.pulls, 0), 3) AS avg_score
           FROM genome_scores gs
           JOIN genomes g ON g.id = gs.genome_id
           WHERE g.profile=%s AND gs.environment_id=%s AND gs.pulls > 0
           ORDER BY (g.family = 'control') DESC, avg_score DESC
           LIMIT %s""",
        (profile, environment_id, min_score_rows),
    )
    return cur.fetchall()


# ----------------------------------------------------------- genome view --

def get_genome(conn, genome_id: str):
    cur = conn.cursor(dictionary=True)
    cur.execute(
        """SELECT g.*, e.name AS env_name
           FROM genomes g
           LEFT JOIN genome_scores gs ON gs.genome_id = g.id
           LEFT JOIN environments e ON e.id = gs.environment_id
           WHERE g.id = %s LIMIT 1""",
        (genome_id,),
    )
    return cur.fetchone()


def genome_scores_by_env(conn, genome_id: str):
    cur = conn.cursor(dictionary=True)
    cur.execute(
        """SELECT e.name AS environment_name, e.provider, gs.pulls, gs.successes,
                  ROUND(gs.total_reward / NULLIF(gs.pulls, 0), 3) AS avg_score, gs.is_production
           FROM genome_scores gs
           JOIN environments e ON e.id = gs.environment_id
           WHERE gs.genome_id = %s
           ORDER BY e.name""",
        (genome_id,),
    )
    return cur.fetchall()


def genome_ancestors(conn, genome_id: str, max_depth: int = 20):
    """Цепочка родителей вверх (parent1) -- история мутаций 'откуда этот
    геном взялся'. parent2 (crossover) добавляется отдельной пометкой на
    каждом узле, не разворачивается рекурсивно вглубь (crossover пока не
    используется в основном цикле, см. README, но модель это допускает)."""
    chain = []
    current_id = genome_id
    seen = set()
    for _ in range(max_depth):
        if not current_id or current_id in seen:
            break
        seen.add(current_id)
        cur = conn.cursor(dictionary=True)
        cur.execute(
            """SELECT id, rendered_args, source, mutation_op, generation,
                      parent1_id, parent2_id, created_at
               FROM genomes WHERE id=%s""",
            (current_id,),
        )
        row = cur.fetchone()
        if not row:
            break
        chain.append(row)
        current_id = row["parent1_id"]
    return chain


def genome_children(conn, genome_id: str, limit: int = 50):
    cur = conn.cursor(dictionary=True)
    cur.execute(
        """SELECT id, rendered_args, mutation_op, generation, created_at
           FROM genomes WHERE parent1_id=%s OR parent2_id=%s
           ORDER BY created_at LIMIT %s""",
        (genome_id, genome_id, limit),
    )
    return cur.fetchall()


# --------------------------------------------------------------- sync IO --

def sync_push(conn, environment_id: int, genomes: list, scores: list) -> dict:
    """genomes: [{id, profile, filter_type, family, fooling, ttl_mode,
    fake_payload, params_json, rendered_args, source, parent1_id,
    parent2_id, mutation_op, generation}, ...] -- как есть из ноды.
    scores: [{genome_id, pulls, successes, total_reward}, ...] --
    СНАПШОТ (не дельта): нода шлёт текущее полное состояние своих
    genome_scores, панель делает SET (перезаписывает), не прибавляет --
    источник истины по pulls/successes для этой ноды -- её собственная
    локальная БД, панель только зеркалит."""
    cur = conn.cursor()
    for g in genomes:
        cur.execute(
            """INSERT IGNORE INTO genomes
               (id, profile, filter_type, family, fooling, ttl_mode, fake_payload,
                params_json, rendered_args, source, parent1_id, parent2_id,
                mutation_op, generation)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                g["id"], g["profile"], g["filter_type"], g["family"], g.get("fooling"),
                g.get("ttl_mode"), g.get("fake_payload"), json.dumps(g["params_json"], ensure_ascii=False)
                if isinstance(g["params_json"], dict) else g["params_json"],
                g["rendered_args"], g["source"], g.get("parent1_id"), g.get("parent2_id"),
                g.get("mutation_op"), g.get("generation", 0),
            ),
        )
    for s in scores:
        cur.execute(
            """INSERT INTO genome_scores (genome_id, environment_id, pulls, successes, total_reward)
               VALUES (%s,%s,%s,%s,%s) AS new
               ON DUPLICATE KEY UPDATE
                 pulls = new.pulls, successes = new.successes, total_reward = new.total_reward""",
            (s["genome_id"], environment_id, s["pulls"], s["successes"], s["total_reward"]),
        )
    touch_node_sync(conn, environment_id)
    return {"genomes_seen": len(genomes), "scores_seen": len(scores)}


def bootstrap_candidates(conn, profile: str, provider: str, min_pulls: int = 3, limit: int = 10):
    """Для свежеразвёрнутой ноды ДО первого прогона main.py: что уже
    известно работающим у ЭТОГО провайдера (tier='provider'), и что
    работает у ЛЮБЫХ >=2 провайдеров разом -- т.е. не завязано на
    конкретную DPI одного провайдера, а более общий паттерн обхода
    (tier='universal'). Оба списка -- только 100%-успешные с min_pulls+
    прогонами (см. sync_pull -- та же осторожность: не тащить на новую
    точку то, что даже у автора было нестабильно). Порядок:
    provider-специфичное сначала (короче путь до совпадения с этой же
    DPI), затем universal как более рискованный, но шире проверенный
    fallback."""
    # distinct_providers -- ГЛОБАЛЬНОЕ число провайдеров у этого генома
    # (корреляционный подзапрос), не число внутри WHERE e.provider=%s
    # (там оно тривиально было бы 1) -- иначе тир 'provider' всегда
    # показывал бы "проверено 1 провайдером", даже если геном ЗАОДНО уже
    # подтверждён и как universal-паттерн.
    cur = conn.cursor(dictionary=True)
    cur.execute(
        """SELECT g.id, g.profile, g.params_json, g.rendered_args, g.generation,
                  'provider' AS tier,
                  SUM(gs.pulls) AS pulls, SUM(gs.successes) AS successes,
                  ROUND(SUM(gs.total_reward) / NULLIF(SUM(gs.pulls), 0), 3) AS avg_score,
                  (SELECT COUNT(DISTINCT e2.provider) FROM genome_scores gs2
                     JOIN environments e2 ON e2.id = gs2.environment_id
                     WHERE gs2.genome_id = g.id AND gs2.pulls > 0 AND gs2.successes = gs2.pulls) AS distinct_providers
           FROM genome_scores gs
           JOIN genomes g ON g.id = gs.genome_id AND g.profile = %s
           JOIN environments e ON e.id = gs.environment_id
           WHERE g.family != 'control' AND e.provider = %s
           GROUP BY g.id, g.profile, g.params_json, g.rendered_args, g.generation
           HAVING pulls >= %s AND successes = pulls
           ORDER BY avg_score DESC, pulls DESC
           LIMIT %s""",
        (profile, provider, min_pulls, limit),
    )
    provider_rows = cur.fetchall()

    cur.execute(
        """SELECT g.id, g.profile, g.params_json, g.rendered_args, g.generation,
                  'universal' AS tier,
                  SUM(gs.pulls) AS pulls, SUM(gs.successes) AS successes,
                  ROUND(SUM(gs.total_reward) / NULLIF(SUM(gs.pulls), 0), 3) AS avg_score,
                  COUNT(DISTINCT e.provider) AS distinct_providers
           FROM genome_scores gs
           JOIN genomes g ON g.id = gs.genome_id AND g.profile = %s
           JOIN environments e ON e.id = gs.environment_id
           WHERE g.family != 'control'
           GROUP BY g.id, g.profile, g.params_json, g.rendered_args, g.generation
           HAVING pulls >= %s AND successes = pulls AND distinct_providers >= 2
           ORDER BY distinct_providers DESC, avg_score DESC
           LIMIT %s""",
        (profile, min_pulls, limit),
    )
    universal_rows = cur.fetchall()

    seen = {r["id"] for r in provider_rows}
    universal_rows = [r for r in universal_rows if r["id"] not in seen]
    return provider_rows + universal_rows


def knowledge_family_rollup(conn):
    """Обзор 'какие семейства/паттерны вообще работают' -- по всем
    профилям и провайдерам разом. Не привязано к конкретному геному
    (точные параметры блобов/TTL у каждого провайдера свои), но family +
    fooling + ttl_mode -- это уже переносимый уровень паттерна ('fake с
    autottl', 'multidisorder+seqovl' и т.п.), см. запрос пользователя про
    'базу знаний по семействам'."""
    cur = conn.cursor(dictionary=True)
    cur.execute(
        """SELECT g.profile, g.family, g.fooling, g.ttl_mode,
                  COUNT(DISTINCT g.id) AS distinct_genomes,
                  COUNT(DISTINCT e.provider) AS distinct_providers,
                  SUM(gs.pulls) AS total_pulls,
                  ROUND(SUM(gs.total_reward) / NULLIF(SUM(gs.pulls), 0), 3) AS avg_score
           FROM genome_scores gs
           JOIN genomes g ON g.id = gs.genome_id
           JOIN environments e ON e.id = gs.environment_id
           WHERE g.family != 'control' AND gs.pulls > 0
           GROUP BY g.profile, g.family, g.fooling, g.ttl_mode
           HAVING distinct_providers >= 1
           ORDER BY distinct_providers DESC, avg_score DESC""",
    )
    return cur.fetchall()


# --------------------------------------------------- realtime node DB API --
# Для нод в ZENITH_DB_MODE=api (своей БД нет вообще, см. orchestrator/
# db_api.py) -- те же запросы, что orchestrator/db.py делает напрямую в
# MySQL, но здесь, за Bearer-токеном ноды (см. db_api.py router и
# auth.require_node). environment_id ВСЕГДА берётся из токена запроса,
# никогда не из тела запроса -- иначе одна нода могла бы дописывать в
# чужой environment_id, просто подставив его в JSON.

def get_domains_for_profile(conn, profile: str):
    cur = conn.cursor(dictionary=True)
    cur.execute(
        """SELECT id, host, path, min_bytes FROM domain_pool
           WHERE profile_hint=%s
             AND (quarantined_until IS NULL OR quarantined_until < NOW())""",
        (profile,),
    )
    return cur.fetchall()


def get_or_create_domain(conn, host: str, path: str, profile: str, min_bytes: int) -> dict:
    """Для main.py --domain на api-режимных нодах -- см.
    orchestrator/db_local.py::get_or_create_domain (тот же запрос)."""
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO domain_pool (host, path, profile_hint, min_bytes)
           VALUES (%s,%s,%s,%s)
           ON DUPLICATE KEY UPDATE id=LAST_INSERT_ID(id)""",
        (host, path, profile, min_bytes),
    )
    domain_id = cur.lastrowid
    return {"id": domain_id, "host": host, "path": path, "min_bytes": min_bytes}


def insert_genome(conn, g: dict) -> str:
    cur = conn.cursor()
    cur.execute(
        """INSERT IGNORE INTO genomes
           (id, profile, filter_type, family, fooling, ttl_mode, fake_payload,
            params_json, rendered_args, source, parent1_id, parent2_id,
            mutation_op, generation)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (
            g["id"], g["profile"], g["filter_type"], g["family"], g.get("fooling"),
            g.get("ttl_mode"), g.get("fake_payload"),
            json.dumps(g["params_json"], ensure_ascii=False)
            if isinstance(g["params_json"], dict) else g["params_json"],
            g["rendered_args"], g["source"], g.get("parent1_id"), g.get("parent2_id"),
            g.get("mutation_op"), g.get("generation", 0),
        ),
    )
    return g["id"]


def insert_control_genome(conn, profile: str, lines: list) -> str:
    rendered = "\n".join(lines)
    gid = hashlib.sha256(f"{profile}:{rendered}".encode()).hexdigest()
    cur = conn.cursor()
    cur.execute(
        """INSERT IGNORE INTO genomes
           (id, profile, filter_type, family, fooling, ttl_mode, fake_payload,
            params_json, rendered_args, source, generation)
           VALUES (%s,%s,'tcp/443','control',NULL,NULL,NULL,%s,%s,'manual',0)""",
        (gid, profile, json.dumps({"lines": lines}, ensure_ascii=False), rendered),
    )
    return gid


def record_experiment(conn, environment_id, genome_id, domain_id, success, bytes_, latency_ms, ban_suspected=False):
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO experiments
           (genome_id, environment_id, domain_id, success, bytes, latency_ms, ban_suspected)
           VALUES (%s,%s,%s,%s,%s,%s,%s)""",
        (genome_id, environment_id, domain_id, success, bytes_, latency_ms, ban_suspected),
    )


def upsert_genome_score(conn, environment_id, genome_id, success: bool, reward: float):
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO genome_scores (genome_id, environment_id, pulls, successes, total_reward)
           VALUES (%s,%s,1,%s,%s)
           ON DUPLICATE KEY UPDATE
             pulls = pulls + 1,
             successes = successes + %s,
             total_reward = total_reward + %s""",
        (genome_id, environment_id, int(success), reward, int(success), reward),
    )


def upsert_operator_stat(conn, environment_id, filter_type, operator, reward):
    if not operator:
        return
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO operator_stats (environment_id, filter_type, operator, pulls, total_reward)
           VALUES (%s,%s,%s,1,%s)
           ON DUPLICATE KEY UPDATE pulls = pulls + 1, total_reward = total_reward + %s""",
        (environment_id, filter_type, operator, reward, reward),
    )


def get_operator_stats(conn, environment_id, filter_type):
    cur = conn.cursor(dictionary=True)
    cur.execute(
        "SELECT operator, pulls, total_reward FROM operator_stats WHERE environment_id=%s AND filter_type=%s",
        (environment_id, filter_type),
    )
    return {row["operator"]: row for row in cur.fetchall()}


def get_genomes_with_scores(conn, profile: str, environment_id: int):
    cur = conn.cursor(dictionary=True)
    cur.execute(
        """SELECT g.params_json, g.generation, gs.pulls, gs.total_reward
           FROM genome_scores gs
           JOIN genomes g ON g.id = gs.genome_id AND g.profile = %s
           WHERE gs.environment_id = %s AND gs.pulls > 0 AND g.family != 'control'""",
        (profile, environment_id),
    )
    return cur.fetchall()


def recent_domain_experiments(conn, domain_id, limit=5):
    cur = conn.cursor(dictionary=True)
    cur.execute(
        """SELECT genome_id, success FROM experiments
           WHERE domain_id=%s ORDER BY tested_at DESC LIMIT %s""",
        (domain_id, limit),
    )
    return cur.fetchall()


def log_ban_event(conn, environment_id, domain_id, reason, cooldown_seconds):
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO ban_events (environment_id, domain_id, trigger_reason, cooldown_until)
           VALUES (%s,%s,%s, NOW() + INTERVAL %s SECOND)""",
        (environment_id, domain_id, reason, cooldown_seconds),
    )


def sync_pull(conn, profile: str, min_pulls: int = 3, limit: int = 20):
    """Топ геномов профиля ПО ВСЕМ окружениям (включая другие ноды) --
    возвращается ноде-клиенту как кандидаты для локального затравливания
    UCB (genome.from_params + source='sync_import'). Только 100%-успешные
    с достаточным числом прогонов -- не тащим на другую ноду то, что даже
    у автора работало нестабильно."""
    cur = conn.cursor(dictionary=True)
    cur.execute(
        """SELECT g.id, g.profile, g.params_json, g.rendered_args, g.generation,
                  SUM(gs.pulls) AS pulls, SUM(gs.successes) AS successes,
                  ROUND(SUM(gs.total_reward) / NULLIF(SUM(gs.pulls), 0), 3) AS avg_score
           FROM genome_scores gs
           JOIN genomes g ON g.id = gs.genome_id AND g.profile = %s
           WHERE g.family != 'control'
           GROUP BY g.id, g.profile, g.params_json, g.rendered_args, g.generation
           HAVING pulls >= %s AND successes = pulls
           ORDER BY avg_score DESC, pulls DESC
           LIMIT %s""",
        (profile, min_pulls, limit),
    )
    return cur.fetchall()
