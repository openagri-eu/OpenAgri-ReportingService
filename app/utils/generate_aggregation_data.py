import io
from typing import List

import pandas as pd
import matplotlib
from core.config import settings
from utils import get_pesticide

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

from schemas import IrrigationOperation, CropProtectionOperation


def get_pest_from_obj(pt: CropProtectionOperation, token: dict | str):
    pest_name = ""
    if not settings.REPORTING_USING_GATEKEEPER:
        return pest_name
    pest_id = pt.usesPesticide.get("@id", None) if pt.usesPesticide else None
    if pest_id:
        pest_id = pest_id.split(":")[3]
        pest = get_pesticide(pest_id, token)
        pest_name = pest.get("hasCommercialName") if pest else ""
    return pest_name


def pesticides_aggregation(
    pests: List[CropProtectionOperation], token: dict | str
) -> pd.DataFrame:
    data_for_df = []
    for pt in pests:
        data_for_df.append(
            {
                "Dose": pt.hasAppliedAmount.numericValue if pt.hasAppliedAmount else 0,
                "Pesticide": get_pest_from_obj(pt, token),
                "Unit": pt.hasAppliedAmount.unit if pt.hasAppliedAmount else "",
            }
        )
    df = pd.DataFrame(data_for_df)
    pesticide_sums = df.groupby(["Pesticide", "Unit"])["Dose"].sum().reset_index()
    return pesticide_sums


# Irrigation depth units: represent an amount per unit area (e.g. "10 mm"
# applied), so total volume across the parcel needs multiplying by area.
# Conversion factor is meters per unit.
DEPTH_UNITS_TO_M = {
    "mm": 0.001, "millimeter": 0.001, "millimetre": 0.001,
    "millimeters": 0.001, "millimetres": 0.001,
    "cm": 0.01, "centimeter": 0.01, "centimetre": 0.01,
    "centimeters": 0.01, "centimetres": 0.01,
    "m": 1.0, "meter": 1.0, "metre": 1.0, "meters": 1.0, "metres": 1.0,
    "in": 0.0254, "inch": 0.0254, "inches": 0.0254,
}

# Volume units: the applied amount already represents the total volume
# delivered in that operation (e.g. "10 litres" from a flow meter), so it
# is not scaled by area and is kept in its original unit (no forced m3
# conversion - there's no dimensional need for one).
VOLUME_UNITS = {
    "l", "liter", "litre", "liters", "litres",
    "m3", "m³", "cubic meter", "cubic metre", "cubic meters", "cubic metres",
    "gal", "gallon", "gallons",
}


def prepare_df_for_calculations(
    irrigation_reports: List[IrrigationOperation],
    parcel_area_m2: float,
) -> pd.DataFrame:
    data_for_df = []
    for irrig in irrigation_reports:
        data_for_df.append(
            {
                "Started Date": irrig.hasStartDatetime,
                "Dose": irrig.hasAppliedAmount.numericValue
                if irrig.hasAppliedAmount
                else 0,
                "Unit": irrig.hasAppliedAmount.unit if irrig.hasAppliedAmount else "",
            }
        )
    df = pd.DataFrame(data_for_df)
    dose_unit = next((u for u in df["Unit"] if u), "")
    dose_unit_key = dose_unit.strip().lower()

    if dose_unit_key in DEPTH_UNITS_TO_M:
        # Depth applied over the parcel: volume = depth(m) * area(m2).
        df["Total Volume"] = df["Dose"] * DEPTH_UNITS_TO_M[dose_unit_key] * parcel_area_m2
        total_volume_unit = "m3"
    elif dose_unit_key in VOLUME_UNITS:
        # Already a total volume for that operation: no scaling needed,
        # keep it in the unit the user entered.
        df["Total Volume"] = df["Dose"]
        total_volume_unit = dose_unit
    else:
        # Unknown/unsupported unit: no reliable conversion to m3 exists,
        # so total volume is left as the raw applied amount (not scaled).
        df["Total Volume"] = df["Dose"]
        total_volume_unit = dose_unit

    df.attrs["dose_unit"] = dose_unit
    df.attrs["total_volume_unit"] = total_volume_unit
    return df


def generate_amount_per_hectare(df: pd.DataFrame) -> io.BytesIO:
    unit = next((u for u in df["Unit"] if u), "")
    df["Started Date"] = pd.to_datetime(df["Started Date"], format="%d/%m/%Y")
    plt.figure(figsize=(14, 7))
    plt.plot(df["Started Date"], df["Dose"], marker="o", color="grey")

    for i, txt in enumerate(df["Dose"]):
        plt.annotate(
            txt,
            (df["Started Date"].iloc[i], df["Dose"].iloc[i]),
            textcoords="offset points",
            xytext=(0, 5),
            ha="center",
        )

    plt.title("Applied amount of water per hectare", fontsize=16)
    plt.ylabel(f"Dose ({unit})" if unit else "Dose", fontsize=12)
    plt.xlabel("Date", fontsize=12)
    plt.grid(True, linestyle="--", alpha=0.6)
    plt.xticks(rotation=45)

    ax = plt.gca()
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m/%Y"))

    plt.tight_layout()
    image_mem = io.BytesIO()
    plt.savefig(image_mem, format="png")
    plt.close()
    return image_mem


def generate_aggregation_table_data(df: pd.DataFrame) -> dict:
    return {
        "Volume of applied water": [df["Dose"].sum(), df["Total Volume"].sum()],
        "Average dose": [df["Dose"].mean(), df["Total Volume"].mean()],
        "Maximum Dose": [df["Dose"].max(), df["Total Volume"].max()],
        "Minimum Dose": [df["Dose"].min(), df["Total Volume"].min()],
    }
