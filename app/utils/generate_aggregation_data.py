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


DEPTH_UNITS = {"mm", "millimeter", "millimetre", "millimeters", "millimetres"}


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

    if dose_unit.strip().lower() in DEPTH_UNITS:
        # Dose is an irrigation depth (e.g. mm): 1 mm applied over 1 m2 = 0.001 m3.
        df["Total Volume"] = (df["Dose"] / 1000.0) * parcel_area_m2
        total_volume_unit = "m3"
    else:
        # Unknown/unsupported unit: no reliable depth->volume conversion exists,
        # so total volume is left as the raw applied amount (not scaled by area).
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
