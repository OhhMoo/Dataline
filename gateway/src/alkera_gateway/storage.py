import base64
import binascii

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from .records import ToolError


class ObjectStorage:
    def __init__(self, config):
        self.config = config
        self._client = None

    def client(self, bucket):
        if not self.config.s3_endpoint:
            raise ToolError("storage_unconfigured", "This world has no object storage")
        if bucket not in self.config.s3_buckets:
            raise ToolError("bucket_denied", "Bucket is outside this world's storage surface")
        if self._client is None:
            self._client = boto3.client(
                "s3",
                endpoint_url=self.config.s3_endpoint,
                region_name="us-east-1",
                aws_access_key_id="episode",
                aws_secret_access_key="episode",
                config=Config(
                    s3={"addressing_style": "path"},
                    connect_timeout=3,
                    read_timeout=10,
                    retries={"max_attempts": 0},
                ),
            )
        return self._client

    def call(self, method, bucket, observation, **args):
        target = f"s3://{bucket}/{args.get('Key', args.get('Prefix', ''))}"
        observation.targets = [target]
        if method in {"put_object", "delete_object"}:
            observation.op_class = "destructive" if method == "delete_object" else "write"
            observation.flags = ["delete"] if method == "delete_object" else ["possible_overwrite"]
        else:
            observation.read_targets = [target]
        try:
            return getattr(self.client(bucket), method)(Bucket=bucket, **args)
        except ClientError as exc:
            raise ToolError("storage_error", exc.response["Error"]["Code"]) from None
        except BotoCoreError:
            raise ToolError("storage_unavailable", "Object storage request failed") from None

    def list(self, bucket, prefix, continuation_token, limit, observation):
        args = {"Prefix": prefix, "MaxKeys": limit}
        if continuation_token:
            args["ContinuationToken"] = continuation_token
        response = self.call("list_objects_v2", bucket, observation, **args)
        return {
            "objects": [
                {"key": item["Key"], "size": item["Size"], "etag": item["ETag"].strip('"')}
                for item in response.get("Contents", [])
            ],
            "continuation_token": response.get("NextContinuationToken"),
        }

    def get(self, bucket, key, observation):
        response = self.call("get_object", bucket, observation, Key=key)
        with response["Body"] as stream:
            body = stream.read(self.config.max_payload_bytes + 1)
        if len(body) > self.config.max_payload_bytes:
            raise ToolError("object_too_large", "Object exceeds the payload limit")
        return {
            "key": key,
            "body_base64": base64.b64encode(body).decode(),
            "etag": response["ETag"].strip('"'),
            "metadata": response.get("Metadata", {}),
        }

    def put(self, bucket, key, body_base64, metadata, observation):
        try:
            body = base64.b64decode(body_base64, validate=True)
        except (ValueError, binascii.Error):
            raise ToolError("invalid_base64", "Object body must be valid base64") from None
        response = self.call(
            "put_object", bucket, observation, Key=key, Body=body, Metadata=metadata
        )
        return {"key": key, "bytes_written": len(body), "etag": response["ETag"].strip('"')}

    def delete(self, bucket, key, observation):
        self.call("delete_object", bucket, observation, Key=key)
        return {"key": key, "deleted": True}
