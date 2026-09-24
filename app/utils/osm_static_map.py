import io
import math

import requests
from PIL import Image

from core import settings

TILE_SIZE = 256


class OSMMapException(Exception):
    pass


def _lonlat_to_pixel(lon: float, lat: float, zoom: int) -> tuple[float, float]:
    n = 2.0 ** zoom
    x = (lon + 180.0) / 360.0 * n * TILE_SIZE
    lat_rad = math.radians(lat)
    y = (1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n * TILE_SIZE
    return x, y


def _pick_zoom(
    bbox: tuple[float, float, float, float],
    max_tiles_per_side: int = 6,
    max_zoom: int = 19,
) -> int:
    min_lon, min_lat, max_lon, max_lat = bbox
    for zoom in range(max_zoom, 0, -1):
        x1, y1 = _lonlat_to_pixel(min_lon, max_lat, zoom)
        x2, y2 = _lonlat_to_pixel(max_lon, min_lat, zoom)
        if (x2 - x1) / TILE_SIZE <= max_tiles_per_side and (y2 - y1) / TILE_SIZE <= max_tiles_per_side:
            return zoom
    return 1


def fetch_osm_map_for_bbox(
    min_lon: float,
    min_lat: float,
    max_lon: float,
    max_lat: float,
    max_tiles_per_side: int = 6,
):
    """
    Fetch and stitch OSM raster tiles covering the given bbox, cropped
    exactly to it - the same tile source used by the dashboard's parcel edit
    map (OpenLayers + OSM), just rendered server-side instead of in-browser.

    Returns (png_bytes, zoom, origin_x, origin_y, pixel_width, pixel_height).
    origin_x/origin_y are the pixel coordinates (in the given zoom level's
    global tile space) of the crop's top-left corner - callers need these,
    together with zoom, to place a (lon, lat) overlay point precisely within
    the returned image via lonlat_to_pixel_in_crop().
    """
    zoom = _pick_zoom((min_lon, min_lat, max_lon, max_lat), max_tiles_per_side)

    top_left_x, top_left_y = _lonlat_to_pixel(min_lon, max_lat, zoom)
    bottom_right_x, bottom_right_y = _lonlat_to_pixel(max_lon, min_lat, zoom)

    tile_x_min = int(top_left_x // TILE_SIZE)
    tile_x_max = int(bottom_right_x // TILE_SIZE)
    tile_y_min = int(top_left_y // TILE_SIZE)
    tile_y_max = int(bottom_right_y // TILE_SIZE)

    canvas = Image.new("RGB", ((tile_x_max - tile_x_min + 1) * TILE_SIZE, (tile_y_max - tile_y_min + 1) * TILE_SIZE))

    headers = {"User-Agent": settings.REPORTING_OSM_USER_AGENT}
    for tx in range(tile_x_min, tile_x_max + 1):
        for ty in range(tile_y_min, tile_y_max + 1):
            url = settings.REPORTING_OSM_TILE_URL.format(z=zoom, x=tx, y=ty)
            try:
                resp = requests.get(url, headers=headers, timeout=10)
                resp.raise_for_status()
                tile_img = Image.open(io.BytesIO(resp.content)).convert("RGB")
            except Exception as e:
                raise OSMMapException(f"Error fetching OSM tile {tx},{ty}@{zoom}: {e}")
            canvas.paste(tile_img, ((tx - tile_x_min) * TILE_SIZE, (ty - tile_y_min) * TILE_SIZE))

    crop_left = int(round(top_left_x)) - tile_x_min * TILE_SIZE
    crop_top = int(round(top_left_y)) - tile_y_min * TILE_SIZE
    crop_right = int(round(bottom_right_x)) - tile_x_min * TILE_SIZE
    crop_bottom = int(round(bottom_right_y)) - tile_y_min * TILE_SIZE
    cropped = canvas.crop((crop_left, crop_top, crop_right, crop_bottom))

    origin_x = tile_x_min * TILE_SIZE + crop_left
    origin_y = tile_y_min * TILE_SIZE + crop_top

    buf = io.BytesIO()
    cropped.save(buf, format="PNG")
    return buf.getvalue(), zoom, origin_x, origin_y, cropped.width, cropped.height


def lonlat_to_pixel_in_crop(
    lon: float, lat: float, zoom: int, origin_x: float, origin_y: float
) -> tuple[float, float]:
    """Pixel offset of (lon, lat) within the image returned by fetch_osm_map_for_bbox."""
    x, y = _lonlat_to_pixel(lon, lat, zoom)
    return x - origin_x, y - origin_y
