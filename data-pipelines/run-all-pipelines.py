import argparse
import subprocess
import sys
from pathlib import Path

# (script, pipeline args, accepts_percentile_cutoff, accepts_force)
PIPELINES: list[tuple[str, list[str], bool, bool]] = [
    ("tranquillity-rasterize.py", [], True, True),
    ("density-rasterize.py", [], True, True),
    ("rasterize-are-metrics.py", ["all"], False, True),
    ("tlm-green-trees-rasterize.py", [], False, True),
    ("meteo-fetch.py", [], False, False),
]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Regenerate all GeoZarr layers on the shared 100 m LV95 grid and optionally upload to B2.",
    )
    parser.add_argument(
        "--upload", action="store_true", help="Upload results to B2 bucket for all pipelines."
    )
    parser.add_argument(
        "--force", action="store_true", help="Overwrite existing output directories."
    )
    parser.add_argument(
        "--percentile-cutoff",
        type=float,
        default=5.0,
        help="Percentile cutoff for normalized layers (tranquillity, STATPOP score).",
    )
    args = parser.parse_args()

    base_path = Path(__file__).parent

    common_flags: list[str] = []
    if args.upload:
        common_flags.append("--upload")

    for pipeline, pipeline_args, accepts_percentile, accepts_force in PIPELINES:
        pipeline_path = base_path / pipeline
        if not pipeline_path.exists():
            print(f"Error: Pipeline script {pipeline} not found in {base_path}")
            continue

        print(f"\n{'=' * 60}")
        print(f"Running pipeline: {pipeline} {' '.join(pipeline_args)}")
        print(f"{'=' * 60}\n")

        extra_flags = list(common_flags)
        if args.force and accepts_force:
            extra_flags.append("--force")
        if accepts_percentile:
            extra_flags.extend(["--percentile-cutoff", str(args.percentile_cutoff)])

        cmd = ["uv", "run", "python", str(pipeline_path), *pipeline_args, *extra_flags]

        try:
            subprocess.run(cmd, check=True)
            print(f"\nSuccessfully completed {pipeline}\n")
        except subprocess.CalledProcessError as e:
            print(f"\nError: Pipeline {pipeline} failed with exit code {e.returncode}\n")
            sys.exit(e.returncode)

    print("\nAll pipelines completed successfully!")
    print("All .zarr stores share SWISS_GRID_100M_EDGE_BOUNDS (EPSG:2056, 100 m cells).")


if __name__ == "__main__":
    main()
