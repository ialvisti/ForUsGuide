"""
Cloud Storage article store.

Read/write KB article JSON files from a GCS bucket. Used by the data
pipeline (ingestion scripts) when running in production instead of
reading from the local filesystem.
"""

import json
import logging
from typing import Optional

from google.cloud import storage

logger = logging.getLogger(__name__)


class ArticleStore:
    """Read/write KB articles from Cloud Storage."""

    def __init__(self, bucket_name: str, project: Optional[str] = None):
        self.client = storage.Client(project=project)
        self.bucket = self.client.bucket(bucket_name)

    def get_article(self, article_id: str) -> dict:
        """Download and parse a single article JSON."""
        blob = self.bucket.blob(f"articles/{article_id}.json")
        content = blob.download_as_text()
        return json.loads(content)

    def list_articles(self, prefix: str = "articles/") -> list[str]:
        """List all article IDs in the bucket."""
        blobs = self.bucket.list_blobs(prefix=prefix)
        return [
            b.name.split("/")[-1].replace(".json", "")
            for b in blobs
            if b.name.endswith(".json")
        ]

    def upload_article(self, article_id: str, data: dict) -> None:
        """Upload an article JSON to the bucket."""
        blob = self.bucket.blob(f"articles/{article_id}.json")
        blob.upload_from_string(
            json.dumps(data, indent=2, ensure_ascii=False),
            content_type="application/json",
        )
        logger.info(f"Uploaded article {article_id} to gs://{self.bucket.name}/articles/{article_id}.json")

    def delete_article(self, article_id: str) -> None:
        """Delete an article JSON from the bucket."""
        blob = self.bucket.blob(f"articles/{article_id}.json")
        blob.delete()
        logger.info(f"Deleted article {article_id} from gs://{self.bucket.name}")

    def article_exists(self, article_id: str) -> bool:
        """Check whether an article exists in the bucket."""
        blob = self.bucket.blob(f"articles/{article_id}.json")
        return blob.exists()
