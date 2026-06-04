"""Verify Swiss temperature coverage for map rendering.

Produces two coverage reports:
1. Switzerland-wide grid coverage
2. Habitat-only coverage (using local population raster mask)

Optionally checks B2 meteo artifacts and manifest freshness.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import requests
import xarray as xr


def _load_meteo_module(repo_root: Path):
    meteo_path = repo_root / "data-pipelines" / "meteo-fetch.py"
    spec = importlib.util.spec_from_file_location("meteo_fetch_module", meteo_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module from {meteo_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _coverage_report(temp_grid: np.ndarray, habitat_mask: np.ndarray) -> dict[str, Any]:
    finite = np.isfinite(temp_grid)
    total = int(temp_grid.size)
    finite_count = int(finite.sum())
    nan_count = total - finite_count

    finite_values = temp_grid[finite]
    global_report = {
        "total_cells": total,
        "finite_cells": finite_count,
        "nan_cells": nan_count,
        "finite_pct": (finite_count / total * 100.0) if total else math.nan,
        "nan_pct": (nan_count / total * 100.0) if total else math.nan,
        "min": float(np.nanmin(finite_values)) if finite_count else math.nan,
        "max": float(np.nanmax(finite_values)) if finite_count else math.nan,
        "p5": float(np.nanpercentile(finite_values, 5)) if finite_count else math.nan,
        "p95": float(np.nanpercentile(finite_values, 95)) if finite_count else math.nan,
    }

    habitat_total = int(habitat_mask.sum())
    habitat_finite = int((finite & habitat_mask).sum())
    habitat_nan = int((~finite & habitat_mask).sum())
    habitat_report = {
        "mask": "population_density_score>0",
        "total_cells": habitat_total,
        "finite_cells": habitat_finite,
        "nan_cells": habitat_nan,
        "finite_pct": (habitat_finite / habitat_total * 100.0) if habitat_total else math.nan,
        "nan_pct": (habitat_nan / habitat_total * 100.0) if habitat_total else math.nan,
    }

    return {"global": global_report, "habitat": habitat_report}


def _load_habitat_mask(data_pipelines_dir: Path) -> np.ndarray:
    pop_store = data_pipelines_dir / "statpop_population_density_100m.zarr"
    if not pop_store.exists():
        raise FileNotFoundError(
            f"Habitat mask source not found: {pop_store}. "
            "Run density-rasterize.py first."
        )

    ds = xr.open_zarr(pop_store, consolidated=True)
    if "population_density_score" in ds.data_vars:
        pop = ds["population_density_score"].values
    elif "population" in ds.data_vars:
        pop = ds["population"].values
    else:
        vars_list = ", ".join(ds.data_vars)
        raise KeyError(f"No supported population variable found. Available: {vars_list}")

    return np.isfinite(pop) & (pop > 0)


def _check_b2(repo_root: Path, max_age_hours: float) -> dict[str, Any]:
    zarr_upload_path = repo_root / "data-pipelines" / "zarr_b2_upload.py"
    try:
        spec = importlib.util.spec_from_file_location("zarr_b2_upload_module", zarr_upload_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Unable to load module from {zarr_upload_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as exc:  # pragma: no cover - defensive path
        return {
            "credentials_configured": False,
            "stores": {},
            "manifest": None,
            "manifest_fresh": False,
            "manifest_age_hours": None,
            "max_age_hours": max_age_hours,
            "error": f"Failed to load B2 upload module: {exc}",
        }

    if not module.credentials_configured():
        return {
            "credentials_configured": False,
            "stores": {},
            "manifest": None,
            "manifest_fresh": False,
            "max_age_hours": max_age_hours,
            "warning": "B2 credentials missing; skipped remote checks.",
        }

    fs = module.create_s3_filesystem()
    bucket = module.BUCKET_NAME

    stores = {
        "meteo_temperature_100m.zarr": bool(fs.exists(f"{bucket}/meteo_temperature_100m.zarr")),
    }

    manifest_path = f"{bucket}/meteo_manifest.json"
    manifest: dict[str, Any] | None = None
    manifest_fresh = False
    manifest_age_hours: float | None = None

    if fs.exists(manifest_path):
        try:
            with fs.open(manifest_path, "r") as handle:
                manifest = json.load(handle)
        except json.JSONDecodeError as exc:
            return {
                "credentials_configured": True,
                "stores": stores,
                "manifest": None,
                "manifest_age_hours": None,
                "manifest_fresh": False,
                "max_age_hours": max_age_hours,
                "error": f"Malformed manifest JSON: {exc}",
            }

        last_updated_raw = manifest.get("last_updated")
        if isinstance(last_updated_raw, str):
            try:
                last_updated = datetime.fromisoformat(last_updated_raw)
                if last_updated.tzinfo is None:
                    last_updated = last_updated.replace(tzinfo=timezone.utc)
                now = datetime.now(timezone.utc)
                manifest_age_hours = (now - last_updated).total_seconds() / 3600.0
                manifest_fresh = manifest_age_hours <= max_age_hours
            except ValueError:
                manifest_age_hours = None
                manifest_fresh = False

    return {
        "credentials_configured": True,
        "stores": stores,
        "manifest": manifest,
        "manifest_age_hours": manifest_age_hours,
        "manifest_fresh": manifest_fresh,
        "max_age_hours": max_age_hours,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check-b2",
        action="store_true",
        help="Also verify B2 meteo stores and manifest freshness.",
    )
    parser.add_argument(
        "--max-manifest-age-hours",
        type=float,
        default=24.0,
        help="Maximum acceptable age for meteo_manifest.json when --check-b2 is used.",
    )
    parser.add_argument(
        "--fail-on-b2",
        action="store_true",
        help="Exit non-zero if B2 checks fail freshness/existence constraints.",
    )
    args = parser.parse_args()
    if args.max_manifest_age_hours <= 0:
        parser.error("--max-manifest-age-hours must be positive")

    repo_root = Path(__file__).resolve().parent.parent
    data_pipelines_dir = repo_root / "data-pipelines"
    meteo = _load_meteo_module(repo_root)

    try:
        payload = meteo.fetch_meteoswiss_temperature()
    except (requests.RequestException, RuntimeError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "error": f"Unable to fetch MeteoSwiss payload: {exc}",
                    "source": "ch.meteoschweiz.messwerte-lufttemperatur-10min",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        raise SystemExit(2)
    try:
        xs, ys, temps = meteo.extract_temperature_samples(payload)
    except (KeyError, TypeError) as exc:
        payload = {
            "error": f"Unexpected MeteoSwiss response structure: {exc}",
            "features_sample": payload.get("features", [])[:2] if isinstance(payload, dict) else payload,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        raise SystemExit(2)

    temp_grid = meteo.interpolate_to_swiss_grid(
        xs,
        ys,
        temps,
        fill_missing_with_nearest=True,
    )
    habitat_mask = _load_habitat_mask(data_pipelines_dir)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_points": int(np.isfinite(temps).sum()),
        "coverage": _coverage_report(temp_grid, habitat_mask),
    }

    b2_report: dict[str, Any] | None = None
    if args.check_b2:
        b2_report = _check_b2(repo_root, args.max_manifest_age_hours)
        report["b2"] = b2_report

    print(json.dumps(report, ensure_ascii=False, indent=2))

    global_nan = report["coverage"]["global"]["nan_cells"]
    habitat_nan = report["coverage"]["habitat"]["nan_cells"]
    exit_code = 0 if global_nan == 0 and habitat_nan == 0 else 1

    if args.check_b2 and args.fail_on_b2 and b2_report is not None:
        stores_ok = all(b2_report.get("stores", {}).values())
        manifest_fresh = bool(b2_report.get("manifest_fresh"))
        if b2_report.get("credentials_configured") and (not stores_ok or not manifest_fresh):
            exit_code = 1

    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
