import argparse
import shutil
from pathlib import Path

import pystac_client
import rioxarray
from rasterio.enums import Resampling

from are_rasterize_lib import align_raster_to_swiss_100m_grid, write_swiss_grid_zarr
from zarr_b2_upload import upload_zarr

# Swiss Federal Geoportal STAC API endpoint
STAC_URL = "https://data.geo.admin.ch/api/stac/v1/"
COLLECTION_ID = "ch.bafu.tranquillity-karte"
DEFAULT_ZARR_PATH = "ch_bafu_tranquillity_karte.zarr"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download BAFU tranquillity map from STAC and write GeoZarr."
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(DEFAULT_ZARR_PATH),
        help="Output .zarr directory.",
    )
    parser.add_argument("--upload", action="store_true", help="Upload to local MinIO (S3-compatible) after writing.")
    parser.add_argument("--force", action="store_true", help="Overwrite existing output directory.")
    parser.add_argument(
        "--percentile-cutoff",
        type=float,
        default=5.0,
        help="Percentage of data to cut off from top and bottom for normalization (default: 5.0).",
    )
    parser.add_argument(
        "--remote-name",
        default=None,
        help="Object prefix inside the B2 bucket (defaults to the output .zarr folder name).",
    )
    args = parser.parse_args()

    if args.out.exists():
        if args.force:
            print(f"Removing existing output: {args.out}")
            shutil.rmtree(args.out)
        else:
            raise RuntimeError(f"Output already exists: {args.out}. Use --force to overwrite.")

    print(f"Connecting to STAC API at {STAC_URL}...")
    client = pystac_client.Client.open(STAC_URL)

    search = client.search(collections=[COLLECTION_ID])
    items = list(search.items())

    if not items:
        raise SystemExit(f"No data items found for collection: {COLLECTION_ID}")

    item = items[0]
    print(f"Found STAC item: {item.id}")

    cog_url = None
    for asset in item.assets.values():
        if str(asset.href).endswith(".tif"):
            cog_url = asset.href
            break

    if not cog_url:
        cog_url = item.assets[list(item.assets.keys())[1]].href

    print(f"Streaming data from COG URL: {cog_url}")

    da = rioxarray.open_rasterio(cog_url, chunks={"x": 1024, "y": 1024})

    print("Aligning to shared Swiss 100 m grid (ARE settlement-quality extent)...")
    da = align_raster_to_swiss_100m_grid(da, resampling=Resampling.nearest)

    da = da.where(da != da.rio.nodata)
    ds = da.to_dataset(name="tranquillity_index")

    from settlement_layer_meta import build_layer_meta, compute_percentile_bounds

    p5, p95 = compute_percentile_bounds(
        ds["tranquillity_index"],
        percentile_cutoff=args.percentile_cutoff,
    )
    meta = build_layer_meta(
        variable="tranquillity_index",
        p5=p5,
        p95=p95,
        higher_is_better=True,
        unit="",
    )

    print(f"Writing raw tranquillity GeoZarr: {args.out} (p5={p5:.4f}, p95={p95:.4f})...")
    write_swiss_grid_zarr(ds, args.out, layer_meta=meta)
    print("Successfully converted and saved to GeoZarr!")

    if args.upload:
        remote = upload_zarr(args.out, remote_name=args.remote_name)
        print(f"Uploaded to {remote}")


if __name__ == "__main__":
    main()
