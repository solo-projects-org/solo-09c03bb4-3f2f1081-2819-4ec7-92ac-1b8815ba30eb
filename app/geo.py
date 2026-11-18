"""GeoJSON 校验、等面积投影与交叠权重计算。

约定：
- 输入为 WGS84 (EPSG:4326) 的 FeatureCollection，几何只允许
  Polygon / MultiPolygon；
- 面积计算在以数据中心为基准的 Albers 等面积投影（米单位）下进行；
- 洞（Polygon 内环）由 Shapely 的交运算自然扣除，仅共边/接触的
  交叠面积为 0，不计入权重。
"""
from __future__ import annotations

from typing import Iterable

from pyproj import CRS, Transformer
from shapely import is_valid_reason, transform as shp_transform
from shapely.geometry import mapping, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from . import config

WGS84 = CRS.from_epsg(4326)
ALLOWED_GEOM_TYPES = {"Polygon", "MultiPolygon"}


class GeoValidationError(ValueError):
    """上传 GeoJSON 不合法（HTTP 400）。"""


# ----------------------------------------------------------------------
# 校验与解析
# ----------------------------------------------------------------------
def check_wgs84_range(geojson: dict) -> None:
    """递归检查所有坐标落在 WGS84 经纬度范围内。"""

    def walk(node):
        if isinstance(node, (list, tuple)):
            if node and isinstance(node[0], (int, float)):
                if len(node) < 2 or not isinstance(node[1], (int, float)):
                    raise GeoValidationError("坐标至少需要经纬度两个数值分量")
                lon, lat = node[0], node[1]
                if not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
                    raise GeoValidationError(
                        f"坐标 ({lon}, {lat}) 超出 WGS84 经纬度范围"
                    )
                if len(node) > 2 and node[2] != 0:
                    # 仅允许二维（或 z=0），等面积计算不支持高程
                    raise GeoValidationError("不支持三维坐标，请提供二维 WGS84 坐标")
            else:
                for child in node:
                    walk(child)

    walk(geojson.get("coordinates", []))


def _check_geojson_crs(geojson: dict) -> None:
    """GeoJSON 顶层 crs 仅允许 WGS84 (EPSG:4326 / CRS84)；缺省即 WGS84。"""
    crs = geojson.get("crs")
    if crs is None:
        return
    name = ""
    if isinstance(crs, dict):
        name = str(crs.get("properties", {}).get("name", ""))
    if name and "4326" not in name and "CRS84" not in name.upper():
        raise GeoValidationError(f"仅支持 WGS84 (EPSG:4326)，收到 crs={name}")


def parse_feature_collection(payload: dict) -> list[tuple[str, BaseGeometry]]:
    """解析并整版校验 FeatureCollection。

    返回 [(township_id, shapely_geometry), ...]；
    任一要素无效、乡镇号缺失/重复、几何非面或面积为 0 均抛 GeoValidationError，
    由调用方保证拒收且不触碰旧版数据。
    """
    if not isinstance(payload, dict):
        raise GeoValidationError("请求体必须是 GeoJSON 对象")
    if payload.get("type") != "FeatureCollection":
        raise GeoValidationError("仅支持 GeoJSON FeatureCollection")

    _check_geojson_crs(payload)

    features = payload.get("features")
    if not isinstance(features, list) or not features:
        raise GeoValidationError("features 为空，至少需要一个面要素")

    seen_ids: set[str] = set()
    parsed: list[tuple[str, BaseGeometry]] = []

    for idx, feature in enumerate(features):
        if not isinstance(feature, dict) or feature.get("type") != "Feature":
            raise GeoValidationError(f"第 {idx} 个要素不是合法 GeoJSON Feature")
        props = feature.get("properties") or {}

        raw_code = None
        for field in config.CODE_FIELDS:
            if field in props and props[field] is not None:
                raw_code = props[field]
                break
        if raw_code is None:
            raise GeoValidationError(
                f"第 {idx} 个要素缺少乡镇号字段（{ ' / '.join(config.CODE_FIELDS) }）"
            )
        township_id = str(raw_code).strip()
        if not township_id:
            raise GeoValidationError(f"第 {idx} 个要素乡镇号为空")
        if township_id in seen_ids:
            raise GeoValidationError(f"乡镇号重复：{township_id}")
        seen_ids.add(township_id)

        geojson_geom = feature.get("geometry")
        if not isinstance(geojson_geom, dict):
            raise GeoValidationError(f"乡镇 {township_id} 缺少 geometry")
        if geojson_geom.get("type") not in ALLOWED_GEOM_TYPES:
            raise GeoValidationError(
                f"乡镇 {township_id} 的几何类型 {geojson_geom.get('type')} 不支持，"
                "仅允许 Polygon / MultiPolygon"
            )
        check_wgs84_range(geojson_geom)

        try:
            geom = shape(geojson_geom)
        except Exception as exc:  # shapely 解析失败
            raise GeoValidationError(f"乡镇 {township_id} 几何无法解析：{exc}") from exc

        if geom.is_empty:
            raise GeoValidationError(f"乡镇 {township_id} 几何为空")
        if not geom.is_valid:
            raise GeoValidationError(
                f"乡镇 {township_id} 几何无效（自交/环未闭合等），"
                f"原因：{is_valid_reason(geom)}"
            )
        if geom.area <= 0:
            raise GeoValidationError(f"乡镇 {township_id} 面面积为 0")

        parsed.append((township_id, geom))

    return parsed


def to_storage_geometries(
    parsed: Iterable[tuple[str, BaseGeometry]],
) -> list[tuple[str, dict]]:
    """把 Shapely 几何转回 GeoJSON dict 以便入库。"""
    return [(tid, mapping(geom)) for tid, geom in parsed]


def parse_county_boundary(payload: dict) -> BaseGeometry:
    """解析并校验单个县界（Polygon / MultiPolygon）。

    兼容三种 GeoJSON 载荷：
    - 纯几何对象 {"type": "Polygon"|"MultiPolygon", ...}
    - 单个 Feature {"type": "Feature", "geometry": ...}
    - 仅含一个面要素的 FeatureCollection
    任一不合法均抛 GeoValidationError，由调用方拒收且不触碰旧县界。
    """
    if not isinstance(payload, dict):
        raise GeoValidationError("请求体必须是 GeoJSON 对象")

    _check_geojson_crs(payload)
    ptype = payload.get("type")
    if ptype in ALLOWED_GEOM_TYPES:
        geom_dict = payload
    elif ptype == "Feature":
        geom_dict = payload.get("geometry")
        if not isinstance(geom_dict, dict):
            raise GeoValidationError("Feature 缺少 geometry")
        _check_geojson_crs(geom_dict)
    elif ptype == "FeatureCollection":
        features = payload.get("features")
        if not isinstance(features, list) or len(features) != 1:
            raise GeoValidationError("县界 FeatureCollection 必须且只能含一个面要素")
        feature = features[0]
        if not isinstance(feature, dict) or feature.get("type") != "Feature":
            raise GeoValidationError("县界要素不是合法 GeoJSON Feature")
        geom_dict = feature.get("geometry")
        if not isinstance(geom_dict, dict):
            raise GeoValidationError("县界要素缺少 geometry")
    else:
        raise GeoValidationError(
            "县界载荷需为 Polygon/MultiPolygon 几何、单个 Feature 或"
            "单要素 FeatureCollection"
        )

    if geom_dict.get("type") not in ALLOWED_GEOM_TYPES:
        raise GeoValidationError(
            f"县界几何类型 {geom_dict.get('type')} 不支持，仅允许 Polygon / MultiPolygon"
        )
    check_wgs84_range(geom_dict)

    try:
        geom = shape(geom_dict)
    except Exception as exc:
        raise GeoValidationError(f"县界几何无法解析：{exc}") from exc

    if geom.is_empty:
        raise GeoValidationError("县界几何为空")
    if not geom.is_valid:
        raise GeoValidationError(
            f"县界几何无效（自交/环未闭合等），原因：{is_valid_reason(geom)}"
        )
    if geom.area <= 0:
        raise GeoValidationError("县界面面积为 0")
    return geom


# ----------------------------------------------------------------------
# 等面积投影
# ----------------------------------------------------------------------
def _centroid_lonlat(geoms: list[BaseGeometry]) -> tuple[float, float]:
    """所有几何并集的质心经纬度，作为投影中央经线/纬度原点。"""
    union = unary_union(geoms)
    lon, lat = union.centroid.x, union.centroid.y
    # Albers 标准纬线相对纬度原点对称偏移 5 度（并夹在合法区间内）
    return lon, lat


def build_equal_area_crs(geoms: list[BaseGeometry]) -> CRS:
    """构造以数据中心为基准的 Albers 等面积投影 CRS（单位：米）。

    中央经线取质心经度，纬度原点取质心纬度，标准纬线偏移 ±5°，
    保证县域范围内面积失真最小且严格等面积。
    """
    lon0, lat0 = _centroid_lonlat(geoms)
    std1 = max(-80.0, min(80.0, lat0 - 5.0))
    std2 = max(-80.0, min(80.0, lat0 + 5.0))
    if std1 == std2:
        std2 = min(80.0, std1 + 0.1)
    return CRS.from_dict(
        {
            "proj": "aea",
            "lat_1": std1,
            "lat_2": std2,
            "lat_0": lat0,
            "lon_0": lon0,
            "x_0": 0.0,
            "y_0": 0.0,
            "datum": "WGS84",
            "units": "m",
            "no_defs": True,
        }
    )


def project_geoms(
    geoms: list[BaseGeometry], target_crs: CRS
) -> list[BaseGeometry]:
    transformer = Transformer.from_crs(
        WGS84, target_crs, always_xy=True, accuracy=1.0
    )

    def _reproject(coords):
        # shapely 2.x 传入 shape (n,2)/(n,3) 坐标数组
        xs, ys = transformer.transform(coords[:, 0], coords[:, 1])
        if coords.shape[1] >= 3:
            import numpy as np

            return np.column_stack([xs, ys, coords[:, 2]])
        import numpy as np

        return np.column_stack([xs, ys])

    return [shp_transform(g, _reproject) for g in geoms]


def project_geoms_shared(
    geoms: list[BaseGeometry], target_crs: CRS
) -> list[BaseGeometry]:
    """带坐标点缓存的批量投影：同一 WGS84 坐标在所有几何中投影为同一投影点。

    等面积投影会把 WGS84 直线密化为曲线；若县界与乡镇的共边只在各自几何内
    独立密化，同一经线处的中间密化点不一致会产生数米宽的“伪缝隙/伪越界”。
    投影变换对确定输入是确定性的，因此按坐标点缓存即可让共边（含公共顶点）
    在投影后逐点严格重合，仅共边仍为零面积，不会引入正面积伪异常。
    """
    import numpy as np

    transformer = Transformer.from_crs(
        WGS84, target_crs, always_xy=True, accuracy=1.0
    )
    cache: dict[tuple, tuple[float, float]] = {}

    def _reproject_cached(coords):
        out = np.empty((coords.shape[0], 2), dtype=np.float64)
        for i in range(coords.shape[0]):
            # 9 位小数约 0.1 毫米，远超边界核对所需精度，且保证同点同键
            key = (round(float(coords[i, 0]), 9), round(float(coords[i, 1]), 9))
            projected = cache.get(key)
            if projected is None:
                x, y = transformer.transform(key[0], key[1])
                projected = (float(x), float(y))
                cache[key] = projected
            out[i, 0], out[i, 1] = projected
        return out

    return [shp_transform(g, _reproject_cached) for g in geoms]


def load_and_project(
    stored: list[tuple[str, dict]], target_crs: CRS
) -> list[tuple[str, BaseGeometry]]:
    """从库存 GeoJSON 重建几何并投影到目标等面积 CRS。"""
    geoms = [shape(g) for _, g in stored]
    projected = project_geoms(geoms, target_crs)
    return [(tid, pg) for (tid, _), pg in zip(stored, projected)]


# ----------------------------------------------------------------------
# 新版互斥检查与权重计算
# ----------------------------------------------------------------------
def find_overlapping_pairs(
    features: list[tuple[str, BaseGeometry]],
) -> list[tuple[str, str]]:
    """返回同版乡镇之间所有“可能正面积交叠”的乡镇号配对。

    基于空间索引做候选筛选；面积是否为正（共边/噪声过滤）交给
    find_positive_overlaps 在等面积投影下按平方米阈值判定。
    """
    pairs: list[tuple[str, str]] = []
    n = len(features)
    try:
        from shapely.strtree import STRtree

        geoms = [g for _, g in features]
        tree = STRtree(geoms)
        candidate_pairs: set[tuple[int, int]] = set()
        for i, geom in enumerate(geoms):
            for j in tree.query(geom):
                if int(j) > i:
                    candidate_pairs.add((i, int(j)))
    except Exception:
        candidate_pairs = {(i, j) for i in range(n) for j in range(i + 1, n)}

    for i, j in sorted(candidate_pairs):
        id_a, geom_a = features[i]
        id_b, geom_b = features[j]
        # disjoint 排除完全不相交；touches 排除仅共边/接触（无正面积交叠）
        if not geom_a.disjoint(geom_b) and not geom_a.touches(geom_b):
            pairs.append((id_a, id_b))
    return pairs


def find_positive_overlaps(
    features: list[tuple[str, BaseGeometry]],
) -> list[dict]:
    """检查同版乡镇之间是否存在正面积交叠（输入须为等面积投影后的几何）。

    采用空间索引避免 O(n²) 全配对；面积阈值（平方米）以下视为仅共边/
    数值噪声。返回 [{township_a, township_b, overlap_area_m2}, ...]。
    """
    overlaps: list[dict] = []
    n = len(features)
    try:
        from shapely.strtree import STRtree

        geoms = [g for _, g in features]
        tree = STRtree(geoms)
        candidate_pairs: set[tuple[int, int]] = set()
        for i, geom in enumerate(geoms):
            for j in tree.query(geom):
                if int(j) > i:
                    candidate_pairs.add((i, int(j)))
    except Exception:
        candidate_pairs = {(i, j) for i in range(n) for j in range(i + 1, n)}

    for i, j in sorted(candidate_pairs):
        id_a, geom_a = features[i]
        id_b, geom_b = features[j]
        inter = geom_a.intersection(geom_b)
        area = float(inter.area)
        if area > config.POSITIVE_AREA_EPS_M2:
            overlaps.append(
                {
                    "township_a": id_a,
                    "township_b": id_b,
                    "overlap_area_m2": round(
                        area, config.AREA_ROUND_DIGITS
                    ),
                }
            )
    return overlaps


def compute_weights(
    old_features: list[tuple[str, BaseGeometry]],
    new_features: list[tuple[str, BaseGeometry]],
) -> list[dict]:
    """逐个旧乡镇计算与各新乡镇的交叠权重。

    返回每个旧乡镇一条记录：
    {
      "old_township_id", "old_area_m2", "uncovered_area_m2",
      "coverage_ratio", "overlaps": [
        {"new_township_id", "intersection_area_m2", "ratio_of_old"}
      ]
    }
    洞与仅共边部分：Shapely 交集天然不含洞面积，共边交集面积为 0 会被过滤。
    """
    try:
        from shapely.strtree import STRtree

        new_geoms = [g for _, g in new_features]
        tree = STRtree(new_geoms)
    except Exception:
        tree = None

    results: list[dict] = []
    r_digits = config.AREA_ROUND_DIGITS
    p_digits = config.RATIO_ROUND_DIGITS

    for old_id, old_geom in old_features:
        old_area = float(old_geom.area)
        overlaps: list[dict] = []
        covered_area = 0.0

        if tree is not None:
            candidates = tree.query(old_geom)
        else:
            candidates = list(range(len(new_features)))

        for j in candidates:
            new_id, new_geom = new_features[int(j)]
            inter = old_geom.intersection(new_geom)
            inter_area = float(inter.area)
            # 仅共边/接触 => 面积 0；阈值以下按噪声忽略
            if inter_area <= config.POSITIVE_AREA_EPS_M2:
                continue
            ratio = inter_area / old_area if old_area > 0 else 0.0
            covered_area += inter_area
            overlaps.append(
                {
                    "new_township_id": new_id,
                    "intersection_area_m2": round(inter_area, r_digits),
                    "ratio_of_old": round(ratio, p_digits),
                }
            )

        # 输出按交叠面积降序，便于人工核对
        overlaps.sort(key=lambda x: x["intersection_area_m2"], reverse=True)

        uncovered = max(0.0, old_area - covered_area)
        results.append(
            {
                "old_township_id": old_id,
                "old_area_m2": round(old_area, r_digits),
                "covered_area_m2": round(covered_area, r_digits),
                "uncovered_area_m2": round(uncovered, r_digits),
                "coverage_ratio": round(
                    covered_area / old_area if old_area > 0 else 0.0,
                    p_digits,
                ),
                "overlaps": overlaps,
            }
        )

    results.sort(key=lambda x: x["old_township_id"])
    return results


def union_area(geoms: list[BaseGeometry]) -> float:
    """并集面积（诊断/自检用）。"""
    if not geoms:
        return 0.0
    return float(unary_union(geoms).area)


# ----------------------------------------------------------------------
# 县界覆盖核验
# ----------------------------------------------------------------------
def _iter_polygon_parts(geom: BaseGeometry):
    """把 Polygon/MultiPolygon/GeometryCollection 拆成单个 Polygon。"""
    from shapely.geometry import MultiPolygon, Polygon

    if geom.is_empty:
        return
    if isinstance(geom, Polygon):
        yield geom
    elif isinstance(geom, MultiPolygon):
        for part in geom.geoms:
            yield part
    else:  # GeometryCollection：递归取其中的面，忽略点/线残余
        for part in geom.geoms:
            yield from _iter_polygon_parts(part)


def verify_county_coverage(
    county_geom_wgs: BaseGeometry,
    township_features_wgs: list[tuple[str, BaseGeometry]],
) -> dict:
    """核验县界与同版乡镇面的覆盖关系。

    分两层处理，兼顾拓扑正确性与面积口径：
    - 拓扑层（WGS84 原始坐标）：县界与乡镇共边在原始坐标下逐点精确重合，
      在此做差集不会因投影曲线密化方式不同而产生数米宽的伪缝隙。
      县界内空白（漏划）= 县界 − 乡镇并集；县界自带内环（洞）由 Shapely
      差集语义自然保留；县界外多划（越界）= 乡镇并集 − 县界；
      仅共边/接触的差集本身为空，天然不算异常。
    - 计量层（等面积投影，单位米）：以全部几何的数据中心构造同一 Albers
      等面积 CRS，把差集图斑与乡镇面投影后量平方米面积；交付图斑再
      反投影回 WGS84。乡镇两两正面积互叠在投影坐标下按平方米阈值判定，
      附冲突乡镇号。
    三类异常均无正面积时 passed 才为 True。
    """
    township_geoms_wgs = [g for _, g in township_features_wgs]
    all_geoms = [county_geom_wgs] + township_geoms_wgs
    equal_area_crs = build_equal_area_crs(all_geoms)

    # 1. WGS84 拓扑运算：共边精确、洞保留、仅接触差集为空
    township_union_wgs = unary_union(township_geoms_wgs)
    gap_wgs = county_geom_wgs.difference(township_union_wgs)
    outside_wgs = township_union_wgs.difference(county_geom_wgs)

    # 2. 图斑投影到等面积 CRS 量面积，并反投影回 WGS84 交付
    inverse = Transformer.from_crs(
        equal_area_crs, WGS84, always_xy=True, accuracy=1.0
    )
    gap_patches, gap_raw = _project_measure_patches(
        gap_wgs, equal_area_crs, inverse, "gap"
    )
    outside_patches, outside_raw = _project_measure_patches(
        outside_wgs, equal_area_crs, inverse, "outside"
    )

    # 3. 乡镇两两正面积互叠（等面积、平方米，附冲突乡镇号）
    township_proj = project_geoms_shared(township_geoms_wgs, equal_area_crs)
    overlaps = find_positive_overlaps(
        [(tid, pg) for (tid, _), pg in zip(township_features_wgs, township_proj)]
    )

    r_digits = config.AREA_ROUND_DIGITS
    return {
        "passed": not gap_patches and not outside_patches and not overlaps,
        "area_unit": "m2",
        "crs": equal_area_crs.to_wkt(version="WKT2_2019"),
        "summary": {
            "township_count": len(township_features_wgs),
            "gap_patch_count": len(gap_patches),
            "outside_patch_count": len(outside_patches),
            "overlap_pair_count": len(overlaps),
            "gap_area_m2": round(gap_raw, r_digits),
            "outside_area_m2": round(outside_raw, r_digits),
        },
        "gaps": {"type": "FeatureCollection", "features": gap_patches},
        "outside": {"type": "FeatureCollection", "features": outside_patches},
        "township_overlaps": overlaps,
    }


def _project_measure_patches(
    diff_wgs: BaseGeometry,
    equal_area_crs: CRS,
    inverse: Transformer,
    kind: str,
) -> tuple[list[dict], float]:
    """把 WGS84 差集拆成单个面，投影到等面积 CRS 量面积并反投影回 WGS84。

    返回 (GeoJSON Feature 列表, 保留图斑的等面积面积合计 m2)。
    投影后面积 <= POSITIVE_AREA_EPS_M2 的部分视为仅共边/数值噪声，不计入异常。
    """
    import numpy as np

    forward = Transformer.from_crs(
        WGS84, equal_area_crs, always_xy=True, accuracy=1.0
    )

    def _forward(coords):
        xs, ys = forward.transform(coords[:, 0], coords[:, 1])
        return np.column_stack([xs, ys])

    def _back(coords):
        xs, ys = inverse.transform(coords[:, 0], coords[:, 1])
        return np.column_stack([xs, ys])

    eps = config.POSITIVE_AREA_EPS_M2
    r_digits = config.AREA_ROUND_DIGITS
    patches: list[dict] = []
    total_raw = 0.0
    for part in _iter_polygon_parts(diff_wgs):
        projected_part = shp_transform(part, _forward)
        area_m2 = float(projected_part.area)
        if area_m2 <= eps:
            continue
        total_raw += area_m2
        # 交付几何回到 WGS84（与输入同一坐标系），面积以等面积投影为准
        wgs_geom = shp_transform(projected_part, _back)
        patches.append(
            {
                "type": "Feature",
                "properties": {
                    "kind": kind,
                    "area_m2": round(area_m2, r_digits),
                },
                "geometry": mapping(wgs_geom),
            }
        )
    return patches, total_raw


__all__ = [
    "ALLOWED_GEOM_TYPES",
    "GeoValidationError",
    "WGS84",
    "build_equal_area_crs",
    "check_wgs84_range",
    "compute_weights",
    "find_overlapping_pairs",
    "find_positive_overlaps",
    "load_and_project",
    "parse_county_boundary",
    "parse_feature_collection",
    "project_geoms",
    "project_geoms_shared",
    "to_storage_geometries",
    "union_area",
    "verify_county_coverage",
]
