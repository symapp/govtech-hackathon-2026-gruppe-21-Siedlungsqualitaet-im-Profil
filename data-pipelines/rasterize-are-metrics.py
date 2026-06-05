#!/usr/bin/env python3
"""Rasterize ARE settlement-quality metrics from geo.admin.ch to GeoZarr (100 m grid)."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from are_metrics_registry import ARE_METRICS, COG, NEW_METRIC_IDS, ZIP
from are_rasterize_lib import rasterize_from_cog, rasterize_from_gpkg
from zarr_b2_upload import upload_zarr


def rasterize_metric(metric_id: str, out_dir: Path, *, force: bool = False) -> Path:
    spec = ARE_METRICS[metric_id]
    out = out_dir / spec.zarr_name

    if out.exists():
        if not force:
            raise RuntimeError(f"Output already exists: {out}. Pass --force to overwrite.")
        print(f"[{metric_id}] removing {out}")
        shutil.rmtree(out)

    print(f"[{metric_id}] {spec.source_url}")

    if spec.source_type == COG:
        rasterize_from_cog(url=spec.source_url, out=out, variable=spec.variable)
    else:
        if spec.field is None:
            raise ValueError(f"Metric {metric_id} has no raster field configured")

        rasterize_from_gpkg(
            url=spec.source_url,
            field=spec.field,
            out=out,
            layer=spec.layer,
            fill=spec.fill,
            prepare=spec.prepare,
            line_buffer_m=spec.line_buffer_m,
            zip_url=spec.source_type == ZIP,
        )

    import xarray as xr

    from metric_layer_meta import METRIC_META
    from settlement_layer_meta import write_meta_for_dataset

    higher_is_better, unit = METRIC_META.get(metric_id, (True, ""))
    ds = xr.open_zarr(out)
    try:
        write_meta_for_dataset(
            out,
            ds,
            spec.variable,
            higher_is_better=higher_is_better,
            unit=unit,
        )
    finally:
        ds.close()

    print(f"[{metric_id}] wrote {out}")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download ARE/geo.admin.ch indicators and write 100 m GeoZarr stores.",
    )
    parser.add_argument(
        "metric",
        nargs="?",
        default="all-new",
        help=(
            "Metric id, 'all-new' (default: ten new indicators), 'all', or a name from --list. "
            "Examples: miv-accessibility, pt-travel-time."
        ),
    )
    parser.add_argument("--list", action="store_true", help="List available metric ids and exit.")
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("."),
        help="Directory for output .zarr folders (default: data-pipelines cwd).",
    )
    parser.add_argument("--force", action="store_true", help="Overwrite existing .zarr outputs.")
    parser.add_argument("--upload", action="store_true", help="Upload each Zarr to local MinIO (S3-compatible).")
    parser.add_argument(
        "--remote-name",
        default=None,
        help="B2 object prefix (defaults to the .zarr folder name; only with a single metric).",
    )
    args = parser.parse_args()

    if args.list:
        for metric_id, spec in ARE_METRICS.items():
            print(f"{metric_id:22} {spec.zarr_name:42} {spec.variable}")
        return

    if args.metric == "all":
        metric_ids = list(ARE_METRICS.keys())
    elif args.metric == "all-new":
        metric_ids = list(NEW_METRIC_IDS)
    else:
        if args.metric not in ARE_METRICS:
            raise SystemExit(f"Unknown metric {args.metric!r}. Use --list.")
        metric_ids = [args.metric]

    args.out_dir.mkdir(parents=True, exist_ok=True)

    for metric_id in metric_ids:
        out = rasterize_metric(metric_id, args.out_dir, force=args.force)
        if args.upload:
            remote_name = args.remote_name if len(metric_ids) == 1 else None
            remote = upload_zarr(out, remote_name=remote_name)
            print(f"[{metric_id}] uploaded to {remote}")


if __name__ == "__main__":
    main()
