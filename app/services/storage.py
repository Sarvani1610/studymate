"""File storage for uploaded course material.

Local disk for development and tests, S3 in production. Keys look like
courses/<course_id>/<sha256>.<ext> so re-uploading the same file is idempotent.
"""
import logging
import os

from flask import current_app

log = logging.getLogger(__name__)


class LocalStorage:
    def __init__(self, root):
        self.root = root
        os.makedirs(root, exist_ok=True)

    def _path(self, key):
        path = os.path.abspath(os.path.join(self.root, key))
        if not path.startswith(os.path.abspath(self.root)):
            raise ValueError("storage key escapes the upload root")
        return path

    def put(self, key, data, content_type=None):
        path = self._path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".part"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
        return key

    def get(self, key):
        with open(self._path(key), "rb") as fh:
            return fh.read()

    def delete(self, key):
        try:
            os.remove(self._path(key))
        except FileNotFoundError:
            pass

    def exists(self, key):
        return os.path.exists(self._path(key))

    def url(self, key, expires=3600):
        return None


class S3Storage:
    def __init__(self, bucket, prefix, region):
        import boto3
        from botocore.config import Config as BotoConfig

        self.bucket = bucket
        self.prefix = prefix
        self.client = boto3.client(
            "s3", region_name=region, config=BotoConfig(retries={"max_attempts": 5, "mode": "adaptive"})
        )

    def _key(self, key):
        return f"{self.prefix}{key}"

    def put(self, key, data, content_type=None):
        extra = {"ServerSideEncryption": "AES256"}
        if content_type:
            extra["ContentType"] = content_type
        self.client.put_object(Bucket=self.bucket, Key=self._key(key), Body=data, **extra)
        return key

    def get(self, key):
        obj = self.client.get_object(Bucket=self.bucket, Key=self._key(key))
        return obj["Body"].read()

    def delete(self, key):
        self.client.delete_object(Bucket=self.bucket, Key=self._key(key))

    def exists(self, key):
        from botocore.exceptions import ClientError

        try:
            self.client.head_object(Bucket=self.bucket, Key=self._key(key))
            return True
        except ClientError:
            return False

    def url(self, key, expires=3600):
        return self.client.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": self._key(key)}, ExpiresIn=expires
        )


def get_storage():
    app = current_app
    storage = app.extensions.get("storage")
    if storage is None:
        if app.config["STORAGE_BACKEND"] == "s3":
            storage = S3Storage(app.config["S3_BUCKET"], app.config["S3_PREFIX"], app.config["AWS_REGION"])
        else:
            storage = LocalStorage(app.config["UPLOAD_DIR"])
        app.extensions["storage"] = storage
    return storage
