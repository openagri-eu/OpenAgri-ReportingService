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
    target_aspect_ratio: float = 4 / 3,
) -> Tuple[float, float, float, float]:
    """
    Returns (min_lon, min_lat, max_lon, max_lat) padded by padding_ratio on
    each side.

    The bbox is always widened (never shrunk) to force its real-world km
    ratio to exactly target_aspect_ratio, landscape, regardless of the
    parcel's own shape - a tall/narrow parcel just zooms out further on its
    short axis. This keeps every fetched map at the same fixed ratio so
    placing it in the PDF never stretches or distorts it.
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

    if aspect > target_aspect_ratio:
        lat_span = (lon_km / target_aspect_ratio) / KM_PER_DEGREE_LAT
    elif aspect < target_aspect_ratio:
        lon_span = (lat_km * target_aspect_ratio) / km_per_degree_lon

    center_lon = (min_lon + max_lon) / 2
    center_lat = (min_lat + max_lat) / 2
    half_lon = (lon_span * (1 + padding_ratio)) / 2
    half_lat = (lat_span * (1 + padding_ratio)) / 2
    return (center_lon - half_lon, center_lat - half_lat, center_lon + half_lon, center_lat + half_lat)
