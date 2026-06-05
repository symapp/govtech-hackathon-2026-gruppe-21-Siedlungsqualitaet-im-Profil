#!/usr/bin/env python3
"""Rasterize swissTLM3D green cover and single-tree density to the shared 100 m GeoZarr grid."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

import geopandas as gpd
import numpy as np
import xarray as xr
from rasterio import features
from rasterio.enums import MergeAlg
from rasterio.transform import from_origin

from are_rasterize_lib import (
    OUTPUT_CRS,
    SWISS_GRID_100M_EDGE_BOUNDS,
    swiss_100m_grid_coords,
    write_swiss_grid_zarr,
)
from metric_layer_meta import METRIC_META
from settlement_layer_meta import write_meta_for_dataset
from tlm_green_trees_config import (
    BODENBEDECKUNG_LAYER_HINTS,
    CELL_AREA_M2,
    COMPOSITE_VARIABLE,
    DEFAULT_OUT_ZARR,
    EINZELBAUM_LAYER_HINTS,
    GREEN_BODENBEDECKUNG_OBJART_CODES,
    GREEN_BODENBEDECKUNG_OBJART_NAMES,
    OBJART_COLUMN_CANDIDATES,
    SINGLE_TREE_OBJECTVAL_CODES,
    SINGLE_TREE_OBJART_NAMES,
    TREES_PER_HA_FACTOR,
)
from tlm_green_trees_download import DEFAULT_CACHE_DIR, download_swisstlm3d_gpkg
from zarr_b2_upload import upload_zarr

METRIC_ID = "tlm-green-trees"
RESOLUTION_M = 100
WINDOW_CELLS = 400


def _grid_shape() -> tuple[int, int]:
    xmin, ymin, xmax, ymax = SWISS_GRID_100M_EDGE_BOUNDS
    width = int(round((xmax - xmin) / RESOLUTION_M))
    height = int(round((ymax - ymin) / RESOLUTION_M))
    return height, width


def _grid_transform():
    xmin, ymin, xmax, ymax = SWISS_GRID_100M_EDGE_BOUNDS
    height, width = _grid_shape()
    return from_origin(xmin, ymax, RESOLUTION_M, RESOLUTION_M), height, width


def _objart_column(frame: gpd.GeoDataFrame) -> str | None:
    for name in OBJART_COLUMN_CANDIDATES:
        if name in frame.columns:
            return name
    return None


def _normalize_objart(value: object) -> str | int | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)) and np.isfinite(value):
        return int(value)
    return str(value).strip()


def _is_green_bodenbedeckung(value: str | int | None) -> bool:
    if value is None:
        return False
    if isinstance(value, int):
        return value in GREEN_BODENBEDECKUNG_OBJART_CODES
    normalized = value.replace("ü", "ue").replace("ö", "oe")
    if value in GREEN_BODENBEDECKUNG_OBJART_NAMES or normalized in GREEN_BODENBEDECKUNG_OBJART_NAMES:
        return True
    return value.replace(" ", "_") in GREEN_BODENBEDECKUNG_OBJART_NAMES


def _is_single_tree(value: str | int | None) -> bool:
    if value is None:
        return False
    if isinstance(value, int):
        return value in SINGLE_TREE_OBJECTVAL_CODES
    if value in SINGLE_TREE_OBJART_NAMES:
        return True
    return value.replace(" ", "_") == "Einzelbaum"


def _list_gpkg_layers(path: Path) -> list[str]:
    try:
        import pyogrio

        listed = pyogrio.list_layers(path)
        if listed.ndim == 2:
            return [str(row[0]) for row in listed]
        return [str(name) for name in listed["name"]]
    except Exception:
        pass

    try:
        import fiona

        return list(fiona.listlayers(path))
    except ImportError:
        pass

    return []


def _find_layer(path: Path, hints: tuple[str, ...]) -> str | None:
    layers = _list_gpkg_layers(path)
    if layers:
        lowered = {layer: layer.lower() for layer in layers}
        for hint in hints:
            hint_lower = hint.lower()
            for layer, layer_lower in lowered.items():
                if hint_lower in layer_lower:
                    return layer

    for hint in hints:
        try:
            gpd.read_file(path, layer=hint, rows=1)
        except Exception:
            continue
        return hint
    return None


def _filter_green_polygons(geodata: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    col = _objart_column(geodata)
    if col is None:
        raise ValueError("Bodenbedeckung layer has no Objektart/ObjectVal column")
    mask = geodata[col].map(_normalize_objart).map(_is_green_bodenbedeckung)
    subset = geodata.loc[mask & geodata.geometry.notna()].copy()
    if subset.crs is None:
        subset = subset.set_crs(OUTPUT_CRS)
    elif subset.crs.to_string() != OUTPUT_CRS:
        subset = subset.to_crs(OUTPUT_CRS)
    return subset


def _filter_single_trees(geodata: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    col = _objart_column(geodata)
    if col is None:
        raise ValueError("Einzelbaum layer has no Objektart/ObjectVal column")
    mask = geodata[col].map(_normalize_objart).map(_is_single_tree)
    points = geodata.loc[mask & geodata.geometry.notna()].copy()
    if points.crs is None:
        points = points.set_crs(OUTPUT_CRS)
    elif points.crs.to_string() != OUTPUT_CRS:
        points = points.to_crs(OUTPUT_CRS)
    points = points[points.geometry.geom_type.isin(["Point", "MultiPoint"])]
    return points


def _rasterize_green_area_m2(green: gpd.GeoDataFrame, transform, out_shape: tuple[int, int]) -> np.ndarray:
    if green.empty:
        return np.zeros(out_shape, dtype=np.float32)

    shapes = []
    for geom in green.geometry:
        if geom is None or geom.is_empty:
            continue
        if geom.geom_type == "MultiPolygon":
            for part in geom.geoms:
                if not part.is_empty:
                    shapes.append((part, float(part.area)))
        elif geom.geom_type == "Polygon":
            shapes.append((geom, float(geom.area)))

    if not shapes:
        return np.zeros(out_shape, dtype=np.float32)

    burned = features.rasterize(
        shapes=shapes,
        out_shape=out_shape,
        transform=transform,
        fill=0.0,
        dtype=np.float32,
        merge_alg=MergeAlg.add,
        all_touched=True,
    )
    return np.asarray(burned, dtype=np.float32)


def _rasterize_tree_counts(trees: gpd.GeoDataFrame, transform, out_shape: tuple[int, int]) -> np.ndarray:
    if trees.empty:
        return np.zeros(out_shape, dtype=np.float32)

    shapes = []
    for geom in trees.geometry:
        if geom is None or geom.is_empty:
            continue
        if geom.geom_type == "MultiPoint":
            for part in geom.geoms:
                shapes.append((part, 1.0))
        elif geom.geom_type == "Point":
            shapes.append((geom, 1.0))

    if not shapes:
        return np.zeros(out_shape, dtype=np.float32)

    burned = features.rasterize(
        shapes=shapes,
        out_shape=out_shape,
        transform=transform,
        fill=0.0,
        dtype=np.float32,
        merge_alg=MergeAlg.add,
    )
    return np.asarray(burned, dtype=np.float32)


def _window_slices(
    bbox: tuple[float, float, float, float],
) -> tuple[slice, slice, tuple[int, int], object]:
    """Map LV95 bbox to row/col slices and a window transform on the national grid."""
    xmin_grid, ymin_grid, xmax_grid, ymax_grid = SWISS_GRID_100M_EDGE_BOUNDS
    bx0, by0, bx1, by1 = bbox

    col_start = max(0, int(np.floor((bx0 - xmin_grid) / RESOLUTION_M)))
    col_end = min(
        int(np.ceil((xmax_grid - xmin_grid) / RESOLUTION_M)),
        int(np.ceil((bx1 - xmin_grid) / RESOLUTION_M)),
    )
    row_start = max(0, int(np.floor((ymax_grid - by1) / RESOLUTION_M)))
    row_end = min(
        int(np.ceil((ymax_grid - ymin_grid) / RESOLUTION_M)),
        int(np.ceil((ymax_grid - by0) / RESOLUTION_M)),
    )

    win_height = row_end - row_start
    win_width = col_end - col_start
    win_transform = from_origin(
        xmin_grid + col_start * RESOLUTION_M,
        ymax_grid - row_start * RESOLUTION_M,
        RESOLUTION_M,
        RESOLUTION_M,
    )
    return slice(row_start, row_end), slice(col_start, col_end), (win_height, win_width), win_transform


def _iter_lv95_windows(window_cells: int = WINDOW_CELLS):
    xmin, ymin, xmax, ymax = SWISS_GRID_100M_EDGE_BOUNDS
    step = window_cells * RESOLUTION_M
    x = xmin
    while x < xmax:
        y = ymin
        x1 = min(x + step, xmax)
        while y < ymax:
            y1 = min(y + step, ymax)
            yield (x, y, x1, y1)
            y = y1
        x = x1


def _accumulate_gpkg_window(
    path: Path,
    bbox: tuple[float, float, float, float],
    *,
    bb_layer: str | None,
    tree_layer: str | None,
    green_total: np.ndarray,
    tree_total: np.ndarray,
) -> None:
    row_slice, col_slice, win_shape, win_transform = _window_slices(bbox)

    if bb_layer is not None:
        raw = gpd.read_file(path, layer=bb_layer, bbox=bbox)
        green_gdf = _filter_green_polygons(raw)
        if not green_gdf.empty:
            patch = _rasterize_green_area_m2(green_gdf, win_transform, win_shape)
            green_total[row_slice, col_slice] += patch

    if tree_layer is not None:
        raw = gpd.read_file(path, layer=tree_layer, bbox=bbox)
        tree_gdf = _filter_single_trees(raw)
        if not tree_gdf.empty:
            patch = _rasterize_tree_counts(tree_gdf, win_transform, win_shape)
            tree_total[row_slice, col_slice] += patch


def process_tile(path: Path) -> tuple[np.ndarray, np.ndarray]:
    """Return (green_area_m2, tree_count) rasters for one GeoPackage (chunked if national)."""
    _, height, width = _grid_transform()
    out_shape = (height, width)

    bb_layer = _find_layer(path, BODENBEDECKUNG_LAYER_HINTS)
    tree_layer = _find_layer(path, EINZELBAUM_LAYER_HINTS)
    if bb_layer is None and tree_layer is None:
        raise ValueError(f"No TLM_BB layers found in {path}")

    green_area = np.zeros(out_shape, dtype=np.float64)
    tree_count = np.zeros(out_shape, dtype=np.float64)

    windows = list(_iter_lv95_windows())
    for index, bbox in enumerate(windows, start=1):
        if index == 1 or index % 10 == 0 or index == len(windows):
            print(f"  window {index}/{len(windows)}")
        _accumulate_gpkg_window(
            path,
            bbox,
            bb_layer=bb_layer,
            tree_layer=tree_layer,
            green_total=green_area,
            tree_total=tree_count,
        )

    return green_area, tree_count


def discover_tile_paths(tiles_dir: Path) -> list[Path]:
    paths = sorted(tiles_dir.rglob("*.gpkg"))
    if not paths:
        raise FileNotFoundError(f"No .gpkg files under {tiles_dir}")
    return paths


def resolve_gpkg_sources(
    *,
    tiles_dir: Path | None,
    cache_dir: Path,
    download: bool,
    force_download: bool,
) -> list[Path]:
    if tiles_dir is not None:
        tiles_dir = tiles_dir.resolve()
        if tiles_dir.is_file() and tiles_dir.suffix.lower() == ".gpkg":
            return [tiles_dir]
        if tiles_dir.exists() and any(tiles_dir.rglob("*.gpkg")):
            return discover_tile_paths(tiles_dir)

    if download:
        return [download_swisstlm3d_gpkg(cache_dir, force=force_download)]

    raise FileNotFoundError(
        f"No swissTLM3D GeoPackage under {tiles_dir}. "
        "Omit --no-download to fetch from geo.admin.ch STAC automatically."
    )


def build_composite(
    green_area_m2: np.ndarray,
    tree_count: np.ndarray,
    *,
    tree_density_percentile: float = 95.0,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    green_fraction = np.clip(green_area_m2 / CELL_AREA_M2, 0.0, 1.0).astype(np.float32)
    tree_density = (tree_count * TREES_PER_HA_FACTOR).astype(np.float32)

    positive = tree_density[np.isfinite(tree_density) & (tree_density > 0)]
    if positive.size == 0:
        tree_norm = np.zeros_like(tree_density, dtype=np.float32)
    else:
        scale = float(np.percentile(positive, tree_density_percentile))
        if scale <= 0:
            scale = 1.0
        tree_norm = np.clip(tree_density / scale, 0.0, 1.0).astype(np.float32)

    composite = (0.5 * green_fraction + 0.5 * tree_norm).astype(np.float32)
    return green_fraction, tree_density, composite


def write_output_zarr(
    out: Path,
    green_fraction: np.ndarray,
    tree_density: np.ndarray,
    composite: np.ndarray,
) -> None:
    x, y = swiss_100m_grid_coords()
    if composite.shape != (len(y), len(x)):
        raise ValueError(f"Raster shape {composite.shape} does not match grid {(len(y), len(x))}")

    ds = xr.Dataset(
        {
            "green_area_fraction": (("y", "x"), green_fraction),
            "single_tree_density_per_ha": (("y", "x"), tree_density),
            COMPOSITE_VARIABLE: (("y", "x"), composite),
        },
        coords={"x": x, "y": y},
    )
    ds = ds.rio.write_crs(OUTPUT_CRS)
    write_swiss_grid_zarr(ds, out)

    higher, unit = METRIC_META[METRIC_ID]
    write_meta_for_dataset(out, ds, COMPOSITE_VARIABLE, higher_is_better=higher, unit=unit)


def rasterize_tlm_green_trees(
    out: Path,
    *,
    tiles_dir: Path | None = None,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    download: bool = True,
    force_download: bool = False,
    force: bool = False,
) -> Path:
    if out.exists():
        if not force:
            raise RuntimeError(f"Output already exists: {out}. Pass --force to overwrite.")
        shutil.rmtree(out)

    tile_paths = resolve_gpkg_sources(
        tiles_dir=tiles_dir,
        cache_dir=cache_dir,
        download=download,
        force_download=force_download,
    )
    print(f"Processing {len(tile_paths)} GeoPackage source(s)")

    green_total = None
    tree_total = None

    for index, tile_path in enumerate(tile_paths, start=1):
        print(f"[{index}/{len(tile_paths)}] {tile_path.name}", flush=True)
        green_area, tree_count = process_tile(tile_path)
        if green_total is None:
            green_total = green_area
            tree_total = tree_count
        else:
            green_total += green_area
            tree_total += tree_count

    assert green_total is not None and tree_total is not None
    green_fraction, tree_density, composite = build_composite(green_total, tree_total)

    out.parent.mkdir(parents=True, exist_ok=True)
    write_output_zarr(out, green_fraction, tree_density, composite)
    print(f"Wrote {out}")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build swissTLM3D green-area and single-tree density GeoZarr (100 m LV95 grid).",
    )
    parser.add_argument(
        "--tiles-dir",
        type=Path,
        default=None,
        help="Local .gpkg file or directory of tiles (optional if downloading from STAC).",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=DEFAULT_CACHE_DIR,
        help="Download/extract cache for swissTLM3D from geo.admin.ch STAC.",
    )
    parser.add_argument(
        "--no-download",
        action="store_true",
        help="Do not download from STAC; require --tiles-dir with existing data.",
    )
    parser.add_argument(
        "--force-download",
        action="store_true",
        help="Re-download and re-extract swissTLM3D even if cached.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(DEFAULT_OUT_ZARR),
        help="Output .zarr directory.",
    )
    parser.add_argument("--force", action="store_true", help="Overwrite existing output.")
    parser.add_argument("--upload", action="store_true", help="Upload to local MinIO (S3-compatible) after writing.")
    parser.add_argument(
        "--remote-name",
        default=None,
        help="B2 object prefix (defaults to the .zarr folder name).",
    )
    args = parser.parse_args()

    out = rasterize_tlm_green_trees(
        args.out,
        tiles_dir=args.tiles_dir,
        cache_dir=args.cache_dir,
        download=not args.no_download,
        force_download=args.force_download,
        force=args.force,
    )

    if args.upload:
        remote = upload_zarr(out, remote_name=args.remote_name)
        print(f"Uploaded to {remote}")


if __name__ == "__main__":
    main()
