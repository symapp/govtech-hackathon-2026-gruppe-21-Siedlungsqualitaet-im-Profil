"""Fetch current MeteoSwiss temperature, rasterize to 100 m LV95 GeoZarr, upload to B2.

Source:
- geo.admin.ch MeteoSwiss 10-minute air temperature observations
    (`ch.meteoschweiz.messwerte-lufttemperatur-10min`)
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import requests
import xarray as xr

from are_rasterize_lib import swiss_100m_grid_coords, write_swiss_grid_zarr
from settlement_layer_meta import build_layer_meta
from zarr_b2_upload import (
    BUCKET_NAME,
    create_s3_filesystem,
    credentials_configured,
    upload_zarr,
)

METEOSWISS_TEMPERATURE_URL = (
    "https://data.geo.admin.ch/ch.meteoschweiz.messwerte-lufttemperatur-10min/"
    "ch.meteoschweiz.messwerte-lufttemperatur-10min_de.json"
)
ZARR_TEMP_NAME = "meteo_temperature_100m.zarr"
MANIFEST_REMOTE = "meteo_manifest.json"


def fetch_meteoswiss_temperature() -> dict[str, Any]:
    """Fetch MeteoSwiss 10-minute air temperature FeatureCollection."""
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            response = requests.get(
                METEOSWISS_TEMPERATURE_URL,
                timeout=(15, 60),
            )
            response.raise_for_status()
            data = response.json()
            break
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt < 3:
                time.sleep(attempt)
            continue
    else:
        raise RuntimeError(f"Failed to fetch MeteoSwiss payload after retries: {last_error}")

    if not isinstance(data, dict):
        raise ValueError("Unexpected MeteoSwiss payload (expected JSON object).")
    return data


def extract_temperature_samples(payload: dict[str, Any]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Extract LV95 station x/y/value arrays from MeteoSwiss FeatureCollection."""
    features = payload.get("features")
    if not isinstance(features, list):
        raise ValueError("MeteoSwiss payload missing 'features' list.")

    xs: list[float] = []
    ys: list[float] = []
    temps: list[float] = []

    for feature in features:
        if not isinstance(feature, dict):
            continue
        geometry = feature.get("geometry")
        properties = feature.get("properties")
        if not isinstance(geometry, dict) or not isinstance(properties, dict):
            continue

        coordinates = geometry.get("coordinates")
        raw_temp = properties.get("value")
        if (
            not isinstance(coordinates, list)
            or len(coordinates) < 2
            or raw_temp is None
        ):
            continue

        try:
            x = float(coordinates[0])
            y = float(coordinates[1])
            temperature = float(raw_temp)
        except (TypeError, ValueError):
            continue

        # MeteoSwiss can emit sentinel-like missing values (e.g. 99999).
        if not np.isfinite(temperature) or temperature < -45 or temperature > 50:
            continue

        xs.append(x)
        ys.append(y)
        temps.append(temperature)

    return (
        np.asarray(xs, dtype=np.float64),
        np.asarray(ys, dtype=np.float64),
        np.asarray(temps, dtype=np.float32),
    )


def interpolate_to_swiss_grid(
    xs: np.ndarray,
    ys: np.ndarray,
    values: np.ndarray,
    *,
    fill_missing_with_nearest: bool = False,
) -> np.ndarray:
    """Interpolate LV95 station observations onto the 100 m Swiss grid."""
    from scipy.interpolate import griddata

    finite = np.isfinite(xs) & np.isfinite(ys) & np.isfinite(values)
    if finite.sum() < 3:
        raise ValueError("Too few finite weather samples for interpolation.")

    xs = xs[finite]
    ys = ys[finite]
    values = values[finite]

    sample_points = np.column_stack([xs, ys])

    x_grid, y_grid = swiss_100m_grid_coords()
    xx, yy = np.meshgrid(x_grid, y_grid)
    target = np.column_stack([xx.ravel(), yy.ravel()])

    grid_values = griddata(sample_points, values, target, method="linear", fill_value=np.nan)

    if fill_missing_with_nearest and np.isnan(grid_values).any():
        missing = np.isnan(grid_values)
        grid_values[missing] = griddata(sample_points, values, target[missing], method="nearest")

    return grid_values.reshape(xx.shape).astype(np.float32)


def build_meteo_dataset(temp_grid: np.ndarray) -> xr.Dataset:
    x_grid, y_grid = swiss_100m_grid_coords()
    ds = xr.Dataset(
        {"temperature_celsius": (["y", "x"], temp_grid)},
        coords={"x": x_grid, "y": y_grid},
    )
    return ds.rio.write_crs("EPSG:2056")


def upload_manifest(*, last_updated: str) -> None:
    if not credentials_configured():
        print("B2 credentials not configured — skipping manifest upload.")
        return

    fs = create_s3_filesystem()
    manifest = {
        "last_updated": last_updated,
        "zarr_stores": [ZARR_TEMP_NAME],
    }
    remote = f"{BUCKET_NAME}/{MANIFEST_REMOTE}"
    with fs.open(remote, "w") as file_handle:
        json.dump(manifest, file_handle)
    print(f"Manifest uploaded → s3://{remote}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upload", action="store_true", help="Upload Zarr stores to B2")
    args = parser.parse_args()

    print("Fetching MeteoSwiss 10-minute temperature data...")
    try:
        payload = fetch_meteoswiss_temperature()
    except requests.RequestException as exc:
        print(f"ERROR: MeteoSwiss request failed: {exc}", file=sys.stderr)
        sys.exit(1)
    except (RuntimeError, ValueError) as exc:
        print(f"ERROR: Invalid MeteoSwiss payload: {exc}", file=sys.stderr)
        sys.exit(1)

    xs, ys, temps = extract_temperature_samples(payload)

    finite_weather = np.isfinite(xs) & np.isfinite(ys) & np.isfinite(temps)
    if finite_weather.sum() < 3:
        print("ERROR: Too few finite MeteoSwiss samples for interpolation.", file=sys.stderr)
        sys.exit(1)

    print(
        f"  {int(finite_weather.sum())} valid points fetched. "
        f"T: {np.nanmin(temps):.1f}–{np.nanmax(temps):.1f} °C"
    )

    print("Interpolating to 100 m LV95 grid...")
    temp_grid = interpolate_to_swiss_grid(xs, ys, temps, fill_missing_with_nearest=True)
    ds = build_meteo_dataset(temp_grid)
    temp_meta = build_layer_meta(
        variable="temperature_celsius",
        p5=float(np.nanpercentile(temp_grid, 5)),
        p95=float(np.nanpercentile(temp_grid, 95)),
        higher_is_better=False,
        unit="°C",
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        temp_path = Path(tmpdir) / ZARR_TEMP_NAME

        write_swiss_grid_zarr(ds[["temperature_celsius"]], temp_path, layer_meta=temp_meta)
        print(f"Zarr stores written to {tmpdir}")

        if args.upload:
            upload_zarr(temp_path)

    now_iso = datetime.now(tz=timezone.utc).isoformat()
    if args.upload:
        upload_manifest(last_updated=now_iso)

    print(f"Done. last_updated={now_iso}")


if __name__ == "__main__":
    main()
