import argparse
import geopandas as gpd
from geocube.api.core import make_geocube

from are_rasterize_lib import align_geocube_to_swiss_100m_grid, write_swiss_grid_zarr
from pathlib import Path
import shutil
import tempfile
import urllib.request
import zipfile
from zarr_b2_upload import upload_zarr

# Public Transport Quality Classes 2026
GPKG_ZIP_URL = "https://data.geo.admin.ch/ch.are.gueteklassen_oev/gueteklassen_oev_2026/gueteklassen_oev_2026_2056.gpkg.zip"


def download_and_extract_gpkg(url: str) -> Path:
    print(f"Downloading {url}...")
    with urllib.request.urlopen(url) as response:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as temp_zip:
            shutil.copyfileobj(response, temp_zip)
            temp_zip_path = Path(temp_zip.name)

    temp_dir = Path(tempfile.mkdtemp())
    with zipfile.ZipFile(temp_zip_path, "r") as zip_ref:
        zip_ref.extractall(temp_dir)

    temp_zip_path.unlink()

    gpkg_files = list(temp_dir.glob("*.gpkg"))
    if not gpkg_files:
        raise FileNotFoundError("No .gpkg file found in the zip archive")

    return gpkg_files[0]


def main():
    parser = argparse.ArgumentParser(
        description="Rasterize public transport quality categories and write GeoZarr."
    )
    parser.add_argument(
        "--out",
        type=str,
        default="pt_quality_swiss_grid_100m.zarr",
        help="Output Zarr path.",
    )
    parser.add_argument(
        "--upload", action="store_true", help="Upload to local MinIO (S3-compatible) after writing."
    )
    parser.add_argument(
        "--force", action="store_true", help="Overwrite existing output (handled by to_zarr mode='w')."
    )
    parser.add_argument(
        "--percentile-cutoff",
        type=float,
        default=5.0,
        help="Percentile cutoff for settlement-layer-meta.json (p5/p95).",
    )
    parser.add_argument(
        "--remote-name",
        type=str,
        default=None,
        help="Object prefix inside the B2 bucket (defaults to the output .zarr folder name).",
    )
    args = parser.parse_args()

    gpkg_file_path = download_and_extract_gpkg(GPKG_ZIP_URL)

    try:
        print(f"Reading {gpkg_file_path}...")
        geodata = gpd.read_file(gpkg_file_path, layer="OeV_Gueteklassen_ARE")

        class_mapping = {"A": 4, "B": 3, "C": 2, "D": 1}
        geodata["KLASSE_NUM"] = geodata["KLASSE"].map(class_mapping).fillna(0).astype(float)

        resolution = 100

        print(f"Rasterizing to {resolution}m grid...")
        geocube_grid = make_geocube(
            vector_data=geodata,
            measurements=["KLASSE_NUM"],
            resolution=(-resolution, resolution),
            output_crs="EPSG:2056",
            fill=0,
        )
        aligned_grid = align_geocube_to_swiss_100m_grid(geocube_grid, fill_value=0)

        from settlement_layer_meta import build_layer_meta, compute_percentile_bounds

        p5, p95 = compute_percentile_bounds(
            aligned_grid["KLASSE_NUM"],
            percentile_cutoff=args.percentile_cutoff,
        )
        meta = build_layer_meta(
            variable="KLASSE_NUM",
            p5=p5,
            p95=p95,
            higher_is_better=True,
            unit="Nr.",
        )

        zarr_path = Path(args.out)
        print(f"Writing to {zarr_path}...")
        write_swiss_grid_zarr(aligned_grid, zarr_path, layer_meta=meta)

        print(f"Success! GeoZarr created at: {zarr_path}")

        if args.upload:
            remote = upload_zarr(zarr_path, remote_name=args.remote_name)
            print(f"Uploaded to {remote}")

    finally:
        if gpkg_file_path.exists():
            temp_dir = gpkg_file_path.parent
            shutil.rmtree(temp_dir)


if __name__ == "__main__":
    main()
