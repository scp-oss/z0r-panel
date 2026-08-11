"""Realtime DB API для нод в ZENITH_DB_MODE=api -- своей БД у них нет
вообще (см. Zenith/orchestrator/db_api.py -- клиент этого router'а),
каждый вызов orchestrator/db.py на такой ноде превращается в один запрос
сюда. Отдельно от sync_api.py: тот двигает СНАПШОТЫ пачкой (push/pull/
bootstrap, для нод с собственной локальной БД-буфером), этот -- по одной
операции за раз, зеркалируя сигнатуры orchestrator/db.py 1:1, чтобы
db_api.py на стороне ноды мог реализовать тот же интерфейс, что db_local.py.

Авторизация -- тот же Bearer node-токен, что и sync_api.py (см.
auth.require_node). environment_id НИКОГДА не берётся из тела запроса --
только node["id"] из проверенного токена, иначе одна нода могла бы писать
в чужой environment_id, просто подставив его в JSON."""
import json

from fastapi import APIRouter, Depends
from pydantic import BaseModel

import auth
import db

router = APIRouter(prefix="/api/v1/db", tags=["db"])


@router.get("/environment")
def get_environment(node: dict = Depends(auth.require_node)):
    """Заменяет orchestrator/db.py::get_or_create_environment в API-режиме
    -- имя/провайдера нода уже сообщила self-report'ом (см.
    auth.require_node), тут просто возвращаем её собственный id."""
    return {"id": node["id"], "name": node["name"], "provider": node["provider"]}


@router.get("/domains")
def domains(profile: str, node: dict = Depends(auth.require_node)):
    conn = db.connect()
    try:
        rows = db.get_domains_for_profile(conn, profile)
    finally:
        conn.close()
    return {"domains": rows}


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


@router.post("/genomes")
def insert_genome(body: GenomeIn, node: dict = Depends(auth.require_node)):
    conn = db.connect()
    try:
        gid = db.insert_genome(conn, body.model_dump())
    finally:
        conn.close()
    return {"id": gid}


class ControlGenomeIn(BaseModel):
    profile: str
    lines: list[str]


@router.post("/control_genomes")
def insert_control_genome(body: ControlGenomeIn, node: dict = Depends(auth.require_node)):
    conn = db.connect()
    try:
        gid = db.insert_control_genome(conn, body.profile, body.lines)
    finally:
        conn.close()
    return {"id": gid}


class ExperimentIn(BaseModel):
    genome_id: str
    domain_id: int
    success: bool
    bytes_: int
    latency_ms: int | None = None
    ban_suspected: bool = False


@router.post("/experiments")
def record_experiment(body: ExperimentIn, node: dict = Depends(auth.require_node)):
    conn = db.connect()
    try:
        db.record_experiment(
            conn, node["id"], body.genome_id, body.domain_id,
            body.success, body.bytes_, body.latency_ms, body.ban_suspected,
        )
    finally:
        conn.close()
    return {"ok": True}


class ScoreUpdateIn(BaseModel):
    genome_id: str
    success: bool
    reward: float


@router.post("/genome_scores")
def upsert_genome_score(body: ScoreUpdateIn, node: dict = Depends(auth.require_node)):
    conn = db.connect()
    try:
        db.upsert_genome_score(conn, node["id"], body.genome_id, body.success, body.reward)
    finally:
        conn.close()
    return {"ok": True}


@router.get("/genome_scores")
def get_genome_scores(profile: str, node: dict = Depends(auth.require_node)):
    conn = db.connect()
    try:
        rows = db.get_genomes_with_scores(conn, profile, node["id"])
    finally:
        conn.close()
    for row in rows:
        if isinstance(row.get("params_json"), str):
            row["params_json"] = json.loads(row["params_json"])
    return {"rows": rows}


class OperatorStatIn(BaseModel):
    filter_type: str
    operator: str
    reward: float


@router.post("/operator_stats")
def upsert_operator_stat(body: OperatorStatIn, node: dict = Depends(auth.require_node)):
    conn = db.connect()
    try:
        db.upsert_operator_stat(conn, node["id"], body.filter_type, body.operator, body.reward)
    finally:
        conn.close()
    return {"ok": True}


@router.get("/operator_stats")
def get_operator_stats(filter_type: str, node: dict = Depends(auth.require_node)):
    conn = db.connect()
    try:
        stats = db.get_operator_stats(conn, node["id"], filter_type)
    finally:
        conn.close()
    return {"stats": stats}


@router.get("/domain_experiments")
def domain_experiments(domain_id: int, limit: int = 5, node: dict = Depends(auth.require_node)):
    conn = db.connect()
    try:
        rows = db.recent_domain_experiments(conn, domain_id, limit)
    finally:
        conn.close()
    return {"rows": rows}


class BanEventIn(BaseModel):
    domain_id: int
    reason: str
    cooldown_seconds: int


@router.post("/ban_events")
def log_ban_event(body: BanEventIn, node: dict = Depends(auth.require_node)):
    conn = db.connect()
    try:
        db.log_ban_event(conn, node["id"], body.domain_id, body.reason, body.cooldown_seconds)
    finally:
        conn.close()
    return {"ok": True}
