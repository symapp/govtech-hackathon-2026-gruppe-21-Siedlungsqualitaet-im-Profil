#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import re
import shutil
import zipfile
from pathlib import Path
from urllib.parse import urlencode

import numpy as np
import pandas as pd
import requests
import xarray as xr

from are_rasterize_lib import (
    SWISS_GRID_100M_EDGE_BOUNDS,
    ensure_swiss_grid_dataset,
    swiss_100m_grid_coords,
    write_swiss_grid_zarr,
)
from zarr_b2_upload import upload_zarr

BFS_ASSETS_URL = "https://dam-api.bfs.admin.ch/hub/api/dam/assets"
STATPOP_QUERY = "%Geodaten STATPOP%"
DEFAULT_OUT = "statpop_population_density_100m.zarr"
CELL_SIZE_M = 100
CELL_AREA_KM2 = (CELL_SIZE_M * CELL_SIZE_M) / 1_000_000


def log(message: str) -> None:
    print(message, flush=True)


def default_verify() -> bool | str:
    try:
        import certifi
    except ImportError:
        return True
    return certifi.where()


def request_json(url: str, verify: bool | str = True) -> dict:
    response = requests.get(url, headers={"Accept": "application/json"}, timeout=60, verify=verify)
    response.raise_for_status()
    return response.json()


def download(url: str, path: Path, verify: bool | str = True) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=120, verify=verify) as response:
        response.raise_for_status()
        total = int(response.headers.get("content-length") or 0)
        written = 0
        last_reported = 0
        with path.open("wb") as f:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)
                    written += len(chunk)
                    if total and written - last_reported >= 25 * 1024 * 1024:
                        log(f"Downloaded {written / 1024 / 1024:.0f} MiB / {total / 1024 / 1024:.0f} MiB")
                        last_reported = written
        if total:
            log(f"Downloaded {written / 1024 / 1024:.1f} MiB to {path}")
        else:
            log(f"Downloaded {written / 1024 / 1024:.1f} MiB to {path}")
    return path


def latest_statpop_asset(year: int | None, verify: bool | str = True) -> dict:
    query = urlencode(
        {
            "extendedSearch": STATPOP_QUERY,
            "limit": 100,
            "orderBy": "LAST_UPDATED",
        }
    )
    data = request_json(f"{BFS_ASSETS_URL}?{query}", verify=verify)
    assets = [
        asset
        for asset in data.get("data", [])
        if asset.get("shop", {}).get("orderNr", "").startswith("ag-b-00.03-vz")
        and any(link.get("rel") == "master" and link.get("format") == "zip" for link in asset.get("links", []))
    ]
    if year is not None:
        assets = [
            asset
            for asset in assets
            if asset.get("description", {}).get("bibliography", {}).get("period") == str(year)
        ]
    if not assets:
        raise RuntimeError(f"No STATPOP geodata ZIP found for year={year!r}")

    return max(assets, key=lambda asset: int(asset["description"]["bibliography"]["period"]))


def master_link(asset: dict) -> str:
    for link in asset.get("links", []):
        if link.get("rel") == "master" and link.get("format") == "zip":
            return link["href"]
    raise RuntimeError("Selected STATPOP asset has no ZIP master link")


def csv_members(zip_path: Path) -> list[str]:
    with zipfile.ZipFile(zip_path) as zf:
        return [name for name in zf.namelist() if name.lower().endswith(".csv")]


def sniff_delimiter(zip_path: Path, member: str) -> str:
    with zipfile.ZipFile(zip_path) as zf, zf.open(member) as f:
        sample = f.read(8192).decode("utf-8-sig", errors="replace")
    return csv.Sniffer().sniff(sample, delimiters=";,|\t,").delimiter


def header(zip_path: Path, member: str) -> list[str]:
    import pandas as pd

    delimiter = sniff_delimiter(zip_path, member)
    with zipfile.ZipFile(zip_path) as zf, zf.open(member) as f:
        return pd.read_csv(f, sep=delimiter, nrows=0).columns.tolist()


def choose_csv(zip_path: Path, year: int) -> tuple[str, list[str]]:
    members = csv_members(zip_path)
    if not members:
        raise RuntimeError(f"No CSV files found inside {zip_path}")

    scored: list[tuple[int, str, list[str]]] = []
    total_col_pattern = re.compile(rf"^(BBTOT|B{str(year)[-2:]}BTOT)$", re.IGNORECASE)
    for member in members:
        cols = header(zip_path, member)
        score = 0
        if find_coordinate_columns(cols, required=False):
            score += 10
        if any(total_col_pattern.match(col) for col in cols):
            score += 10
        if "statpop" in member.lower():
            score += 2
        scored.append((score, member, cols))

    score, member, cols = max(scored, key=lambda item: item[0])
    if score < 20:
        raise RuntimeError(
            "Could not confidently identify the STATPOP hectare CSV. "
            f"Best candidate was {member} with columns: {cols[:20]}"
        )
    return member, cols


def find_coordinate_columns(columns: list[str], required: bool = True) -> tuple[str, str] | None:
    candidates = [
        ("E_KOORD", "N_KOORD"),
        ("E_Koord", "N_Koord"),
        ("E", "N"),
        ("X", "Y"),
        ("x", "y"),
    ]
    normalized = {col.lower(): col for col in columns}
    for east, north in candidates:
        if east.lower() in normalized and north.lower() in normalized:
            return normalized[east.lower()], normalized[north.lower()]

    if required:
        raise RuntimeError(f"Could not find LV95 coordinate columns in: {columns[:30]}")
    return None


def find_population_column(columns: list[str], year: int) -> str:
    preferred = ["BBTOT", f"B{str(year)[-2:]}BTOT"]
    normalized = {col.upper(): col for col in columns}
    for candidate in preferred:
        if candidate in normalized:
            return normalized[candidate]

    fallback = [col for col in columns if re.match(r"^B\d{2}BTOT$", col, re.IGNORECASE)]
    if fallback:
        return sorted(fallback)[-1]

    raise RuntimeError(f"Could not find a STATPOP total-population column like BBTOT or {preferred[-1]}")


def read_density_table(zip_path: Path, member: str, columns: list[str], year: int) -> pd.DataFrame:
    try:
        import pandas as pd
    except ImportError as exc:
        raise RuntimeError("Conversion requires pandas. Install with: python -m pip install pandas") from exc

    east_col, north_col = find_coordinate_columns(columns)
    population_col = find_population_column(columns, year)
    delimiter = sniff_delimiter(zip_path, member)
    log(f"Using columns x={east_col}, y={north_col}, population={population_col}")

    with zipfile.ZipFile(zip_path) as zf, zf.open(member) as f:
        df = pd.read_csv(f, sep=delimiter, usecols=[east_col, north_col, population_col])

    df = df.rename(
        columns={
            east_col: "x",
            north_col: "y",
            population_col: "population",
        }
    )
    df["x"] = pd.to_numeric(df["x"], errors="raise").astype("int64")
    df["y"] = pd.to_numeric(df["y"], errors="raise").astype("int64")
    df["population"] = pd.to_numeric(df["population"], errors="coerce").fillna(0).astype("float32")
    df["population_density_per_km2"] = (df["population"] / CELL_AREA_KM2).astype("float32")
    return df


def density_to_dataset(df: pd.DataFrame, year: int, percentile_cutoff: float = 5.0) -> xr.Dataset:
    """Rasterize STATPOP onto the same 100 m LV95 grid as ARE settlement-quality layers."""
    x, y = swiss_100m_grid_coords()
    xmin, ymin, xmax, ymax = SWISS_GRID_100M_EDGE_BOUNDS
    log(
        f"Building grid on shared Swiss 100 m LV95 extent "
        f"[{xmin}, {ymin}, {xmax}, {ymax}]: {len(x)} columns x {len(y)} rows"
    )

    # STATPOP E_KOORD/N_KOORD are the south-west corners of 100 m cells (multiples of 100 m),
    # not cell centers. Rounding against center coords collapses alternating columns/rows.
    xi = ((df["x"].to_numpy() - xmin) // CELL_SIZE_M).astype("int64")
    yi = ((ymax - df["y"].to_numpy()) // CELL_SIZE_M).astype("int64")
    in_bounds = (xi >= 0) & (xi < len(x)) & (yi >= 0) & (yi < len(y))
    if not in_bounds.all():
        dropped = int((~in_bounds).sum())
        log(f"Dropping {dropped} cells outside the shared Swiss 100 m grid")
        df = df.loc[in_bounds].reset_index(drop=True)
        xi = xi[in_bounds]
        yi = yi[in_bounds]

    population_density_score = np.full((len(y), len(x)), np.nan, dtype="float32")
    population = np.full((len(y), len(x)), np.nan, dtype="float32")
    population_density_score[yi, xi] = df["population_density_per_km2"].to_numpy(dtype="float32")
    population[yi, xi] = df["population"].to_numpy(dtype="float32")

    ds = xr.Dataset(
        data_vars={
            "population_density_score": (("y", "x"), population_density_score),
            "population": (("y", "x"), population),
        },
        coords={"x": x.astype("float64"), "y": y.astype("float64")},
        attrs={
            "title": f"BFS STATPOP population density {year}",
            "source": "Swiss Federal Statistical Office STATPOP geodata",
            "crs": "EPSG:2056",
            "cell_size_m": CELL_SIZE_M,
            "cell_area_km2": CELL_AREA_KM2,
            "grid_bounds_lv95": list(SWISS_GRID_100M_EDGE_BOUNDS),
        },
    )
    ds["spatial_ref"] = xr.DataArray(
        0,
        attrs={
            "grid_mapping_name": "transverse_mercator",
            "epsg_code": "EPSG:2056",
            "crs_wkt": "EPSG:2056",
        },
    )
    for var in ("population_density_score", "population"):
        ds[var].attrs["grid_mapping"] = "spatial_ref"
    return ds


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download BFS STATPOP geodata, compute population density, and write GeoZarr-style Zarr."
    )
    parser.add_argument("--year", type=int, default=None, help="STATPOP year. Defaults to the newest available year.")
    parser.add_argument("--zip", type=Path, default=None, help="Use an existing STATPOP ZIP instead of downloading.")
    parser.add_argument("--out", type=Path, default=Path(DEFAULT_OUT), help="Output .zarr directory.")
    parser.add_argument(
        "--percentile-cutoff",
        type=float,
        default=5.0,
        help="Percentage of data to cut off from top and bottom for normalization (default: 5.0).",
    )
    parser.add_argument("--cache-dir", "--download-dir", dest="cache_dir", type=Path, default=Path("data"), help="Download/cache directory.")
    parser.add_argument("--chunk-size", type=int, default=1024, help="Zarr chunk size for x and y.")
    parser.add_argument("--force", action="store_true", help="Redownload source ZIP and overwrite output if present.")
    parser.add_argument("--dry-run", action="store_true", help="Resolve and print the source asset without downloading/converting.")
    parser.add_argument("--ca-bundle", type=Path, default=None, help="Path to a PEM CA bundle. Defaults to certifi if installed.")
    parser.add_argument("--insecure", action="store_true", help="Disable TLS verification for local development only.")
    parser.add_argument("--upload", action="store_true", help="Upload the Zarr store to local MinIO (S3-compatible) after writing.")
    parser.add_argument(
        "--remote-name",
        default=None,
        help="Object prefix inside the B2 bucket (defaults to the output .zarr folder name).",
    )
    args = parser.parse_args()
    verify: bool | str = False if args.insecure else str(args.ca_bundle) if args.ca_bundle else default_verify()

    if args.zip:
        zip_path = args.zip
        year = args.year
        if year is None:
            match = re.search(r"vz(\d{4})statpop", zip_path.name, re.IGNORECASE)
            if not match:
                raise RuntimeError("Pass --year when using a ZIP path whose filename does not contain the year.")
            year = int(match.group(1))
    else:
        log("Looking up BFS STATPOP geodata asset")
        asset = latest_statpop_asset(args.year, verify=verify)
        year = int(asset["description"]["bibliography"]["period"])
        url = master_link(asset)
        log(f"Selected STATPOP {year}: DAM id {asset['ids']['damId']} / {asset['shop']['orderNr']}")
        log(f"Source URL: {url}")
        if args.dry_run:
            log("Dry run complete. No files were downloaded or converted.")
            return
        zip_path = args.cache_dir / f"statpop_geodata_{year}.zip"
        if args.force and zip_path.exists():
            zip_path.unlink()
        if not zip_path.exists():
            log(f"Downloading STATPOP {year} to {zip_path}")
            download(url, zip_path, verify=verify)
        else:
            log(f"Using cached ZIP: {zip_path}")

    member, columns = choose_csv(zip_path, year)
    log(f"Reading {member}")
    df = read_density_table(zip_path, member, columns, year)
    ds = density_to_dataset(df, year, percentile_cutoff=args.percentile_cutoff)

    if args.out.exists():
        if not args.force:
            raise RuntimeError(f"Output already exists: {args.out}. Use --force to overwrite.")
        log(f"Removing existing output: {args.out}")
        shutil.rmtree(args.out)
    encoding = {}
    for variable in ds.data_vars:
        if {"y", "x"}.issubset(ds[variable].dims):
            encoding[variable] = {
                "chunks": (
                    min(args.chunk_size, ds.sizes["y"]),
                    min(args.chunk_size, ds.sizes["x"]),
                )
            }

    from settlement_layer_meta import build_layer_meta, compute_percentile_bounds

    ds_aligned = ensure_swiss_grid_dataset(ds)
    p5, p95 = compute_percentile_bounds(
        ds_aligned["population_density_score"],
        percentile_cutoff=args.percentile_cutoff,
    )
    meta = build_layer_meta(
        variable="population_density_score",
        p5=p5,
        p95=p95,
        higher_is_better=False,
        unit="per km²",
    )

    log(f"Writing GeoZarr/Zarr store: {args.out}")
    write_swiss_grid_zarr(ds_aligned, args.out, encoding=encoding, layer_meta=meta)
    log(f"Wrote {args.out}")

    if args.upload:
        remote = upload_zarr(args.out, remote_name=args.remote_name)
        log(f"Uploaded to {remote}")


if __name__ == "__main__":
    try:
        main()
    except requests.exceptions.SSLError as exc:
        print(f"ERROR: {exc}")
        print(
            "\nTLS certificate verification failed in this Python environment.\n"
            "Try one of these:\n"
            "  1. python -m pip install certifi\n"
            "  2. python scripts/download_population_density_to_geozarr.py --year 2024 --insecure --out data/population_density_2024.zarr\n"
            "  3. python scripts/download_population_density_to_geozarr.py --year 2024 --ca-bundle /path/to/ca-bundle.pem --out data/population_density_2024.zarr\n"
        )
        raise SystemExit(1)
