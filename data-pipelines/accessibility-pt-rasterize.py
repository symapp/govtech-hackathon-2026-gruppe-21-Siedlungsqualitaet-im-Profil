import argparse
import shutil
import geopandas as gpd
from geocube.api.core import make_geocube

from are_rasterize_lib import align_geocube_to_swiss_100m_grid, write_swiss_grid_zarr
from pathlib import Path
import tempfile
import urllib.request

from zarr_b2_upload import upload_zarr

GPKG_URL = "https://data.geo.admin.ch/ch.are.erreichbarkeit-oev/erreichbarkeit-oev/erreichbarkeit-oev_2056.gpkg"
DEFAULT_ZARR_PATH = "erreichbarkeit_swiss_grid_100m.zarr"


def download_gpkg(url: str) -> Path:
    with urllib.request.urlopen(url) as response:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".gpkg") as temp_file:
            shutil.copyfileobj(response, temp_file)
            return Path(temp_file.name)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rasterize public transport accessibility and write GeoZarr."
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

    gpkg_file_path = download_gpkg(GPKG_URL)

    try:
        geodata = gpd.read_file(gpkg_file_path)

        resolution = 100

        geocube_grid = make_geocube(
            vector_data=geodata,
            measurements=["OeV_Erreichb_EW"],
            resolution=(-resolution, resolution),
            output_crs="EPSG:2056",
            fill=0,
        )
        aligned_grid = align_geocube_to_swiss_100m_grid(geocube_grid, fill_value=0)

        from settlement_layer_meta import build_layer_meta, compute_percentile_bounds

        ew = aligned_grid["OeV_Erreichb_EW"]
        # Geocube fill is 0; percentiles on the full grid collapse toward 0–1 in the UI.
        # Use only cells with ARE EW values so p5/p95 match the real accessibility spread.
        populated = ew.values > 0
        p5, p95 = compute_percentile_bounds(
            ew,
            percentile_cutoff=args.percentile_cutoff,
            mask=populated,
        )
        meta = build_layer_meta(
            variable="OeV_Erreichb_EW",
            p5=p5,
            p95=p95,
            higher_is_better=True,
            unit="EW",
        )

        write_swiss_grid_zarr(aligned_grid, args.out, layer_meta=meta)

        print(f"Success! GeoZarr created at: {args.out}")
        print(aligned_grid.head())

        if args.upload:
            remote = upload_zarr(args.out, remote_name=args.remote_name)
            print(f"Uploaded to {remote}")
    finally:
        gpkg_file_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
