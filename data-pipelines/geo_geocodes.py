Settlement quality
#!/usr/bin/env python3
"""Rasterize swissBOUNDARIES3D municipality polygons to a 100 m GeoZarr with BFS geocodes.

Each pixel receives the BFS Gemeinde-Nummer (BFS_NUMMER) of the municipality it falls
within.  Pixels outside any municipality boundary are set to 0.

Output
------
geocodes_municipalities_100m.zarr  — xarray Dataset with variable "geocode" (int32).
"""

from __future__ import annotations

import argparse
import shutil
import tempfile
import urllib.request
import zipfile
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import xarray as xr
from geocube.api.core import make_geocube

from are_rasterize_lib import OUTPUT_CRS, SWISS_GRID_100M_EDGE_BOUNDS
from zarr_b2_upload import upload_zarr

# ---------------------------------------------------------------------------
# Download URL for swissBOUNDARIES3D (LV95 / GPKG), updated annually.
# Override via --gpkg-path if you already have the file.
# ---------------------------------------------------------------------------
SWISSBOUNDARIES3D_URL = (
    "https://data.geo.admin.ch/ch.swisstopo.swissboundaries3d/"
    "swissboundaries3d_2025-01/swissboundaries3d_2025-01_2056_5728.gpkg.zip"
)

# Local data directory (relative to this script).
DATA_DIR = Path(__file__).parent / "data"

DEFAULT_GPKG_PATH = DATA_DIR / "swissBOUNDARIES3D_1_5_LV95_LN02.gpkg"
DEFAULT_GEMEINDESTAND_PATH = DATA_DIR / "Gemeindestand.xlsx"

# Municipality layer name inside the GPKG (v1.5 naming convention).
# Inspected with: pyogrio.list_layers(path)
MUNICIPALITY_LAYER_CANDIDATES = [
    "swissBOUNDARIES3D_1_5_LV95_LN02_GEM",
    "tlm_hoheitsgebiet",
    "TLM_HOHEITSGEBIET",
    "GEM",
]

# Column in swissBOUNDARIES3D that holds the official BFS Gemeinde-Nummer.
BFS_COLUMN_CANDIDATES = ["BFS_NUMMER", "GMDE_NR", "OBJECTVAL", "BFS_NR"]

RESOLUTION_M = 100
DEFAULT_OUT = "geocodes_municipalities_100m.zarr"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _swiss_100m_bounds() -> tuple[float, float, float, float]:
    """Return (xmin, ymin, xmax, ymax) snapped to 100 m edges."""
    xmin, ymin, xmax, ymax = SWISS_GRID_100M_EDGE_BOUNDS
    snap = RESOLUTION_M
    xmin = snap * np.floor(xmin / snap)
    ymin = snap * np.floor(ymin / snap)
    xmax = snap * np.ceil(xmax / snap)
    ymax = snap * np.ceil(ymax / snap)
    return xmin, ymin, xmax, ymax


def _build_100m_target_grid() -> xr.DataArray:
    xmin, ymin, xmax, ymax = _swiss_100m_bounds()
    half = RESOLUTION_M / 2.0
    x = np.arange(xmin + half, xmax, RESOLUTION_M, dtype=np.float64)
    y = np.arange(ymax - half, ymin, -RESOLUTION_M, dtype=np.float64)
    target = xr.DataArray(
        np.zeros((len(y), len(x)), dtype=np.int32),
        coords={"y": y, "x": x},
        dims=("y", "x"),
    )
    import rioxarray  # noqa: F401 – registers .rio accessor
    return target.rio.write_crs(OUTPUT_CRS)


def _list_gpkg_layers(gpkg_path: Path) -> list[str]:
    """Return layer names from a GPKG without requiring fiona directly."""
    # geopandas >= 1.0 exposes gpd.list_layers()
    if hasattr(gpd, "list_layers"):
        return list(gpd.list_layers(str(gpkg_path))["name"])
    # pyogrio is the default engine in recent geopandas and is always available.
    try:
        import pyogrio
        return [info[0] for info in pyogrio.list_layers(str(gpkg_path))]
    except Exception:
        pass
    # Last resort: try reading with osgeo/GDAL directly.
    try:
        from osgeo import ogr
        ds = ogr.Open(str(gpkg_path))
        return [ds.GetLayerByIndex(i).GetName() for i in range(ds.GetLayerCount())]
    except Exception:
        pass
    raise RuntimeError(
        f"Cannot list layers in {gpkg_path}. "
        "Install pyogrio or geopandas >= 1.0."
    )


def _detect_layer(gpkg_path: Path) -> str:
    layers = _list_gpkg_layers(gpkg_path)
    print(f"  Available layers: {layers}")
    for candidate in MUNICIPALITY_LAYER_CANDIDATES:
        if candidate in layers:
            return candidate
    # Fall back to the first layer whose name contains "GEM"
    for layer in layers:
        if "GEM" in layer.upper():
            return layer
    raise ValueError(
        f"Cannot find a municipality layer in {gpkg_path}. "
        f"Available: {layers}. Pass --layer explicitly."
    )


def _detect_bfs_column(gdf: gpd.GeoDataFrame) -> str:
    columns_by_lower = {str(col).lower(): col for col in gdf.columns}
    for candidate in BFS_COLUMN_CANDIDATES:
        exact = columns_by_lower.get(candidate.lower())
        if exact is not None:
            return exact

    # Fall back to a generic fuzzy match such as bfs_nummer / bfs_nr variants.
    for lower_name, original_name in columns_by_lower.items():
        if "bfs" in lower_name and ("nummer" in lower_name or lower_name.endswith("_nr") or lower_name == "nr"):
            return original_name

    raise ValueError(
        f"Cannot find BFS number column.  Available columns: {list(gdf.columns)}. "
        "Pass --bfs-column explicitly."
    )


def download_and_extract_gpkg(url: str) -> tuple[Path, Path]:
    """Download a zipped GPKG and return (gpkg_path, temp_dir)."""
    print(f"Downloading {url} ...")
    with urllib.request.urlopen(url) as response:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as tmp:
            shutil.copyfileobj(response, tmp)
            zip_path = Path(tmp.name)

    temp_dir = Path(tempfile.mkdtemp())
    with zipfile.ZipFile(zip_path, "r") as archive:
        archive.extractall(temp_dir)
    zip_path.unlink(missing_ok=True)

    gpkg_files = list(temp_dir.glob("**/*.gpkg"))
    if not gpkg_files:
        raise FileNotFoundError(
            "No .gpkg file found in the downloaded archive.")
    return gpkg_files[0], temp_dir


def load_municipalities(
    gpkg_path: Path,
    layer: str | None,
    bfs_column: str | None,
    gemeindestand_path: Path | None,
) -> gpd.GeoDataFrame:
    """Load municipality polygons with a 'geocode' column (BFS Gde-nummer)."""
    layer = layer or _detect_layer(gpkg_path)
    print(f"  Reading layer '{layer}' from {gpkg_path.name} ...")
    gdf = gpd.read_file(gpkg_path, layer=layer)

    # Keep only actual municipalities (filter out districts / cantons if present).
    columns_by_lower = {str(col).lower(): col for col in gdf.columns}
    objektart_col = columns_by_lower.get(
        "objektart") or columns_by_lower.get("objektart_ch")
    if objektart_col is not None:
        gdf = gdf[gdf[objektart_col].astype(
            str).str.lower() == "gemeindegebiet"].copy()

    bfs_col = bfs_column or _detect_bfs_column(gdf)
    print(
        f"  Using BFS column '{bfs_col}' ({gdf[bfs_col].nunique()} municipalities).")

    gdf = gdf[[bfs_col, "geometry"]].copy()
    gdf = gdf.rename(columns={bfs_col: "geocode"})
    gdf["geocode"] = gdf["geocode"].astype(int)

    # Optionally validate/restrict to geocodes present in Gemeindestand.xlsx.
    if gemeindestand_path is not None:
        print(f"  Cross-checking with {gemeindestand_path.name} ...")
        xlsx = pd.read_excel(gemeindestand_path)
        # Accept multiple possible column-name variants.
        bfs_xlsx_col = next(
            (c for c in xlsx.columns if "BFS" in str(
                c) and "nummer" in str(c).lower()),
            None,
        )
        if bfs_xlsx_col is None:
            bfs_xlsx_col = next(
                (c for c in xlsx.columns if "Gde-nummer" in str(c)
                 or "Gde_nummer" in str(c)),
                None,
            )
        if bfs_xlsx_col:
            valid_codes = set(xlsx[bfs_xlsx_col].dropna().astype(int))
            before = len(gdf)
            gdf = gdf[gdf["geocode"].isin(valid_codes)]
            print(
                f"  Filtered from {before} to {len(gdf)} municipalities "
                f"using Gemeindestand.xlsx (column '{bfs_xlsx_col}')."
            )

    gdf = gdf.to_crs(OUTPUT_CRS)
    return gdf


def rasterize_geocodes(gdf: gpd.GeoDataFrame) -> xr.Dataset:
    """Rasterize municipality polygons at 100 m resolution over Switzerland."""
    print(f"Rasterizing {len(gdf)} municipalities at {RESOLUTION_M} m ...")
    target_grid = _build_100m_target_grid()

    geocube = make_geocube(
        vector_data=gdf,
        measurements=["geocode"],
        like=target_grid,
        fill=0,
    )
    geocube["geocode"] = geocube["geocode"].astype(np.int32)
    return geocube


def write_zarr(dataset: xr.Dataset, out: Path) -> None:
    """Write the geocode dataset to a Zarr store with sensible chunking."""
    # Chunk size chosen so each chunk is ~2 MB (int32 = 4 bytes).
    # 512 × 512 × 4 B ≈ 1 MB — a good balance for streaming.
    encoding = {
        "geocode": {
            "dtype": "int32",
            "chunks": [512, 512],
        }
    }
    print(f"Writing {out} ...")
    dataset.to_zarr(str(out), mode="w", consolidated=True, encoding=encoding)
    print(f"  geocode array shape: {dataset['geocode'].shape}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Rasterize swissBOUNDARIES3D municipality polygons to a 100 m GeoZarr "
            "where each pixel carries the BFS Gemeinde-Nummer (geocode)."
        )
    )
    parser.add_argument(
        "--gpkg-path",
        type=Path,
        default=DEFAULT_GPKG_PATH,
        help="Path to the swissBOUNDARIES3D .gpkg file (default: data/swissBOUNDARIES3D_1_5_LV95_LN02.gpkg).",
    )
    parser.add_argument(
        "--gpkg-url",
        type=str,
        default=SWISSBOUNDARIES3D_URL,
        help="URL for swissBOUNDARIES3D GPKG zip (used when --gpkg-path is omitted).",
    )
    parser.add_argument(
        "--layer",
        type=str,
        default=None,
        help="Layer name inside the GPKG (auto-detected if omitted).",
    )
    parser.add_argument(
        "--bfs-column",
        type=str,
        default=None,
        help="Column name for the BFS Gde-nummer in the GPKG (auto-detected if omitted).",
    )
    parser.add_argument(
        "--gemeindestand",
        type=Path,
        default=DEFAULT_GEMEINDESTAND_PATH,
        help="Path to Gemeindestand.xlsx (default: data/Gemeindestand.xlsx).",
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
    args = parser.parse_args()

    temp_dir: Path | None = None
    try:
        if args.gpkg_path.exists():
            gpkg_path = args.gpkg_path.resolve()
        else:
            print(f"Local GPKG not found at {args.gpkg_path}, downloading ...")
            gpkg_path, temp_dir = download_and_extract_gpkg(args.gpkg_url)

        gemeindestand = args.gemeindestand if args.gemeindestand.exists() else None

        gdf = load_municipalities(
            gpkg_path,
            layer=args.layer,
            bfs_column=args.bfs_column,
            gemeindestand_path=gemeindestand,
        )
        dataset = rasterize_geocodes(gdf)
        write_zarr(dataset, args.out)
        print(f"Success! GeoZarr written to {args.out}")

        if args.upload:
            remote = upload_zarr(args.out, remote_name=args.remote_name)
            print(f"Uploaded to {remote}")

    finally:
        if temp_dir is not None and temp_dir.exists():
            shutil.rmtree(temp_dir)


if __name__ == "__main__":
    main()
