"""Upload local Zarr stores to Backblaze B2 via the S3-compatible API."""

from __future__ import annotations

import os
import time
from pathlib import Path

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parent.parent
for _env_path in (_REPO_ROOT / ".env", Path(__file__).resolve().parent / ".env"):
    if _env_path.is_file():
        load_dotenv(_env_path)
        break
else:
    load_dotenv()

B2_KEY_ID = (
    os.getenv("B2_KEY_ID")
    or os.getenv("AWS_ACCESS_KEY_ID")
    or os.getenv("MINIO_ROOT_USER")
)
B2_APPLICATION_KEY = (
    os.getenv("B2_APPLICATION_KEY")
    or os.getenv("AWS_SECRET_ACCESS_KEY")
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
CONNECT_TIMEOUT_SECONDS = int(os.getenv("B2_CONNECT_TIMEOUT_SECONDS", "30"))
READ_TIMEOUT_SECONDS = int(os.getenv("B2_READ_TIMEOUT_SECONDS", "180"))
UPLOAD_RETRIES = int(os.getenv("B2_UPLOAD_RETRIES", "4"))


def credentials_configured() -> bool:
    return bool(B2_KEY_ID and B2_APPLICATION_KEY)


def create_s3_filesystem():
    """S3-compatible filesystem for Backblaze B2."""
    if not credentials_configured():
        raise RuntimeError(
            "B2 credentials missing. Set B2_KEY_ID and B2_APPLICATION_KEY."
        )

    import s3fs

    return s3fs.S3FileSystem(
        key=B2_KEY_ID,
        secret=B2_APPLICATION_KEY,
        endpoint_url=ENDPOINT_URL,
        config_kwargs={
            "max_pool_connections": 50,
            "connect_timeout": CONNECT_TIMEOUT_SECONDS,
            "read_timeout": READ_TIMEOUT_SECONDS,
            "retries": {"max_attempts": 10, "mode": "standard"},
        },
    )


def s3_storage_options() -> dict:
    return {
        "key": B2_KEY_ID,
        "secret": B2_APPLICATION_KEY,
        "client_kwargs": {"endpoint_url": ENDPOINT_URL},
    }


def _normalize_s3_uri(path: str) -> str:
    return path if path.startswith("s3://") else f"s3://{path}"


def _dedupe_nested_zarr_stores(stores: list[str]) -> list[str]:
    """Drop nested paths when a parent `.zarr` store is already listed."""
    sorted_stores = sorted(set(stores), key=len)
    kept: list[str] = []
    for store in sorted_stores:
        if any(
            other != store and store.startswith(other.rstrip("/") + "/")
            for other in kept
        ):
            continue
        kept.append(store)
    return kept


def discover_zarr_stores_s3(
    fs, bucket: str, prefix: str = ""
) -> list[str]:
    """List Zarr store URIs under bucket/prefix (recursive metadata scan)."""
    base = f"{bucket}/{prefix.strip('/')}".strip("/")
    stores: set[str] = set()

    try:
        for entry in fs.ls(base, detail=False):
            name = entry.rstrip("/").split("/")[-1]
            if name.endswith(".zarr"):
                stores.add(_normalize_s3_uri(entry))
    except FileNotFoundError:
        return []

    for path in fs.find(base):
        if not (path.endswith(".zmetadata") or path.endswith("zarr.json")):
            continue
        store_root = path.rsplit("/", 1)[0]
        while store_root and not store_root.rstrip("/").split("/")[-1].endswith(
            ".zarr"
        ):
            parent = store_root.rsplit("/", 1)[0]
            if parent == store_root or parent == base:
                break
            store_root = parent
        if store_root.rstrip("/").split("/")[-1].endswith(".zarr"):
            stores.add(_normalize_s3_uri(store_root))

    return _dedupe_nested_zarr_stores(list(stores))


def upload_zarr(local_path: Path | str, remote_name: str | None = None) -> str:
    """Upload a local Zarr directory to B2. Returns the remote s3:// path."""
    if not credentials_configured():
        raise RuntimeError(
            "B2 credentials missing. Set B2_KEY_ID and B2_APPLICATION_KEY."
        )

    local = Path(local_path)
    if not local.is_dir():
        raise FileNotFoundError(f"Local Zarr store not found: {local}")

    remote = remote_name or local.name
    target_path = f"{BUCKET_NAME}/{remote}"

    from botocore.exceptions import ClientError, ReadTimeoutError
    import s3fs

    print("Connecting to Backblaze B2...")
    fs = s3fs.S3FileSystem(
        key=B2_KEY_ID,
        secret=B2_APPLICATION_KEY,
        endpoint_url=ENDPOINT_URL,
        config_kwargs={
            "max_pool_connections": 50,
            "connect_timeout": CONNECT_TIMEOUT_SECONDS,
            "read_timeout": READ_TIMEOUT_SECONDS,
            "retries": {"max_attempts": 10, "mode": "standard"},
        },
    )

    remote_uri = f"s3://{target_path}"
    if fs.exists(target_path):
        print(f"Removing existing remote store at {remote_uri}...")
        try:
            fs.rm(target_path, recursive=True)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "Unknown")
            if code in {"AccessDenied", "AllAccessDisabled", "UnauthorizedAccess"}:
                print(
                    "WARN: Missing delete permission on target prefix; continuing with overwrite uploads."
                )
            else:
                raise

    print(f"Uploading '{local}' to '{remote_uri}'...")

    files = [path for path in local.rglob("*") if path.is_file()]
    if not files:
        raise RuntimeError(f"No files found to upload in {local}")

    for index, file_path in enumerate(files, start=1):
        relative = file_path.relative_to(local).as_posix()
        target_file = f"{target_path}/{relative}"

        attempt = 1
        while True:
            try:
                fs.put(lpath=str(file_path), rpath=target_file)
                break
            except ReadTimeoutError:
                if attempt >= UPLOAD_RETRIES:
                    raise
                wait_seconds = min(2 ** (attempt - 1), 8)
                print(
                    f"Timeout uploading {relative} (attempt {attempt}/{UPLOAD_RETRIES}). "
                    f"Retrying in {wait_seconds}s..."
                )
                time.sleep(wait_seconds)
                attempt += 1
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "Unknown")
                if code in {"AccessDenied", "AllAccessDisabled", "UnauthorizedAccess"}:
                    raise RuntimeError(
                        "B2 upload denied (HTTP 403). Verify key permissions "
                        f"for bucket '{BUCKET_NAME}' and endpoint '{ENDPOINT_URL}'."
                    ) from exc
                if attempt >= UPLOAD_RETRIES:
                    raise
                wait_seconds = min(2 ** (attempt - 1), 8)
                print(
                    f"S3 error {code} uploading {relative} (attempt {attempt}/{UPLOAD_RETRIES}). "
                    f"Retrying in {wait_seconds}s..."
                )
                time.sleep(wait_seconds)
                attempt += 1

        if index % 200 == 0 or index == len(files):
            print(f"  uploaded {index}/{len(files)} files")

    print("Upload completed successfully!")
    return f"s3://{target_path}"
