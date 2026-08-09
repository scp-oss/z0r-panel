"""HTTP API для удалённых нод (другие провайдеры) -- push своего снапшота
genomes+genome_scores, pull агрегированного топа по профилю со всех нод.
Авторизация -- Bearer-токен ноды (см. auth.require_node), НЕ человеческая
сессия панели. См. orchestrator/sync_client.py -- клиент, который сюда
стучится."""
from fastapi import APIRouter, Depends
from pydantic import BaseModel

import auth
import db

router = APIRouter(prefix="/api/v1/sync", tags=["sync"])


class GenomeIn(BaseModel):
    id: str
    profile: str
    filter_type: str
    family: str
    fooling: str | None = None
    ttl_mode: str | None = None
    fake_payload: str | None = None
    params_json: dict
    rendered_args: str
    source: str
    parent1_id: str | None = None
    parent2_id: str | None = None
    mutation_op: str | None = None
    generation: int = 0


class ScoreIn(BaseModel):
    genome_id: str
    pulls: int
    successes: int
    total_reward: float


class PushRequest(BaseModel):
    genomes: list[GenomeIn] = []
    scores: list[ScoreIn] = []


@router.post("/push")
def push(body: PushRequest, node: dict = Depends(auth.require_node)):
    conn = db.connect()
    try:
        result = db.sync_push(
            conn,
            node["id"],
            [g.model_dump() for g in body.genomes],
            [s.model_dump() for s in body.scores],
        )
    finally:
        conn.close()
    return {"ok": True, "node": node["name"], **result}


@router.get("/pull")
def pull(profile: str, min_pulls: int = 3, limit: int = 20, node: dict = Depends(auth.require_node)):
    conn = db.connect()
    try:
        rows = db.sync_pull(conn, profile, min_pulls=min_pulls, limit=limit)
    finally:
        conn.close()
    for row in rows:
        if isinstance(row.get("params_json"), str):
            import json
            row["params_json"] = json.loads(row["params_json"])
    return {"ok": True, "profile": profile, "candidates": rows}


@router.get("/bootstrap")
def bootstrap(profile: str, min_pulls: int = 3, limit: int = 10, node: dict = Depends(auth.require_node)):
    """Для СВЕЖЕЙ ноды перед первым прогоном main.py -- см.
    orchestrator/bootstrap.py. node['provider'] берётся из токена самой
    ноды (см. auth.require_node), не из query-параметра -- нода не может
    запросить чужой provider-tier, представившись кем-то другим."""
    conn = db.connect()
    try:
        rows = db.bootstrap_candidates(conn, profile, node["provider"], min_pulls=min_pulls, limit=limit)
    finally:
        conn.close()
    for row in rows:
        if isinstance(row.get("params_json"), str):
            import json
            row["params_json"] = json.loads(row["params_json"])
    return {"ok": True, "profile": profile, "provider": node["provider"], "candidates": rows}
