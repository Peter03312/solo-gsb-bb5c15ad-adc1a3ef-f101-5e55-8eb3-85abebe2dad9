"""责任证据封存测试。

覆盖：
* 封存成功：一致已提交视图中保存区间责任片段、当时有效表态、自有事件
  （原始 / 有效温度与版本）、车载读数（有效温度 / 版本 + 适用 / 采用标记）、
  当前装卸窗口（含稳定身份与窗口版本）、车次快照（含修订号），并返回
  规范化内容的 SHA-256 摘要；
* 区间消失拒绝（404 interval_not_found）、区间重开拒绝（422 interval_open）、
  快照已变拒绝（409 stale_snapshot），均不留封存记录；
* 幂等：同案件编号同请求重传即使现况已变也返回原包（200 replayed）；
  同案件编号异请求 409 seal_case_conflict；
* 查询封存包返回原内容与摘要，晚到记录 / 更正不改写原包（摘要不变）；
* 对照现况：责任片段增 / 删 / 变更、证据项增删与字段级前后值，区间消失 /
  重开给出明确状态，后续晚到记录与更正只呈现于对照；
* 车载读数场景：applicable / adopted 标记、测温更正后有效温度 / 版本变更、
  装卸窗口更正后窗口版本与适用性变更。
"""
from fastapi.testclient import TestClient

from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import EvidenceSeal


def _reset():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def setup_function(_):
    _reset()


def _cargo(c, cargo_id="C1", limit="5", holder="A", start="2026-01-01T08:00:00+00:00"):
    r = c.post(
        "/cargo",
        json={
            "cargo_id": cargo_id,
            "started_at": start,
            "temp_upper_limit": limit,
            "initial_holder": holder,
        },
    )
    assert r.status_code == 201, r.text


def _event(c, event_id, cargo_id, at, **kw):
    body = {"event_id": event_id, "cargo_id": cargo_id, "occurred_at": at, **kw}
    r = c.post("/events", json=body)
    assert r.status_code in (200, 201), r.text
    return r


def _closed_interval(c, cargo_id="C1"):
    r = c.get(f"/cargo/{cargo_id}/responsibilities")
    assert r.status_code == 200, r.text
    ivs = r.json()["intervals"]
    assert ivs, "期望至少一个已闭合区间"
    iv = ivs[0]
    return iv["interval_id"], iv["snapshot_id"]


def _seal(c, case_id, cargo_id, interval_id, snapshot_id):
    return c.post(
        "/evidence-seals",
        json={
            "case_id": case_id,
            "cargo_id": cargo_id,
            "interval_id": interval_id,
            "snapshot_id": snapshot_id,
        },
    )


def _seal_count() -> int:
    db = SessionLocal()
    try:
        return db.query(EvidenceSeal).count()
    finally:
        db.close()


def _build_two_segment_case(c, cargo_id="C1"):
    """10:00 超限开启，11:00 A->B 交接，12:00 不超限闭合：两个责任片段。"""
    _cargo(c, cargo_id=cargo_id)
    _event(c, "e1", cargo_id, "2026-01-01T10:00:00+00:00", temperature="8")
    _event(c, "e2", cargo_id, "2026-01-01T11:00:00+00:00", giver="A", receiver="B")
    _event(c, "e3", cargo_id, "2026-01-01T12:00:00+00:00", temperature="3")
    return _closed_interval(c, cargo_id)


# ----------------------------------------------------------- 封存成功与内容


def test_seal_succeeds_and_persists_full_package():
    with TestClient(app) as c:
        iid, snap = _build_two_segment_case(c)
        r = _seal(c, "CASE-1", "C1", iid, snap)
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["case_id"] == "CASE-1"
        assert body["interval_id"] == iid
        assert body["snapshot_id"] == snap
        assert body["content_digest"].startswith("sha256:")

        content = body["content"]
        assert content["case_id"] == "CASE-1"
        assert content["case_payload_version"] == 1
        iv = content["interval"]
        assert iv["interval_id"] == iid
        assert [s["responsible_party"] for s in iv["segments"]] == ["A", "B"]
        assert iv["snapshot_id"] == snap
        # 重建所用自有事件全部固化（原始温度恒为录入值）
        events = {e["event_id"]: e for e in content["own_events"]}
        assert set(events) == {"e1", "e2", "e3"}
        assert events["e1"]["temperature"] == "8.000"
        assert events["e1"]["effective_temperature"] == "8.000"
        assert events["e1"]["version"] == 0
        assert events["e2"]["event_type"] == "handover"
        assert events["e2"]["temperature"] is None
        # 货物基线
        assert content["cargo"] == {
            "cargo_id": "C1",
            "started_at": "2026-01-01T08:00:00Z",
            "temp_upper_limit": "5.000",
            "initial_holder": "A",
        }
        assert _seal_count() == 1


def test_seal_persists_effective_stances_at_seal_time():
    with TestClient(app) as c:
        iid, snap = _build_two_segment_case(c)
        # A 确认；B 异议
        r = c.post(
            f"/cargo/C1/responsibilities/{iid}/stances",
            json={"snapshot_id": snap, "responsible_party": "A",
                  "stance": "confirm", "stance_id": "st-a"},
        )
        assert r.status_code == 201, r.text
        r = c.post(
            f"/cargo/C1/responsibilities/{iid}/stances",
            json={"snapshot_id": snap, "responsible_party": "B",
                  "stance": "dispute", "reason": "未接管", "stance_id": "st-b"},
        )
        assert r.status_code == 201, r.text

        r = _seal(c, "CASE-1", "C1", iid, snap)
        assert r.status_code == 201, r.text
        stances = {s["responsible_party"]: s for s in r.json()["content"]["stances"]}
        assert set(stances) == {"A", "B"}
        assert stances["A"]["stance"] == "confirm"
        assert stances["A"]["is_current"] is True
        assert stances["B"]["stance"] == "dispute"
        assert stances["B"]["reason"] == "未接管"
        assert stances["B"]["segments"][0]["responsible_party"] == "B"


def test_seal_rejects_open_interval_and_missing():
    with TestClient(app) as c:
        _cargo(c)
        _event(c, "e1", "C1", "2026-01-01T10:00:00+00:00", temperature="8")
        # 区间开放
        r = _seal(c, "CASE-1", "C1", "e1", "sha256:" + "0" * 64)
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "interval_open"
        assert _seal_count() == 0
        # 区间不存在
        r = _seal(c, "CASE-2", "C1", "nope", "sha256:" + "0" * 64)
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "interval_not_found"
        assert _seal_count() == 0
        # 货物不存在
        r = _seal(c, "CASE-3", "NOPE", "e1", "sha256:" + "0" * 64)
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "cargo_not_found"


def test_seal_rejects_stale_snapshot_without_record():
    with TestClient(app) as c:
        iid, snap = _build_two_segment_case(c)
        r = _seal(c, "CASE-1", "C1", iid, "sha256:" + "f" * 64)
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "stale_snapshot"
        assert _seal_count() == 0


def test_seal_digest_is_sha256_of_canonical_content():
    import hashlib
    import json

    with TestClient(app) as c:
        iid, snap = _build_two_segment_case(c)
        body = _seal(c, "CASE-1", "C1", iid, snap).json()
        content = body["content"]
        canonical = json.dumps(
            content, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        expect = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        assert body["content_digest"] == expect


# ----------------------------------------------------------- 幂等 / 冲突


def test_same_case_same_request_replays_original_even_when_state_changed():
    with TestClient(app) as c:
        iid, snap = _build_two_segment_case(c)
        first = _seal(c, "CASE-1", "C1", iid, snap)
        assert first.status_code == 201
        digest = first.json()["content_digest"]

        # 封存后让区间消失：把开启测温更正为不超限（版本 0，新温度 2）
        r = c.post(
            "/corrections",
            json={
                "correction_id": "corr-1",
                "target_type": "own_event",
                "target_id": "e1",
                "expected_version": 0,
                "new_temperature": "2",
            },
        )
        assert r.status_code == 201, r.text

        # 区间已消失：新封存应 404 且不留记录
        r = _seal(c, "CASE-NEW", "C1", iid, snap)
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "interval_not_found"
        assert _seal_count() == 1

        # 同案件同请求重传：即使区间已消失仍返回原包
        r = _seal(c, "CASE-1", "C1", iid, snap)
        assert r.status_code == 200
        assert r.json()["replayed"] is True
        assert r.json()["content_digest"] == digest
        assert len(r.json()["content"]["interval"]["segments"]) == 2
        assert _seal_count() == 1


def test_same_case_different_request_conflicts_and_leaves_original():
    with TestClient(app) as c:
        iid, snap = _build_two_segment_case(c)
        first = _seal(c, "CASE-1", "C1", iid, snap)
        assert first.status_code == 201

        # 异请求（快照标识不同）-> 冲突
        r = _seal(c, "CASE-1", "C1", iid, "sha256:" + "a" * 64)
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "seal_case_conflict"
        # 原包不变
        got = c.get("/evidence-seals/CASE-1")
        assert got.json()["content_digest"] == first.json()["content_digest"]
        assert _seal_count() == 1

        # 不同货物 / 区间也属异请求
        r = _seal(c, "CASE-1", "C2", iid, snap)
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "seal_case_conflict"


# ----------------------------------------------------------- 查询原包


def test_get_seal_returns_original_immutable_package():
    with TestClient(app) as c:
        iid, snap = _build_two_segment_case(c)
        first = _seal(c, "CASE-1", "C1", iid, snap).json()

        # 后续：A 表态 + 新增测温更正（开启值仍超限：8->9，快照不变）
        c.post(
            f"/cargo/C1/responsibilities/{iid}/stances",
            json={"snapshot_id": snap, "responsible_party": "A",
                  "stance": "confirm", "stance_id": "st-a"},
        )
        r = c.post(
            "/corrections",
            json={
                "correction_id": "corr-1",
                "target_type": "own_event",
                "target_id": "e1",
                "expected_version": 0,
                "new_temperature": "9",
            },
        )
        assert r.status_code == 201, r.text

        got = c.get("/evidence-seals/CASE-1")
        assert got.status_code == 200
        body = got.json()
        assert body["content_digest"] == first["content_digest"]
        e1 = next(e for e in body["content"]["own_events"] if e["event_id"] == "e1")
        # 原包固化的是封存时的有效温度（8 / v0），后续更正不改写
        assert e1["temperature"] == "8.000"
        assert e1["effective_temperature"] == "8.000"
        assert e1["version"] == 0
        assert body["content"]["stances"] == []

        assert c.get("/evidence-seals/NOPE").status_code == 404


# ----------------------------------------------------------- 对照现况


def test_comparison_matches_when_unchanged():
    with TestClient(app) as c:
        iid, snap = _build_two_segment_case(c)
        _seal(c, "CASE-1", "C1", iid, snap)
        r = c.get("/evidence-seals/CASE-1/comparison")
        assert r.status_code == 200
        comp = r.json()
        assert comp["interval_status"] == "sealed"
        assert comp["snapshot_changed"] is False
        assert comp["current_snapshot_id"] == snap
        assert comp["matches_current"] is True
        for section in comp["evidence"].values():
            assert section == {"added": [], "removed": [], "changed": []}
        assert comp["segments"] == {"added": [], "removed": [], "changed": []}


def test_comparison_segment_change_via_added_handover_inside_interval():
    with TestClient(app) as c:
        # 构造可在区间内合法插入交接的链：先封存，再用“晚到交接”把区间分三段，
        # 且不破坏既有交接合法性——区间内原本无交接，区间后也无交接。
        _cargo(c)
        _event(c, "t1", "C1", "2026-01-01T10:00:00+00:00", temperature="8")
        _event(c, "t2", "C1", "2026-01-01T12:00:00+00:00", temperature="3")
        iid, snap = _closed_interval(c)
        _seal(c, "CASE-1", "C1", iid, snap)

        # 晚到交接 11:00 A->B（区间内，此前无交接，链仍合法）
        _event(c, "h1", "C1", "2026-01-01T11:00:00+00:00", giver="A", receiver="B")

        comp = c.get("/evidence-seals/CASE-1/comparison").json()
        assert comp["interval_status"] == "sealed"
        assert comp["snapshot_changed"] is True
        seg = comp["segments"]
        # 封存片段 [10:00,12:00) A：被拆为 A [10,11) + B [11,12)
        added = {(s["start"], s["responsible_party"]) for s in seg["added"]}
        assert added == {
            ("2026-01-01T11:00:00Z", "B"),
        }
        assert seg["removed"] == []
        assert len(seg["changed"]) == 1
        ch = seg["changed"][0]
        assert ch["key"] == {
            "start": "2026-01-01T10:00:00Z",
            "responsible_party": "A",
        }
        assert ch["before"]["end"] == "2026-01-01T12:00:00Z"
        assert ch["after"]["end"] == "2026-01-01T11:00:00Z"
        # 新增交接事件出现在证据增列
        added_events = [e["event_id"] for e in comp["evidence"]["own_events"]["added"]]
        assert "h1" in added_events


def test_comparison_interval_disappeared_status_explicit():
    with TestClient(app) as c:
        iid, snap = _build_two_segment_case(c)
        _seal(c, "CASE-1", "C1", iid, snap)
        # 开启测温更正为不超限 -> 区间消失
        r = c.post(
            "/corrections",
            json={
                "correction_id": "corr-1",
                "target_type": "own_event",
                "target_id": "e1",
                "expected_version": 0,
                "new_temperature": "2",
            },
        )
        assert r.status_code == 201
        comp = c.get("/evidence-seals/CASE-1/comparison").json()
        assert comp["interval_status"] == "disappeared"
        assert comp["current_snapshot_id"] is None
        assert comp["snapshot_changed"] is False
        assert comp["matches_current"] is False
        # 原责任片段全部列出为删除
        removed = {s["responsible_party"] for s in comp["segments"]["removed"]}
        assert removed == {"A", "B"}
        # 原封存包仍可查且未改
        got = c.get("/evidence-seals/CASE-1").json()
        assert got["interval_id"] == iid
        assert len(got["content"]["interval"]["segments"]) == 2
        # 证据变更：e1 的有效温度 / 版本字段级前后值
        changed = {
            ch["key"]["event_id"]: ch["fields"]
            for ch in comp["evidence"]["own_events"]["changed"]
        }
        assert changed["e1"]["effective_temperature"] == {
            "before": "8.000",
            "after": "2.000",
        }
        assert changed["e1"]["version"] == {"before": 0, "after": 1}
        # 原始温度永不改
        assert changed["e1"].get("temperature") is None


def test_comparison_interval_reopened_status_explicit():
    with TestClient(app) as c:
        iid, snap = _build_two_segment_case(c)
        _seal(c, "CASE-1", "C1", iid, snap)
        # 闭合测温更正为仍超限 -> 区间不再闭合（同一开启读数 e1 仍开启）
        r = c.post(
            "/corrections",
            json={
                "correction_id": "corr-1",
                "target_type": "own_event",
                "target_id": "e3",
                "expected_version": 0,
                "new_temperature": "9",
            },
        )
        assert r.status_code == 201
        comp = c.get("/evidence-seals/CASE-1/comparison").json()
        assert comp["interval_status"] == "reopened"
        assert comp["current_snapshot_id"] is None
        assert comp["matches_current"] is False
        # 重开后尝试重新封存：拒绝（不留记录），原包同请求重传仍返回原包
        r = _seal(c, "CASE-2", "C1", iid, snap)
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "interval_open"
        assert _seal_count() == 1
        r = _seal(c, "CASE-1", "C1", iid, snap)
        assert r.status_code == 200 and r.json()["replayed"] is True


def test_comparison_reports_late_reading_added_and_stance_invalidation():
    with TestClient(app) as c:
        # 车载场景：车次 + 装卸窗口 + 车载读数开启 / 闭合
        _cargo(c, limit="4")
        c.post("/trips", json={"trip_id": "T1", "vehicle_no": "V1"})
        r = c.post(
            "/trips/T1/manifest",
            json={
                "items": [
                    {
                        "cargo_id": "C1",
                        "loaded_at": "2026-01-01T09:00:00+00:00",
                        "unloaded_at": "2026-01-01T18:00:00+00:00",
                    }
                ]
            },
        )
        assert r.status_code == 201
        c.post(
            "/vehicle-readings",
            json={"reading_id": "vr-1", "trip_id": "T1",
                  "read_at": "2026-01-01T10:00:00+00:00", "temperature": "7"},
        )
        c.post(
            "/vehicle-readings",
            json={"reading_id": "vr-2", "trip_id": "T1",
                  "read_at": "2026-01-01T12:00:00+00:00", "temperature": "2"},
        )
        iid, snap = _closed_interval(c)
        assert iid == "veh:vr-1"
        # 表态后封存
        c.post(
            f"/cargo/C1/responsibilities/{iid}/stances",
            json={"snapshot_id": snap, "responsible_party": "A",
                  "stance": "confirm", "stance_id": "st-a"},
        )
        body = _seal(c, "CASE-1", "C1", iid, snap).json()
        readings = {r["reading_id"]: r for r in body["content"]["vehicle_readings"]}
        assert readings["vr-1"]["applicable"] is True
        assert readings["vr-1"]["adopted"] is True
        assert readings["vr-1"]["version"] == 0
        # 封存时车次修订号
        assert {t["trip_id"]: t["revision"] for t in body["content"]["trips"]} == {
            "T1": 3
        }

        # 晚到一笔区间内车载读数（仍超限）：不改变区间边界，证据增列
        c.post(
            "/vehicle-readings",
            json={"reading_id": "vr-late", "trip_id": "T1",
                  "read_at": "2026-01-01T11:00:00+00:00", "temperature": "9"},
        )
        comp = c.get("/evidence-seals/CASE-1/comparison").json()
        assert comp["interval_status"] == "sealed"
        added_readings = [
            r["reading_id"] for r in comp["evidence"]["vehicle_readings"]["added"]
        ]
        assert added_readings == ["vr-late"]
        # 车次修订号因新读数 +1
        trip_changed = {
            ch["key"]["trip_id"]: ch["fields"]
            for ch in comp["evidence"]["trips"]["changed"]
        }
        assert trip_changed["T1"]["revision"] == {"before": 3, "after": 4}


def test_comparison_reports_reading_correction_effective_change():
    with TestClient(app) as c:
        _cargo(c, limit="4")
        c.post("/trips", json={"trip_id": "T1", "vehicle_no": "V1"})
        c.post(
            "/trips/T1/manifest",
            json={
                "items": [
                    {
                        "cargo_id": "C1",
                        "loaded_at": "2026-01-01T09:00:00+00:00",
                        "unloaded_at": "2026-01-01T18:00:00+00:00",
                    }
                ]
            },
        )
        c.post(
            "/vehicle-readings",
            json={"reading_id": "vr-1", "trip_id": "T1",
                  "read_at": "2026-01-01T10:00:00+00:00", "temperature": "7"},
        )
        c.post(
            "/vehicle-readings",
            json={"reading_id": "vr-2", "trip_id": "T1",
                  "read_at": "2026-01-01T12:00:00+00:00", "temperature": "2"},
        )
        iid, snap = _closed_interval(c)
        _seal(c, "CASE-1", "C1", iid, snap)

        # 车载读数更正：vr-1 7 -> 8（仍超限，区间仍在）
        r = c.post(
            "/corrections",
            json={
                "correction_id": "corr-1",
                "target_type": "vehicle_reading",
                "target_id": "vr-1",
                "expected_version": 0,
                "new_temperature": "8",
            },
        )
        assert r.status_code == 201, r.text
        comp = c.get("/evidence-seals/CASE-1/comparison").json()
        assert comp["interval_status"] == "sealed"
        changed_readings = {
            ch["key"]["reading_id"]: ch["fields"]
            for ch in comp["evidence"]["vehicle_readings"]["changed"]
        }
        assert changed_readings["vr-1"]["effective_temperature"] == {
            "before": "7.000",
            "after": "8.000",
        }
        assert changed_readings["vr-1"]["version"] == {"before": 0, "after": 1}
        # 原包未改
        got = c.get("/evidence-seals/CASE-1").json()
        vr1 = next(r for r in got["content"]["vehicle_readings"] if r["reading_id"] == "vr-1")
        assert vr1["effective_temperature"] == "7.000" and vr1["version"] == 0
        assert got["content_digest"]  # 摘要仍可校验


def test_seal_vehicle_reading_applicable_boundary_and_supplanted():
    with TestClient(app) as c:
        _cargo(c, limit="4")
        c.post("/trips", json={"trip_id": "T1", "vehicle_no": "V1"})
        c.post(
            "/trips/T1/manifest",
            json={
                "items": [
                    {
                        "cargo_id": "C1",
                        "loaded_at": "2026-01-01T09:00:00+00:00",
                        "unloaded_at": "2026-01-01T12:00:00+00:00",
                    }
                ]
            },
        )
        # 在车、被采用
        c.post(
            "/vehicle-readings",
            json={"reading_id": "vr-on", "trip_id": "T1",
                  "read_at": "2026-01-01T10:00:00+00:00", "temperature": "7"},
        )
        # 恰在卸车时刻：左闭右开 -> 不在车
        c.post(
            "/vehicle-readings",
            json={"reading_id": "vr-edge", "trip_id": "T1",
                  "read_at": "2026-01-01T12:00:00+00:00", "temperature": "9"},
        )
        # 同刻自有测温优先：vr-sup 被取代（adopted=false）
        _event(c, "own-1", "C1", "2026-01-01T11:00:00+00:00", temperature="2")
        c.post(
            "/vehicle-readings",
            json={"reading_id": "vr-sup", "trip_id": "T1",
                  "read_at": "2026-01-01T11:00:00+00:00", "temperature": "7"},
        )
        iid, snap = _closed_interval(c)
        body = _seal(c, "CASE-1", "C1", iid, snap).json()
        readings = {r["reading_id"]: r for r in body["content"]["vehicle_readings"]}
        assert readings["vr-on"]["applicable"] is True
        assert readings["vr-on"]["adopted"] is True
        assert readings["vr-edge"]["applicable"] is False
        assert readings["vr-edge"]["adopted"] is False
        assert readings["vr-sup"]["applicable"] is True
        assert readings["vr-sup"]["adopted"] is False


def test_comparison_reports_loading_window_correction():
    with TestClient(app) as c:
        _cargo(c, limit="4")
        c.post("/trips", json={"trip_id": "T1", "vehicle_no": "V1"})
        c.post(
            "/trips/T1/manifest",
            json={
                "items": [
                    {
                        "cargo_id": "C1",
                        "loaded_at": "2026-01-01T09:00:00+00:00",
                        "unloaded_at": "2026-01-01T12:00:00+00:00",
                    }
                ]
            },
        )
        c.post(
            "/vehicle-readings",
            json={"reading_id": "vr-1", "trip_id": "T1",
                  "read_at": "2026-01-01T10:00:00+00:00", "temperature": "7"},
        )
        # 闭合读数在 13:00（原窗口外，需先更正窗口）——先放一笔窗口内闭合
        c.post(
            "/vehicle-readings",
            json={"reading_id": "vr-2", "trip_id": "T1",
                  "read_at": "2026-01-01T11:00:00+00:00", "temperature": "2"},
        )
        iid, snap = _closed_interval(c)
        _seal(c, "CASE-1", "C1", iid, snap)

        # 更正装卸窗口：延长至 14:00（排除自身，无重叠）
        r = c.post(
            "/trips/T1/loading-corrections",
            json={
                "correction_id": "lc-1",
                "cargo_id": "C1",
                "origin_loaded_at": "2026-01-01T09:00:00+00:00",
                "expected_version": 0,
                "new_loaded_at": "2026-01-01T09:00:00+00:00",
                "new_unloaded_at": "2026-01-01T14:00:00+00:00",
                "reason": "卸车时刻录早",
            },
        )
        assert r.status_code == 201, r.text

        comp = c.get("/evidence-seals/CASE-1/comparison").json()
        win_changed = {
            (ch["key"]["trip_id"], ch["key"]["cargo_id"],
             ch["key"]["origin_loaded_at"]): ch["fields"]
            for ch in comp["evidence"]["loading_windows"]["changed"]
        }
        key = ("T1", "C1", "2026-01-01T09:00:00Z")
        assert win_changed[key]["unloaded_at"] == {
            "before": "2026-01-01T12:00:00Z",
            "after": "2026-01-01T14:00:00Z",
        }
        assert win_changed[key]["window_version"] == {"before": 0, "after": 1}
        # 车次修订号推进（窗口更正使 revision +1）
        trip_changed = {
            ch["key"]["trip_id"]: ch["fields"]
            for ch in comp["evidence"]["trips"]["changed"]
        }
        assert "revision" in trip_changed["T1"]


def test_rejected_seals_leave_no_record_and_case_id_reusable():
    with TestClient(app) as c:
        iid, snap = _build_two_segment_case(c)
        # 快照错误被拒
        r = _seal(c, "CASE-1", "C1", iid, "sha256:" + "1" * 64)
        assert r.status_code == 409
        assert _seal_count() == 0
        # 编号可复用：随后同内容（正确快照）成功
        r = _seal(c, "CASE-1", "C1", iid, snap)
        assert r.status_code == 201
        assert _seal_count() == 1
