"""县界覆盖核验端到端测试。

覆盖关键交错：
- 完全贴合 -> passed；仅共边不算异常
- 县界内空白（漏划）图斑与面积；县界外多划（越界）图斑与面积
- 县界洞内空白保留（不被误判为漏划）
- 乡镇互叠 -> 不通过并指出冲突乡镇号
- Polygon/MultiPolygon、Feature/FeatureCollection 载荷
- 同版乡镇图/县界更正 -> 旧核验立即 stale（含完成后与进行中）
- 重启：记录可重查；中断任务落 failed；stale 不复活为通过
"""
from __future__ import annotations

import json
import time

from tests.conftest import (
    as_feature,
    county_multipolygon,
    county_polygon,
    county_polygon_with_hole,
    feature_collection,
    multipolygon_geojson,
    rect_geojson,
    rect_with_hole,
)


def _put_map(client, version, *feats):
    r = client.put(f"/api/maps/{version}", json=feature_collection(*feats))
    assert r.status_code == 200, r.text
    return r


def _put_county(client, version, geometry):
    r = client.put(f"/api/county-boundaries/{version}", json=geometry)
    assert r.status_code == 200, r.text
    return r


def _submit(client, version="v1"):
    return client.post("/api/verifications/jobs", json={"version": version})


def _wait(client, verification_id, timeout=10.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        r = client.get(f"/api/verifications/jobs/{verification_id}")
        last = r
        if r.status_code in (409, 422):
            return r
        state = r.json()["state"]
        if state in ("completed", "failed", "stale"):
            return r
        time.sleep(0.01)
    raise AssertionError(f"核验未在 {timeout}s 内结束：{last.text if last else None}")


# ----------------------------------------------------------------------
# 受理与校验
# ----------------------------------------------------------------------
def test_county_boundary_accepts_polygon_feature_and_fc(client):
    r1 = _put_county(client, "v1", county_polygon(0, 0, 2, 1))
    assert r1.json()["revision"] == 1
    assert r1.json()["geometry_type"] == "Polygon"
    assert r1.json()["replaced_previous"] is False

    r2 = client.put(
        "/api/county-boundaries/v2", json=as_feature(county_polygon(0, 0, 2, 1))
    )
    assert r2.status_code == 200

    fc = {"type": "FeatureCollection", "features": [as_feature(county_polygon(0, 0, 2, 1))]}
    r3 = client.put("/api/county-boundaries/v3", json=fc)
    assert r3.status_code == 200

    r4 = client.put(
        "/api/county-boundaries/v4", json=county_multipolygon([(0, 0, 1, 1), (2, 2, 3, 3)])
    )
    assert r4.status_code == 200
    assert r4.json()["geometry_type"] == "MultiPolygon"

    versions = {v["version"] for v in client.get("/api/county-boundaries").json()}
    assert versions == {"v1", "v2", "v3", "v4"}
    meta = client.get("/api/county-boundaries/v1").json()
    assert meta["revision"] == 1 and meta["geometry_type"] == "Polygon"


def test_county_boundary_rejection_keeps_old(client):
    _put_county(client, "v1", county_polygon(0, 0, 2, 1))

    # 非面几何
    point = {"type": "Point", "coordinates": [0, 0]}
    r = client.put("/api/county-boundaries/v1", json=point)
    assert r.status_code == 400
    assert "Polygon" in r.json()["detail"]

    # 多要素 FeatureCollection 拒收
    r = client.put(
        "/api/county-boundaries/v1",
        json=feature_collection(
            as_feature(county_polygon(0, 0, 1, 1)),
            as_feature(county_polygon(1, 0, 2, 1)),
        ),
    )
    assert r.status_code == 400

    # 坐标越界
    r = client.put("/api/county-boundaries/v1", json=county_polygon(0, 0, 1, 95))
    assert r.status_code == 400
    assert "WGS84" in r.json()["detail"]

    # 坏 JSON / 空体
    r = client.put(
        "/api/county-boundaries/v1",
        content=b"{bad",
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 400
    r = client.put(
        "/api/county-boundaries/v1",
        content=b"",
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 400

    # 旧县界原样保留，修订号仍为 1
    assert client.get("/api/county-boundaries/v1").json()["revision"] == 1


def test_county_boundary_reupload_bumps_revision(client):
    _put_county(client, "v1", county_polygon(0, 0, 2, 1))
    r = _put_county(client, "v1", county_polygon(0, 0, 3, 3))
    assert r.json()["revision"] == 2
    assert r.json()["replaced_previous"] is True


def test_multipart_county_upload(client):
    r = client.put(
        "/api/county-boundaries/v1",
        files={
            "file": (
                "county.geojson",
                json.dumps(county_polygon(0, 0, 2, 1)),
                "application/geo+json",
            )
        },
    )
    assert r.status_code == 200
    assert r.json()["revision"] == 1


# ----------------------------------------------------------------------
# 核验结论
# ----------------------------------------------------------------------
def test_exact_fit_passes_and_shared_edge_not_anomaly(client):
    # 县界 2x1；两个 1x1 乡镇恰好铺满，乡镇之间仅共边
    _put_county(client, "v1", county_polygon(0, 0, 2, 1))
    _put_map(
        client,
        "v1",
        rect_geojson("T1", 0, 0, 1, 1),
        rect_geojson("T2", 1, 0, 2, 1),
    )
    body = _wait(client, _submit(client).json()["verification_id"]).json()
    assert body["state"] == "completed"
    result = body["result"]
    assert result["passed"] is True
    assert result["summary"]["gap_patch_count"] == 0
    assert result["summary"]["outside_patch_count"] == 0
    assert result["summary"]["overlap_pair_count"] == 0
    assert result["summary"]["gap_area_m2"] == 0.0
    assert result["summary"]["outside_area_m2"] == 0.0
    assert result["gaps"]["features"] == []
    assert result["outside"]["features"] == []
    assert result["township_overlaps"] == []


def test_gap_inside_county_returned_as_geojson_patch(client):
    # 县界 2x2；乡镇只铺左下 1x1，右上 3/4 为漏划
    _put_county(client, "v1", county_polygon(0, 0, 2, 2))
    _put_map(client, "v1", rect_geojson("T1", 0, 0, 1, 1))

    body = _wait(client, _submit(client).json()["verification_id"]).json()
    result = body["result"]
    assert result["passed"] is False
    assert result["summary"]["gap_patch_count"] >= 1
    assert result["summary"]["gap_area_m2"] > 0
    # 漏划约占县界实体面积的 3/4（等面积投影口径，比例稳定）
    from shapely.geometry import shape

    gap_union = shape(
        {
            "type": "MultiPolygon",
            "coordinates": [f["geometry"]["coordinates"] for f in result["gaps"]["features"]],
        }
    )
    county_area = shape(county_polygon(0, 0, 2, 2)).area
    assert abs(gap_union.area / county_area - 0.75) < 1e-6
    # 每个图斑自带平方米面积，且为 WGS84 面几何
    for feat in result["gaps"]["features"]:
        assert feat["properties"]["kind"] == "gap"
        assert feat["properties"]["area_m2"] > 0
        assert feat["geometry"]["type"] in ("Polygon", "MultiPolygon")
    # 没有越界
    assert result["outside"]["features"] == []
    assert result["township_overlaps"] == []


def test_outside_county_delineation_returned(client):
    # 县界 2x1；一个乡镇贴合，另一个完全划在县界外（越界）
    _put_county(client, "v1", county_polygon(0, 0, 2, 1))
    _put_map(
        client,
        "v1",
        rect_geojson("T1", 0, 0, 2, 1),
        rect_geojson("T2", 2, 0, 3, 1),
    )
    result = _wait(client, _submit(client).json()["verification_id"]).json()["result"]
    assert result["passed"] is False
    assert result["summary"]["outside_patch_count"] >= 1
    assert result["summary"]["outside_area_m2"] > 0
    assert result["gaps"]["features"] == []
    outside_feat = result["outside"]["features"][0]
    assert outside_feat["properties"]["kind"] == "outside"
    # 越界图斑落在县界之外（经度 >= 2）
    from shapely.geometry import shape

    assert shape(outside_feat["geometry"]).bounds[0] >= 2.0 - 1e-9


def test_township_crossing_boundary_splits_into_gap_and_outside(client):
    # 县界 [0,2]x[0,2]；单个乡镇 [1,3]x[0,2]：左半在县界内、右半越界，
    # 县界左半为漏划
    _put_county(client, "v1", county_polygon(0, 0, 2, 2))
    _put_map(client, "v1", rect_geojson("T1", 1, 0, 3, 2))
    result = _wait(client, _submit(client).json()["verification_id"]).json()["result"]
    assert result["passed"] is False
    assert result["summary"]["gap_area_m2"] > 0
    assert result["summary"]["outside_area_m2"] > 0


def test_township_outside_county_even_if_only_touching_is_outside(client):
    # 县界被 T1/T2 铺满（彼此仅共边，通过）；额外的 T3 整个在县界外、
    # 仅与县界共边相邻——其正面积实体仍属越界，共边线本身零面积不单独成斑
    _put_county(client, "v1", county_polygon(0, 0, 2, 1))
    _put_map(
        client,
        "v1",
        rect_geojson("T1", 0, 0, 1, 1),
        rect_geojson("T2", 1, 0, 2, 1),
        rect_geojson("T3", 2, 0, 3, 1),
    )
    result = _wait(client, _submit(client).json()["verification_id"]).json()["result"]
    assert result["passed"] is False
    assert result["summary"]["outside_patch_count"] == 1
    assert result["outside"]["features"][0]["properties"]["area_m2"] > 0
    assert result["summary"]["gap_patch_count"] == 0


def test_hole_inside_county_is_preserved_not_counted_gap(client):
    # 县界是带 1x1 洞的 4x4 矩形；乡镇铺满整个 4x4（含洞位）。
    # 洞必须保留：洞内空白不算漏划 -> 无 gap 图斑；
    # 乡镇实体伸进洞内/超出县界的部分构成越界（洞面积 1 个度平方）。
    _put_county(
        client, "v1", county_polygon_with_hole(0, 0, 4, 4, 1, 1, 2, 2)
    )
    _put_map(client, "v1", rect_geojson("T1", 0, 0, 4, 4))
    result = _wait(client, _submit(client).json()["verification_id"]).json()["result"]
    # 县界实体部分被完全覆盖 -> 没有漏划图斑
    assert result["summary"]["gap_patch_count"] == 0
    assert result["summary"]["gap_area_m2"] == 0.0
    # 洞内被乡镇占据 -> 越界图斑，面积约等于洞（1/16 个度平方量级）
    assert result["summary"]["outside_patch_count"] >= 1
    from shapely.geometry import shape

    outside_union = shape(
        {
            "type": "MultiPolygon",
            "coordinates": [
                f["geometry"]["coordinates"] for f in result["outside"]["features"]
            ],
        }
    )
    hole_area = 1.0
    assert abs(outside_union.area - hole_area) < 1e-6


def test_township_with_matching_hole_passes(client):
    # 县界带洞；乡镇面同样带洞且外环贴合 -> 实体完全互贴，核验通过
    _put_county(
        client, "v1", county_polygon_with_hole(0, 0, 4, 4, 1, 1, 2, 2)
    )
    _put_map(client, "v1", rect_with_hole("T1", 0, 0, 4, 4, 1, 1, 2, 2))
    result = _wait(client, _submit(client).json()["verification_id"]).json()["result"]
    assert result["passed"] is True


def test_township_overlap_reports_conflicting_ids(client):
    _put_county(client, "v1", county_polygon(0, 0, 3, 3))
    _put_map(
        client,
        "v1",
        rect_geojson("T1", 0, 0, 2, 2),
        rect_geojson("T2", 1, 1, 3, 3),
    )
    result = _wait(client, _submit(client).json()["verification_id"]).json()["result"]
    assert result["passed"] is False
    overlaps = result["township_overlaps"]
    assert len(overlaps) == 1
    pair = overlaps[0]
    assert {pair["township_a"], pair["township_b"]} == {"T1", "T2"}
    assert pair["overlap_area_m2"] > 1.0  # 平方米量级真实互叠


def test_multipolygon_inputs_supported(client):
    _put_county(
        client, "v1", county_multipolygon([(0, 0, 1, 1), (5, 5, 6, 6)])
    )
    _put_map(
        client,
        "v1",
        multipolygon_geojson("T1", [(0, 0, 1, 1), (5, 5, 6, 6)]),
    )
    result = _wait(client, _submit(client).json()["verification_id"]).json()["result"]
    assert result["passed"] is True


def test_submit_requires_both_sources(client):
    # 只有乡镇图，没有县界
    _put_map(client, "v1", rect_geojson("T1", 0, 0, 1, 1))
    r = _submit(client)
    assert r.status_code == 404
    assert "县界" in r.json()["detail"]

    # 只有县界，没有乡镇图
    _put_county(client, "v2", county_polygon(0, 0, 1, 1))
    r = client.post("/api/verifications/jobs", json={"version": "v2"})
    assert r.status_code == 404
    assert "乡镇" in r.json()["detail"]

    # 完全不存在的版号
    assert _submit(client, "nope").status_code == 404


# ----------------------------------------------------------------------
# 失效语义：来源更正立即作废旧结论
# ----------------------------------------------------------------------
def test_township_correction_invalidates_completed_verification(client):
    _put_county(client, "v1", county_polygon(0, 0, 2, 1))
    _put_map(
        client,
        "v1",
        rect_geojson("T1", 0, 0, 1, 1),
        rect_geojson("T2", 1, 0, 2, 1),
    )
    vid = _submit(client).json()["verification_id"]
    body = _wait(client, vid).json()
    assert body["state"] == "completed"
    assert body["result"]["passed"] is True

    # 同版乡镇图整版更正 -> 旧核验立即失效
    r = _put_map(client, "v1", rect_geojson("T1", 0, 0, 1, 1))  # 只剩半个县
    listed = client.get("/api/verifications/jobs").json()["jobs"]
    assert listed[0]["state"] == "stale"

    r = client.get(f"/api/verifications/jobs/{vid}")
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["state"] == "stale"
    assert "result" not in detail  # 旧图斑不得交付
    assert "重新提交" in detail["error"]

    # 用同版号重新核验 -> 基于新来源的结论可交付，且为不通过
    vid2 = _submit(client).json()["verification_id"]
    body2 = _wait(client, vid2).json()
    assert body2["state"] == "completed"
    assert body2["result"]["passed"] is False
    assert body2["result"]["summary"]["gap_area_m2"] > 0


def test_county_correction_invalidates_and_reports_count(client):
    _put_county(client, "v1", county_polygon(0, 0, 2, 1))
    _put_map(
        client,
        "v1",
        rect_geojson("T1", 0, 0, 1, 1),
        rect_geojson("T2", 1, 0, 2, 1),
    )
    vid = _submit(client).json()["verification_id"]
    _wait(client, vid)

    r = _put_county(client, "v1", county_polygon(0, 0, 3, 1))  # 县界变大
    assert r.json()["invalidated_verifications"] == 1
    assert r.json()["revision"] == 2

    r = client.get(f"/api/verifications/jobs/{vid}")
    assert r.status_code == 409
    assert r.json()["detail"]["state"] == "stale"


def test_inflight_verification_never_delivers_stale_patches(client, monkeypatch):
    _put_county(client, "v1", county_polygon(0, 0, 2, 1))
    _put_map(
        client,
        "v1",
        rect_geojson("T1", 0, 0, 1, 1),
        rect_geojson("T2", 1, 0, 2, 1),
    )

    # 让核验计算在工作线程中放慢，确保更正发生在核验进行期间
    import app.verifications as vmod

    real_verify = vmod.geo.verify_county_coverage

    def slow_verify(*args, **kwargs):
        time.sleep(0.3)
        return real_verify(*args, **kwargs)

    monkeypatch.setattr(vmod.geo, "verify_county_coverage", slow_verify)

    vid = _submit(client).json()["verification_id"]
    time.sleep(0.05)  # 确认任务已进入 running
    # 核验进行中更正县界
    _put_county(client, "v1", county_polygon(0, 0, 2, 2))

    r = _wait(client, vid)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["state"] == "stale"
    assert "result" not in detail  # 来源变化，绝不交付旧图斑

    # 重新提交的核验基于新县界，正常完成
    monkeypatch.undo()
    vid2 = _submit(client).json()["verification_id"]
    body2 = _wait(client, vid2).json()
    assert body2["state"] == "completed"


def test_failed_verification_never_revived_to_passed(client, monkeypatch):
    _put_county(client, "v1", county_polygon(0, 0, 1, 1))
    _put_map(client, "v1", rect_geojson("T1", 0, 0, 1, 1))

    def boom(*args, **kwargs):
        raise RuntimeError("模拟核验崩溃")

    monkeypatch.setattr("app.verifications.geo.verify_county_coverage", boom)
    vid = _submit(client).json()["verification_id"]
    r = _wait(client, vid)
    assert r.status_code == 422
    assert r.json()["detail"]["state"] == "failed"

    # 反复查询仍是 failed，不会变成通过
    monkeypatch.undo()
    r = client.get(f"/api/verifications/jobs/{vid}")
    assert r.status_code == 422
    assert r.json()["detail"]["state"] == "failed"


# ----------------------------------------------------------------------
# 持久化与重启
# ----------------------------------------------------------------------
def test_verification_records_survive_reopen_and_stale_not_revived(client, tmp_db_path):
    _put_county(client, "v1", county_polygon(0, 0, 2, 1))
    _put_map(
        client,
        "v1",
        rect_geojson("T1", 0, 0, 1, 1),
        rect_geojson("T2", 1, 0, 2, 1),
    )
    vid = _submit(client).json()["verification_id"]
    _wait(client, vid)
    # 更正使结论变 stale
    _put_county(client, "v1", county_polygon(0, 0, 3, 1))
    assert client.get(f"/api/verifications/jobs/{vid}").status_code == 409

    # 模拟进程重启：新 Database + 新 VerificationManager
    from app.db import Database
    from app.verifications import VerificationManager

    db2 = Database(tmp_db_path)
    try:
        manager2 = VerificationManager(db2)
        interrupted = manager2.recover_after_restart()
        assert interrupted == 0  # 没有中断任务
        record = manager2.get(vid)
        assert record["state"] == "stale"  # 过期结论不复活
        record = manager2.deliver(vid)
        assert record["state"] == "stale"
        assert record["result"] is None
        manager2.shutdown()
    finally:
        db2.close()


def test_interrupted_verification_marked_failed_after_restart(client, tmp_db_path):
    _put_county(client, "v1", county_polygon(0, 0, 1, 1))
    _put_map(client, "v1", rect_geojson("T1", 0, 0, 1, 1))

    # 直接在库里造一条 running 记录，模拟重启瞬间被打断的核验
    from datetime import datetime, timezone

    db = client.app.state.db
    db.insert_verification(
        {
            "verification_id": "interrupted-1",
            "version": "v1",
            "state": "running",
            "pinned_township_revision": db.get_revision("v1"),
            "pinned_county_revision": db.get_county_revision("v1"),
            "submitted_at": datetime.now(timezone.utc).isoformat(),
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
            "error": None,
            "error_detail": None,
            "result": None,
        }
    )

    from app.db import Database
    from app.verifications import VerificationManager

    db2 = Database(tmp_db_path)
    try:
        manager2 = VerificationManager(db2)
        assert manager2.recover_after_restart() == 1
        record = manager2.get("interrupted-1")
        assert record["state"] == "failed"
        assert "重启" in record["error"]
        # 再重启一次也不会复活
        assert manager2.recover_after_restart() == 0
        assert manager2.get("interrupted-1")["state"] == "failed"
        manager2.shutdown()
    finally:
        db2.close()


def test_verification_listing_filter_and_unknown_id(client):
    _put_county(client, "v1", county_polygon(0, 0, 1, 1))
    _put_map(client, "v1", rect_geojson("T1", 0, 0, 1, 1))
    _put_county(client, "v2", county_polygon(0, 0, 1, 1))
    _put_map(client, "v2", rect_geojson("T9", 0, 0, 1, 1))
    _submit(client, "v1")
    _submit(client, "v2")

    all_jobs = client.get("/api/verifications/jobs").json()["jobs"]
    assert len(all_jobs) == 2
    v1_jobs = client.get("/api/verifications/jobs", params={"version": "v1"}).json()["jobs"]
    assert len(v1_jobs) == 1
    assert v1_jobs[0]["version"] == "v1"
    assert (
        client.get("/api/verifications/jobs/does-not-exist").status_code == 404
    )
