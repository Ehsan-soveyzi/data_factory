"""Descriptive EDA on selected clean columns; no changes to factory outputs."""

import json
from pathlib import Path

import numpy as np
import pandas as pd


CASE_COLUMNS = ["unique_key", "complaint_type", "created_date", "created_month", "status",
                "closure_hours", "population_join_status", "borough_comparison"]


def load_cases(path):
    """Read JSONL in chunks, retaining only columns needed for the analysis."""
    chunks = []
    for chunk in pd.read_json(path, lines=True, chunksize=25000, convert_dates=False,
                              dtype={"unique_key": "string"}):
        chunks.append(chunk[CASE_COLUMNS].copy())
    if not chunks:
        raise ValueError("EDA requires at least one accepted case.")
    frame = pd.concat(chunks, ignore_index=True)
    for column in ("complaint_type", "created_month", "status", "population_join_status", "borough_comparison"):
        frame[column] = frame[column].astype("category")
    frame["closure_hours"] = pd.to_numeric(frame["closure_hours"], errors="raise")
    return frame


def summarize_cases(cases, wide_path, month_coverage):
    """Return exact descriptive tables, keeping open and extreme cases visible."""
    durations = cases["closure_hours"].dropna()
    description = durations.describe(percentiles=[.25, .5, .75, .9, .95, .99])
    q1, q3 = durations.quantile([.25, .75])
    fence = q3 + 1.5 * (q3 - q1)
    outliers = {
        "upper_iqr_fence_hours": float(fence),
        "above_upper_fence": int((durations > fence).sum()),
        "above_upper_fence_percent": float(100 * (durations > fence).mean()),
        "zero_duration_records": int(durations.eq(0).sum()),
        "missing_closure_records": int(cases["closure_hours"].isna().sum()),
        "policy": "Retain logically valid extremes; IQR flags describe a skewed distribution and are not quarantine rules.",
    }
    grouped = cases.groupby("complaint_type", observed=True)["closure_hours"]
    complaints = grouped.agg(case_count="size", recorded_closures="count", mean_hours="mean", median_hours="median")
    complaints["p90_hours"] = grouped.quantile(.9)
    complaints["missing_closure_percent"] = 100 * (1 - complaints["recorded_closures"] / complaints["case_count"])
    status = pd.crosstab(cases["status"].astype("string").fillna("MISSING"), cases["closure_hours"].notna())
    status = status.rename(columns={False: "missing_closure", True: "recorded_closure"})
    wide = pd.read_csv(wide_path, dtype={"analysis_area_key": "string", "population_area": "string"})
    months = [m for m, state in month_coverage.items() if state == "complete"]
    rates = wide.loc[wide["metric"].eq("complaints_per_population_scale")].copy()
    counts = wide.loc[wide["metric"].eq("case_count")].copy()
    area = counts.groupby("analysis_area_key", observed=True).agg(
        population=("reference_population", "first"), neighborhood=("reference_neighborhood", "first"))
    area["case_count_complete_months"] = counts.set_index("analysis_area_key")[months].sum(axis=1, min_count=1).groupby(level=0).sum(min_count=1)
    area["rate_per_100k_complete_months"] = rates.set_index("analysis_area_key")[months].sum(axis=1, min_count=1).groupby(level=0).sum(min_count=1)
    area = area.loc[area["population"].gt(0) & area["case_count_complete_months"].notna()]
    if not months:
        area = area.iloc[:0]
    positive = area.loc[area["case_count_complete_months"].gt(0)]
    numeric = positive[["population", "case_count_complete_months", "rate_per_100k_complete_months"]]
    rank_correlation = numeric.rank().corr()
    monthly = cases.groupby("created_month", observed=True)["closure_hours"].agg(
        case_count="size", median_hours="median", mean_hours="mean")
    monthly["coverage"] = [month_coverage[str(m)] for m in monthly.index]
    return {"duration_description": description, "outliers": outliers, "complaints": complaints,
            "status_closure": status, "area": area, "rank_correlation": rank_correlation,
            "monthly": monthly, "complete_months": months,
            "memory_megabytes": float(cases.memory_usage(deep=True).sum() / 1024**2)}


def make_figures(cases, summary):
    """Four figures with explicit units and coverage; no random subsampling."""
    import matplotlib.pyplot as plt

    figures = {}
    durations = cases["closure_hours"].dropna()
    fig, ax = plt.subplots(figsize=(9, 4))
    ax.hist(np.log1p(durations), bins=60, color="#287e8d", edgecolor="white", linewidth=.3)
    ax.set(xlabel="log(1 + recorded closure hours)", ylabel="Recorded closures",
           title="Recorded closure durations have a long right tail")
    fig.tight_layout()
    figures["duration"] = fig
    complaint = summary["complaints"].sort_values("median_hours")
    fig, ax = plt.subplots(figsize=(10, 4.8))
    labels = [str(x).replace("Noise - ", "") for x in complaint.index]
    positions = np.arange(len(labels))
    ax.barh(positions - .18, complaint["median_hours"], .35, label="Median", color="#287e8d")
    ax.barh(positions + .18, complaint["p90_hours"], .35, label="90th percentile", color="#cc793c")
    ax.set(yticks=positions, yticklabels=labels, xlabel="Recorded closure hours",
           title="Typical duration and the upper tail vary by complaint type")
    ax.legend()
    fig.tight_layout()
    figures["complaint"] = fig
    monthly = summary["monthly"]
    fig, axes = plt.subplots(2, 1, figsize=(10, 6), sharex=True)
    labels = [str(x) for x in monthly.index]
    colors = ["#287e8d" if state == "complete" else "#b5bdc3" for state in monthly["coverage"]]
    axes[0].bar(labels, monthly["case_count"], color=colors)
    axes[0].set(ylabel="Created cases", title="Creation-month cohorts; gray bars indicate incomplete acquisition")
    axes[1].plot(labels, monthly["median_hours"], marker="o", color="#cc793c")
    axes[1].set(xlabel="Creation month", ylabel="Median closure hours")
    axes[1].tick_params(axis="x", rotation=40)
    fig.tight_layout()
    figures["monthly"] = fig
    area = summary["area"]
    fig, ax = plt.subplots(figsize=(8, 5))
    scatter = ax.scatter(area["population"], area["case_count_complete_months"],
                         c=area["rate_per_100k_complete_months"], cmap="viridis", alpha=.8, s=30)
    ax.set(xlabel="Reference population (2019)", ylabel="Cases in complete months",
           title="Raw counts and population-normalized rates describe different patterns")
    if len(area):
        fig.colorbar(scatter, ax=ax, label="Eligible cases per 100,000 residents")
    else:
        ax.text(.5, .5, "No complete months: population comparison unavailable", transform=ax.transAxes, ha="center")
    fig.tight_layout()
    figures["population"] = fig
    return figures
