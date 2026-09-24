import json
import logging
import os
from typing import List, Union

from fastapi import HTTPException
from fpdf.fonts import FontFace

from core import settings
from utils import EX, add_fonts, decode_jwt_token, decode_dates_filters, get_parcel_info, FarmInfo, notify_stress_test_callback
from schemas.animals import *
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


def _fetch_animal_activities(animal_id: str, token: dict[str, str], params: dict) -> list:
    """
    Fetch AnimalActivity and AnimalLactatingActivity records for a FarmAnimal.
    These are the FarmCalendar records logged against an animal (there is no
    "Observation" resource linked to animals - Observations only relate to
    parcels). Returns [] on any failure.
    """
    if not animal_id:
        return []
    activity_params = {**params, "animal": animal_id}
    results = []
    for url_key in ("animal_activities", "animal_lactating_activities"):
        url = f'{settings.REPORTING_FARMCALENDAR_BASE_URL}{settings.REPORTING_FARMCALENDAR_URLS[url_key]}'
        result = make_get_request(url=url, token=token, params=activity_params)
        if isinstance(result, list):
            results.extend(result)
    return results


def _hr_cell(hr) -> str:
    if not hr or hr.hasValue in (None, ""):
        return "—"
    unit = f" {hr.unit}" if hr.unit else ""
    return f"{hr.hasValue}{unit}"


def _urn_ref_cell(ref: Optional[dict]) -> str:
    ref_id = (ref or {}).get("@id") or ""
    return ref_id.split(":")[-1] if ref_id else "—"


def _fetch_machine_name(machine_id: str, token: dict[str, str]) -> Optional[str]:
    """Look up an AgriculturalMachine's display name. Returns None on any failure."""
    if not machine_id:
        return None
    result = make_get_request(
        url=f'{settings.REPORTING_FARMCALENDAR_BASE_URL}{settings.REPORTING_FARMCALENDAR_URLS["machines"]}{machine_id}/',
        token=token,
        params={"format": "json"},
    )
    return result.get("name") if isinstance(result, dict) else None


def _collect_machine_names(animal_activities_by_animal: dict, token: dict[str, str]) -> dict[str, str]:
    """Resolve every distinct machine id referenced across all fetched activities, once each."""
    machine_names: dict[str, str] = {}
    if not settings.REPORTING_USING_GATEKEEPER:
        return machine_names
    machine_ids = set()
    for acts in animal_activities_by_animal.values():
        for act in acts:
            for m in act.usesAgriculturalMachinery:
                m_id = (m.get("@id") or "").split(":")[-1]
                if m_id:
                    machine_ids.add(m_id)
    for m_id in machine_ids:
        name = _fetch_machine_name(m_id, token)
        if name:
            machine_names[m_id] = name
    return machine_names


def _collect_parcel_identifiers(animal_activities_by_animal: dict, token: dict[str, str]) -> dict[str, str]:
    """Resolve every distinct parcel id referenced across all fetched activities, once each."""
    parcel_identifiers: dict[str, str] = {}
    if not settings.REPORTING_USING_GATEKEEPER:
        return parcel_identifiers
    parcel_ids = set()
    for acts in animal_activities_by_animal.values():
        for act in acts:
            p_id = ((act.hasAgriParcel or {}).get("@id") or "").split(":")[-1]
            if p_id:
                parcel_ids.add(p_id)
    for p_id in parcel_ids:
        try:
            _, _, identifier = get_parcel_info(p_id, token, geolocator, identifier_flag=True)
        except Exception as e:
            logger.error(f"Error fetching parcel info for {p_id}: {e}")
            identifier = None
        if identifier:
            parcel_identifiers[p_id] = identifier
    return parcel_identifiers


def _date_range_cell(start, end) -> str:
    if not start:
        return "—"
    start_str = start.strftime("%d/%m/%Y")
    if not end:
        return start_str
    end_str = end.strftime("%d/%m/%Y")
    if start_str == end_str:
        return f"{start_str} ({start.strftime('%H:%M')}-{end.strftime('%H:%M')})"
    return f"{start_str} - {end_str}"


def _parcel_cell(ref: Optional[dict], parcel_identifiers: dict) -> str:
    ref_id = (ref or {}).get("@id") or ""
    if not ref_id:
        return "—"
    p_id = ref_id.split(":")[-1]
    return parcel_identifiers.get(p_id) or p_id or "—"


def _machinery_cell(machinery: List[dict], machine_names: dict) -> str:
    if not machinery:
        return "—"
    names = []
    for m in machinery:
        m_id = (m.get("@id") or "").split(":")[-1]
        names.append(machine_names.get(m_id) or m_id or "—")
    return ", ".join(names)


def _part_of_cell(ref: Optional[dict], title_by_id: dict) -> str:
    ref_id = (ref or {}).get("@id") or ""
    if not ref_id:
        return "—"
    return title_by_id.get(ref_id) or _urn_ref_cell(ref)


def _render_activities_table(
    pdf: EX,
    activities: List[AnimalActivity],
    title_by_id: dict,
    machine_names: dict,
    parcel_identifiers: dict,
    empty_message: str = "No animal activities recorded for this animal.",
):
    """Every field the user can fill in the Register Activity form (shared by Activities and Milk Recording)."""
    if not activities:
        pdf.set_font("FreeSerif", "", 10)
        pdf.cell(0, 8, empty_message, ln=True)
        return

    try:
        activities = sorted(activities, key=lambda a: a.hasStartDatetime or datetime.min)
    except Exception:
        pass

    pdf.set_font("FreeSerif", "B", 8)
    with pdf.table(
        text_align="CENTER", padding=0.5,
        col_widths=(1.8, 1.3, 1.6, 0.9, 1.1, 1.1, 1.2),
    ) as table:
        row = table.row()
        row.cell("Date")
        row.cell("Title")
        row.cell("Details")
        row.cell("Parcel")
        row.cell("Machinery")
        row.cell("Responsible Agent")
        row.cell("Part Of")
        pdf.set_font("FreeSerif", "", 8)
        for act in activities:
            row = table.row()
            row.cell(_date_range_cell(act.hasStartDatetime, act.hasEndDatetime))
            row.cell(act.title or "—")
            row.cell(act.details or "—")
            row.cell(_parcel_cell(act.hasAgriParcel, parcel_identifiers))
            row.cell(_machinery_cell(act.usesAgriculturalMachinery, machine_names))
            row.cell(act.responsibleAgent or "—")
            row.cell(_part_of_cell(act.isPartOfActivity, title_by_id))


def _render_milk_metrics_table(pdf: EX, activities: List[AnimalActivity]):
    """AnimalLactatingActivity entries, one column per lactation metric (base fields shown separately)."""
    if not activities:
        pdf.set_font("FreeSerif", "", 10)
        pdf.cell(0, 8, "No milk recording data for this animal.", ln=True)
        return

    try:
        activities = sorted(activities, key=lambda a: a.hasStartDatetime or datetime.min)
    except Exception:
        pass

    pdf.set_font("FreeSerif", "B", 8)
    with pdf.table(
        text_align="CENTER", padding=0.5,
        col_widths=(2.2, 0.9, 0.9, 0.9, 1, 1, 0.7, 0.8, 0.9, 0.8, 0.9, 1.3),
    ) as table:
        row = table.row()
        row.cell("Date")
        row.cell("Days in Milk")
        row.cell("Lactation #")
        row.cell("Control")
        row.cell("Milk Yield")
        row.cell("Total Yield")
        row.cell("Fat")
        row.cell("Protein")
        row.cell("RCS")
        row.cell("Urea")
        row.cell("Dry Matter")
        row.cell("Responsible Agent")
        pdf.set_font("FreeSerif", "", 8)
        for act in activities:
            row = table.row()
            row.cell(_date_range_cell(act.hasStartDatetime, act.hasEndDatetime))
            row.cell(act.hasDaysInMilk or "—")
            row.cell(act.hasLactationNumber or "—")
            row.cell(act.hasControl or "—")
            row.cell(_hr_cell(act.hasMilkYield))
            row.cell(_hr_cell(act.hasTotalMilkYield))
            row.cell(_hr_cell(act.hasFat))
            row.cell(_hr_cell(act.hasProtein))
            row.cell(_hr_cell(act.hasRCS))
            row.cell(_hr_cell(act.hasUrea))
            row.cell(_hr_cell(act.hasDryMatter))
            row.cell(act.responsibleAgent or "—")


def _render_animal_activities(pdf: EX, activities: List[AnimalActivity], machine_names: dict, parcel_identifiers: dict):
    lactating = [a for a in activities if a.hasMilkYield is not None]
    regular = [a for a in activities if a.hasMilkYield is None]
    title_by_id = {a.id: a.title for a in activities if a.id and a.title}

    pdf.set_font("FreeSerif", "B", 11)
    pdf.cell(0, 8, "Activities", ln=True)
    _render_activities_table(pdf, regular, title_by_id, machine_names, parcel_identifiers)

    pdf.ln(3)
    pdf.set_font("FreeSerif", "B", 11)
    pdf.cell(0, 8, "Milk Recording", ln=True)
    _render_milk_metrics_table(pdf, lactating)


def create_pdf_from_animals(
    animals: List[Animal],
    token: dict[str, str],
    animal_activities_by_animal: dict[str, List[AnimalActivity]] = None,
    machine_names: dict[str, str] = None,
    parcel_identifiers: dict[str, str] = None,
):
    """
    Create PDF report from animal records
    """
    animal_activities_by_animal = animal_activities_by_animal or {}
    machine_names = machine_names or {}
    parcel_identifiers = parcel_identifiers or {}
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
        pdf.cell(0, 8, "Animal Activities:", ln=True)
        _render_animal_activities(pdf, animal_activities_by_animal.get(an.id, []), machine_names, parcel_identifiers)

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
        pdf.cell(0, 10, "Animal Activities", ln=True)
        for animal in animals:
            pdf.set_font("FreeSerif", "B", 11)
            pdf.cell(0, 8, f"{animal.name or animal.id}:", ln=True)
            _render_animal_activities(pdf, animal_activities_by_animal.get(animal.id, []), machine_names, parcel_identifiers)
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

    animal_activities_by_animal: dict[str, list] = {}
    if animals and settings.REPORTING_USING_GATEKEEPER:
        activity_params = {"format": "json"}
        decode_dates_filters(activity_params, from_date, to_date)
        for an in animals:
            raw_animal_id = (
                farm_animal_id if farm_animal_id else an.id.split(":")[-1] if an.id else None
            )
            raw_activities = _fetch_animal_activities(raw_animal_id, token, activity_params)
            try:
                animal_activities_by_animal[an.id] = [
                    AnimalActivity.model_validate(item) for item in raw_activities
                ]
            except Exception as e:
                logger.error(f"Error parsing animal activities for animal {an.id}: {e}")
                animal_activities_by_animal[an.id] = []

    machine_names = _collect_machine_names(animal_activities_by_animal, token)
    parcel_identifiers = _collect_parcel_identifiers(animal_activities_by_animal, token)

    try:
        anima_pdf = create_pdf_from_animals(
            animals, token, animal_activities_by_animal, machine_names, parcel_identifiers
        )
    except Exception:
        raise HTTPException(
            status_code=400, detail="PDF generation of animal report failed."
        )


    pdf_dir = f"{settings.PDF_DIRECTORY}{pdf_file_name}"
    os.makedirs(os.path.dirname(f"{pdf_dir}.pdf"), exist_ok=True)
    anima_pdf.output(f"{pdf_dir}.pdf")
    notify_stress_test_callback(pdf_file_name)
