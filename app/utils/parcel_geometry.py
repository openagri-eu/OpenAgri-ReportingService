import math
from typing import List, Tuple

from shapely import wkt as shapely_wkt

KM_PER_DEGREE_LAT = 111.32


class ParcelGeometryException(Exception):
    pass


def parse_wkt_rings(wkt_str: str) -> List[List[Tuple[float, float]]]:
    """
    Parse a WKT Polygon/MultiPolygon into a list of exterior rings, each a
    list of (lon, lat) tuples. Raises ParcelGeometryException on invalid or
    unsupported input.
    """
    try:
        geom = shapely_wkt.loads(wkt_str)
    except Exception as e:
        raise ParcelGeometryException(f"Could not parse WKT geometry: {e}")

    if geom.geom_type == "Polygon":
        polygons = [geom]
    elif geom.geom_type == "MultiPolygon":
        polygons = list(geom.geoms)
    else:
        raise ParcelGeometryException(f"Unsupported geometry type: {geom.geom_type}")

    rings = [list(p.exterior.coords) for p in polygons if not p.is_empty]
    if not rings:
        raise ParcelGeometryException("Geometry has no exterior rings")
    return rings


def compute_padded_bbox(
    rings: List[List[Tuple[float, float]]],
    padding_ratio: float = 0.15,
    max_aspect_ratio: float = 2.5,
) -> Tuple[float, float, float, float]:
    """
    Returns (min_lon, min_lat, max_lon, max_lat) padded by padding_ratio on
    each side.

    A long, narrow parcel (e.g. an irrigated strip field) has a real-world
    width:height ratio that can be extreme. Rather than rendering the map at
    that same extreme ratio - which either overflows the page or squeezes
    down to an unreadably thin sliver - the shorter side's padding is
    widened so the fetched map never exceeds max_aspect_ratio:1 (or 1:that,
    for a tall/narrow shape). This shows more surrounding context on the
    short axis, the same way a map naturally "zooms out" a bit further to
    keep an elongated feature legible, instead of cropping tightly to it.
    """
    lons = [pt[0] for ring in rings for pt in ring]
    lats = [pt[1] for ring in rings for pt in ring]
    min_lon, max_lon = min(lons), max(lons)
    min_lat, max_lat = min(lats), max(lats)

    lon_span = (max_lon - min_lon) or 0.0005
    lat_span = (max_lat - min_lat) or 0.0005

    mean_lat_rad = math.radians((min_lat + max_lat) / 2)
    km_per_degree_lon = KM_PER_DEGREE_LAT * math.cos(mean_lat_rad) or 1e-9
    lon_km = lon_span * km_per_degree_lon
    lat_km = lat_span * KM_PER_DEGREE_LAT
    aspect = lon_km / lat_km

    if aspect > max_aspect_ratio:
        # Too wide relative to its height - widen the height (in degrees)
        # until width:height == max_aspect_ratio.
        lat_span = (lon_km / max_aspect_ratio) / KM_PER_DEGREE_LAT
    elif aspect < 1 / max_aspect_ratio:
        # Too tall relative to its width - widen the width instead.
        lon_span = (lat_km / max_aspect_ratio) / km_per_degree_lon

    center_lon = (min_lon + max_lon) / 2
    center_lat = (min_lat + max_lat) / 2
    half_lon = (lon_span * (1 + padding_ratio)) / 2
    half_lat = (lat_span * (1 + padding_ratio)) / 2
    return (center_lon - half_lon, center_lat - half_lat, center_lon + half_lon, center_lat + half_lat)
