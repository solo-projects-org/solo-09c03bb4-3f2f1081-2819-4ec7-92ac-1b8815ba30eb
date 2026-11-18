"""县界核验 geo 层单元测试：解析、等面积口径、洞/共边语义与图斑反投影。"""
from __future__ import annotations

import pytest
from shapely.geometry import shape

from app import geo
from tests.conftest import (
    as_feature,
    county_polygon,
    county_polygon_with_hole,
    feature_collection,
    rect_geojson,
)


def test_parse_county_boundary_three_payload_shapes():
    g = county_polygon(0, 0, 1, 1)
    assert geo.parse_county_boundary(g).geom_type == "Polygon"
    assert geo.parse_county_boundary(as_feature(g)).geom_type == "Polygon"
    fc = feature_collection(as_feature(g))
    assert geo.parse_county_boundary(fc).geom_type == "Polygon"


def test_parse_county_boundary_rejects_bad_inputs():
    with pytest.raises(geo.GeoValidationError):
        geo.parse_county_boundary({"type": "Point", "coordinates": [0, 0]})
    with pytest.raises(geo.GeoValidationError):
        geo.parse_county_boundary(
            feature_collection(
                as_feature(county_polygon(0, 0, 1, 1)),
                as_feature(county_polygon(1, 0, 2, 1)),
            )
        )
    with pytest.raises(geo.GeoValidationError):
        geo.parse_county_boundary({"type": "Feature", "geometry": None})
    with pytest.raises(geo.GeoValidationError):
        geo.parse_county_boundary(county_polygon(0, 0, 1, 95))


def test_parse_county_boundary_rejects_non_wgs84_crs():
    payload = county_polygon(0, 0, 1, 1)
    payload["crs"] = {
        "type": "name",
        "properties": {"name": "urn:ogc:def:crs:EPSG::3857"},
    }
    with pytest.raises(geo.GeoValidationError) as exc:
        geo.parse_county_boundary(payload)
    assert "WGS84" in str(exc.value)
    # Feature 包装里携带非 WGS84 crs 同样拒收
    feat = as_feature(county_polygon(0, 0, 1, 1))
    feat["crs"] = {"type": "name", "properties": {"name": "EPSG:3857"}}
    with pytest.raises(geo.GeoValidationError):
        geo.parse_county_boundary(feat)


def _wgs_features(*specs):
    return [(code, shape(rect_geojson(code, *coords)["geometry"])) for code, *coords in specs]


def test_verify_exact_fit_and_shared_edge():
    county = shape(county_polygon(0, 0, 2, 1))
    townships = _wgs_features(("T1", 0, 0, 1, 1), ("T2", 1, 0, 2, 1))
    result = geo.verify_county_coverage(county, townships)
    assert result["passed"] is True
    assert result["gaps"]["features"] == []
    assert result["outside"]["features"] == []
    assert result["township_overlaps"] == []


def test_verify_gap_and_outside_areas_in_square_metres():
    # 用真实地理坐标（约 1km 网格），面积应为平方米量级
    county = shape(
        {
            "type": "Polygon",
            "coordinates": [
                [[110.0, 30.0], [110.02, 30.0], [110.02, 30.02],
                 [110.0, 30.02], [110.0, 30.0]]
            ],
        }
    )
    # 乡镇只覆盖左半：漏划右半；另一个乡镇向北越界 0.01°
    townships = [
        ("T1", shape(rect_geojson("T1", 110.0, 30.0, 110.01, 30.02)["geometry"])),
        ("T2", shape(rect_geojson("T2", 110.0, 30.02, 110.01, 30.03)["geometry"])),
    ]
    result = geo.verify_county_coverage(county, townships)
    assert result["passed"] is False
    assert result["area_unit"] == "m2"
    assert result["summary"]["gap_area_m2"] > 1.0e5      # 约 1km x 1km
    assert result["summary"]["outside_area_m2"] > 1.0e5
    # 输出图斑已反投影回 WGS84：经度落在合法区间
    for feat in result["gaps"]["features"] + result["outside"]["features"]:
        g = shape(feat["geometry"])
        assert g.bounds[0] >= 109.999 and g.bounds[2] <= 110.021
        # 图斑自带面积与等面积口径合计一致（精度内）
        assert feat["properties"]["area_m2"] > 0


def test_verify_hole_preserved():
    county = shape(county_polygon_with_hole(0, 0, 4, 4, 1, 1, 2, 2))
    # 完整矩形乡镇：实体覆盖县界实体，洞位被占据 -> 只报越界，不报漏划
    townships = [("T1", shape(rect_geojson("T1", 0, 0, 4, 4)["geometry"]))]
    result = geo.verify_county_coverage(county, townships)
    assert result["summary"]["gap_patch_count"] == 0
    assert result["summary"]["outside_patch_count"] >= 1


def test_verify_overlap_identifies_pair():
    county = shape(county_polygon(0, 0, 3, 3))
    townships = _wgs_features(("T1", 0, 0, 2, 2), ("T2", 1, 1, 3, 3))
    result = geo.verify_county_coverage(county, townships)
    assert result["passed"] is False
    pair = result["township_overlaps"][0]
    assert {pair["township_a"], pair["township_b"]} == {"T1", "T2"}
    assert pair["overlap_area_m2"] > 0


def test_verify_empty_township_marks_whole_county_gap():
    county = shape(county_polygon(0, 0, 2, 2))
    result = geo.verify_county_coverage(county, [])
    assert result["passed"] is False
    assert result["summary"]["gap_patch_count"] >= 1
    assert result["outside"]["features"] == []
    # 漏划图斑拼回后约等于整个县界
    gap = shape(
        {
            "type": "MultiPolygon",
            "coordinates": [f["geometry"]["coordinates"] for f in result["gaps"]["features"]],
        }
    )
    assert abs(gap.area - county.area) / county.area < 1e-9


def test_verify_patch_areas_sum_to_summary():
    county = shape(county_polygon(0, 0, 2, 2))
    townships = _wgs_features(("T1", 0, 0, 1, 1))
    result = geo.verify_county_coverage(county, townships)
    summed = sum(f["properties"]["area_m2"] for f in result["gaps"]["features"])
    assert abs(summed - result["summary"]["gap_area_m2"]) < 1e-4
