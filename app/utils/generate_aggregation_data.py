import io
from typing import List

import pandas as pd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

from schemas import IrrigationOperation


def prepare_df_for_calculations(
    irrigation_reports: List[IrrigationOperation],
) -> pd.DataFrame:
    data_for_df = []
    for irrig in irrigation_reports:
        data_for_df.append(
            {
                "Started Date": irrig.hasStartDatetime,
                "Dose": irrig.hasAppliedAmount.numericValue
                if irrig.hasAppliedAmount
                else 0,
            }
        )
    df = pd.DataFrame(data_for_df)
    return df


def generate_total_volume_graph(df: pd.DataFrame, parcel_area: int) -> io.BytesIO:
    df["Started Date"] = pd.to_datetime(df["Started Date"], format="%d/%m/%Y")
    df["Total Volume"] = df["Dose"] * parcel_area
    df = df.sort_values(by="Started Date")
    plt.figure(figsize=(14, 7))
    plt.plot(df["Started Date"], df["Total Volume"], marker="o", color="#8B8000")

    for i, txt in enumerate(df["Total Volume"]):
        plt.annotate(
            txt,
            (df["Started Date"].iloc[i], df["Total Volume"].iloc[i]),
            textcoords="offset points",
            xytext=(0, 5),
            ha="center",
        )

    plt.title("Total Volume of applied water per irrigation activity", fontsize=16)
    plt.ylabel("Total Volume (m3)", fontsize=12)
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


def generate_amount_per_hectare(df: pd.DataFrame) -> io.BytesIO:
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
    plt.ylabel("Dose (m3/Ha)", fontsize=12)
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
