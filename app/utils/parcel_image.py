import io
import logging
import math

from fpdf.enums import PathPaintRule
from fpdf.drawing import DeviceRGB

from utils.osm_static_map import fetch_osm_map_for_bbox, lonlat_to_pixel_in_crop, OSMMapException
from utils.parcel_geometry import parse_wkt_rings, compute_padded_bbox, compute_point_bbox, ParcelGeometryException

logger = logging.getLogger(__name__)

PARCEL_IMAGE_WIDTH_MM = 100
PARCEL_IMAGE_ASPECT_RATIO = 4 / 3
PARCEL_POINT_MARKER_RADIUS_MM = 2.5


def place_centered_parcel_image(pdf, image_bytes: bytes, aspect: float):
    """
    Place a parcel image centered on the page at a fixed width; height
    follows from aspect (pixel_width / pixel_height).
    """
    pdf.ln(2)
    w = PARCEL_IMAGE_WIDTH_MM
    h = PARCEL_IMAGE_WIDTH_MM / aspect
    x_start = (pdf.w - w) / 2
    pdf.set_x(x_start)
    info = pdf.image(io.BytesIO(image_bytes), type="png", w=w, h=h)
    return info, x_start


def render_parcel_geometry_image(pdf, parcel_data) -> bool:
    """
    Render an OSM map image (same tile source as the dashboard's parcel edit
    map) sized to the parcel's real geometry extent, with the parcel
    boundary drawn as a line overlay on top. Returns True on success, False
    if there is no usable geometry or the fetch/parse fails (caller should
    fall back to render_parcel_point_image).

    OSM raster tiles are used instead of the WMS satellite layer for this
    close a zoom: EOX's Sentinel-2 imagery is ~10m/pixel, so a single parcel
    (typically a few hundred meters across) only has a few dozen real source
    pixels - the rest is heavy upscaling/blur. OSM tiles are pre-rendered
    vector data, so they stay sharp at any zoom level.
    """
    if not parcel_data.geometry_wkt:
        return False

    # Fetch + parse failures happen before anything is drawn, so the caller
    # can cleanly fall back to the point-centered OSM image instead.
    try:
        rings = parse_wkt_rings(parcel_data.geometry_wkt)
        bbox = compute_padded_bbox(rings, target_aspect_ratio=PARCEL_IMAGE_ASPECT_RATIO)
        image_bytes, zoom, origin_x, origin_y, px_w, px_h = fetch_osm_map_for_bbox(*bbox)
    except (ParcelGeometryException, OSMMapException) as e:
        logger.info(f"Parcel geometry image unavailable, falling back: {e}")
        return False

    info, x_start = place_centered_parcel_image(pdf, image_bytes, aspect=px_w / px_h)
    # pdf.image() may trigger fpdf2's own auto-page-break internally (when y
    # isn't given explicitly and the image doesn't fit in the remaining
    # space), which moves to a new page before drawing - so the image's
    # actual top can't be read reliably until *after* placing it, by which
    # point pdf.y has already been advanced by the rendered height.
    y_start = pdf.get_y() - info.rendered_height

    # The map image is already on the page at this point, so a failure here
    # (e.g. a pathological ring) should not fall back to drawing a second,
    # different image on top - just skip the boundary overlay and keep the
    # map.
    original_draw_color = pdf.draw_color
    original_line_width = pdf.line_width
    try:
        pdf.set_draw_color(255, 40, 40)
        pdf.set_line_width(0.6)
        for ring in rings:
            points = []
            for point in ring:
                # WKT can carry a Z (elevation) coordinate - POLYGON Z rings
                # yield 3-tuples from shapely; only lon/lat matter here.
                lon, lat = point[0], point[1]
                px, py = lonlat_to_pixel_in_crop(lon, lat, zoom, origin_x, origin_y)
                points.append((
                    x_start + (px / px_w) * info.rendered_width,
                    y_start + (py / px_h) * info.rendered_height,
                ))
            pdf.polygon(points, style="D")
    except Exception as e:
        logger.error(f"Error drawing parcel boundary overlay, map image kept without it: {e}")
    finally:
        pdf.set_draw_color(original_draw_color)
        pdf.set_line_width(original_line_width)
    return True


def draw_pin_marker(pdf, tip_x: float, tip_y: float, radius_mm: float = PARCEL_POINT_MARKER_RADIUS_MM) -> None:
    """
    Draw a classic map pin (teardrop) with its tip - the exact location -
    at (tip_x, tip_y) and its round head above it, built from tangent lines
    and a circular arc rather than an icon file, so it scales cleanly.
    """
    distance_to_tip = 2.4 * radius_mm
    tangent_angle = math.acos(1 / 2.4)
    head_x, head_y = tip_x, tip_y - distance_to_tip
    angle_right = math.radians(90) - tangent_angle
    angle_left = math.radians(90) + tangent_angle
    tangent_right = (
        head_x + radius_mm * math.cos(angle_right),
        head_y + radius_mm * math.sin(angle_right),
    )
    tangent_left = (
        head_x + radius_mm * math.cos(angle_left),
        head_y + radius_mm * math.sin(angle_left),
    )

    with pdf.new_path(paint_rule=PathPaintRule.STROKE_FILL_NONZERO) as path:
        path.style.fill_color = DeviceRGB(0.86, 0.13, 0.13)
        path.style.stroke_color = DeviceRGB(0.55, 0.05, 0.05)
        path.style.stroke_width = 0.25
        path.move_to(tip_x, tip_y)
        path.line_to(*tangent_left)
        path.arc_to(rx=radius_mm, ry=radius_mm, rotation=0, large_arc=True, positive_sweep=True, x=tangent_right[0], y=tangent_right[1])
        path.line_to(tip_x, tip_y)
        path.close()

    with pdf.new_path(paint_rule=PathPaintRule.FILL_NONZERO) as path:
        path.style.fill_color = DeviceRGB(1, 1, 1)
        path.circle(head_x, head_y, radius_mm * 0.38)


def render_parcel_point_image(pdf, lat: float, lon: float) -> bool:
    """
    Render an OSM map centered on (lat, lon) with a pin marker, for when no
    parcel boundary is available - only a single point location is known.
    """
    try:
        bbox = compute_point_bbox(lat, lon, target_aspect_ratio=PARCEL_IMAGE_ASPECT_RATIO)
        image_bytes, zoom, origin_x, origin_y, px_w, px_h = fetch_osm_map_for_bbox(*bbox)
    except OSMMapException as e:
        logger.info(f"Point map unavailable: {e}")
        return False

    info, x_start = place_centered_parcel_image(pdf, image_bytes, aspect=px_w / px_h)
    y_start = pdf.get_y() - info.rendered_height

    px, py = lonlat_to_pixel_in_crop(lon, lat, zoom, origin_x, origin_y)
    marker_x = x_start + (px / px_w) * info.rendered_width
    marker_y = y_start + (py / px_h) * info.rendered_height

    try:
        draw_pin_marker(pdf, marker_x, marker_y)
    except Exception as e:
        logger.error(f"Error drawing point marker, map image kept without it: {e}")
    return True
