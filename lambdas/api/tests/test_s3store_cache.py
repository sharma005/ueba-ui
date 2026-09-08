"""The ETag cache is keyed by bucket *and* key.

Keying by key alone happened to work while every tenant's prefix contained its
own id — but that is a naming coincidence, not something the registry enforces.
A tenant added with the same relative layout in a different bucket would have
served the first tenant's cached document, and the response would have looked
entirely normal.

The pathological case is constructed directly here rather than waited for: same
key, same ETag, two buckets.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from serving import s3store  # noqa: E402

KEY = "state/ui/snapshot/v1/meta.json"


class FakeS3:
    """Serves per-bucket bodies and lets the caller pin the ETag."""

    def __init__(self, bodies, etag='"same-etag"'):
        self.bodies = bodies
        self.etag = etag
        self.gets = 0

    def head_object(self, Bucket, Key):  # noqa: N803 - boto3's own casing
        return {"ETag": self.etag}

    def get_object(self, Bucket, Key):  # noqa: N803
        import io
        import json

        self.gets += 1
        return {"Body": io.BytesIO(json.dumps(self.bodies[Bucket]).encode())}


class CacheKeying(unittest.TestCase):
    def setUp(self):
        s3store.clear_cache()
        self.fake = FakeS3({"bucket-a": {"who": "a"}, "bucket-b": {"who": "b"}})
        self.orig = s3store.client
        s3store.client = lambda region=None: self.fake

    def tearDown(self):
        s3store.client = self.orig
        s3store.clear_cache()

    def test_same_key_same_etag_two_buckets_do_not_collide(self):
        self.assertEqual(s3store.get_json_cached("bucket-a", KEY)["who"], "a")
        self.assertEqual(s3store.get_json_cached("bucket-b", KEY)["who"], "b")
        # And re-reading each still gives its own document.
        self.assertEqual(s3store.get_json_cached("bucket-a", KEY)["who"], "a")
        self.assertEqual(s3store.get_json_cached("bucket-b", KEY)["who"], "b")

    def test_an_unchanged_etag_is_served_from_memory(self):
        s3store.get_json_cached("bucket-a", KEY)
        s3store.get_json_cached("bucket-a", KEY)
        self.assertEqual(self.fake.gets, 1, "the warm read went back to S3")

    def test_a_moved_etag_is_refetched(self):
        s3store.get_json_cached("bucket-a", KEY)
        self.fake.etag = '"rebuilt"'
        self.fake.bodies["bucket-a"] = {"who": "a2"}
        self.assertEqual(s3store.get_json_cached("bucket-a", KEY)["who"], "a2")

    def test_clear_cache_empties_every_bucket(self):
        s3store.get_json_cached("bucket-a", KEY)
        s3store.get_json_cached("bucket-b", KEY)
        s3store.clear_cache()
        self.assertEqual(s3store._cache, {})


class BucketIsRequired(unittest.TestCase):
    """A caller that forgets the tenant must fail loudly, not read the default."""

    def test_every_entry_point_demands_a_bucket(self):
        import inspect

        for name in ("get_json", "get_json_cached", "put_json", "list_keys",
                     "get_many_json", "put_many_json", "download"):
            with self.subTest(fn=name):
                params = list(inspect.signature(getattr(s3store, name)).parameters)
                self.assertEqual(params[0], "bucket",
                                 f"{name} must take bucket first")
                self.assertIs(
                    inspect.signature(getattr(s3store, name)).parameters["bucket"].default,
                    inspect.Parameter.empty,
                    f"{name}'s bucket must have no default — a default is how a "
                    f"forgotten tenant silently becomes the wrong one")


if __name__ == "__main__":
    unittest.main()
