"""Small helpers for the OpenAQ public archive on AWS Open Data (anonymous HTTPS, no account, no key)."""
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

BUCKET_URL = "https://openaq-data-archive.s3.amazonaws.com/"
PREFIX = "records/csv.gz/"
_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"


def fetch(url, headers=None, tries=5):
    """GET a URL with retries and backoff; returns bytes."""
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=headers or {})
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except Exception:
            if attempt == tries - 1:
                raise
            time.sleep(2 ** attempt)


def list_objects(prefix, delimiter=None, max_keys=None):
    """Yield (key, size, etag) for objects under prefix, or common prefixes when a delimiter is given."""
    token = None
    while True:
        q = {"list-type": "2", "prefix": prefix, "max-keys": str(max_keys or 1000)}
        if delimiter:
            q["delimiter"] = delimiter
        if token:
            q["continuation-token"] = token
        root = ET.fromstring(fetch(BUCKET_URL + "?" + urllib.parse.urlencode(q)))
        if delimiter:
            for p in root.iter(_NS + "CommonPrefixes"):
                yield p.find(_NS + "Prefix").text
        for c in root.iter(_NS + "Contents"):
            yield c.find(_NS + "Key").text, int(c.find(_NS + "Size").text), c.find(_NS + "ETag").text.strip('"')
        nxt = root.find(_NS + "NextContinuationToken")
        if nxt is None or max_keys:
            return
        token = nxt.text
