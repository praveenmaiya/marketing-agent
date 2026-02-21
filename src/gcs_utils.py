"""Google Cloud Storage utilities."""

import logging
from pathlib import Path
from typing import Optional

from google.cloud import storage

logger = logging.getLogger(__name__)


def get_client() -> storage.Client:
    """Get GCS client.

    Returns:
        Authenticated GCS client.
    """
    return storage.Client()


def upload_blob(
    local_path: str,
    bucket_name: str,
    blob_name: str,
    client: Optional[storage.Client] = None,
) -> str:
    """Upload a file to GCS.

    Args:
        local_path: Local file path.
        bucket_name: GCS bucket name.
        blob_name: Destination blob name.
        client: Optional GCS client.

    Returns:
        GCS URI of uploaded file.
    """
    if client is None:
        client = get_client()

    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_name)

    blob.upload_from_filename(local_path)

    uri = f"gs://{bucket_name}/{blob_name}"
    logger.info(f"Uploaded {local_path} to {uri}")

    return uri


def download_blob(
    bucket_name: str,
    blob_name: str,
    local_path: str,
    client: Optional[storage.Client] = None,
) -> str:
    """Download a file from GCS.

    Args:
        bucket_name: GCS bucket name.
        blob_name: Source blob name.
        local_path: Local destination path.
        client: Optional GCS client.

    Returns:
        Local path of downloaded file.
    """
    if client is None:
        client = get_client()

    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_name)

    # Create parent directories
    Path(local_path).parent.mkdir(parents=True, exist_ok=True)

    blob.download_to_filename(local_path)

    logger.info(f"Downloaded gs://{bucket_name}/{blob_name} to {local_path}")

    return local_path


def parse_gcs_uri(uri: str) -> tuple[str, str]:
    """Parse GCS URI into bucket and blob name.

    Args:
        uri: GCS URI (gs://bucket/path/to/blob).

    Returns:
        Tuple of (bucket_name, blob_name).
    """
    if not uri.startswith("gs://"):
        raise ValueError(f"Invalid GCS URI: {uri}")

    path = uri[5:]  # Remove 'gs://'
    parts = path.split("/", 1)

    if len(parts) != 2:
        raise ValueError(f"Invalid GCS URI: {uri}")

    return parts[0], parts[1]


def upload_model(
    local_path: str,
    gcs_path: str,
    client: Optional[storage.Client] = None,
) -> str:
    """Upload model artifact to GCS.

    Args:
        local_path: Local model file path.
        gcs_path: GCS URI (gs://bucket/path).
        client: Optional GCS client.

    Returns:
        GCS URI of uploaded model.
    """
    bucket_name, blob_name = parse_gcs_uri(gcs_path)
    return upload_blob(local_path, bucket_name, blob_name, client)


def download_model(
    gcs_path: str,
    local_path: str,
    client: Optional[storage.Client] = None,
) -> str:
    """Download model artifact from GCS.

    Args:
        gcs_path: GCS URI (gs://bucket/path).
        local_path: Local destination path.
        client: Optional GCS client.

    Returns:
        Local path of downloaded model.
    """
    bucket_name, blob_name = parse_gcs_uri(gcs_path)
    return download_blob(bucket_name, blob_name, local_path, client)


def list_blobs(
    bucket_name: str,
    prefix: str = "",
    client: Optional[storage.Client] = None,
) -> list[str]:
    """List blobs in a bucket with optional prefix.

    Args:
        bucket_name: GCS bucket name.
        prefix: Optional prefix to filter blobs.
        client: Optional GCS client.

    Returns:
        List of blob names.
    """
    if client is None:
        client = get_client()

    bucket = client.bucket(bucket_name)
    blobs = bucket.list_blobs(prefix=prefix)

    return [blob.name for blob in blobs]
