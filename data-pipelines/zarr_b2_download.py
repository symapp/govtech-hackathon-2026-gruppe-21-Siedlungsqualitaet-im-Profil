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

S3_ACCESS_KEY_ID = (
    os.getenv("B2_KEY_ID")
    or os.getenv("MINIO_ROOT_USER")
)
S3_SECRET_ACCESS_KEY = (
    os.getenv("B2_APPLICATION_KEY")
    or os.getenv("MINIO_ROOT_PASSWORD")
)
ENDPOINT_URL = (
    os.getenv("B2_ENDPOINT_URL")
    or os.getenv("S3_ENDPOINT_URL")
    or os.getenv("MINIO_ENDPOINT_URL")
    or "http://127.0.0.1:9000"
)
BUCKET_NAME = (
    os.getenv("B2_BUCKET_NAME")
    or os.getenv("S3_BUCKET_NAME")
    or os.getenv("MINIO_BUCKET_NAME")
    or "egov-hackathon"
)

DEFAULT_OUTPUT_DIR = _REPO_ROOT / "data-pipelines" / "output" / "zarr_download"


def create_fs():
    if not (S3_ACCESS_KEY_ID and S3_SECRET_ACCESS_KEY):
        raise RuntimeError(
            "S3 credentials missing. Set one of: "
            "B2_KEY_ID/B2_APPLICATION_KEY, "
            "or MINIO_ROOT_USER/MINIO_ROOT_PASSWORD."
        )
    import s3fs

    return s3fs.S3FileSystem(
        key=S3_ACCESS_KEY_ID,
        secret=S3_SECRET_ACCESS_KEY,
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
