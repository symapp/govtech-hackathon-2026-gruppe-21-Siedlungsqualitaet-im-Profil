#!/usr/bin/env python3
"""Fetch Leerwohnungsziffer 2025 from BFS mapexplorer and write a 100 m GeoZarr.

For each municipality geocode (BFS Gde-nummer) found in the geocodes raster,
the script fetches the vacancy-rate time series from the BFS mapexplorer API
and extracts the value for the year 2025.

Municipalities with no data receive NaN.

Input
-----
geocodes_municipalities_100m.zarr  — produced by geo_geocodes.py

Output
------
leerwohnungsziffer_municipalities_100m.zarr  — xarray Dataset, variable
"leerwohnungsziffer" (float32).
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import numpy as np
import requests
import xarray as xr

from are_rasterize_lib import OUTPUT_CRS
from settlement_layer_meta import build_layer_meta, compute_percentile_bounds
from zarr_b2_upload import upload_zarr

# ---------------------------------------------------------------------------
# BFS mapexplorer API
# ---------------------------------------------------------------------------
_BFS_API_TEMPLATE = (
    "https://mapexplorer.bfs.admin.ch/GC_infosel.php"
    "?lang=de&allindics=0&obs=main&nivgeo=polg2025_04_06"
    "&view=map227&codgeo={codgeo}"
    "&dataset=ch_09_03b&indic=leerwohnungsziffer"
)

TARGET_YEAR = "2025"
DATA_KEY = "leerwohnungsziffer"
DATASET_KEY = "ch_09_03b"

DEFAULT_GEOCODES_ZARR = "geocodes_municipalities_100m.zarr"
DEFAULT_OUT = "leerwohnungsziffer_municipalities_100m.zarr"
DEFAULT_CACHE = "leerwohnungsziffer_cache.json"

# Polite delay between API calls per thread (seconds).
REQUEST_DELAY_S = 0.05
REQUEST_TIMEOUT_S = 30
FETCH_WORKERS = 5
CACHE_SAVE_INTERVAL = 50  # save after every N completed fetches


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def fetch_leerwohnungsziffer(codgeo: int, session: requests.Session) -> Optional[float]:
    """Return the Leerwohnungsziffer for *codgeo* in TARGET_YEAR, or None."""
    url = _BFS_API_TEMPLATE.format(codgeo=codgeo)
    try:
        response = session.get(url, timeout=REQUEST_TIMEOUT_S)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        print(f"    WARNING: request failed for codgeo={codgeo}: {exc}")
        return None

    try:
        records = payload["content"]["data"][DATASET_KEY]
    except (KeyError, TypeError):
        return None

    # Only keep records belonging to this municipality (not national reference).
    territory_prefix = f"polg2025_04_06@{codgeo}"
    for record in records:
        if record.get("territory") == territory_prefix and str(record.get("annee")) == TARGET_YEAR:
            value = record.get(DATA_KEY)
            if value is not None:
                return float(value)
    return None


def load_cache(cache_path: Path) -> dict[int, float]:
    """Load an existing fetch cache, or return an empty dict."""
    if cache_path.exists():
        with cache_path.open() as f:
            raw = json.load(f)
        print(f"Resuming: loaded {len(raw)} cached values from {cache_path}")
        return {int(k): v for k, v in raw.items()}
    return {}


def save_cache(cache_path: Path, mapping: dict[int, float]) -> None:
    """Persist the current mapping to a JSON cache file."""
    with cache_path.open("w") as f:
        json.dump({str(k): v for k, v in mapping.items()}, f)


def get_unique_geocodes(zarr_path: Path) -> list[int]:
    """Extract unique non-zero geocodes by scanning zarr chunks directly.

    Avoids loading the full (multi-GB) array into memory at once.
    """
    import zarr as _zarr

    store = _zarr.open(str(zarr_path), mode="r")
    arr = store["geocode"]
    cy = (arr.shape[0] + arr.chunks[0] - 1) // arr.chunks[0]
    cx = (arr.shape[1] + arr.chunks[1] - 1) // arr.chunks[1]
    print(f"  Scanning {cy * cx} chunks for unique geocodes ...", flush=True)
    unique_set: set[int] = set()
    for iy in range(cy):
        y0, y1 = iy * \
            arr.chunks[0], min((iy + 1) * arr.chunks[0], arr.shape[0])
        for ix in range(cx):
            x0, x1 = ix * \
                arr.chunks[1], min((ix + 1) * arr.chunks[1], arr.shape[1])
            chunk = arr[y0:y1, x0:x1]
            unique_set.update(int(v) for v in np.unique(chunk) if v != 0)
    return sorted(unique_set)


def build_geocode_to_value_map(
    unique_codes: list[int],
    *,
    cache_path: Path,
    verbose: bool = True,
) -> dict[int, float]:
    """Fetch Leerwohnungsziffer for every unique geocode.

    Already-fetched values are loaded from *cache_path* and skipped,
    enabling interrupted runs to be resumed.
    """
    total = len(unique_codes)

    mapping = load_cache(cache_path)
    # Codes whose result is already known (including explicit None → stored as
    # a sentinel so we don't re-fetch known-missing municipalities).
    already_done = set(mapping.keys())
    pending = [c for c in unique_codes if c not in already_done]

    print(
        f"Fetching Leerwohnungsziffer ({TARGET_YEAR}) for {len(pending)}/{total} "
        "municipalities (rest already cached) ..."
    )

    session = requests.Session()
    session.headers.update({"Accept": "application/json"})

    lock = threading.Lock()
    completed = 0
    missing = 0

    def _fetch_one(code: int) -> tuple[int, Optional[float]]:
        # Each thread uses its own session to avoid shared state issues.
        s = requests.Session()
        s.headers.update({"Accept": "application/json"})
        time.sleep(REQUEST_DELAY_S)
        return code, fetch_leerwohnungsziffer(code, s)

    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as executor:
        futures = {executor.submit(_fetch_one, code): code for code in pending}
        for future in as_completed(futures):
            code, value = future.result()
            with lock:
                if value is not None:
                    mapping[code] = value
                else:
                    missing += 1
                completed += 1
                if verbose and completed % CACHE_SAVE_INTERVAL == 0:
                    save_cache(cache_path, mapping)
                    print(
                        f"  {completed}/{len(pending)} fetched  "
                        f"(missing so far: {missing}) — cache saved",
                        flush=True,
                    )

    save_cache(cache_path, mapping)
    print(
        f"Done.  {len(mapping)}/{total} municipalities have data for {TARGET_YEAR}.")
    return mapping


# ---------------------------------------------------------------------------
# Raster construction
# ---------------------------------------------------------------------------

def apply_mapping_to_geocodes(
    geocodes_zarr_path: Path,
    geocodes_ds: xr.Dataset,
    mapping: dict[int, float],
) -> xr.DataArray:
    """Build a float32 DataArray by applying *mapping* to the geocode raster.

    Uses zarr directly (int32 chunks) and dask map_blocks to avoid loading
    the full multi-GB array into memory at once.
    """
    import zarr as _zarr
    import dask.array as dsa

    # Build lookup table.
    max_code = max(mapping.keys()) if mapping else 0
    lut = np.full(max_code + 1, np.nan, dtype=np.float32)
    for code, val in mapping.items():
        lut[code] = val

    # Open geocode array as a dask array (int32, not upcast to float64).
    zarr_arr = _zarr.open(str(geocodes_zarr_path), mode="r")["geocode"]
    dask_codes = dsa.from_zarr(zarr_arr)  # dtype int32

    def _apply_lut(block: np.ndarray, lut: np.ndarray = lut, max_code: int = max_code) -> np.ndarray:
        result = np.full(block.shape, np.nan, dtype=np.float32)
        mask = (block > 0) & (block <= max_code)
        result[mask] = lut[block[mask]]
        return result

    result_dask = dask_codes.map_blocks(_apply_lut, dtype=np.float32)

    geocodes_da = geocodes_ds["geocode"]
    return xr.DataArray(
        result_dask,
        coords=geocodes_da.coords,
        dims=geocodes_da.dims,
        attrs={"long_name": f"Leerwohnungsziffer {TARGET_YEAR}", "units": "%"},
    )


def write_zarr(dataset: xr.Dataset, out: Path) -> None:
    encoding = {
        DATA_KEY: {
            "dtype": "float32",
            "chunks": [512, 512],
        }
    }
    print(f"Writing {out} ...")
    dataset.to_zarr(str(out), mode="w", consolidated=True, encoding=encoding)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            f"Build a 100 m GeoZarr of Leerwohnungsziffer {TARGET_YEAR} by municipality, "
            "fetched from the BFS mapexplorer API."
        )
    )
    parser.add_argument(
        "--geocodes-zarr",
        type=Path,
        default=Path(DEFAULT_GEOCODES_ZARR),
        help=f"Path to the geocodes GeoZarr produced by geo_geocodes.py (default: {DEFAULT_GEOCODES_ZARR}).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(DEFAULT_OUT),
        help=f"Output Zarr path (default: {DEFAULT_OUT}).",
    )
    parser.add_argument(
        "--upload",
        action="store_true",
        help="Upload to local MinIO (S3-compatible) after writing.",
    )
    parser.add_argument(
        "--remote-name",
        type=str,
        default=None,
        help="Object prefix inside the B2 bucket (defaults to the .zarr folder name).",
    )
    parser.add_argument(
        "--cache",
        type=Path,
        default=Path(DEFAULT_CACHE),
        help=f"JSON cache file for resumable fetching (default: {DEFAULT_CACHE}).",
    )
    parser.add_argument(
        "--percentile-cutoff",
        type=float,
        default=5.0,
        help="Percentile cutoff for settlement-layer-meta.json (default: 5.0).",
    )
    args = parser.parse_args()

    # 1. Load geocodes metadata (lazy — no data loaded into memory).
    print(f"Loading geocodes from {args.geocodes_zarr} ...", flush=True)
    geocodes_ds = xr.open_zarr(str(args.geocodes_zarr), consolidated=True)

    # 2. Extract unique municipality codes by scanning zarr chunks directly.
    unique_codes = get_unique_geocodes(args.geocodes_zarr)
    print(f"Found {len(unique_codes)} unique municipalities.", flush=True)

    # 3. Fetch BFS data.
    mapping = build_geocode_to_value_map(unique_codes, cache_path=args.cache)

    # 4. Build result raster (dask-backed, not yet computed).
    print("Applying mapping to geocode raster ...", flush=True)
    result_da = apply_mapping_to_geocodes(
        args.geocodes_zarr, geocodes_ds, mapping)
    result_da.name = DATA_KEY

    # Preserve CRS metadata.
    import rioxarray  # noqa: F401
    result_da = result_da.rio.write_crs(OUTPUT_CRS)
    dataset = result_da.to_dataset(name=DATA_KEY)

    # 5. Compute layer metadata.
    p5, p95 = compute_percentile_bounds(
        result_da, percentile_cutoff=args.percentile_cutoff
    )
    meta = build_layer_meta(
        variable=DATA_KEY,
        p5=p5,
        p95=p95,
        higher_is_better=False,
        unit="%",
    )

    # 6. Write zarr.
    write_zarr(dataset, args.out)

    # 7. Write settlement-layer-meta.json sidecar.
    from settlement_layer_meta import write_settlement_layer_meta
    write_settlement_layer_meta(args.out, meta)

    print(f"Success! GeoZarr written to {args.out}")

    if args.upload:
        remote = upload_zarr(args.out, remote_name=args.remote_name)
        print(f"Uploaded to {remote}")


if __name__ == "__main__":
    main()
