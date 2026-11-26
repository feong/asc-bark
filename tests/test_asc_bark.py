import gzip
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

import jwt

import asc_bark

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
API = "https://asc.test"
FILES = "https://files.test"
APP_ID = "1000000001"
BARK_KEY = "bark-device-key-0001"
DOWNLOADS = (FIXTURES / "downloads_standard.tsv").read_text(encoding="utf-8")
DISCOVERY = (FIXTURES / "discovery_standard.tsv").read_text(encoding="utf-8")

DL_HEADER = (
    "Date\tApp Name\tApp Apple Identifier\tDownload Type\tApp Version\tDevice\t"
    "Platform Version\tSource Type\tPage Type\tPre-Order\tTerritory\tCounts"
)
DISC_HEADER = (
    "Date\tApp Name\tApp Apple Identifier\tEvent\tPage Type\tSource Type\t"
    "Engagement Type\tDevice\tPlatform Version\tTerritory\tCounts\tUnique Counts"
)


def gzip_text(text: str) -> bytes:
    if not text.endswith("\n"):
        text += "\n"
    return gzip.compress(text.encode("utf-8"))


def split_tsv(text: str) -> tuple[str, str]:
    lines = [line for line in text.splitlines() if line.strip()]
    header, rows = lines[0], lines[1:]
    mid = max(1, len(rows) // 2)

    def pack(chunk: list[str]) -> str:
        return header + "\n" + "\n".join(chunk) + "\n"

    return pack(rows[:mid]), pack(rows[mid:])


def dl_row(day: str, kind: str, count: int, *, version: str = "1.0", app: str = APP_ID, name: str = "Sample") -> str:
    return (
        f"{day}\t{name}\t{app}\t{kind}\t{version}\tiPhone\t17.4\t"
        f"App Store search\tProduct page\tfalse\tUS\t{count}"
    )


def disc_row(day: str, event: str, page: str, count: int, unique: int = 9) -> str:
    return (
        f"{day}\tSample\t{APP_ID}\t{event}\t{page}\tApp Store search\t\t"
        f"iPhone\t17.4\tUS\t{count}\t{unique}"
    )


def tsv(header: str, rows: list[str]) -> str:
    return header + "\n" + "\n".join(rows) + "\n"


def shape_checksum(text: str, predicate, *, discovery: bool = False) -> str:
    base = text if text.endswith("\n") else text + "\n"
    for index in range(400):
        if discovery:
            row = (
                f"2024-03-30\tOther\t1000000002\tImpression\tNo page\tApp Store search\t\t"
                f"iPhone\t17.4\tUS\t1\t{index}\n"
            )
        else:
            row = dl_row(
                "2024-03-30",
                "First-time Download",
                1,
                version=f"pad{index:03d}",
                app="1000000002",
                name="Other",
            ) + "\n"
        candidate = base + row
        digest = hashlib.md5(gzip_text(candidate)).hexdigest()
        if predicate(digest):
            return candidate
    raise AssertionError("could not shape checksum")


class Response:
    def __init__(self, status: int, payload=None, content: bytes | None = None, headers=None):
        self.status_code = status
        self._payload = payload
        if content is None:
            content = json.dumps(payload).encode("utf-8") if payload is not None else b""
        self.content = content
        self.headers = headers or {}
        self.text = self.content.decode("utf-8", "replace")

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def accept_bark(handler, posts=None):
    def wrapped(method, url, params, body, headers, timeout):
        if method == "POST" and str(url).startswith("https://bark.test/"):
            if posts is not None:
                posts.append(body)
            return Response(200, {"code": 200, "message": "success"})
        return handler(method, url, params, body, headers, timeout)

    return wrapped


class Session:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        headers = headers or {}
        self.calls.append(("GET", url, params, None, headers, timeout))
        result = self.handler("GET", url, params, None, headers, timeout)
        if isinstance(result, Exception):
            raise result
        return result

    def post(self, url, json=None, headers=None, timeout=None, data=None):
        headers = headers or {}
        self.calls.append(("POST", url, None, json, headers, timeout))
        result = self.handler("POST", url, None, json, headers, timeout)
        if isinstance(result, Exception):
            raise result
        return result


def page(data, nxt=None):
    body = {"data": data, "links": {}, "meta": {"paging": {"total": len(data), "limit": 200}}}
    if nxt:
        body["links"]["next"] = nxt
    return body


def resource(kind: str, rid: str, attributes: dict) -> dict:
    return {"type": kind, "id": rid, "attributes": attributes}


def recording_sleeper():
    delays = []

    def sleep(seconds):
        if seconds > asc_bark.MAX_BACKOFF_SECONDS:
            raise AssertionError(f"backoff {seconds} exceeds {asc_bark.MAX_BACKOFF_SECONDS}")
        delays.append(seconds)

    return delays, sleep


def assert_bounded_timeouts(test: unittest.TestCase, session: Session) -> None:
    test.assertTrue(session.calls)
    for method, url, _params, _body, _headers, timeout in session.calls:
        test.assertIsInstance(timeout, tuple, url)
        test.assertEqual(len(timeout), 2, url)
        test.assertGreater(timeout[0], 0)
        test.assertLessEqual(timeout[0], 30)
        test.assertGreater(timeout[1], 0)
        test.assertLessEqual(timeout[1], 180)


class ReadyCatalog:
    """Synthetic Analytics Reports API with the documented fixtures."""

    def __init__(self, downloads_text: str = DOWNLOADS, discovery_text: str = DISCOVERY):
        part_a, part_b = split_tsv(discovery_text)
        self.blobs = {
            "dl-daily": gzip_text(downloads_text),
            "disc-a": gzip_text(part_a),
            "disc-b": gzip_text(part_b),
            "dl-weekly": gzip_text(
                tsv(DL_HEADER, [
                    dl_row("2024-04-15", "First-time Download", 99999),
                    dl_row("2024-04-15", "Redownload", 1),
                ])
            ),
            "disc-weekly": gzip_text(
                tsv(DISC_HEADER, [
                    disc_row("2024-04-15", "Impression", "No page", 99999, 1),
                    disc_row("2024-04-15", "Page view", "Product page", 99999, 1),
                ])
            ),
            "dl-detail": gzip_text(tsv(DL_HEADER, [dl_row("2024-03-30", "First-time Download", 1000)])),
            "disc-detail": gzip_text(tsv(DISC_HEADER, [disc_row("2024-03-30", "Impression", "No page", 1000, 1)])),
        }
        self.bad = {}
        self.posts = []
        self.file_error = False
        self.no_requests = False
        self.no_reports = False
        self.only_detailed = False
        self.no_instances = False
        self.pending_newest = False
        self.bark_codes = None
        self._bark_index = 0

    def handler(self, method, url, params, body, headers, timeout):
        if method == "POST":
            if url.startswith("https://bark.test/"):
                self.posts.append(body)
                code = 200
                if self.bark_codes:
                    code = self.bark_codes[min(self._bark_index, len(self.bark_codes) - 1)]
                    self._bark_index += 1
                status = 200 if code != 503 else 503
                return Response(status, {"code": code, "message": "success" if code == 200 else "no"})
            raise AssertionError(f"unexpected POST {url}")
        parsed = urlparse(url)
        if parsed.netloc == "files.test":
            if self.file_error:
                raise requests.ConnectionError(url)
            seg_id = Path(parsed.path).name.replace(".gz", "")
            if self.bad.get(seg_id, 0):
                self.bad[seg_id] -= 1
                return Response(200, content=b"not-the-report")
            if seg_id not in self.blobs:
                raise AssertionError(f"unknown segment {seg_id}")
            return Response(200, content=self.blobs[seg_id])
        path = parsed.path.rstrip("/")
        parts = path.strip("/").split("/")
        if path.endswith("/analyticsReportRequests") and "apps" in parts:
            if self.no_requests:
                return Response(200, page([]))
            return Response(200, page([
                resource("analyticsReportRequests", "req-ongoing", {
                    "accessType": "ONGOING",
                    "stoppedDueToInactivity": False,
                })
            ]))
        if path.endswith("/reports"):
            return self._reports(url, params)
        if path.endswith("/instances"):
            return Response(200, page(self._instances(parts[-2])))
        if path.endswith("/segments"):
            return Response(200, page(self._segments(parts[-2])))
        raise AssertionError(f"unexpected GET {url}")

    def _reports(self, url, params):
        category = None
        if params:
            category = params.get("filter[category]")
        if category is None:
            category = (parse_qs(urlparse(url).query).get("category") or [None])[0]
        if self.no_reports:
            return Response(200, page([]))
        if category == "COMMERCE":
            data = []
            if not self.only_detailed:
                data.append(resource("analyticsReports", "dl-std", {
                    "name": "App Store Downloads Standard",
                    "category": "COMMERCE",
                }))
            data.append(resource("analyticsReports", "dl-detail", {
                "name": "App Store Downloads Detailed",
                "category": "COMMERCE",
            }))
            return Response(200, page(data))
        if category == "APP_STORE_ENGAGEMENT":
            detailed = resource("analyticsReports", "disc-detail", {
                "name": "App Store Discovery and Engagement Detailed",
                "category": "APP_STORE_ENGAGEMENT",
            })
            if "cursor=2" not in url:
                nxt = f"{API}/v1/analyticsReportRequests/req-ongoing/reports?cursor=2&category=APP_STORE_ENGAGEMENT"
                return Response(200, page([detailed], nxt=nxt))
            if self.only_detailed:
                return Response(200, page([]))
            return Response(200, page([
                resource("analyticsReports", "disc-std", {
                    "name": "App Store Discovery and Engagement Standard",
                    "category": "APP_STORE_ENGAGEMENT",
                })
            ]))
        raise AssertionError(f"unexpected report category {category}")

    def _instances(self, report_id: str):
        if self.no_instances or self.only_detailed:
            return []
        if report_id == "dl-std":
            rows = [
                ("dl-daily", "DAILY", "2024-04-01"),
                ("dl-weekly", "WEEKLY", "2024-04-20"),
            ]
            if self.pending_newest:
                rows.append(("dl-pending", "DAILY", "2024-04-09"))
            return [
                resource("analyticsReportInstances", rid, {"granularity": grain, "processingDate": processed})
                for rid, grain, processed in rows
            ]
        if report_id == "disc-std":
            rows = [
                ("disc-daily", "DAILY", "2024-04-02"),
                ("disc-weekly", "WEEKLY", "2024-04-20"),
            ]
            if self.pending_newest:
                rows.append(("disc-pending", "DAILY", "2024-04-09"))
            return [
                resource("analyticsReportInstances", rid, {"granularity": grain, "processingDate": processed})
                for rid, grain, processed in rows
            ]
        if report_id in {"dl-detail", "disc-detail"}:
            return [resource("analyticsReportInstances", report_id + "-daily", {
                "granularity": "DAILY",
                "processingDate": "2024-04-08",
            })]
        raise AssertionError(f"unexpected report instances {report_id}")

    def _segments(self, instance_id: str):
        mapping = {
            "dl-daily": ["dl-daily"],
            "disc-daily": ["disc-a", "disc-b"],
            "dl-weekly": ["dl-weekly"],
            "disc-weekly": ["disc-weekly"],
            "dl-detail-daily": ["dl-detail"],
            "disc-detail-daily": ["disc-detail"],
        }
        if instance_id in {"dl-pending", "disc-pending"}:
            return []
        if instance_id not in mapping:
            raise AssertionError(f"unexpected segments {instance_id}")
        return [
            resource("analyticsReportSegments", seg_id, {
                "checksum": hashlib.md5(self.blobs[seg_id]).hexdigest(),
                "sizeInBytes": len(self.blobs[seg_id]),
                "url": f"{FILES}/{seg_id}.gz?X-Amz-Signature=deadbeefdeadbeef",
            })
            for seg_id in mapping[instance_id]
        ]


class OverlapCatalog:
    def __init__(self):
        ongoing = tsv(DL_HEADER, [
            dl_row("2024-03-30", "First-time Download", 5),
            dl_row("2024-03-30", "First-time Download", 2),
            dl_row("2024-03-30", "Redownload", 1),
            dl_row("2024-03-30", "Manual update", 100),
            dl_row("2024-03-30", "Auto-update", 40),
            dl_row("2024-03-30", "Restore", 6),
        ])
        snapshot = tsv(DL_HEADER, [
            dl_row("2024-03-30", "First-time Download", 50),
            dl_row("2024-03-30", "Redownload", 9),
        ])
        self.low_hash_holder = {}
        ongoing = shape_checksum(ongoing, lambda _digest: True)
        self.ongoing_hash = hashlib.md5(gzip_text(ongoing)).hexdigest()
        snapshot = shape_checksum(snapshot, lambda digest: digest > self.ongoing_hash)
        self.snapshot_hash = hashlib.md5(gzip_text(snapshot)).hexdigest()
        if not self.snapshot_hash > self.ongoing_hash:
            raise AssertionError("checksum trap was not armed")
        older = tsv(DL_HEADER, [
            dl_row("2024-03-30", "First-time Download", 10),
            dl_row("2024-03-30", "Redownload", 10),
            dl_row("2024-02-01", "First-time Download", 4),
            dl_row("2024-02-01", "Redownload", 1),
        ])
        discovery = tsv(DISC_HEADER, [
            disc_row("2024-03-30", "Impression", "No page", 4, 100),
            disc_row("2024-03-30", "Page view", "Product page", 2, 80),
            disc_row("2024-03-30", "Page view", "Store sheet", 30, 20),
        ])
        discovery_snapshot = tsv(DISC_HEADER, [
            disc_row("2024-03-30", "Impression", "No page", 80, 1),
            disc_row("2024-03-30", "Page view", "Product page", 40, 1),
        ])
        discovery = shape_checksum(discovery, lambda _digest: True, discovery=True)
        self.discovery_hash = hashlib.md5(gzip_text(discovery)).hexdigest()
        discovery_snapshot = shape_checksum(
            discovery_snapshot,
            lambda digest: digest > self.discovery_hash,
            discovery=True,
        )
        self.blobs = {
            "ongo": gzip_text(ongoing),
            "zzzz": gzip_text(snapshot),
            "old": gzip_text(older),
            "disc-ongo": gzip_text(discovery),
            "disc-zzzz": gzip_text(discovery_snapshot),
        }
        self.posts = []

    def handler(self, method, url, params, body, headers, timeout):
        if method == "POST":
            if url.startswith("https://bark.test/"):
                self.posts.append(body)
                return Response(200, {"code": 200, "message": "success"})
            raise AssertionError(f"unexpected POST {url}")
        parsed = urlparse(url)
        if parsed.netloc == "files.test":
            seg_id = Path(parsed.path).name.replace(".gz", "")
            return Response(200, content=self.blobs[seg_id])
        path = parsed.path.rstrip("/")
        parts = path.strip("/").split("/")
        if path.endswith("/analyticsReportRequests"):
            return Response(200, page([
                resource("analyticsReportRequests", "req-ongoing", {
                    "accessType": "ONGOING",
                    "stoppedDueToInactivity": False,
                }),
                resource("analyticsReportRequests", "req-snapshot", {
                    "accessType": "ONE_TIME_SNAPSHOT",
                    "stoppedDueToInactivity": False,
                }),
            ]))
        if path.endswith("/reports"):
            category = (params or {}).get("filter[category]")
            if "req-ongoing" in path and category == "COMMERCE":
                data = [resource("analyticsReports", "dl-ongo", {
                    "name": "App Store Downloads Standard",
                    "category": "COMMERCE",
                })]
            elif "req-snapshot" in path and category == "COMMERCE":
                data = [resource("analyticsReports", "dl-snap", {
                    "name": "App Store Downloads Standard",
                    "category": "COMMERCE",
                })]
            elif "req-ongoing" in path and category == "APP_STORE_ENGAGEMENT":
                data = [resource("analyticsReports", "disc-report-ongo", {
                    "name": "App Store Discovery and Engagement Standard",
                    "category": "APP_STORE_ENGAGEMENT",
                })]
            elif "req-snapshot" in path and category == "APP_STORE_ENGAGEMENT":
                data = [resource("analyticsReports", "disc-report-snap", {
                    "name": "App Store Discovery and Engagement Standard",
                    "category": "APP_STORE_ENGAGEMENT",
                })]
            else:
                raise AssertionError(f"unexpected reports {url} {params}")
            return Response(200, page(data))
        if path.endswith("/instances"):
            report_id = parts[-2]
            mapping = {
                "dl-ongo": [("ongo", "2024-04-03"), ("old", "2024-04-01")],
                "dl-snap": [("zzzz", "2024-04-03")],
                "disc-report-ongo": [("disc-ongo", "2024-04-02")],
                "disc-report-snap": [("disc-zzzz", "2024-04-02")],
            }
            return Response(200, page([
                resource("analyticsReportInstances", rid, {
                    "granularity": "DAILY",
                    "processingDate": processed,
                })
                for rid, processed in mapping[report_id]
            ]))
        if path.endswith("/segments"):
            seg_id = parts[-2]
            blob = self.blobs[seg_id]
            return Response(200, page([
                resource("analyticsReportSegments", seg_id, {
                    "checksum": hashlib.md5(blob).hexdigest(),
                    "sizeInBytes": len(blob),
                    "url": f"{FILES}/{seg_id}.gz?X-Amz-Signature=deadbeefdeadbeef",
                })
            ]))
        raise AssertionError(f"unexpected GET {url}")


class AscBarkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        key = ec.generate_private_key(ec.SECP256R1())
        cls.pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("ascii")
        cls.public = key.public_key()
        cls._root = Path(tempfile.mkdtemp(prefix="asc-bark-key-"))
        cls.key_path = cls._root / "AuthKey.p8"
        cls.key_path.write_text(cls.pem, encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._root, ignore_errors=True)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name) / "data"
        self.data.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def config(self, *, bark: bool = True) -> asc_bark.Config:
        return asc_bark.Config(
            issuer_id="issuer-example-0001",
            key_id="KEYEXAMPLE1",
            app_id=APP_ID,
            private_key=self.pem,
            key_path=str(self.key_path),
            bark_key=BARK_KEY if bark else None,
            bark_server="https://bark.test",
            data_dir=self.data,
            api_base=API,
        )

    def env(self, **extra) -> dict[str, str]:
        values = {
            "ASC_ISSUER_ID": "issuer-example-0001",
            "ASC_KEY_ID": "KEYEXAMPLE1",
            "ASC_APP_ID": APP_ID,
            "ASC_PRIVATE_KEY_PATH": str(self.key_path),
            "BARK_KEY": BARK_KEY,
            "BARK_SERVER": "https://bark.test",
            "ASC_DATA_DIR": str(self.data),
        }
        values.update(extra)
        return values

    def test_jwt_es256_claims(self):
        now = int(time.time())
        token = asc_bark.make_token("issuer-example-0001", "KEYEXAMPLE1", self.pem, now=now)
        header = jwt.get_unverified_header(token)
        self.assertEqual(header["alg"], "ES256")
        self.assertEqual(header["kid"], "KEYEXAMPLE1")
        self.assertEqual(header["typ"], "JWT")
        claims = jwt.decode(token, self.public, algorithms=["ES256"], audience="appstoreconnect-v1")
        self.assertEqual(claims["iss"], "issuer-example-0001")
        self.assertEqual(claims["aud"], "appstoreconnect-v1")
        self.assertEqual(claims["iat"], now)
        self.assertEqual(claims["exp"] - claims["iat"], 15 * 60)
        self.assertLessEqual(claims["exp"] - claims["iat"], 20 * 60)

    def test_missing_and_bad_config(self):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            self.assertEqual(asc_bark.main(["run"], {}), 2)
            bad = self.data / "bad.p8"
            bad.write_text("-----BEGIN PRIVATE KEY-----\nnot-a-key\n-----END PRIVATE KEY-----\n", encoding="utf-8")
            code = asc_bark.main(["run"], self.env(ASC_PRIVATE_KEY_PATH=str(bad)))
            self.assertEqual(code, 2)
            self.assertEqual(
                asc_bark.main(["run"], self.env(BARK_SERVER="https://bark.test/push")),
                2,
            )
            self.assertEqual(
                asc_bark.main(["run"], self.env(ASC_APP_ID="not-numeric")),
                2,
            )
        text = output.getvalue()
        self.assertIn("缺少 BARK_KEY", text)
        self.assertNotIn("not-a-key", text)

    def test_documented_schema_segments_and_unique_counts(self):
        self.assertIn("\t", DOWNLOADS.splitlines()[0])
        self.assertIn("Unique Counts", DISCOVERY.splitlines()[0])
        downloads = asc_bark.aggregate_segments("downloads", [gzip_text(DOWNLOADS)], APP_ID)
        self.assertEqual(downloads["2024-03-30"]["first_time_downloads"], 12)
        self.assertEqual(downloads["2024-03-30"]["redownloads"], 3)
        self.assertEqual(downloads["2024-03-31"]["first_time_downloads"], 1)
        self.assertIsNone(downloads["2024-03-31"]["redownloads"])
        self.assertNotIn("2024-03-29", downloads)

        reordered = (
            "Unique Counts\tCounts\tPage Type\tEvent\tTerritory\tApp Apple Identifier\tDate\tApp Name\n"
            "100\t20\tNo page\timpression\tUS\t1000000001\t2024-03-30\tSample\n"
            "100\t5\tPRODUCT PAGE\tPAGE VIEW\tCN\t1000000001\t2024-03-30\tSample\n"
            "1\t7\tStore sheet\tPage view\tJP\t1000000001\t2024-03-30\tSample\n"
        )
        left, right = split_tsv(DISCOVERY)
        discovery = asc_bark.aggregate_segments(
            "discovery",
            [gzip_text(left), gzip_text(right), gzip_text(reordered)],
            APP_ID,
        )
        self.assertEqual(discovery["2024-03-30"]["impressions"], 45)
        self.assertEqual(discovery["2024-03-30"]["product_page_views"], 14)
        self.assertEqual(discovery["2024-03-29"]["impressions"], 3)
        self.assertIsNone(discovery["2024-03-29"]["product_page_views"])

        explicit = asc_bark.aggregate_rows("downloads", [
            {
                "date": "2024-03-30",
                "app_id": APP_ID,
                "app_name": "Sample",
                "download_type": "First-time Download",
                "counts": "0",
            }
        ], APP_ID)
        self.assertEqual(explicit["2024-03-30"]["first_time_downloads"], 0)
        self.assertIsNone(explicit["2024-03-30"]["redownloads"])

    def test_overlap_corrections_latest_day_and_wording(self):
        instances = [
            {
                "id": "ongo",
                "processing_date": "2024-04-03",
                "access_type": "ONGOING",
                "checksum": "0aaa",
                "dates": {"2024-03-30": {"first_time_downloads": 7, "redownloads": 1}},
            },
            {
                "id": "zzzz",
                "processing_date": "2024-04-03",
                "access_type": "ONE_TIME_SNAPSHOT",
                "checksum": "ffff",
                "dates": {"2024-03-30": {"first_time_downloads": 50, "redownloads": 9}},
            },
            {
                "id": "old",
                "processing_date": "2024-04-01",
                "access_type": "ONGOING",
                "checksum": "ffff",
                "dates": {
                    "2024-03-30": {"first_time_downloads": 10, "redownloads": 10},
                    "2024-02-01": {"first_time_downloads": 4, "redownloads": 1},
                },
            },
            {
                "id": "snap-new",
                "processing_date": "2024-04-05",
                "access_type": "ONE_TIME_SNAPSHOT",
                "checksum": "0000",
                "dates": {"2024-03-30": {"first_time_downloads": 11, "redownloads": 2}},
            },
        ]
        winners = asc_bark.select_dates(instances)
        self.assertEqual(winners["2024-03-30"]["instance_id"], "snap-new")
        self.assertEqual(winners["2024-03-30"]["metrics"]["first_time_downloads"], 11)
        self.assertEqual(winners["2024-02-01"]["instance_id"], "old")
        self.assertEqual(winners["2024-02-01"]["metrics"]["first_time_downloads"], 4)
        self.assertIsNone(asc_bark.latest_common_day({"2024-03-31": {}}, {"2024-03-30": {}}))
        self.assertEqual(
            asc_bark.latest_common_day({"2024-03-30": {}, "2024-03-31": {}}, {"2024-03-30": {}}),
            "2024-03-30",
        )
        summary = asc_bark.build_summary(
            APP_ID,
            "2024-03-30",
            {
                "processing_date": "2024-04-05",
                "instance_id": "snap-new",
                "metrics": {"first_time_downloads": 0, "redownloads": None, "app_name": "Sample"},
            },
            {
                "processing_date": "2024-04-02",
                "instance_id": "disc",
                "metrics": {"impressions": 4, "product_page_views": None, "app_name": "Sample"},
            },
        )
        self.assertIsNone(summary["conversion"])
        body = asc_bark.format_summary(summary)
        self.assertIn("事件日 2024-03-30", body)
        self.assertIn("处理日 下载 2024-04-05 / 发现 2024-04-02", body)
        self.assertIn("首次下载 0（事件次数）", body)
        self.assertIn("重新下载 不可用", body)
        self.assertIn("下载合计 不可用", body)
        self.assertIn("展示次数 4（事件次数，不含浏览）", body)
        self.assertIn("产品页浏览 不可用", body)
        self.assertIn("转化率 不可用", body)
        self.assertNotIn("90", body)
        self.assertNotIn("%", body)
        self.assertNotIn("完整", body)

    def test_run_schema_pagination_dedup_and_correction(self):
        catalog = ReadyCatalog()
        catalog.bad["dl-daily"] = 1
        delays, sleep = recording_sleeper()
        session = Session(catalog.handler)
        result = asc_bark.cmd_run(self.config(), session, dry_run=False, sleeper=sleep)
        self.assertEqual(result.code, 0, result.message)
        self.assertIn("已推送", result.message)
        self.assertIn("事件日 2024-03-30", result.message)
        self.assertIn("处理日 下载 2024-04-01 / 发现 2024-04-02", result.message)
        self.assertIn("首次下载 12（事件次数）", result.message)
        self.assertIn("重新下载 3（事件次数）", result.message)
        self.assertIn("下载合计 15（事件次数）", result.message)
        self.assertIn("展示次数 25（事件次数，不含浏览）", result.message)
        self.assertIn("产品页浏览 9（事件次数）", result.message)
        self.assertIn("转化率 不可用", result.message)
        self.assertNotIn("2024-04-15", result.message)
        self.assertNotIn("99999", result.message)
        self.assertNotIn("deadbeef", result.message)
        self.assertNotIn("eyJ", result.message)
        self.assertEqual(len(catalog.posts), 1)
        self.assertEqual(catalog.posts[0]["title"], "Sample 2024-03-30")
        self.assertEqual(catalog.posts[0]["group"], "asc-bark")
        self.assertNotIn("device_key", result.message)
        self.assertTrue(delays)
        self.assertLessEqual(max(delays), 8)
        assert_bounded_timeouts(self, session)
        self._assert_downloads_are_anonymous(session)
        state_text = (self.data / "state.json").read_text(encoding="utf-8")
        self.assertNotIn("deadbeef", state_text)
        self.assertNotIn("X-Amz", state_text)
        self.assertNotIn("PRIVATE KEY", state_text)
        self.assertNotIn(BARK_KEY, state_text)
        self.assertNotIn("_dirty", state_text)
        self.assertEqual((self.data / "state.json").stat().st_mode & 0o777, 0o600)
        state = json.loads(state_text)
        self.assertIn("2024-03-30", state["pushed"])
        file_gets = self._file_gets(session)
        self.assertEqual(file_gets.count("dl-daily"), 2)
        self.assertNotIn("dl-weekly", file_gets)
        self.assertNotIn("dl-detail", file_gets)
        self.assertNotIn("disc-weekly", file_gets)
        self.assertTrue(any(params and params.get("filter[granularity]") == "DAILY" for _m, _u, params, _b, _h, _t in session.calls))
        self.assertTrue(any(params is None and "cursor=2" in url for _m, url, params, _b, _h, _t in session.calls))
        self.assertFalse(any(method == "POST" and "analyticsReportRequests" in url for method, url, *_rest in session.calls))

        second = Session(catalog.handler)
        again = asc_bark.cmd_run(self.config(), second, dry_run=False, sleeper=sleep)
        self.assertIn("没有变化", again.message)
        self.assertEqual(len(catalog.posts), 1)
        self.assertEqual(self._file_gets(second), [])

        changed = DOWNLOADS.replace("\t2\n", "\t4\n", 1)
        catalog.blobs["dl-daily"] = gzip_text(changed)
        third = Session(catalog.handler)
        updated = asc_bark.cmd_run(self.config(), third, dry_run=False, sleeper=sleep)
        self.assertIn("首次下载 14（事件次数）", updated.message)
        self.assertIn("下载合计 17（事件次数）", updated.message)
        self.assertEqual(len(catalog.posts), 2)
        self.assertEqual(self._file_gets(third), ["dl-daily"])

    def test_overlap_instances_do_not_sum(self):
        catalog = OverlapCatalog()
        self.assertGreater(catalog.snapshot_hash, catalog.ongoing_hash)
        session = Session(catalog.handler)
        result = asc_bark.cmd_run(self.config(), session, dry_run=False, sleeper=recording_sleeper()[1])
        self.assertEqual(result.code, 0, result.message)
        self.assertIn("首次下载 7（事件次数）", result.message)
        self.assertIn("重新下载 1（事件次数）", result.message)
        self.assertIn("下载合计 8（事件次数）", result.message)
        self.assertIn("展示次数 4（事件次数，不含浏览）", result.message)
        self.assertIn("产品页浏览 2（事件次数）", result.message)
        self.assertNotIn("首次下载 50", result.message)
        self.assertNotIn("首次下载 10", result.message)
        self.assertNotIn("展示次数 80", result.message)
        self.assertIn("处理日 下载 2024-04-03 / 发现 2024-04-02", result.message)
        self.assertEqual(len(catalog.posts), 1)
        self.assertFalse(any(method == "POST" and API in url for method, url, *_rest in session.calls))

    def test_no_data_pending_and_name_mismatch(self):
        disjoint = ReadyCatalog(
            downloads_text=tsv(DL_HEADER, [dl_row("2024-03-31", "First-time Download", 1), dl_row("2024-03-31", "Redownload", 1)]),
            discovery_text=tsv(DISC_HEADER, [disc_row("2024-03-29", "Impression", "No page", 3, 1)]),
        )
        session = Session(disjoint.handler)
        result = asc_bark.cmd_run(self.config(), session, dry_run=False, sleeper=recording_sleeper()[1])
        self.assertEqual(result.code, 0, result.message)
        self.assertIn("没有共同事件日", result.message)
        self.assertIn("不把缺失的一天补成 0", result.message)
        self.assertEqual(disjoint.posts, [])
        self.assertNotIn("99999", result.message)

        empty_metrics = ReadyCatalog(
            downloads_text=tsv(DL_HEADER, [dl_row("2024-03-30", "Manual update", 12)]),
            discovery_text=tsv(DISC_HEADER, [disc_row("2024-03-30", "Tap", "Product page", 6, 1)]),
        )
        quiet = Session(empty_metrics.handler)
        skipped = asc_bark.cmd_run(self.config(), quiet, dry_run=False, sleeper=recording_sleeper()[1])
        self.assertEqual(skipped.code, 0, skipped.message)
        self.assertIn("缺失不是 0", skipped.message)
        self.assertEqual(empty_metrics.posts, [])

        pending = ReadyCatalog()
        pending.pending_newest = True
        pending_session = Session(pending.handler)
        waiting = asc_bark.cmd_run(self.config(), pending_session, dry_run=False, sleeper=recording_sleeper()[1])
        self.assertEqual(waiting.code, 0, waiting.message)
        self.assertIn("还没有可下载分段", waiting.message)
        self.assertEqual(pending.posts, [])
        self.assertEqual(self._file_gets(pending_session), [])

        none = ReadyCatalog()
        none.no_instances = True
        none_result = asc_bark.cmd_run(self.config(), Session(none.handler), dry_run=False, sleeper=recording_sleeper()[1])
        self.assertEqual(none_result.code, 0)
        self.assertIn("还没有 DAILY 实例", none_result.message)

        missing = ReadyCatalog()
        missing.no_reports = True
        missing_result = asc_bark.cmd_run(self.config(), Session(missing.handler), dry_run=False, sleeper=recording_sleeper()[1])
        self.assertEqual(missing_result.code, 0)
        self.assertIn("还没有列出", missing_result.message)

        absent = ReadyCatalog()
        absent.no_requests = True
        absent_session = Session(absent.handler)
        absent_result = asc_bark.cmd_run(self.config(), absent_session, dry_run=False, sleeper=recording_sleeper()[1])
        self.assertEqual(absent_result.code, 0)
        self.assertIn("init", absent_result.message)
        self.assertEqual(absent_session.calls[0][0], "GET")
        self.assertFalse(any(call[0] == "POST" for call in absent_session.calls))

        detailed = ReadyCatalog()
        detailed.only_detailed = True
        with self.assertRaises(asc_bark.AscError) as caught:
            asc_bark.cmd_run(self.config(), Session(detailed.handler), dry_run=False, sleeper=recording_sleeper()[1])
        self.assertIn("没有改用 Detailed", str(caught.exception))
        self.assertIn("App Store Downloads Detailed", str(caught.exception))

    def test_bark_failure_is_not_marked_and_retries(self):
        failed = ReadyCatalog()
        failed.bark_codes = [400]
        delays, sleep = recording_sleeper()
        with self.assertRaises(asc_bark.AscError) as caught:
            asc_bark.cmd_run(self.config(), Session(failed.handler), dry_run=False, sleeper=sleep)
        self.assertIn("未记录为已推送", str(caught.exception))
        self.assertEqual(len(failed.posts), 1)
        self.assertEqual(delays, [])
        state_path = self.data / "state.json"
        if state_path.exists():
            self.assertEqual(json.loads(state_path.read_text(encoding="utf-8"))["pushed"], {})

        retry = ReadyCatalog()
        retry.bark_codes = [503, 200]
        delays, sleep = recording_sleeper()
        result = asc_bark.cmd_run(self.config(), Session(retry.handler), dry_run=False, sleeper=sleep)
        self.assertEqual(result.code, 0, result.message)
        self.assertEqual(len(retry.posts), 2)
        self.assertEqual(delays, [1.0])
        pushed = json.loads((self.data / "state.json").read_text(encoding="utf-8"))["pushed"]
        self.assertIn("2024-03-30", pushed)
        self.assertTrue(pushed["2024-03-30"]["pushed_at"])

    def test_download_error_redacts_signed_url(self):
        catalog = ReadyCatalog()
        catalog.file_error = True
        delays, sleep = recording_sleeper()
        session = Session(catalog.handler)
        with self.assertRaises(asc_bark.AscError) as caught:
            asc_bark.cmd_run(self.config(), session, dry_run=False, sleeper=sleep)
        self.assertIn("下载报表分段失败", str(caught.exception))
        self.assertNotIn("deadbeef", str(caught.exception))
        self.assertNotIn("X-Amz", str(caught.exception))
        self.assertEqual(len(self._file_gets(session)), 3)
        self.assertEqual(delays, [1.0, 2.0])
        state_path = self.data / "state.json"
        if state_path.exists():
            self.assertEqual(json.loads(state_path.read_text(encoding="utf-8")).get("pushed"), {})

    def test_dry_run_does_not_post_or_mark(self):
        catalog = ReadyCatalog()
        session = Session(catalog.handler)
        result = asc_bark.cmd_run(self.config(bark=False), session, dry_run=True, sleeper=recording_sleeper()[1])
        self.assertEqual(result.code, 0, result.message)
        self.assertIn("dry-run：未调用 Bark", result.message)
        self.assertIn("首次下载 12（事件次数）", result.message)
        self.assertEqual(catalog.posts, [])
        self.assertFalse(any(call[0] == "POST" for call in session.calls))
        state = json.loads((self.data / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["pushed"], {})
        follow = Session(catalog.handler)
        pushed = asc_bark.cmd_run(self.config(), follow, dry_run=False, sleeper=recording_sleeper()[1])
        self.assertIn("已推送", pushed.message)
        self.assertEqual(len(catalog.posts), 1)

    def test_init_is_idempotent_and_snapshot_is_optional(self):
        store = {"items": [], "posts": [], "gets": 0}
        barks = []

        def handler(method, url, params, body, headers, timeout):
            if method == "POST":
                access = body["data"]["attributes"]["accessType"]
                store["posts"].append(body)
                if store.get("conflict"):
                    return Response(409, {"errors": [{"status": "409"}]})
                rid = "created-" + access.lower()
                store["items"].append({"id": rid, "access": access, "stopped": False})
                return Response(201, {"data": {"id": rid, "type": "analyticsReportRequests"}})
            store["gets"] += 1
            if store.get("paginate") and "cursor" not in url:
                nxt = f"{API}/v1/apps/{APP_ID}/analyticsReportRequests?cursor=2"
                return Response(200, page([], nxt=nxt))
            if store.get("paginate"):
                self.assertIsNone(params)
            data = [
                resource("analyticsReportRequests", item["id"], {
                    "accessType": item["access"],
                    "stoppedDueToInactivity": item["stopped"],
                })
                for item in store["items"]
            ]
            return Response(200, page(data))

        delays, sleep = recording_sleeper()
        created = asc_bark.cmd_init(self.config(), Session(accept_bark(handler, barks)), snapshot=False, sleeper=sleep)
        self.assertIn("已创建 ONGOING", created.message)
        self.assertIn("已发送初始化通知", created.message)
        self.assertEqual(len(barks), 1)
        self.assertIn("ONGOING 请求已创建", barks[0]["body"])
        self.assertIn("第一份报表通常需要 1–2 天", barks[0]["body"])
        self.assertEqual(len(store["posts"]), 1)
        self.assertEqual(store["posts"][0], {
            "data": {
                "type": "analyticsReportRequests",
                "attributes": {"accessType": "ONGOING"},
                "relationships": {"app": {"data": {"type": "apps", "id": APP_ID}}},
            }
        })
        again = asc_bark.cmd_init(self.config(), Session(accept_bark(handler, barks)), snapshot=True, sleeper=sleep)
        self.assertIn("未重复创建", again.message)
        self.assertIn("已创建 ONE_TIME_SNAPSHOT", again.message)
        self.assertEqual([item["data"]["attributes"]["accessType"] for item in store["posts"]], ["ONGOING", "ONE_TIME_SNAPSHOT"])
        third = asc_bark.cmd_init(self.config(), Session(accept_bark(handler, barks)), snapshot=True, sleeper=sleep)
        self.assertIn("未重复创建", third.message)
        self.assertEqual(len(store["posts"]), 2)

        stopped = {"items": [{"id": "old-ongoing", "access": "ONGOING", "stopped": True}], "posts": []}

        def stopped_handler(method, url, params, body, headers, timeout):
            if method == "POST":
                stopped["posts"].append(body["data"]["attributes"]["accessType"])
                return Response(201, {"data": {"id": "new-ongoing"}})
            data = [
                resource("analyticsReportRequests", item["id"], {
                    "accessType": item["access"],
                    "stoppedDueToInactivity": item["stopped"],
                })
                for item in stopped["items"]
            ]
            return Response(200, page(data))

        resumed = asc_bark.cmd_init(
            self.config(),
            Session(accept_bark(stopped_handler, barks)),
            snapshot=False,
            sleeper=sleep,
        )
        self.assertIn("停止", resumed.message)
        self.assertEqual(stopped["posts"], ["ONGOING"])

        paged = {"posts": []}

        def paged_handler(method, url, params, body, headers, timeout):
            if method == "POST":
                paged["posts"].append(body)
                return Response(201, {"data": {"id": "should-not"}})
            if "cursor" not in url:
                return Response(200, page([], nxt=f"{API}/v1/apps/{APP_ID}/analyticsReportRequests?cursor=2"))
            self.assertIsNone(params)
            return Response(200, page([
                resource("analyticsReportRequests", "already", {
                    "accessType": "ONGOING",
                    "stoppedDueToInactivity": False,
                })
            ]))

        paged_result = asc_bark.cmd_init(
            self.config(),
            Session(accept_bark(paged_handler, barks)),
            snapshot=False,
            sleeper=sleep,
        )
        self.assertIn("已存在 ONGOING", paged_result.message)
        self.assertEqual(paged["posts"], [])

        conflict = {"gets": 0, "posts": 0}

        def conflict_handler(method, url, params, body, headers, timeout):
            if method == "POST":
                conflict["posts"] += 1
                return Response(409, {"errors": [{"status": "409"}]})
            conflict["gets"] += 1
            if conflict["gets"] == 1:
                return Response(200, page([]))
            return Response(200, page([
                resource("analyticsReportRequests", "already", {
                    "accessType": "ONGOING",
                    "stoppedDueToInactivity": False,
                })
            ]))

        conflicted = asc_bark.cmd_init(
            self.config(),
            Session(accept_bark(conflict_handler, barks)),
            snapshot=False,
            sleeper=sleep,
        )
        self.assertIn("已存在", conflicted.message)
        self.assertEqual(conflict["posts"], 1)
        self.assertEqual(len(barks), 6)
        self.assertIn("ONGOING 请求已存在", barks[2]["body"])
        self.assertIn("ONGOING 请求已创建", barks[3]["body"])
        self.assertIn("ONGOING 请求已存在", barks[5]["body"])
        self.assertNotIn(APP_ID, barks[0]["title"] + barks[0]["body"])
        self.assertNotIn(BARK_KEY, barks[0]["title"] + barks[0]["body"])
        self.assertFalse((self.data / "serve.json").exists())

    def test_apple_retries_pagination_and_download_auth(self):
        delays, sleep = recording_sleeper()
        calls = {"n": 0}

        def retry_handler(method, url, params, body, headers, timeout):
            calls["n"] += 1
            if calls["n"] == 1:
                return Response(500, {"errors": []}, headers={"Retry-After": "999"})
            if calls["n"] == 2:
                return Response(500, {"errors": []})
            return Response(200, page([]))

        client = self._client(Session(retry_handler), sleep)
        self.assertEqual(client.list_requests(), [])
        self.assertEqual(calls["n"], 3)
        self.assertEqual(delays, [8.0, 2.0])

        def denied(method, url, params, body, headers, timeout):
            calls["denied"] = calls.get("denied", 0) + 1
            return Response(401, {"errors": [{"status": "401"}]})

        denied_client = self._client(Session(denied), recording_sleeper()[1])
        with self.assertRaises(asc_bark.AscError) as caught:
            denied_client.list_requests()
        self.assertIn("Admin", str(caught.exception))
        self.assertEqual(calls["denied"], 1)

        def evil(method, url, params, body, headers, timeout):
            if "evil.test" in url:
                raise AssertionError(url)
            return Response(200, page([], nxt="https://evil.test/steal?token=secret-token"))

        with self.assertRaises(asc_bark.AscError) as caught:
            self._client(Session(evil), recording_sleeper()[1]).list_requests()
        self.assertIn("分页", str(caught.exception))
        self.assertNotIn("secret-token", str(caught.exception))

        seen = {"pages": 0}

        def paged(method, url, params, body, headers, timeout):
            seen["pages"] += 1
            if "cursor" not in url:
                self.assertEqual(params["filter[category]"], "APP_STORE_ENGAGEMENT")
                self.assertEqual(params["limit"], "200")
                nxt = f"{API}/v1/analyticsReportRequests/req/reports?cursor=abc&category=APP_STORE_ENGAGEMENT"
                return Response(200, page([
                    resource("analyticsReports", "detailed", {
                        "name": "App Store Discovery and Engagement Detailed",
                        "category": "APP_STORE_ENGAGEMENT",
                    })
                ], nxt=nxt))
            self.assertIsNone(params)
            return Response(200, page([
                resource("analyticsReports", "standard", {
                    "name": "App Store Discovery and Engagement Standard",
                    "category": "APP_STORE_ENGAGEMENT",
                })
            ]))

        reports = self._client(Session(paged), recording_sleeper()[1]).list_reports("req", "APP_STORE_ENGAGEMENT")
        self.assertEqual([item["name"] for item in reports], [
            "App Store Discovery and Engagement Detailed",
            "App Store Discovery and Engagement Standard",
        ])
        self.assertEqual(seen["pages"], 2)

        blob = gzip_text(tsv(DL_HEADER, [dl_row("2024-03-30", "First-time Download", 2), dl_row("2024-03-30", "Redownload", 2)]))
        digest = hashlib.md5(blob).hexdigest()
        downloaded = {"n": 0}

        def files(method, url, params, body, headers, timeout):
            downloaded["n"] += 1
            self.assertNotIn("Authorization", headers)
            self.assertNotIn("Bearer", str(headers))
            if downloaded["n"] == 1:
                return Response(200, content=b"bad")
            return Response(200, content=blob)

        file_session = Session(files)
        payload = self._client(file_session, recording_sleeper()[1]).download_verified({
            "url": f"{FILES}/one.gz?X-Amz-Signature=deadbeefdeadbeef",
            "checksum": digest,
            "size": len(blob),
        })
        self.assertEqual(payload, blob)
        self.assertEqual(downloaded["n"], 2)
        self.assertNotIn("deadbeef", "")

    def test_redaction_and_unhandled_traceback(self):
        pem = "-----BEGIN PRIVATE KEY-----\nSECRETDATA\n-----END PRIVATE KEY-----"
        raw = (
            f"fail {pem} https://files.test/a?X-Amz-Signature=deadbeefdeadbeef "
            "eyJhbGciOiJFUzI1NiJ9.eyJpc3MiOiJ4In0.sigpartvalue "
            f'{{"device_key":"{BARK_KEY}"}} {BARK_KEY}'
        )
        cleaned = asc_bark.redact(raw, [BARK_KEY, pem])
        self.assertNotIn("SECRETDATA", cleaned)
        self.assertNotIn("deadbeef", cleaned)
        self.assertNotIn("eyJhbGci", cleaned)
        self.assertNotIn(BARK_KEY, cleaned)
        self.assertIn("[redacted-url]", cleaned)
        self.assertIn("[redacted-jwt]", cleaned)
        self.assertIn("[redacted-key]", cleaned)

        class Boom:
            def get(self, *args, **kwargs):
                raise RuntimeError(
                    "https://files.test/a?X-Amz-Signature=deadbeefdeadbeef " + BARK_KEY
                )

            def post(self, *args, **kwargs):
                raise AssertionError("posted")

        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            code = asc_bark.main(["run"], self.env(), session=Boom())
        text = output.getvalue()
        self.assertEqual(code, 1)
        self.assertNotIn("deadbeef", text)
        self.assertNotIn(BARK_KEY, text)
        self.assertIn("[redacted-url]", text)

    def test_one_shot_run_does_not_record_serve_and_restart_does_not_fetch(self):
        catalog = ReadyCatalog()
        catalog.no_requests = True
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            once = asc_bark.cmd_run(self.config(), Session(catalog.handler), dry_run=False)
        self.assertEqual(once.code, 0, once.message)
        self.assertIn("init", once.message)
        self.assertEqual(catalog.posts, [])
        self.assertTrue((self.data / "state.json").exists())
        self.assertFalse((self.data / "serve.json").exists())

        seen = {"before_fetch": False}

        def handler(method, url, params, body, headers, timeout):
            record = json.loads((self.data / "serve.json").read_text(encoding="utf-8"))
            self.assertEqual(record["runs"], 1)
            self.assertEqual(record["first_trigger"], "startup")
            self.assertEqual(record["last_trigger"], "startup")
            seen["before_fetch"] = True
            return catalog.handler(method, url, params, body, headers, timeout)

        shanghai = ZoneInfo("Asia/Shanghai")
        now = datetime(2026, 11, 26, 8, 0, tzinfo=shanghai)
        session = Session(handler)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            served = asc_bark.cmd_serve(
                self.config(),
                session,
                schedule=asc_bark.parse_schedule("0 9 * * *"),
                tz=shanghai,
                clock=lambda: now,
                wait=lambda _delay: True,
            )
        self.assertEqual(served.code, 0, stderr.getvalue())
        self.assertEqual(served.message, "已停止。")
        self.assertTrue(seen["before_fetch"])
        self.assertEqual([call[0] for call in session.calls], ["GET", "POST"])
        self.assertTrue(session.calls[1][1].startswith("https://bark.test/push"))
        startup = session.calls[1][3]
        self.assertEqual(startup["title"], "asc-bark 已启动")
        self.assertIn("进程已启动", startup["body"])
        self.assertIn("日程 0 9 * * *", startup["body"])
        self.assertIn("时区 Asia/Shanghai", startup["body"])
        self.assertIn("下次运行 2026-11-26T09:00:00+08:00", startup["body"])
        self.assertIn("本次启动会立刻抓取", startup["body"])
        self.assertIn("启动抓取已完成", startup["body"])
        self.assertIn("请先执行 init", startup["body"])
        self.assertNotIn("本次启动只等待", startup["body"])
        self.assertNotIn(APP_ID, startup["title"] + startup["body"])
        self.assertNotIn(BARK_KEY, startup["title"] + startup["body"])
        self.assertEqual(len(catalog.posts), 1)
        self.assertIn("先抓取一次", stdout.getvalue())
        self.assertIn("下次运行：2026-11-26T09:00:00+08:00", stdout.getvalue())
        self.assertIn("还没有分析报表请求", stdout.getvalue())
        self.assertNotIn(BARK_KEY, (self.data / "serve.json").read_text(encoding="utf-8"))
        self.assertNotIn(self.pem, (self.data / "serve.json").read_text(encoding="utf-8"))
        state = json.loads((self.data / "state.json").read_text(encoding="utf-8"))
        self.assertNotIn("serve", state)
        self.assertNotIn("startup", (self.data / "state.json").read_text(encoding="utf-8"))

        restart = Session(handler)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            again = asc_bark.cmd_serve(
                self.config(),
                restart,
                schedule=asc_bark.parse_schedule("09:00"),
                tz=shanghai,
                clock=lambda: now,
                wait=lambda _delay: True,
            )
        self.assertEqual(again.code, 0, stderr.getvalue())
        self.assertEqual(again.message, "已停止。")
        self.assertEqual(len(restart.calls), 1)
        self.assertEqual(restart.calls[0][0], "POST")
        waiting = restart.calls[0][3]
        self.assertEqual(waiting["title"], "asc-bark 已启动")
        self.assertIn("进程已启动", waiting["body"])
        self.assertIn("本次启动只等待，不会立刻抓取", waiting["body"])
        self.assertNotIn("本次启动会立刻抓取", waiting["body"])
        self.assertNotIn("请先执行 init", waiting["body"])
        self.assertEqual(len(catalog.posts), 2)
        self.assertIn("不会立刻抓取", stdout.getvalue())
        self.assertNotIn("先抓取一次", stdout.getvalue())
        record = json.loads((self.data / "serve.json").read_text(encoding="utf-8"))
        self.assertEqual(record["runs"], 1)
        self.assertEqual(record["first_trigger"], "startup")

    def test_init_requires_bark_before_apple_and_reports_both_outcomes(self):
        def forbidden(method, url, params, body, headers, timeout):
            raise AssertionError(method + " " + str(url))

        blocked = Session(forbidden)
        output = io.StringIO()
        env = self.env()
        env.pop("BARK_KEY")
        with redirect_stdout(output), redirect_stderr(output):
            code = asc_bark.main(["init"], env, session=blocked)
        self.assertEqual(code, 2)
        self.assertIn("缺少 BARK_KEY", output.getvalue())
        self.assertEqual(blocked.calls, [])
        self.assertNotIn(BARK_KEY, output.getvalue())
        with self.assertRaises(asc_bark.AscError) as caught:
            asc_bark.cmd_init(self.config(bark=False), blocked, snapshot=False)
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(blocked.calls, [])

        serve_output = io.StringIO()
        with redirect_stdout(serve_output), redirect_stderr(serve_output):
            serve_code = asc_bark.main(["serve"], env, session=blocked)
        self.assertEqual(serve_code, 2)
        self.assertIn("缺少 BARK_KEY", serve_output.getvalue())
        self.assertEqual(blocked.calls, [])
        self.assertFalse((self.data / "serve.json").exists())

        store = {"items": [], "apples": [], "barks": [], "fail": True}

        def handler(method, url, params, body, headers, timeout):
            if method == "POST" and str(url).startswith("https://bark.test/"):
                store["barks"].append(body)
                if store["fail"]:
                    return Response(200, {"code": 400, "message": "no"})
                return Response(200, {"code": 200, "message": "success"})
            if method == "POST":
                store["apples"].append(body)
                store["items"].append("req-1")
                return Response(201, {"data": {"id": "req-1", "type": "analyticsReportRequests"}})
            data = [
                resource("analyticsReportRequests", item, {
                    "accessType": "ONGOING",
                    "stoppedDueToInactivity": False,
                })
                for item in store["items"]
            ]
            return Response(200, page(data))

        with self.assertRaises(asc_bark.AscError) as caught:
            asc_bark.cmd_init(self.config(), Session(handler), snapshot=False, sleeper=recording_sleeper()[1])
        self.assertIn("请求仍在", str(caught.exception))
        self.assertIn("Bark 通知失败", str(caught.exception))
        self.assertIn("已创建 ONGOING", str(caught.exception))
        self.assertEqual(len(store["apples"]), 1)
        self.assertEqual(len(store["barks"]), 1)
        self.assertIn("ONGOING 请求已创建", store["barks"][0]["body"])
        self.assertIn("第一份报表通常需要 1–2 天", store["barks"][0]["body"])

        store["fail"] = False
        again = asc_bark.cmd_init(self.config(), Session(handler), snapshot=False, sleeper=recording_sleeper()[1])
        self.assertEqual(again.code, 0, again.message)
        self.assertIn("已存在 ONGOING", again.message)
        self.assertIn("asc-bark 初始化完成", again.message)
        self.assertEqual(len(store["apples"]), 1)
        self.assertEqual(len(store["barks"]), 2)
        self.assertIn("ONGOING 请求已存在", store["barks"][1]["body"])
        self.assertIn("第一份报表通常需要 1–2 天", store["barks"][1]["body"])
        self.assertNotIn(APP_ID, store["barks"][1]["title"] + store["barks"][1]["body"])
        self.assertNotIn(BARK_KEY, again.message)

        failed = {"barks": []}

        def denied(method, url, params, body, headers, timeout):
            if method == "POST" and str(url).startswith("https://bark.test/"):
                failed["barks"].append(body)
                return Response(200, {"code": 200, "message": "success"})
            if method == "POST":
                raise AssertionError("created a request after a read failure")
            return Response(401, {"errors": [{"status": "401"}]})

        with self.assertRaises(asc_bark.AscError) as caught:
            asc_bark.cmd_init(self.config(), Session(denied), snapshot=False, sleeper=recording_sleeper()[1])
        self.assertIn("Admin", str(caught.exception))
        self.assertNotIn("请求仍在", str(caught.exception))
        self.assertEqual(len(failed["barks"]), 1)
        self.assertEqual(failed["barks"][0]["title"], "asc-bark 初始化未完成")
        self.assertIn("初始化没有完成", failed["barks"][0]["body"])
        self.assertIn("Admin", failed["barks"][0]["body"])
        self.assertNotIn("ONGOING 请求已创建", failed["barks"][0]["body"])

        silent = {"barks": []}

        def denied_and_bark_down(method, url, params, body, headers, timeout):
            if method == "POST" and str(url).startswith("https://bark.test/"):
                silent["barks"].append(body)
                return Response(200, {"code": 400, "message": "no"})
            return Response(403, {"errors": [{"status": "403"}]})

        with self.assertRaises(asc_bark.AscError) as caught:
            asc_bark.cmd_init(
                self.config(),
                Session(denied_and_bark_down),
                snapshot=False,
                sleeper=recording_sleeper()[1],
            )
        self.assertIn("Admin", str(caught.exception))
        self.assertIn("Bark 通知失败", str(caught.exception))
        self.assertNotIn("请求仍在", str(caught.exception))
        self.assertEqual(len(silent["barks"]), 1)

    def test_serve_startup_folds_summary_and_later_cycles_notify_once(self):
        catalog = ReadyCatalog()
        shanghai = ZoneInfo("Asia/Shanghai")
        now = datetime(2026, 11, 26, 8, 0, tzinfo=shanghai)
        schedule = asc_bark.parse_schedule("09:00")

        def serve(wait):
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                result = asc_bark.cmd_serve(
                    self.config(),
                    Session(catalog.handler),
                    schedule=schedule,
                    tz=shanghai,
                    clock=lambda: now,
                    wait=wait,
                    sleeper=recording_sleeper()[1],
                )
            return result, stdout.getvalue(), stderr.getvalue()

        started, _stdout, stderr = serve(lambda _delay: True)
        self.assertEqual(started.code, 0, stderr)
        self.assertEqual(len(catalog.posts), 1)
        folded = catalog.posts[0]
        self.assertEqual(folded["title"], "asc-bark 已启动")
        self.assertIn("进程已启动", folded["body"])
        self.assertIn("日程 0 9 * * *", folded["body"])
        self.assertIn("时区 Asia/Shanghai", folded["body"])
        self.assertIn("下次运行 2026-11-26T09:00:00+08:00", folded["body"])
        self.assertIn("本次启动会立刻抓取", folded["body"])
        self.assertIn("启动抓取已完成，汇总如下", folded["body"])
        self.assertIn("首次下载 12（事件次数）", folded["body"])
        self.assertNotIn(APP_ID, folded["title"])
        self.assertNotIn(BARK_KEY, folded["body"])
        pushed = json.loads((self.data / "state.json").read_text(encoding="utf-8"))["pushed"]
        self.assertIn("2024-03-30", pushed)

        waits = {"n": 0}

        def one_cycle(_delay: float) -> bool:
            waits["n"] += 1
            return waits["n"] >= 2

        again, _stdout, stderr = serve(one_cycle)
        self.assertEqual(again.code, 0, stderr)
        self.assertEqual(len(catalog.posts), 3)
        self.assertIn("本次启动只等待，不会立刻抓取", catalog.posts[1]["body"])
        self.assertEqual(catalog.posts[2]["title"], "asc-bark 本轮结果")
        self.assertIn("本轮已执行", catalog.posts[2]["body"])
        self.assertIn("没有变化", catalog.posts[2]["body"])
        self.assertNotIn("首次下载", catalog.posts[2]["body"])
        self.assertNotIn("本次不推送", catalog.posts[2]["body"])

        changed = DOWNLOADS.replace("\t2\n", "\t4\n", 1)
        catalog.blobs["dl-daily"] = gzip_text(changed)
        waits["n"] = 0
        updated, _stdout, stderr = serve(one_cycle)
        self.assertEqual(updated.code, 0, stderr)
        self.assertEqual(len(catalog.posts), 5)
        self.assertEqual(catalog.posts[3]["title"], "asc-bark 已启动")
        self.assertIn("只等待", catalog.posts[3]["body"])
        self.assertEqual(catalog.posts[4]["title"], "Sample 2024-03-30")
        self.assertIn("首次下载 14（事件次数）", catalog.posts[4]["body"])
        self.assertNotIn("本轮已执行", catalog.posts[4]["body"])
        self.assertNotIn("asc-bark 本轮结果", catalog.posts[4]["title"])

    def test_scheduled_cycle_reports_quiet_results_and_failures(self):
        def quiet(setup):
            if (self.data / "state.json").exists():
                (self.data / "state.json").unlink()
            catalog = setup()
            result = asc_bark.cmd_run(
                self.config(),
                Session(catalog.handler),
                dry_run=False,
                sleeper=recording_sleeper()[1],
                notice="cycle",
            )
            self.assertEqual(result.code, 0, result.message)
            self.assertEqual(len(catalog.posts), 1)
            notice = catalog.posts[0]
            self.assertEqual(notice["title"], "asc-bark 本轮结果")
            self.assertIn("本轮已执行", notice["body"])
            self.assertNotIn("本次不推送", notice["body"])
            self.assertNotIn(BARK_KEY, notice["body"])
            return notice["body"]

        self.assertIn("请先执行 init", quiet(lambda: self._quiet_catalog(no_requests=True)))
        self.assertIn("还没有列出", quiet(lambda: self._quiet_catalog(no_reports=True)))
        self.assertIn("还没有 DAILY 实例", quiet(lambda: self._quiet_catalog(no_instances=True)))
        self.assertIn("还没有可下载分段", quiet(lambda: self._quiet_catalog(pending=True)))
        self.assertIn("没有共同事件日", quiet(self._disjoint_catalog))
        self.assertIn("没有把缺失的一天补成 0", quiet(self._disjoint_catalog))

        if (self.data / "state.json").exists():
            (self.data / "state.json").unlink()
        same = ReadyCatalog()
        first = asc_bark.cmd_run(
            self.config(),
            Session(same.handler),
            dry_run=False,
            sleeper=recording_sleeper()[1],
            notice="cycle",
        )
        self.assertIn("已推送", first.message)
        self.assertEqual(same.posts[0]["title"], "Sample 2024-03-30")
        self.assertEqual(len(same.posts), 1)
        second = asc_bark.cmd_run(
            self.config(),
            Session(same.handler),
            dry_run=False,
            sleeper=recording_sleeper()[1],
            notice="cycle",
        )
        self.assertIn("没有变化", second.message)
        self.assertEqual(len(same.posts), 2)
        self.assertIn("没有变化", same.posts[1]["body"])

        denied = {"posts": []}

        def denied_handler(method, url, params, body, headers, timeout):
            if method == "POST" and str(url).startswith("https://bark.test/"):
                denied["posts"].append(body)
                return Response(200, {"code": 200, "message": "success"})
            return Response(401, {"errors": [{"status": "401"}]})

        with self.assertRaises(asc_bark.AscError) as caught:
            asc_bark.cmd_run(
                self.config(),
                Session(denied_handler),
                dry_run=False,
                sleeper=recording_sleeper()[1],
                notice="cycle",
            )
        self.assertIn("Admin", str(caught.exception))
        self.assertEqual(len(denied["posts"]), 1)
        self.assertEqual(denied["posts"][0]["title"], "asc-bark 本轮失败")
        self.assertIn("本轮已执行，但没有完成", denied["posts"][0]["body"])
        self.assertIn("Admin", denied["posts"][0]["body"])

        if (self.data / "state.json").exists():
            (self.data / "state.json").unlink()
        failed_push = ReadyCatalog()
        failed_push.bark_codes = [400]
        facts = asc_bark.StartupFacts(
            schedule="0 9 * * *",
            timezone="Asia/Shanghai",
            next_run="2026-11-26T09:00:00+08:00",
            immediate=True,
        )
        with self.assertRaises(asc_bark.AscError) as caught:
            asc_bark.cmd_run(
                self.config(),
                Session(failed_push.handler),
                dry_run=False,
                sleeper=recording_sleeper()[1],
                notice="startup",
                startup=facts,
            )
        self.assertTrue(caught.exception.bark_attempted)
        self.assertEqual(len(failed_push.posts), 1)
        self.assertIn("进程已启动", failed_push.posts[0]["body"])
        self.assertIn("本次启动会立刻抓取", failed_push.posts[0]["body"])
        self.assertIn("首次下载", failed_push.posts[0]["body"])
        saved = json.loads((self.data / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["pushed"], {})

    def _quiet_catalog(self, **flags):
        catalog = ReadyCatalog()
        for name, enabled in flags.items():
            if name == "pending":
                catalog.pending_newest = enabled
            else:
                setattr(catalog, name, enabled)
        return catalog

    def _disjoint_catalog(self):
        return ReadyCatalog(
            downloads_text=tsv(
                DL_HEADER,
                [dl_row("2024-03-31", "First-time Download", 1), dl_row("2024-03-31", "Redownload", 1)],
            ),
            discovery_text=tsv(DISC_HEADER, [disc_row("2024-03-29", "Impression", "No page", 3, 1)]),
        )

    def test_corrupt_state_is_not_overwritten(self):
        path = self.data / "state.json"
        path.write_text("{", encoding="utf-8")
        with self.assertRaises(asc_bark.AscError) as caught:
            asc_bark.cmd_run(
                self.config(),
                Session(lambda *args: (_ for _ in ()).throw(AssertionError("network"))),
                dry_run=True,
            )
        self.assertIn("未覆盖", str(caught.exception))
        self.assertEqual(path.read_text(encoding="utf-8"), "{")

    def test_gunzip_limit(self):
        self.assertEqual(asc_bark.gunzip_limited(gzip.compress(b"abc"), limit=100), b"abc")
        with self.assertRaises(asc_bark.AscError):
            asc_bark.gunzip_limited(gzip.compress(b"a" * 150), limit=100)
        with self.assertRaises(asc_bark.AscError):
            asc_bark.gunzip_limited(b"not-gzip", limit=100)

    def test_test_notification_does_not_call_apple(self):
        def handler(method, url, params, body, headers, timeout):
            if method != "POST" or not url.startswith("https://bark.test/push"):
                raise AssertionError(method + " " + url)
            return Response(200, {"code": 200, "message": "success"})

        session = Session(handler)
        output = io.StringIO()
        env = {
            "BARK_KEY": BARK_KEY,
            "BARK_SERVER": "https://bark.test",
            "ASC_DATA_DIR": str(self.data),
        }
        with redirect_stdout(output), redirect_stderr(output):
            code = asc_bark.main(["test-notification"], env, session=session)
        self.assertEqual(code, 0)
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(session.calls[0][3]["body"], "这是一条测试通知，不包含 App Store 数据。")
        self.assertNotIn(BARK_KEY, output.getvalue())
        self.assertNotIn("apple.com", session.calls[0][1])
        self.assertFalse((self.data / "serve.json").exists())

    def test_concurrent_lock(self):
        lock_path = self.data / "asc-bark.lock"
        held = asc_bark.acquire_lock(lock_path)
        script = (
            "import sys\n"
            "import asc_bark\n"
            "try:\n"
            "    asc_bark.acquire_lock(asc_bark.Path(sys.argv[1]))\n"
            "except asc_bark.AscError as exc:\n"
            "    print(exc)\n"
            "    raise SystemExit(1)\n"
            "raise SystemExit(0)\n"
        )
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT)
        try:
            proc = subprocess.run(
                [sys.executable, "-c", script, str(lock_path)],
                cwd=str(ROOT),
                env=env,
                text=True,
                capture_output=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
            self.assertIn("正在运行", proc.stdout + proc.stderr)
        finally:
            asc_bark.release_lock(held)
        again = subprocess.run(
            [sys.executable, "-c", script, str(lock_path)],
            cwd=str(ROOT),
            env=env,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)

    def _client(self, session: Session, sleeper) -> asc_bark.AppleClient:
        return asc_bark.AppleClient(
            session,
            api_base=API,
            app_id=APP_ID,
            issuer_id="issuer-example-0001",
            key_id="KEYEXAMPLE1",
            private_key=self.pem,
            sleeper=sleeper,
        )

    def _file_gets(self, session: Session) -> list[str]:
        found = []
        for method, url, _params, _body, _headers, _timeout in session.calls:
            if method == "GET" and url.startswith(FILES):
                found.append(Path(urlparse(url).path).name.replace(".gz", ""))
        return found

    def _assert_downloads_are_anonymous(self, session: Session) -> None:
        bearer = None
        for _method, url, _params, _body, headers, _timeout in session.calls:
            if url.startswith(API) and "Authorization" in headers:
                bearer = headers["Authorization"]
                break
        self.assertIsNotNone(bearer)
        self.assertTrue(bearer.startswith("Bearer "))
        for _method, url, _params, _body, headers, _timeout in session.calls:
            if url.startswith(FILES):
                self.assertNotIn("Authorization", headers)
                self.assertNotIn(bearer, str(headers))
                self.assertNotIn(self.pem, str(headers))


if __name__ == "__main__":
    unittest.main()
