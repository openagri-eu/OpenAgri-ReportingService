import io
import json
import logging
import math
import os
from datetime import datetime
from typing import Optional, List

from fastapi import HTTPException
from fpdf.enums import PathPaintRule
from fpdf.drawing import DeviceRGB

from core import settings
from schemas import IrrigationOperation, FertilizationOperation, CropProtectionOperation
from utils.osm_static_map import fetch_osm_map_for_bbox, lonlat_to_pixel_in_crop, OSMMapException
from utils.parcel_geometry import parse_wkt_rings, compute_padded_bbox, compute_point_bbox, ParcelGeometryException
from utils import EX, add_fonts, decode_dates_filters, get_parcel_info, display_pdf_parcel_details, FarmInfo, notify_stress_test_callback
from utils.farm_calendar_report import geolocator
from utils.generate_aggregation_data import (
    generate_amount_per_hectare,
    prepare_df_for_calculations,
    generate_aggregation_table_data,
    get_pest_from_obj,
    pesticides_aggregation,
)
from utils.json_handler import make_get_request

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def parse_irrig_fert_operations(
    data: dict,
    irrigation_flag: bool = True,
    fertilization_flag: bool = False,
) -> Optional[
    List[IrrigationOperation | FertilizationOperation | CropProtectionOperation]
]:
    """
    Parse list of irrigation or fertilization operations from JSON data
    """
    try:
        if irrigation_flag:
            return [IrrigationOperation.model_validate(item) for item in data]
        elif fertilization_flag:
            return [FertilizationOperation.model_validate(item) for item in data]
        else:
            return [CropProtectionOperation.model_validate(item) for item in data]
    except Exception as e:
        logger.error(f"Error parsing irrigation/fertilization operations: {e}")
        raise HTTPException(
            status_code=400,
            detail=f"Reporting service failed during PDF generation. File is not correct JSON. {e}",
        )


PARCEL_IMAGE_WIDTH_MM = 100
PARCEL_IMAGE_ASPECT_RATIO = 4 / 3


def _place_centered_parcel_image(pdf: EX, image_bytes: bytes, aspect: float):
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


def _render_parcel_geometry_image(pdf: EX, parcel_data) -> bool:
    """
    Render an OSM map image (same tile source as the dashboard's parcel edit
    map) sized to the parcel's real geometry extent, with the parcel
    boundary drawn as a line overlay on top. Returns True on success, False
    if there is no usable geometry or the fetch/parse fails (caller should
    fall back to _render_parcel_point_image).

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

    info, x_start = _place_centered_parcel_image(pdf, image_bytes, aspect=px_w / px_h)
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


PARCEL_POINT_MARKER_RADIUS_MM = 2.5


def _draw_pin_marker(pdf: EX, tip_x: float, tip_y: float, radius_mm: float = PARCEL_POINT_MARKER_RADIUS_MM) -> None:
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


def _render_parcel_point_image(pdf: EX, lat: float, lon: float) -> bool:
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

    info, x_start = _place_centered_parcel_image(pdf, image_bytes, aspect=px_w / px_h)
    y_start = pdf.get_y() - info.rendered_height

    px, py = lonlat_to_pixel_in_crop(lon, lat, zoom, origin_x, origin_y)
    marker_x = x_start + (px / px_w) * info.rendered_width
    marker_y = y_start + (py / px_h) * info.rendered_height

    try:
        _draw_pin_marker(pdf, marker_x, marker_y)
    except Exception as e:
        logger.error(f"Error drawing point marker, map image kept without it: {e}")
    return True


def create_pdf_from_operations(
    operations: List[IrrigationOperation]
    | List[FertilizationOperation | CropProtectionOperation],
    token: dict[str, str] = None,
    data_used: bool = False,
    parcel_id: str = None,
    from_date: datetime.date = None,
    to_date: datetime.date = None,
    irrigation_flag: bool = True,
    fertilization_flag: bool = False,
):
    """
    Create PDF report from irrigation operations
    """
    pdf = EX()
    add_fonts(pdf)
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    EX.ln(pdf)

    today = datetime.now().strftime("%d/%m/%Y")
    pdf.set_font("FreeSerif", "B", 14)
    title = "Pesticide"
    if irrigation_flag:
        title = "Irrigation"
    elif fertilization_flag:
        title = "Fertilization"

    pdf.cell(0, 10, f"{title} Operation Report", ln=True, align="C")
    pdf.set_font("FreeSerif", style="", size=9)
    pdf.cell(
        0,
        7,
        f"Data Generated - {today}",
        ln=True,
        align="C",
    )
    pdf.ln(5)

    pdf.set_font("FreeSerif", "B", 12)
    pdf.set_fill_color(240, 240, 240)

    y_position = pdf.get_y()
    line_end_x = pdf.w - pdf.l_margin - pdf.r_margin
    pdf.line(pdf.l_margin, y_position, line_end_x, y_position)
    pdf.ln(5)

    address = ""
    identifier = ""
    farm = FarmInfo(description="", administrator="", vatID="", name="", municipality="", contactPerson="")
    parcel_defined = None
    from_date_local, to_date_local = today, None
    if from_date:
        from_date_local = from_date.strftime("%Y-%m-%d")

    if to_date:
        to_date_local = to_date.strftime("%Y-%m-%d")
    else:
        to_date_local = ""


    pdf.set_font("FreeSerif", "B", 15)
    pdf.set_x((pdf.w/4)-30  )
    pdf.cell(30, 8, "1. Farm Details", align='L')
    pdf.multi_cell(0, 8, f"", ln=True, fill=False)
    pdf.set_font("FreeSerif", "B", 10)
    pdf.cell(40, 8, "Reporting Period")
    pdf.set_font("FreeSerif", "", 10)
    pdf.multi_cell(0, 8, f"{from_date_local} / {to_date_local}", ln=True, fill=True)

    if parcel_id:
        parcel_data = display_pdf_parcel_details(pdf, parcel_id, geolocator, token)
        if not _render_parcel_geometry_image(pdf, parcel_data):
            if parcel_data.long != 0 and parcel_data.lat != 0:
                _render_parcel_point_image(pdf, parcel_data.lat, parcel_data.long)
        parcel_defined = True

    if len(operations) == 1:
        op = operations[0]
        if not parcel_defined:
            parcel_id = op.operatedOn.get("@id") if op.operatedOn else None
            identifier = ""
            if parcel_id:
                parcel = parcel_id.split(":")[3] if op.operatedOn else None
                if parcel:
                    parcel_data, farm, identifier = get_parcel_info(
                        parcel_id.split(":")[-1],
                        token,
                        geolocator,
                        identifier_flag=True,
                    )
                    address = parcel_data.address
        start_time = (
            op.hasStartDatetime.strftime("%d/%m/%Y") if op.hasStartDatetime else ""
        )
        end_time = op.hasEndDatetime.strftime("%d/%m/%Y") if op.hasEndDatetime else ""
        pdf.set_font("FreeSerif", "B", 10)
        pdf.cell(40, 8, "Star-End :")
        pdf.set_font("FreeSerif", "", 10)
        pdf.multi_cell(0, 8, f"{start_time}-{end_time}", ln=True, fill=True)

        pdf.set_font("FreeSerif", "B", 10)
        pdf.cell(40, 8, "Parcel Location:")
        pdf.set_font("FreeSerif", "", 10)
        pdf.multi_cell(0, 8, address, ln=True, fill=True)

        pdf.set_font("FreeSerif", "B", 10)
        pdf.cell(40, 8, "Parcel Identifier:")
        pdf.set_font("FreeSerif", "", 10)
        pdf.multi_cell(0, 8, identifier, ln=True, fill=True)

        pdf.set_font("FreeSerif", "B", 10)
        pdf.cell(
            40,
            8,
            "Farm Location:",
        )
        pdf.set_font("FreeSerif", "", 10)
        farm_local = f"Name: {farm.name} | Municipality: {farm.municipality}"
        pdf.multi_cell(0, 8, farm_local, ln=True, fill=True)

        pdf.set_font("FreeSerif", "B", 10)
        pdf.cell(
            40,
            8,
            "Administrator:",
        )
        pdf.set_font("FreeSerif", "", 10)
        pdf.multi_cell(0, 8, farm.administrator, ln=True, fill=True)

        pdf.set_font("FreeSerif", "B", 10)
        pdf.cell(
            40,
            8,
            "Contact Person:",
        )
        pdf.set_font("FreeSerif", "", 10)
        pdf.multi_cell(0, 8, farm.contactPerson, ln=True, fill=True)

        pdf.set_font("FreeSerif", "B", 10)
        pdf.cell(
            40,
            8,
            "Farm vat:",
        )
        pdf.set_font("FreeSerif", "", 10)
        pdf.multi_cell(0, 8, farm.vatID, ln=True, fill=True)

        pdf.set_font("FreeSerif", "B", 10)
        pdf.cell(
            40,
            8,
            "Farm Description:",
        )
        pdf.set_font("FreeSerif", "", 10)
        pdf.multi_cell(0, 8, farm.description, ln=True, fill=True)

        pdf.set_font("FreeSerif", "B", 10)
        pdf.cell(
            40,
            8,
            "Value info:",
        )
        pdf.set_font("FreeSerif", "", 10)
        pdf.multi_cell(
            0,
            8,
            f"{op.hasAppliedAmount.numericValue} ({op.hasAppliedAmount.unit})",
            fill=True,
        )

    if len(operations) > 1:
        if not data_used:
            operations.sort(key=lambda x: x.hasStartDatetime)
        pdf.set_font("FreeSerif", "B", 15)
        pdf.ln(2)
        pdf.set_x((pdf.w / 4) - 30)
        pdf.cell(30, 2,f"2. {title}s", align='L', ln=True)
        pdf.set_fill_color(0, 255, 255)
        pdf.ln(4)
        with pdf.table(text_align="CENTER") as table:
            row = table.row()
            pdf.set_font("FreeSerif", "B", 10)
            row.cell("Start - End")
            if not parcel_defined:
                row.cell("Parcel")
                row.cell("Parcel Identifier")
                row.cell("Farm")
            row.cell("Dose")
            row.cell("Unit")
            if irrigation_flag:
                row.cell("Irrigation System")
            elif fertilization_flag:
                row.cell("Fertilizer")
                row.cell("Application Method")
            else:
                row.cell("Pesticide")
            pdf.set_font("FreeSerif", "", 9)
            pdf.set_fill_color(255, 255, 240)
            for op in operations:
                # Operation Header
                row = table.row()
                start_time = (
                    op.hasStartDatetime.strftime("%d/%m/%Y")
                    if op.hasStartDatetime
                    else ""
                )
                end_time = (
                    op.hasEndDatetime.strftime("%d/%m/%Y") if op.hasEndDatetime else ""
                )
                row.cell(f"{start_time} - {end_time}")

                if not parcel_defined:
                    parcel_id = op.operatedOn.get("@id") if op.operatedOn else None
                    address = ""
                    farm = FarmInfo(description="", administrator="", vatID="", name="", municipality="", contactPerson="")
                    identifier = ""
                    if parcel_id:
                        parcel = parcel_id.split(":")[3] if op.operatedOn else None
                        if parcel:
                            parcel_data, farm, identifier = get_parcel_info(
                                parcel_id.split(":")[-1],
                                token,
                                geolocator,
                                identifier_flag=True,
                            )
                            address = parcel_data.address

                    row.cell(address)
                    row.cell(identifier)
                    farm_local = (
                        f"Name: {farm.name} | Municipality: {farm.municipality}"
                    )
                    row.cell(farm_local)

                row.cell(
                    f"{op.hasAppliedAmount.numericValue}",
                )
                row.cell(
                    f"{op.hasAppliedAmount.unit}",
                )

                if irrigation_flag:
                    if isinstance(op.usesIrrigationSystem, dict):
                        local_sys = op.usesIrrigationSystem.get("name")
                    else:
                        local_sys = op.usesIrrigationSystem
                    row.cell(local_sys)
                elif fertilization_flag:
                    row.cell("Yes" if op.usesFertilizer else "No")
                    row.cell(op.hasApplicationMethod)
                else:
                    pest = ""
                    if op.usesPesticide:
                        pest = get_pest_from_obj(op, token)
                    row.cell(pest)


    if operations and parcel_defined:
        if irrigation_flag:
            pdf.ln(4)
            parcel_area_m2 = max(float(parcel_data.area), 0.0)
            df_for_calc = prepare_df_for_calculations(operations, parcel_area_m2)
            amount_per_hc_graph = generate_amount_per_hectare(df_for_calc)
            pdf.add_page()
            pdf.set_font("FreeSerif", "B", 15)
            pdf.set_x((pdf.w / 4) - 30)
            pdf.cell(30, 2, "3. Graphs: ", ln=2, align='L')
            pdf.ln(2)
            pdf.set_font("FreeSerif", "", 10)
            pdf.image(amount_per_hc_graph, type="png", w=180)

            dict_average_table = generate_aggregation_table_data(df_for_calc)
            pdf.set_fill_color(0, 255, 255)
            pdf.set_font("FreeSerif", "B", 15)
            pdf.add_page()
            pdf.set_x((pdf.w / 4) - 30)
            pdf.cell(30, 2, "4. Aggregates:", align='L', ln=True)
            pdf.ln(4)
            pdf.set_font("FreeSerif", "B", 10)
            with pdf.table(text_align="CENTER") as table:
                row = table.row()
                row.cell("Data")
                row.cell("Per hectare (m3/hectare)")
                row.cell("Total volume (m3)")

                pdf.set_font("FreeSerif", "", 9)
                pdf.set_fill_color(255, 255, 240)
                for k, v in dict_average_table.items():
                    row = table.row()
                    row.cell(k)
                    row.cell(f"{v[0]:.2f}")
                    row.cell(f"{v[1]:.2f}")

        elif isinstance(operations[0], CropProtectionOperation):
            pesticide_sums = pesticides_aggregation(operations, token)
            pdf.set_fill_color(0, 255, 255)
            pdf.set_font("FreeSerif", "B", 15)
            pdf.add_page()
            pdf.set_x((pdf.w / 4) - 30)
            pdf.cell(30, 2, "3. Final report:", align='L', ln=True)
            pdf.ln(4)
            pdf.set_font("FreeSerif", "B", 10)
            with pdf.table(text_align="CENTER") as table:
                row = table.row()
                row.cell("Pesticide")
                row.cell("Total")
                pdf.set_font("FreeSerif", "", 9)
                pdf.set_fill_color(255, 255, 240)
                for _, row_df in pesticide_sums.iterrows():
                    row = table.row()
                    row.cell(row_df["Pesticide"])
                    row.cell(f"{row_df['Dose']:.2f} {row_df['Unit']}")

    return pdf


def process_irrigation_fertilization_data(
    data,
    token: dict[str, str],
    pdf_file_name: str,
    from_date: datetime.date = None,
    to_date: datetime.date = None,
    operation_id: str = None,
    parcel_id: str = None,
    irrigation_flag: bool = True,
    fertilization_flag: bool = False,
    pesticides_flag: bool = False,
) -> None:
    """
    Process irrigation data and generate PDF report
    """
    data_used = False
    url_use = "irrigations"

    if fertilization_flag:
        url_use = "fertilization"
    elif pesticides_flag:
        url_use = "pesticides"

    if operation_id:
        json_data = make_get_request(
            url=f"{settings.REPORTING_FARMCALENDAR_BASE_URL}{settings.REPORTING_FARMCALENDAR_URLS[url_use]}{operation_id}/",
            token=token,
            params={"format": "json"},
        )

        json_data = [json_data] if json_data else None

    else:
        if not data:
            params = {"format": "json"}
            if parcel_id:
                params["parcel"] = parcel_id

            decode_dates_filters(params, from_date, to_date)
            json_data = make_get_request(
                url=f"{settings.REPORTING_FARMCALENDAR_BASE_URL}{settings.REPORTING_FARMCALENDAR_URLS[url_use]}",
                token=token,
                params=params,
            )

        else:
            data_used = True
            json_data = json.loads(data)
            if json_data:
                json_data = json_data["@graph"]

    if json_data:
        operations = parse_irrig_fert_operations(
            json_data,
            irrigation_flag=irrigation_flag,
            fertilization_flag=fertilization_flag,
        )
    else:
        operations = []

    try:
        pdf = create_pdf_from_operations(
            operations,
            token,
            data_used,
            parcel_id=parcel_id,
            from_date=from_date,
            to_date=to_date,
            irrigation_flag=irrigation_flag,
            fertilization_flag=fertilization_flag,
        )
    except Exception:
        raise HTTPException(
            status_code=400, detail="PDF generation of irrigation report failed."
        )
    pdf_dir = f"{settings.PDF_DIRECTORY}{pdf_file_name}"
    os.makedirs(os.path.dirname(f"{pdf_dir}.pdf"), exist_ok=True)
    pdf.output(f"{pdf_dir}.pdf")
    notify_stress_test_callback(pdf_file_name)
