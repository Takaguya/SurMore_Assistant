"""Where original uploaded files live.

STORAGE_BACKEND=local  files on the server's disk (DATA_DIR/uploads). Fine on your machine or a host with a
                       persistent disk; on hosts with a throwaway disk (e.g. Render free) files vanish on redeploy.
STORAGE_BACKEND=s3     any S3-compatible bucket: Cloudflare R2 (10 GB free), Supabase Storage, AWS S3, MinIO.

The Markdown, embeddings and chats live in Postgres either way; this only holds the original files.
"""

import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import BinaryIO
from urllib.parse import quote

from fastapi.responses import FileResponse, RedirectResponse, Response

from .config import get_settings

COPY_CHUNK = 1024 * 1024
LINK_SECONDS = 300


class FileTooLarge(Exception):
    pass


class FileMissing(Exception):
    pass


def _copy_limited(src: BinaryIO, dst: BinaryIO, max_bytes: int) -> int:
    size = 0
    while chunk := src.read(COPY_CHUNK):
        size += len(chunk)
        if size > max_bytes:
            raise FileTooLarge
        dst.write(chunk)
    return size


def _content_disposition(filename: str) -> str:
    return f"inline; filename*=UTF-8''{quote(filename)}"


class LocalStorage:
    def __init__(self, root: Path):
        self.root = root

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise ValueError("Invalid storage key")
        return path

    def save(self, key: str, src: BinaryIO, max_bytes: int) -> int:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("wb") as out:
                return _copy_limited(src, out, max_bytes)
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    @contextmanager
    def local_copy(self, key: str) -> Iterator[Path]:
        path = self._path(key)
        if not path.exists():
            raise FileMissing
        yield path

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def delete_prefix(self, prefix: str) -> None:
        shutil.rmtree(self._path(prefix), ignore_errors=True)

    def response(self, key: str, filename: str, media_type: str) -> Response:
        path = self._path(key)
        if not path.exists():
            raise FileMissing
        return FileResponse(path, media_type=media_type, filename=filename, content_disposition_type="inline")


class S3Storage:
    def __init__(self):
        import boto3
        from botocore.config import Config

        s = get_settings()
        self.bucket = s.s3_bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=s.s3_endpoint_url or None,
            region_name=s.s3_region or None,
            aws_access_key_id=s.s3_access_key_id or None,
            aws_secret_access_key=s.s3_secret_access_key or None,
            config=Config(signature_version="s3v4", retries={"max_attempts": 3, "mode": "standard"}),
        )

    def save(self, key: str, src: BinaryIO, max_bytes: int) -> int:
        # Spool to a temp file first so the size limit is enforced before anything reaches the bucket.
        with tempfile.TemporaryFile() as tmp:
            size = _copy_limited(src, tmp, max_bytes)
            if size:
                tmp.seek(0)
                self.client.upload_fileobj(tmp, self.bucket, key)
            return size

    @contextmanager
    def local_copy(self, key: str) -> Iterator[Path]:
        from botocore.exceptions import ClientError

        fd, name = tempfile.mkstemp(suffix=Path(key).suffix)
        path = Path(name)
        try:
            with open(fd, "wb") as out:
                try:
                    self.client.download_fileobj(self.bucket, key, out)
                except ClientError as exc:
                    if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey"):
                        raise FileMissing from exc
                    raise
            yield path
        finally:
            path.unlink(missing_ok=True)

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def delete_prefix(self, prefix: str) -> None:
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix.rstrip("/") + "/"):
            keys = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
            if keys:
                self.client.delete_objects(Bucket=self.bucket, Delete={"Objects": keys, "Quiet": True})

    def response(self, key: str, filename: str, media_type: str) -> Response:
        # Send the browser straight to the bucket with a short-lived signed link instead of proxying the file.
        url = self.client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": self.bucket,
                "Key": key,
                "ResponseContentType": media_type,
                "ResponseContentDisposition": _content_disposition(filename),
            },
            ExpiresIn=LINK_SECONDS,
        )
        return RedirectResponse(url, status_code=307)


@lru_cache
def get_storage() -> LocalStorage | S3Storage:
    s = get_settings()
    if s.storage_backend == "s3":
        if not s.s3_bucket:
            raise RuntimeError("STORAGE_BACKEND=s3 needs S3_BUCKET (and S3_ENDPOINT_URL / keys) in the environment.")
        return S3Storage()
    return LocalStorage(s.upload_dir)
