"""责任证据封存包的构造、内容摘要与现况对照（纯逻辑，便于单测）。

封存包（payload）
-----------------
封存发生在一致的已提交数据视图中（见 app/seal_crud.py），把“重建该已闭合
越温区间所用的全部证据”固化为一个**规范化 JSON**文档：

* ``interval``：区间起止、开启 / 闭合读数来源坐标与**责任片段**；
* ``stances``：封存时各责任方的**当时有效表态**（每责任方取其最新一条对当时
  快照仍有效的表态，含确认 / 异议、理由、表态编号、提交时间、所证片段）；
* ``own_events``：该货物全部自有事件（测温含**原始温度**与当前**有效温度 /
  版本**；交接含交出 / 接收方）；
* ``vehicle_readings``：相关车次上的全部车载读数（原始温度、当前有效温度 /
  版本），并对每个读数标记其对该货物是否适用（``applicable``：读数时刻在车，
  左闭右开）与是否被采用（``adopted``：在车且同刻无自有测温）；
* ``loading_windows``：该货物全部车次的**当前装卸窗口**（当前时刻、稳定身份
  与窗口更正版本）；
* ``trips``：相关车次快照（编号、车牌、备注、**修订号**）；
* ``cargo``：货物基线（编号、起始时间、温度上限、起始保管方）。

规范化 JSON 为单行、无多余空白、键固定按字典序排列；时间统一 UTC
（``YYYY-MM-DDTHH:MM:SSZ``），温度统一量化到千分位的十进制字符串。
内容摘要：

    content_digest = "sha256:" + hex(sha256(规范化 JSON))

现况对照（compare）
-------------------
封存包不可变，对照只是**只读**地拿封存内容与当前重建结果比对：

* 区间当前状态：``sealed``（仍存在且已闭合）/ ``disappeared``（已消失）/
  ``reopened``（仍存在但已开放）；
* 责任片段按「起点 + 责任方」为稳定键做集合差异，输出新增 / 删除 / 变更；
* 证据项（自有事件、车载读数、装卸窗口、车次、有效表态）按各自稳定键做
  added / removed / changed（changed 给出字段级 before/after）。

封存的原始证据均为 append-only（原始测温 / 读数永不修改、窗口只就地平移），
故晚到记录通常只产生 added / changed（有效温度、版本、当前窗口、表态有效性
等），不会删除原包任何内容；removed 保留用于完整表达集合差异。
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal

from app import chain, responsibility, timeline
from app.chain import interval_id
from app.timeutil import ensure_utc

_TEMP_SCALE = Decimal("0.001")


# ----------------------------------------------------------- 规范化基元


def ts(dt) -> str | None:
    """UTC 秒级 ISO 8601，Z 后缀（None 透传，用于未闭合 / 可空字段）。"""
    if dt is None:
        return None
    return ensure_utc(dt).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def temp(value) -> str | None:
    """温度指纹 / 存储形式：量化到千分位的十进制字符串（None 透传）。"""
    if value is None:
        return None
    return str(Decimal(value).quantize(_TEMP_SCALE))


def canonical_json(payload) -> str:
    """规范化 JSON：单行、键按字典序、ensure_ascii=False（保留中文责任方名）。"""
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )


def content_digest_of(payload) -> tuple[str, str]:
    """返回 (规范化 JSON 字符串, 其内容摘要)。"""
    canonical = canonical_json(payload)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return canonical, f"sha256:{digest}"


def _segment_triple(seg) -> dict:
    return {
        "start": ts(seg.start),
        "end": ts(seg.end),
        "responsible_party": seg.party,
    }


# ----------------------------------------------------------- 封存包构造


def _interval_doc(ex) -> dict:
    return {
        "interval_id": interval_id(ex),
        "start": ts(ex.start),
        "end": ts(ex.end),
        "start_source": ex.start_source,
        "end_source": ex.end_source,
        "start_event_id": ex.start_event_id,
        "end_event_id": ex.end_event_id,
        "start_reading_id": ex.start_reading_id,
        "end_reading_id": ex.end_reading_id,
        "start_trip_id": ex.start_trip_id,
        "end_trip_id": ex.end_trip_id,
        "snapshot_id": responsibility.snapshot_id(ex),
        "segments": [_segment_triple(s) for s in ex.segments],
    }


def _stance_doc(stance, *, superseded: bool, segments) -> dict:
    kind = stance.kind.name if hasattr(stance.kind, "name") else str(stance.kind)
    return {
        "stance_id": stance.stance_id,
        "responsible_party": stance.responsible_party,
        "stance": kind,
        "reason": stance.reason,
        "created_at": ts(stance.created_at),
        "snapshot_id": stance.snapshot_id,
        "is_current": True,       # 封存内容只收当时有效的最新表态
        "superseded": superseded,
        "segments": [
            {"start": ts(s.start), "end": ts(s.end), "responsible_party": s.party}
            for s in segments
        ],
    }


def _effective_stance_docs(ex, stance_rows) -> list[dict]:
    """各责任方封存时的“最新一条有效表态”（按片段顺序、再按表态时间稳定输出）。"""
    latest_by_party: dict[str, object] = {}
    for s in stance_rows:
        if not responsibility.is_stance_current(s, ex):
            continue
        prev = latest_by_party.get(s.responsible_party)
        if prev is None or (s.created_at, s.stance_id) > (
            prev.created_at,
            prev.stance_id,
        ):
            latest_by_party[s.responsible_party] = s
    docs = []
    for seg in ex.segments:
        s = latest_by_party.get(seg.party)
        if s is None or any(d["stance_id"] == s.stance_id for d in docs):
            continue
        party_segments = [x for x in ex.segments if x.party == s.responsible_party]
        # 同责任方更新的有效表态是否存在（最新一条之外的更早有效表态）：封存时恒 False
        docs.append(_stance_doc(s, superseded=False, segments=party_segments))
    return docs


def _own_event_docs(events, event_effective: dict) -> list[dict]:
    docs = []
    for e in events:
        is_measurement = e.event_type.name == "measurement"
        eff = event_effective.get(e.event_id)
        doc = {
            "event_id": e.event_id,
            "event_type": e.event_type.name,
            "occurred_at": ts(e.occurred_at),
        }
        if is_measurement:
            doc.update(
                temperature=temp(e.temperature),
                effective_temperature=temp(eff[0] if eff else e.temperature),
                version=eff[1] if eff else 0,
                giver=None,
                receiver=None,
            )
        else:
            doc.update(
                temperature=None,
                effective_temperature=None,
                version=0,
                giver=e.giver,
                receiver=e.receiver,
            )
        docs.append(doc)
    return docs


def _reading_doc(r, applicable: bool, adopted: bool, eff) -> dict:
    return {
        "reading_id": r.reading_id,
        "trip_id": r.trip_id,
        "read_at": ts(r.read_at),
        "temperature": temp(r.temperature),
        "effective_temperature": temp(eff[0] if eff else r.temperature),
        "version": eff[1] if eff else 0,
        "applicable": applicable,
        "adopted": adopted,
    }


def _loading_doc(l) -> dict:
    return {
        "trip_id": l.trip_id,
        "cargo_id": l.cargo_id,
        "origin_loaded_at": ts(l.origin_loaded_at),
        "origin_unloaded_at": ts(l.origin_unloaded_at),
        "loaded_at": ts(l.loaded_at),
        "unloaded_at": ts(l.unloaded_at),
        "window_version": l.window_version or 0,
    }


def _trip_docs(trips_by_id: dict, trip_ids) -> list[dict]:
    return [
        {
            "trip_id": t.trip_id,
            "vehicle_no": t.vehicle_no,
            "note": t.note,
            "revision": t.revision or 0,
        }
        for t in (trips_by_id.get(tid) for tid in sorted(trip_ids))
        if t is not None
    ]


def build_sealed_payload(
    *,
    case_id: str,
    cargo,
    excursion,
    stance_rows,
    events,
    readings,
    loadings,
    trips_by_id,
    effective,
) -> dict:
    """在一致视图中组装封存包（调用方已完成时间 / Decimal 归一化）。

    ``readings`` 为该货物相关车次（其全部装卸窗口所属车次）的车载读数；
    ``trips_by_id`` 为这些车次的 ORM 行；``effective`` 为
    ``(event_effective, reading_effective)`` 更正有效值映射。
    """
    event_effective, reading_effective = effective
    own_measure_at = {
        e.occurred_at
        for e in events
        if e.event_type.name == "measurement"
    }
    applicable_trip_ids = set()
    reading_docs = []
    for r in readings:
        applicable = any(
            timeline.is_reading_applicable(l, r) for l in loadings
        )
        if applicable:
            applicable_trip_ids.add(r.trip_id)
        reading_docs.append(
            _reading_doc(
                r,
                applicable=applicable,
                adopted=bool(applicable and r.read_at not in own_measure_at),
                eff=reading_effective.get(r.reading_id),
            )
        )
    trip_ids = {l.trip_id for l in loadings} | applicable_trip_ids
    return {
        "case_payload_version": 1,
        "case_id": case_id,
        "cargo": {
            "cargo_id": cargo.cargo_id,
            "started_at": ts(cargo.started_at),
            "temp_upper_limit": temp(cargo.temp_upper_limit),
            "initial_holder": cargo.initial_holder,
        },
        "interval": _interval_doc(excursion),
        "stances": _effective_stance_docs(excursion, stance_rows),
        "own_events": _own_event_docs(events, event_effective),
        "vehicle_readings": sorted(
            reading_docs, key=lambda d: (d["read_at"], d["reading_id"])
        ),
        "loading_windows": sorted(
            (_loading_doc(l) for l in loadings),
            key=lambda d: (d["loaded_at"], d["trip_id"], d["origin_loaded_at"]),
        ),
        "trips": _trip_docs(trips_by_id, trip_ids),
    }


# ----------------------------------------------------------- 现况快照（与封存包同构的证据集）


def build_current_view(
    sealed_interval_id: str,
    *,
    cargo,
    events,
    readings,
    loadings,
    trips_by_id,
    effective,
    stance_loader,
) -> dict:
    """按当前数据重建“与封存包同构”的现况视图。

    ``stance_loader(interval_id)`` 返回该区间标识下的全部表态行（含历史）。
    返回字典包含：``status``（sealed / disappeared / reopened）、``interval``
    （已闭合区间文档或 None）、以及与封存包同名的各证据集
    （``stances`` 为当前各责任方最新有效表态；区间不存在 / 开放时为空）。
    """
    points = timeline.adopted_timeline(
        events, readings, loadings, effective[0], effective[1]
    )
    result = chain.rebuild_chain(
        cargo.initial_holder, Decimal(cargo.temp_upper_limit), points
    )
    closed = {interval_id(ex): ex for ex in result.excursions if not ex.is_open}
    ex = closed.get(sealed_interval_id)
    open_ex = next(
        (e for e in result.excursions if interval_id(e) == sealed_interval_id),
        None,
    )
    if ex is not None:
        status, current_ex = "sealed", ex
    elif open_ex is not None:
        status, current_ex = "reopened", None
    else:
        status, current_ex = "disappeared", None

    own_measure_at = {
        e.occurred_at
        for e in events
        if e.event_type.name == "measurement"
    }
    reading_docs = []
    for r in readings:
        applicable = any(
            timeline.is_reading_applicable(l, r) for l in loadings
        )
        eff = effective[1].get(r.reading_id)
        reading_docs.append(
            _reading_doc(
                r,
                applicable=applicable,
                adopted=bool(applicable and r.read_at not in own_measure_at),
                eff=eff,
            )
        )
    trip_ids = {l.trip_id for l in loadings}
    stance_docs = []
    if current_ex is not None:
        rows = stance_loader(sealed_interval_id)
        latest_by_party: dict[str, object] = {}
        for s in rows:
            if not responsibility.is_stance_current(s, current_ex):
                continue
            prev = latest_by_party.get(s.responsible_party)
            if prev is None or (s.created_at, s.stance_id) > (
                prev.created_at,
                prev.stance_id,
            ):
                latest_by_party[s.responsible_party] = s
        current_rows = [
            s for s in rows if responsibility.is_stance_current(s, current_ex)
        ]
        for seg in current_ex.segments:
            s = latest_by_party.get(seg.party)
            if s is None or any(d["stance_id"] == s.stance_id for d in stance_docs):
                continue
            # 被同责任方更新的有效表态改口：另有一条同责任方有效表态比它更新
            superseded = any(
                x.stance_id != s.stance_id
                and x.responsible_party == s.responsible_party
                and (x.created_at, x.stance_id) > (s.created_at, s.stance_id)
                for x in current_rows
            )
            party_segments = [
                x for x in current_ex.segments if x.party == s.responsible_party
            ]
            stance_docs.append(
                _stance_doc(s, superseded=superseded, segments=party_segments)
            )
    return {
        "status": status,
        "interval": _interval_doc(current_ex) if current_ex is not None else None,
        "snapshot_id": (
            responsibility.snapshot_id(current_ex) if current_ex is not None else None
        ),
        "cargo": {
            "cargo_id": cargo.cargo_id,
            "started_at": ts(cargo.started_at),
            "temp_upper_limit": temp(cargo.temp_upper_limit),
            "initial_holder": cargo.initial_holder,
        },
        "stances": stance_docs,
        "own_events": _own_event_docs(events, effective[0]),
        "vehicle_readings": sorted(
            reading_docs, key=lambda d: (d["read_at"], d["reading_id"])
        ),
        "loading_windows": sorted(
            (_loading_doc(l) for l in loadings),
            key=lambda d: (d["loaded_at"], d["trip_id"], d["origin_loaded_at"]),
        ),
        "trips": _trip_docs(trips_by_id, trip_ids),
    }


# ----------------------------------------------------------- 对照差异


# 证据集 -> (稳定键字段, 参与字段级比对的字段)
_EVIDENCE_SPECS = {
    "own_event": ("event_id", (
        "event_type", "occurred_at", "temperature", "effective_temperature",
        "version", "giver", "receiver",
    )),
    "vehicle_reading": ("reading_id", (
        "trip_id", "read_at", "temperature", "effective_temperature",
        "version", "applicable", "adopted",
    )),
    "loading_window": (
        ("trip_id", "cargo_id", "origin_loaded_at"),
        (
            "origin_unloaded_at", "loaded_at", "unloaded_at", "window_version",
        ),
    ),
    "trip": ("trip_id", ("vehicle_no", "note", "revision")),
    "stance": ("stance_id", (
        "responsible_party", "stance", "reason", "created_at", "snapshot_id",
        "is_current", "superseded", "segments",
    )),
}

# 封存包字段名 -> 证据类型
_SECTION_KIND = {
    "own_events": "own_event",
    "vehicle_readings": "vehicle_reading",
    "loading_windows": "loading_window",
    "trips": "trip",
    "stances": "stance",
}


def _key_value(item, key_fields):
    if isinstance(key_fields, tuple):
        return tuple(item[k] for k in key_fields)
    return item[key_fields]


def _key_label(key_fields, key_value) -> dict:
    if isinstance(key_fields, tuple):
        return dict(zip(key_fields, key_value))
    return {key_fields: key_value}


def _evidence_diff(sealed_items, current_items, kind):
    key_fields, compare_fields = _EVIDENCE_SPECS[kind]
    sealed_map = {_key_value(d, key_fields): d for d in sealed_items}
    current_map = {_key_value(d, key_fields): d for d in current_items}
    added, removed, changed = [], [], []
    for key in sorted(current_map, key=str):
        if key not in sealed_map:
            added.append(_key_label(key_fields, key))
    for key in sorted(sealed_map, key=str):
        if key not in current_map:
            removed.append(_key_label(key_fields, key))
            continue
        old, new = sealed_map[key], current_map[key]
        fields = {}
        for f in compare_fields:
            if old.get(f) != new.get(f):
                fields[f] = {"before": old.get(f), "after": new.get(f)}
        if fields:
            changed.append({"key": _key_label(key_fields, key), "fields": fields})
    return added, removed, changed


_SEGMENT_KEY = ("start", "responsible_party")


def _segment_diff(sealed_segments, current_segments):
    """责任片段以 (起点, 责任方) 为稳定键做差异；end 变化计为 changed。"""
    def map_of(segs):
        return {tuple(s[k] for k in _SEGMENT_KEY): s for s in segs}

    old_map, new_map = map_of(sealed_segments), map_of(current_segments)
    added = [s for k, s in sorted(new_map.items()) if k not in old_map]
    removed = [s for k, s in sorted(old_map.items()) if k not in new_map]
    changed = []
    for k in sorted(old_map, key=str):
        if k in new_map and old_map[k].get("end") != new_map[k].get("end"):
            changed.append(
                {
                    "key": dict(zip(_SEGMENT_KEY, k)),
                    "before": old_map[k],
                    "after": new_map[k],
                }
            )
    return added, removed, changed


def compare_sealed(sealed_payload: dict, current: dict) -> dict:
    """封存包 vs 现况视图 -> 对照结果（纯函数，不触碰原包）。"""
    sealed_interval = sealed_payload["interval"]
    status = current["status"]

    seg_added = seg_removed = seg_changed = []
    if status == "sealed":
        seg_added, seg_removed, seg_changed = _segment_diff(
            sealed_interval["segments"], current["interval"]["segments"]
        )
    else:
        # 区间已消失 / 重开：封存的责任片段在当前视图中全部不再成立
        seg_removed = list(sealed_interval["segments"])

    evidence = {}
    for section, kind in _SECTION_KIND.items():
        added, removed, changed = _evidence_diff(
            sealed_payload.get(section, []), current.get(section, []), kind
        )
        evidence[section] = {
            "added": added,
            "removed": removed,
            "changed": changed,
        }

    snapshot_changed = (
        status == "sealed"
        and sealed_interval["snapshot_id"] != current["snapshot_id"]
    )
    matches_current = (
        status == "sealed"
        and not snapshot_changed
        and not (seg_added or seg_removed or seg_changed)
        and not any(
            v["added"] or v["removed"] or v["changed"]
            for v in evidence.values()
        )
    )
    return {
        "case_id": sealed_payload.get("case_id"),
        "cargo_id": sealed_payload["cargo"]["cargo_id"],
        "interval_id": sealed_interval["interval_id"],
        "sealed_snapshot_id": sealed_interval["snapshot_id"],
        "current_snapshot_id": current["snapshot_id"],
        "snapshot_changed": snapshot_changed,
        "interval_status": status,
        "matches_current": matches_current,
        "segments": {
            "added": seg_added,
            "removed": seg_removed,
            "changed": seg_changed,
        },
        "evidence": evidence,
    }
