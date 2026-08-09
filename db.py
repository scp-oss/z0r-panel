"""Панель читает/пишет ТУ ЖЕ БД, что и локальный orchestrator (см.
config.py) -- отдельной схемы для панели нет. Часть функций здесь --
дубликаты запросов из orchestrator/db.py в форме, удобной для страниц
(dictionary-курсоры, агрегаты для таблиц), часть -- новые: реестр нод
(токены) и sync push/pull.
"""
import hashlib
import json
import secrets

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


def create_node(conn, name: str, provider: str) -> str:
    """Создаёт (или перевыпускает токен для уже существующей) ноду.
    Токен показывается ОДИН раз в UI в момент создания -- в БД лежит
    только его sha256, как пароль."""
    token = secrets.token_urlsafe(32)
    token_hash = hash_token(token)
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO environments (name, provider, api_token_hash)
           VALUES (%s, %s, %s) AS new
           ON DUPLICATE KEY UPDATE provider=new.provider, api_token_hash=new.api_token_hash""",
        (name, provider, token_hash),
    )
    return token


def get_environment_by_token(conn, token: str):
    cur = conn.cursor(dictionary=True)
    cur.execute(
        "SELECT id, name, provider FROM environments WHERE api_token_hash=%s AND active=1",
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
        """SELECT e.id, e.name, e.provider, e.is_production, e.active,
                  e.api_token_hash IS NOT NULL AS is_remote_node,
                  e.last_sync_at, e.created_at,
                  COUNT(DISTINCT gs.genome_id) AS genome_count,
                  COALESCE(SUM(gs.pulls), 0) AS total_pulls
           FROM environments e
           LEFT JOIN genome_scores gs ON gs.environment_id = e.id
           GROUP BY e.id
           ORDER BY e.is_production DESC, e.name""",
    )
    return cur.fetchall()


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
