"""Download all Zarr stores from Backblaze B2 to a local directory."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parent.parent
for _env_path in (_REPO_ROOT / ".env", Path(__file__).resolve().parent / ".env"):
    if _env_path.is_file():
        load_dotenv(_env_path)
        break
else:
    load_dotenv()

B2_KEY_ID = os.getenv("B2_KEY_ID")
B2_APPLICATION_KEY = os.getenv("B2_APPLICATION_KEY")
ENDPOINT_URL = os.getenv(
    "B2_ENDPOINT_URL", "https://s3.eu-central-003.backblazeb2.com"
)
BUCKET_NAME = os.getenv("B2_BUCKET_NAME", "egov-hackathon")

DEFAULT_OUTPUT_DIR = _REPO_ROOT / "data-pipelines" / "output" / "zarr_download"


def create_fs():
    if not (B2_KEY_ID and B2_APPLICATION_KEY):
        raise RuntimeError(
            "B2 credentials missing. Set B2_KEY_ID and B2_APPLICATION_KEY in .env"
        )
    import s3fs

    return s3fs.S3FileSystem(
        key=B2_KEY_ID,
        secret=B2_APPLICATION_KEY,
        endpoint_url=ENDPOINT_URL,
        config_kwargs={"max_pool_connections": 50},
    )


def list_files(fs, prefix: str = "") -> list[str]:
    """Return all file paths under bucket/prefix."""
    base = f"{BUCKET_NAME}/{prefix.strip('/')}".strip("/")
    all_files = fs.find(base)
    return all_files


def download_all(
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    prefix: str = "",
    dry_run: bool = False,
) -> None:
    fs = create_fs()

    print(f"Listing files in s3://{BUCKET_NAME}/{prefix} ...")
    files = list_files(fs, prefix)
    print(f"Found {len(files)} files.\n")

    output_dir.mkdir(parents=True, exist_ok=True)

    for i, remote_path in enumerate(sorted(files), 1):
        # remote_path is like "egov-hackathon/some.zarr/var/c/0/0"
        # strip the bucket prefix to get the relative path
        rel = remote_path.removeprefix(BUCKET_NAME).lstrip("/")
        local_path = output_dir / rel

        if dry_run:
            print(f"[{i}/{len(files)}] DRY-RUN  s3://{remote_path}  →  {local_path}")
            continue

        if local_path.exists():
            print(f"[{i}/{len(files)}] SKIP (exists)  {rel}")
            continue

        local_path.parent.mkdir(parents=True, exist_ok=True)
        print(f"[{i}/{len(files)}] Downloading  {rel} ...", end="", flush=True)
        try:
            fs.get(remote_path, str(local_path))
            size = local_path.stat().st_size
            print(f"  {size:,} bytes")
        except Exception as exc:
            print(f"  ERROR: {exc}")

    print(f"\nDone. Files saved to: {output_dir}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Download all Zarr stores from Backblaze B2."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Local directory to download into (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--prefix",
        default="",
        help="Optional key prefix to filter (e.g. 'ch_bafu_tranquillity_karte.zarr')",
    )
    parser.add_argument(
        "--list-only",
        action="store_true",
        help="Only list remote files, do not download",
    )
    args = parser.parse_args()

    download_all(
        output_dir=args.output_dir,
        prefix=args.prefix,
        dry_run=args.list_only,
    )
