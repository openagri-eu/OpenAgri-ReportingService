import json
import logging
import os
from typing import List, Union

from fastapi import HTTPException
from fpdf.fonts import FontFace

from core import settings
from utils import EX, add_fonts, decode_jwt_token, decode_dates_filters, get_parcel_info, FarmInfo, notify_stress_test_callback
from schemas.animals import *
from schemas.compost import CropObservation
from utils.farm_calendar_report import geolocator
from utils.json_handler import make_get_request


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def parse_animal_data(data: Union[List[dict], str]) -> Optional[List[Animal]]:
    """
    Parse list of animal records from JSON data
    """
    try:
        res = [Animal.model_validate(item) for item in data]
        return res
    except Exception as e:
        logger.error(f"Error parsing animal data: {e}")
        return None


def _fetch_animal_observations(animal_id: str, token: dict[str, str], params: dict) -> list:
    """Fetch Observations nested under a FarmAnimal record. Returns [] on any failure."""
    if not animal_id:
        return []
    obs_url = (
        f'{settings.REPORTING_FARMCALENDAR_BASE_URL}'
        f'{settings.REPORTING_FARMCALENDAR_URLS["animals"]}{animal_id}'
        f'{settings.REPORTING_FARMCALENDAR_URLS["observations"]}'
    )
    result = make_get_request(url=obs_url, token=token, params=params)
    return result if isinstance(result, list) else []


def _render_observations_table(pdf: EX, observations: List[CropObservation]):
    if not observations:
        pdf.set_font("FreeSerif", "", 10)
        pdf.cell(0, 8, "No observations recorded for this animal.", ln=True)
        return

    try:
        observations = sorted(
            observations, key=lambda o: o.hasStartDatetime or o.phenomenonTime or datetime.min
        )
    except Exception:
        pass

    pdf.set_font("FreeSerif", "B", 9)
    with pdf.table(text_align="CENTER", padding=0.5) as table:
        row = table.row()
        row.cell("Date")
        row.cell("Title")
        row.cell("Observed Property")
        row.cell("Value")
        row.cell("Unit")
        row.cell("Details")
        pdf.set_font("FreeSerif", "", 9)
        for obs in observations:
            row = table.row()
            date_val = obs.hasStartDatetime or obs.phenomenonTime
            row.cell(date_val.strftime("%d/%m/%Y") if date_val else "—")
            row.cell(obs.title or "—")
            row.cell(obs.observedProperty or "—")
            row.cell(
                str(obs.hasResult.hasValue)
                if obs.hasResult and obs.hasResult.hasValue else "—"
            )
            row.cell(
                obs.hasResult.unit if obs.hasResult and obs.hasResult.unit else "—"
            )
            row.cell(obs.details or "—")


def create_pdf_from_animals(
    animals: List[Animal],
    token: dict[str, str],
    observations_by_animal: dict[str, List[CropObservation]] = None,
):
    """
    Create PDF report from animal records
    """
    observations_by_animal = observations_by_animal or {}
    pdf = EX()
    add_fonts(pdf)
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    EX.ln(pdf)

    pdf.set_font("FreeSerif", "B", 14)
    pdf.cell(0, 10, f"Animal Data Report", ln=True, align="C")
    pdf.set_font("FreeSerif", style="", size=9)
    pdf.cell(
        0,
        7,
        f"Data Generated - {datetime.now().strftime('%d/%m/%Y')}",
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

    if len(animals) == 1:
        an = animals[0]
        parcel_id = an.hasAgriParcel.id if an.hasAgriParcel else None
        address = ""
        farm = FarmInfo(description="", administrator="", vatID="", name="", municipality="", contactPerson="")
        identifier = ""
        if parcel_id:
            parcel = parcel_id.split(":")[3]
            if parcel:
                parcel_data, farm, identifier = get_parcel_info(
                    parcel_id.split(":")[-1], token, geolocator, identifier_flag=True
                )
                address = parcel_data.address

        pdf.set_font("FreeSerif", "B", 10)
        pdf.cell(40, 8, "Created:")
        pdf.set_font("FreeSerif", "", 10)
        pdf.multi_cell(
            0, 8, f"{an.dateCreated.strftime('%d/%m/%Y')}", ln=True, fill=True
        )

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
            "Farm information:",
        )
        pdf.set_font("FreeSerif", "", 10)
        farm_local = f"Name: {farm.name} | Municipality: {farm.municipality}"
        pdf.multi_cell(0, 8, farm_local, ln=True, fill=True)

        pdf.set_font("FreeSerif", "B", 10)
        pdf.cell(
            40,
            8,
            "Animal:",
        )
        pdf.set_font("FreeSerif", "", 10)
        pdf.multi_cell(
            0,
            8,
            f"Name: {an.name}, Sex: {an.sex}, Birthdate {an.birthdate.strftime('%d/%m/%Y')}",
            ln=True,
            fill=True,
        )

        pdf.set_font("FreeSerif", "B", 10)
        pdf.cell(
            40,
            8,
            "Species:",
        )
        pdf.set_font("FreeSerif", "", 10)
        pdf.multi_cell(0, 8, an.species, ln=True, fill=True)

        pdf.set_font("FreeSerif", "B", 10)
        pdf.cell(
            40,
            8,
            "Castrated:",
        )
        pdf.set_font("FreeSerif", "", 10)
        pdf.multi_cell(0, 8, f"{an.isCastrated}", ln=True, fill=True)

        pdf.set_font("FreeSerif", "B", 10)
        pdf.cell(
            40,
            8,
            "Invalidated:",
        )
        pdf.set_font("FreeSerif", "", 10)
        pdf.multi_cell(
            0,
            8,
            f"{an.invalidatedAtTime.strftime('%d/%m/%Y') if an.invalidatedAtTime else 'No'}",
            ln=True,
            fill=True,
        )

        pdf.set_font("FreeSerif", "B", 10)
        pdf.cell(
            40,
            8,
            "Group Member:",
        )
        pdf.set_font("FreeSerif", "", 10)
        pdf.multi_cell(
            0,
            8,
            f"{an.isMemberOfAnimalGroup.hasName if an.isMemberOfAnimalGroup else 'No'}",
            ln=True,
            fill=True,
        )

        pdf.ln(4)
        pdf.set_font("FreeSerif", "B", 12)
        pdf.cell(0, 8, "Observations:", ln=True)
        _render_observations_table(pdf, observations_by_animal.get(an.id, []))

    if len(animals) > 1:
        animals.sort(key=lambda x: x.dateCreated)
        pdf.set_fill_color(0, 255, 255)
        with pdf.table(text_align="CENTER", padding=0.5) as table:
            row = table.row()
            pdf.set_font("FreeSerif", "B", 10)
            row.cell("Date")
            row.cell("Animal")
            row.cell("Description")
            row.cell("Parcel")
            row.cell("Parcel Identifier")
            row.cell("Species")
            row.cell("Sex")
            row.cell("Birthdate")
            row.cell("Invalidated")
            row.cell("Group Member")
            pdf.set_fill_color(255, 255, 240)
            pdf.set_font("FreeSerif", "", 9)
            for animal in animals:
                row = table.row()
                row.cell(animal.dateCreated.strftime("%d/%m/%Y"))
                row.cell(animal.name)
                row.cell(animal.description)

                address = ""
                identifier = ""
                parcel_id = animal.hasAgriParcel.id if animal.hasAgriParcel else None
                if parcel_id:
                    parcel = parcel_id.split(":")[3]
                    if parcel:
                        parcel_data, _, identifier = get_parcel_info(
                            parcel_id.split(":")[-1],
                            token,
                            geolocator,
                            identifier_flag=True,
                        )
                        address = parcel_data.address

                row.cell(address)
                row.cell(identifier)
                row.cell(animal.species)
                row.cell(
                    f"{'Male' if animal.sex == 0 else 'Female'} | Castrated: {animal.isCastrated}",
                )
                row.cell(animal.birthdate.strftime("%d/%m/%Y"))
                row.cell(
                    f"{animal.invalidatedAtTime if animal.invalidatedAtTime else 'N/A'}"
                )
                row.cell(
                    f"{animal.isMemberOfAnimalGroup.hasName if animal.isMemberOfAnimalGroup else 'N/A'}"
                )
                pdf.ln(10)

        pdf.ln(6)
        pdf.set_font("FreeSerif", "B", 14)
        pdf.cell(0, 10, "Observations", ln=True)
        for animal in animals:
            pdf.set_font("FreeSerif", "B", 11)
            pdf.cell(0, 8, f"{animal.name or animal.id}:", ln=True)
            _render_observations_table(pdf, observations_by_animal.get(animal.id, []))
            pdf.ln(3)

    return pdf


def process_animal_data(
    token: dict[str, str],
    pdf_file_name: str,
    params: dict | None = None,
    data=None,
    from_date: datetime.date = None,
    to_date: datetime.date = None,
    farm_animal_id: str = None,
) -> None:
    """
    Process animal data and generate PDF report
    """
    if farm_animal_id:
        json_data = make_get_request(
            url=f'{settings.REPORTING_FARMCALENDAR_BASE_URL}{settings.REPORTING_FARMCALENDAR_URLS["animals"]}{farm_animal_id}/',
            token=token,
            params={"format": "json"},
        )

        json_data = [json_data] if json_data else None

    else:
        if params:
            params["format"] = "json"
            decode_dates_filters(params, from_date, to_date)
            json_data = make_get_request(
                url=f'{settings.REPORTING_FARMCALENDAR_BASE_URL}{settings.REPORTING_FARMCALENDAR_URLS["animals"]}',
                token=token,
                params=params,
            )

        else:
            if not settings.REPORTING_USING_GATEKEEPER:
                data = json.loads(data)
                json_data = data.get("@graph")
            else:
                json_data = make_get_request(
                    url=f'{settings.REPORTING_FARMCALENDAR_BASE_URL}{settings.REPORTING_FARMCALENDAR_URLS["animals"]}',
                    token=token,
                    params={"format": "json"},
                )
    if json_data:
        animals = parse_animal_data(json_data)
    else:
        animals = []

    observations_by_animal: dict[str, list] = {}
    if animals and settings.REPORTING_USING_GATEKEEPER:
        obs_params = {"format": "json"}
        decode_dates_filters(obs_params, from_date, to_date)
        for an in animals:
            raw_animal_id = (
                farm_animal_id if farm_animal_id else an.id.split(":")[-1] if an.id else None
            )
            raw_observations = _fetch_animal_observations(raw_animal_id, token, obs_params)
            try:
                observations_by_animal[an.id] = [
                    CropObservation.model_validate(item) for item in raw_observations
                ]
            except Exception as e:
                logger.error(f"Error parsing observations for animal {an.id}: {e}")
                observations_by_animal[an.id] = []

    try:
        anima_pdf = create_pdf_from_animals(animals, token, observations_by_animal)
    except Exception:
        raise HTTPException(
            status_code=400, detail="PDF generation of animal report failed."
        )


    pdf_dir = f"{settings.PDF_DIRECTORY}{pdf_file_name}"
    os.makedirs(os.path.dirname(f"{pdf_dir}.pdf"), exist_ok=True)
    anima_pdf.output(f"{pdf_dir}.pdf")
    notify_stress_test_callback(pdf_file_name)
