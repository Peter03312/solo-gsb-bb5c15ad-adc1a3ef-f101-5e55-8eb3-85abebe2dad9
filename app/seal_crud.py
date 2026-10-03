"""责任证据封存的写入 / 查询事务。

封存（``seal_evidence``）在**单事务**内完成，并基于一个**一致的已提交数据
视图**：PostgreSQL 下先把事务提升为 REPEATABLE READ（快照在首条 SQL 时建立，
整个事务看到同一已提交版本），再与事件 / 表态 / 车载读数写入共用同一把货物
行锁（``SELECT ... FOR UPDATE``）——故封存所见必为持锁期间的最新时间线，
且封存持锁期间不会有任何能改变该货物重建结果的写入混入（SQLite 内存库
单连接天然一致）。

校验（全部在持锁事务内，失败整事务回滚、不留任何封存记录）
--------------------------------------------------------
* 货物不存在 → ``404 cargo_not_found``；
* 区间不存在 → ``404 interval_not_found``；区间仍开放 → ``422 interval_open``；
* 请求快照标识与区间当前快照不一致（区间边界 / 责任片段已变）→
  ``409 stale_snapshot``（“快照已变时拒绝且不留记录”）。

幂等 / 冲突
-----------
* 同案件编号、同请求（货物编号 + 区间标识 + 快照标识全相同）重传：即使现况
  已变（区间此后消失 / 重开 / 快照变化）也**返回原封存包**（``200 replayed``）；
* 同案件编号、异请求 → ``409 seal_case_conflict``；
* 案件编号不存在的查询 → ``404 seal_not_found``。

封存行（``evidence_seal``）只追加、永不更新；内容摘要与规范化 JSON 一并
落库，查询 / 对照均以封存行中的原内容为准，后续晚到记录与更正不得改写原包。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

from app import sealing, timeline
from app.crud import ApiError, _load_cargo_locked
from app.models import Cargo, EvidenceSeal, Trip
from app.schemas import SealCreate


def _conflict(message: str, code: str) -> ApiError:
    return ApiError(409, code, message)


def _unprocessable(message: str, code: str) -> ApiError:
    return ApiError(422, code, message)


@dataclass
class SealResult:
    seal: EvidenceSeal
    payload: dict
    replayed: bool


# ----------------------------------------------------------- 上下文读取


def _load_context(db, cargo):
    """读齐重建所需上下文并完成 UTC / Decimal 归一化。

    返回 (events, loadings, readings, trips_by_id, effective)。readings 为该货物
    全部装卸窗口所属车次上的车载读数（与 timeline.load_cargo_context 同口径）。
    """
    events, loadings, readings, effective = timeline.load_cargo_context(
        db, cargo.cargo_id
    )
    trip_ids = {l.trip_id for l in loadings}
    trips_by_id = {
        t.trip_id: t
        for t in db.scalars(
            select(Trip).where(Trip.trip_id.in_(sorted(trip_ids) or [""]))
        ).all()
    }
    return events, loadings, readings, trips_by_id, effective


def _rebuild_closed(cargo, events, loadings, readings, effective):
    """在给定上下文（同一快照）上重建保管链，返回 (chain结果, 闭合区间映射)。"""
    from app import chain
    from app.chain import interval_id as iid

    points = timeline.adopted_timeline(
        events, readings, loadings, effective[0], effective[1]
    )
    result = chain.rebuild_chain(
        cargo.initial_holder, Decimal(cargo.temp_upper_limit), points
    )
    closed = {iid(ex): ex for ex in result.excursions if not ex.is_open}
    return result, closed


def _resolve_closed(cargo, interval_id, events, loadings, readings, effective):
    """在当前合并时间线上定位已闭合区间：不存在 404；仍开放 422。"""
    from app.chain import interval_id as iid

    result, closed = _rebuild_closed(
        cargo, events, loadings, readings, effective
    )
    ex = closed.get(interval_id)
    if ex is not None:
        return ex
    if any(iid(e) == interval_id for e in result.excursions):
        raise _unprocessable(
            f"区间 {interval_id} 已重新开放（开放区间不得封存责任证据）",
            code="interval_open",
        )
    raise ApiError(
        404,
        "interval_not_found",
        f"货物 {cargo.cargo_id} 不存在标识为 {interval_id} 的已闭合越温区间",
    )


# ----------------------------------------------------------- 封存


def _same_request(existing: EvidenceSeal, payload: SealCreate) -> bool:
    return (
        existing.cargo_id == payload.cargo_id
        and existing.interval_id == payload.interval_id
        and existing.snapshot_id == payload.snapshot_id
    )


def seal_evidence(db, payload: SealCreate) -> SealResult:
    try:
        return _seal_tx(db, payload)
    except OperationalError:
        # SQLite 文件库锁等待耗尽 / PG 死锁检测中止：整事务重放，语义不变
        db.rollback()
        return _seal_tx(db, payload)


def _seal_tx(db, payload: SealCreate) -> SealResult:
    # 一致的已提交视图：PG 下 REPEATABLE READ，必须在本事务首条 SQL 前指定
    # （快照在首条查询时建立，整个事务看到同一已提交版本）；幂等快查、区间
    # 重建与插入因此共用同一快照。SQLite 内存库 StaticPool 单连接天然一致。
    bind = db.get_bind()
    if bind.dialect.name == "postgresql":
        db.connection(execution_options={"isolation_level": "REPEATABLE READ"})

    # 幂等优先：案件编号已存在时，同请求直接返回原包（即使现况已变），
    # 异请求冲突——此时不读当前数据、不做任何写入。
    existing = db.get(EvidenceSeal, payload.case_id)
    if existing is not None:
        if not _same_request(existing, payload):
            db.rollback()
            raise _conflict(
                f"案件编号 {payload.case_id} 已封存其他证据"
                "（货物 / 区间 / 快照标识不同），拒绝覆盖；请使用新的案件编号",
                code="seal_case_conflict",
            )
        db.rollback()
        return SealResult(
            seal=existing,
            payload=decode_payload(existing),
            replayed=True,
        )

    try:
        # 与事件 / 表态 / 读数 / 更正 / 装卸写入串行：锁货物行
        cargo = _load_cargo_locked(db, payload.cargo_id)

        # 持锁复核幂等（并发首传竞争）
        winner = db.get(EvidenceSeal, payload.case_id)
        if winner is not None:
            if not _same_request(winner, payload):
                raise _conflict(
                    f"案件编号 {payload.case_id} 已封存其他证据"
                    "（货物 / 区间 / 快照不同），拒绝覆盖；请使用新的案件编号",
                    code="seal_case_conflict",
                )
            db.rollback()
            return SealResult(
                seal=winner, payload=decode_payload(winner), replayed=True
            )

        # 在同一快照上一次性读齐重建 / 组包上下文，再定位区间与校验快照
        events, loadings, readings, trips_by_id, effective = _load_context(
            db, cargo
        )
        ex = _resolve_closed(
            cargo, payload.interval_id, events, loadings, readings, effective
        )
        from app import responsibility

        current_snapshot = responsibility.snapshot_id(ex)
        if payload.snapshot_id != current_snapshot:
            raise _conflict(
                f"快照已变化：请求快照 {payload.snapshot_id} 与当前快照 "
                f"{current_snapshot} 不一致（区间边界或责任片段可能已因晚到记录 / "
                "更正 / 装卸窗口更正变化），拒绝封存且不留记录",
                code="stale_snapshot",
            )

        from app.stance_crud import load_interval_stances

        stance_rows = load_interval_stances(
            db, cargo.cargo_id, payload.interval_id
        )
        doc = sealing.build_sealed_payload(
            case_id=payload.case_id,
            cargo=cargo,
            excursion=ex,
            stance_rows=stance_rows,
            events=events,
            readings=readings,
            loadings=loadings,
            trips_by_id=trips_by_id,
            effective=effective,
        )
        canonical, digest = sealing.content_digest_of(doc)

        seal = EvidenceSeal(
            case_id=payload.case_id,
            cargo_id=payload.cargo_id,
            interval_id=payload.interval_id,
            snapshot_id=current_snapshot,
            content_digest=digest,
            payload=canonical,
        )
        db.add(seal)
        db.commit()
    except ApiError:
        db.rollback()
        raise
    except IntegrityError:
        db.rollback()
        winner = db.get(EvidenceSeal, payload.case_id)
        if winner is not None:
            if _same_request(winner, payload):
                return SealResult(
                    seal=winner, payload=decode_payload(winner), replayed=True
                )
            raise _conflict(
                f"案件编号 {payload.case_id} 已封存其他证据，拒绝覆盖",
                code="seal_case_conflict",
            )
        raise ApiError(
            409, "integrity_conflict", f"封存 {payload.case_id} 与既有数据冲突"
        )

    return SealResult(seal=seal, payload=doc, replayed=False)


# ----------------------------------------------------------- 查询


def decode_payload(seal: EvidenceSeal) -> dict:
    """封存行中的规范化 JSON -> dict（原内容，永不改写）。"""
    import json

    return json.loads(seal.payload)


def get_seal(db, case_id: str) -> SealResult:
    _use_consistent_snapshot(db)
    seal = db.get(EvidenceSeal, case_id)
    if seal is None:
        raise ApiError(404, "seal_not_found", f"案件 {case_id} 的封存包不存在")
    return SealResult(seal=seal, payload=decode_payload(seal), replayed=False)


def _use_consistent_snapshot(db) -> None:
    """只读查询提升为一致的已提交视图（须在该事务首条 SQL 前调用）。

    PG 下 REPEATABLE READ：整个请求看到同一已提交版本，封存包读取与现况重建
    不会混入查询期间的并发提交；SQLite 内存库单连接天然一致。
    """
    bind = db.get_bind()
    if bind.dialect.name == "postgresql":
        db.connection(execution_options={"isolation_level": "REPEATABLE READ"})


def get_current_view(db, seal: EvidenceSeal) -> dict:
    """按当前数据重建与封存包同构的现况视图（只读，不影响原包）。"""
    cargo = db.get(Cargo, seal.cargo_id)
    if cargo is None:
        # 货物无删除路径；防御性处理为“区间已消失”，原包仍可查
        raise ApiError(404, "cargo_not_found", f"货物 {seal.cargo_id} 不存在")
    from app.timeutil import ensure_utc

    cargo.started_at = ensure_utc(cargo.started_at)
    events, loadings, readings, trips_by_id, effective = _load_context(db, cargo)
    from app.stance_crud import load_interval_stances

    return sealing.build_current_view(
        seal.interval_id,
        cargo=cargo,
        events=events,
        readings=readings,
        loadings=loadings,
        trips_by_id=trips_by_id,
        effective=effective,
        stance_loader=lambda iid: load_interval_stances(db, cargo.cargo_id, iid),
    )
