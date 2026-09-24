from typing import List, Tuple

from shapely import wkt as shapely_wkt


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
    rings: List[List[Tuple[float, float]]], padding_ratio: float = 0.15
) -> Tuple[float, float, float, float]:
    """Returns (min_lon, min_lat, max_lon, max_lat) padded by padding_ratio on each side."""
    lons = [pt[0] for ring in rings for pt in ring]
    lats = [pt[1] for ring in rings for pt in ring]
    min_lon, max_lon = min(lons), max(lons)
    min_lat, max_lat = min(lats), max(lats)

    lon_span = (max_lon - min_lon) or 0.0005
    lat_span = (max_lat - min_lat) or 0.0005
    pad_lon = lon_span * padding_ratio
    pad_lat = lat_span * padding_ratio
    return (min_lon - pad_lon, min_lat - pad_lat, max_lon + pad_lon, max_lat + pad_lat)
