#!/usr/bin/env python3
"""Send App Store Connect analytics summaries to Bark.

``run`` fetches one summary and exits. ``serve`` stays up, waits for the next
time in ``ASC_SCHEDULE``, runs that same fetch and push, then waits again.
The schedule is interpreted in the ``TZ`` timezone.

This uses the Analytics Reports API only. It does not call Sales and Trends
and it does not scrape the App Store Connect website.

Reports, both standard and daily granularity only:

- ``App Store Downloads Standard`` (``COMMERCE``)
- ``App Store Discovery and Engagement Standard`` (``APP_STORE_ENGAGEMENT``)

Apple's ``name`` is the report title plus a ``Standard`` or ``Detailed``
suffix. Detailed reports are a different privacy treatment, not an extra
slice of the same events, so they are never added in.

Within one report instance every segment is a partition of that instance.
Across instances, a later ``processingDate`` replaces the same event ``Date``;
rows for that date are not merged. ``checksum`` identifies bytes. It is not
a timestamp.
"""

from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import io
import json
import os
import re
import signal
import sys
import threading
import time
import traceback
import zlib
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator, Mapping
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import jwt
import requests

API_BASE = "https://api.appstoreconnect.apple.com"
DEFAULT_BARK_SERVER = "https://api.day.app"
DOWNLOADS_REPORT = "App Store Downloads Standard"
DISCOVERY_REPORT = "App Store Discovery and Engagement Standard"
DOWNLOADS_CATEGORY = "COMMERCE"
DISCOVERY_CATEGORY = "APP_STORE_ENGAGEMENT"
USER_AGENT = "asc-bark"
TOKEN_TTL_SECONDS = 15 * 60
TOKEN_REFRESH_MARGIN_SECONDS = 60
CONNECT_TIMEOUT_SECONDS = 10
READ_TIMEOUT_SECONDS = 120
TIMEOUT = (CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS)
MAX_ATTEMPTS = 3
MAX_BACKOFF_SECONDS = 8
MAX_REPORT_BYTES = 32 * 1024 * 1024
RETRY_STATUS = {408, 429, 500, 502, 503, 504}
STATE_VERSION = 1
ACCESS_RANK = {"ONGOING": 2, "ONE_TIME_SNAPSHOT": 1}
DEFAULT_SCHEDULE = "09:00"
DEFAULT_TIMEZONE = "Asia/Shanghai"
SCHEDULE_HORIZON_YEARS = 8
SHUTDOWN_POLL_SECONDS = 1.0
SCHEDULE_HINT = (
    "ASC_SCHEDULE 应为每天的时刻 HH:MM（例如 09:00），"
    "或五段 cron：分 时 日 月 周（例如 0 9 * * *）。"
    "周几用数字，0 和 7 都是星期日。"
)

REPORT_SPECS = (
    {
        "kind": "downloads",
        "category": DOWNLOADS_CATEGORY,
        "name": DOWNLOADS_REPORT,
    },
    {
        "kind": "discovery",
        "category": DISCOVERY_CATEGORY,
        "name": DISCOVERY_REPORT,
    },
)

DOWNLOAD_COLUMNS = {
    "date": "date",
    "app name": "app_name",
    "app apple identifier": "app_id",
    "download type": "download_type",
    "counts": "counts",
}
DISCOVERY_COLUMNS = {
    "date": "date",
    "app name": "app_name",
    "app apple identifier": "app_id",
    "event": "event",
    "page type": "page_type",
    "counts": "counts",
}
REQUIRED_COLUMNS = {
    "downloads": ("date", "app apple identifier", "download type", "counts"),
    "discovery": ("date", "app apple identifier", "event", "page type", "counts"),
}
METRIC_KEYS = {
    "downloads": ("first_time_downloads", "redownloads"),
    "discovery": ("impressions", "product_page_views"),
}
COUNTED_DOWNLOAD_TYPES = {
    "first-time download": "first_time_downloads",
    "redownload": "redownloads",
}

_PEM_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
_URL_RE = re.compile(r"https?://[^\s\"'<>]+")
_DEVICE_KEY_RE = re.compile(r'("device_key"\s*:\s*")[^"]*"')
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class AscError(Exception):
    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Config:
    issuer_id: str
    key_id: str
    app_id: str
    private_key: str
    key_path: str
    bark_key: str | None
    bark_server: str
    data_dir: Path
    api_base: str = API_BASE

    def secret_values(self) -> list[str]:
        values = [self.private_key]
        if self.bark_key:
            values.append(self.bark_key)
        return [value for value in values if len(value) >= 8]


@dataclass(frozen=True)
class Result:
    code: int
    message: str


def canon_name(value: str) -> str:
    return " ".join(value.casefold().split())


def redact(text: str, secrets: list[str] | tuple[str, ...] | None = None) -> str:
    redacted = _PEM_RE.sub("[redacted-key]", text)
    redacted = _JWT_RE.sub("[redacted-jwt]", redacted)

    def hide_url(match: re.Match[str]) -> str:
        url = match.group(0)
        parsed = urlparse(url)
        if parsed.query or parsed.fragment or "X-Amz-" in url or "Signature=" in url:
            return "[redacted-url]"
        return url

    redacted = _URL_RE.sub(hide_url, redacted)
    redacted = _DEVICE_KEY_RE.sub(r'\1[redacted]"', redacted)
    for secret in secrets or ():
        if secret and len(secret) >= 8:
            redacted = redacted.replace(secret, "[redacted]")
    return redacted


def emit(message: str, secrets: list[str] | None = None, *, err: bool = False) -> None:
    print(redact(message, secrets), file=sys.stderr if err else sys.stdout)


def backoff_seconds(attempt: int, retry_after: str | None = None) -> float:
    if retry_after:
        match = re.search(r"\d+", str(retry_after))
        if match:
            return float(min(int(match.group(0)), MAX_BACKOFF_SECONDS))
    return float(min(2 ** max(attempt - 1, 0), MAX_BACKOFF_SECONDS))


def make_token(issuer_id: str, key_id: str, private_key: str, *, now: int | None = None) -> str:
    issued = int(time.time() if now is None else now)
    payload = {
        "iss": issuer_id,
        "iat": issued,
        "exp": issued + TOKEN_TTL_SECONDS,
        "aud": "appstoreconnect-v1",
    }
    headers = {"alg": "ES256", "kid": key_id, "typ": "JWT"}
    try:
        token = jwt.encode(payload, private_key, algorithm="ES256", headers=headers)
    except Exception:
        raise AscError("无法用该 p8 签发 ES256 JWT。请确认密钥、Key ID 与 Issuer ID。", code=2) from None
    if isinstance(token, bytes):
        token = token.decode("ascii")
    return token


def gunzip_limited(blob: bytes, limit: int = MAX_REPORT_BYTES) -> bytes:
    if len(blob) > limit:
        raise AscError("分段文件过大。")
    decoder = zlib.decompressobj(wbits=16 + zlib.MAX_WBITS)
    pending = blob
    out = bytearray()
    try:
        while True:
            room = max(limit - len(out) + 1, 1)
            chunk = decoder.decompress(pending, room)
            pending = decoder.unconsumed_tail
            if chunk:
                out += chunk
                if len(out) > limit:
                    raise AscError("解压后的报表过大。")
            if decoder.eof:
                break
            if not chunk and not pending:
                extra = decoder.flush()
                if extra:
                    out += extra
                if len(out) > limit or not decoder.eof:
                    raise AscError("解压后的报表过大。" if len(out) > limit else "报表不是 gzip 压缩的 TSV。")
                break
            if not chunk and pending:
                raise AscError("报表不是 gzip 压缩的 TSV。")
    except AscError:
        raise
    except zlib.error:
        raise AscError("报表不是 gzip 压缩的 TSV。") from None
    return bytes(out)


def parse_tsv(text: str, kind: str) -> list[dict[str, str]]:
    stripped = text.lstrip("\ufeff")
    if not stripped.strip():
        raise AscError("报表是空的。")
    header_line = stripped.splitlines()[0]
    if "\t" not in header_line:
        raise AscError("报表不是制表符分隔的 TSV。")
    reader = csv.DictReader(io.StringIO(stripped), delimiter="\t")
    if not reader.fieldnames:
        raise AscError("报表缺少表头。")
    canonical = [canon_name(name) for name in reader.fieldnames if name is not None]
    if any(not name for name in canonical):
        raise AscError("报表表头有空列。")
    if len(set(canonical)) != len(canonical):
        raise AscError("报表表头有重复列。")
    missing = [name for name in REQUIRED_COLUMNS[kind] if name not in canonical]
    if missing:
        raise AscError("报表缺少列：" + "、".join(missing))
    columns = DOWNLOAD_COLUMNS if kind == "downloads" else DISCOVERY_COLUMNS
    index = {canon_name(name): name for name in reader.fieldnames if name is not None}
    rows: list[dict[str, str]] = []
    for raw in reader:
        if raw is None or all(not (value or "").strip() for value in raw.values()):
            continue
        mapped: dict[str, str] = {}
        for source, target in columns.items():
            original = index.get(source)
            mapped[target] = (raw.get(original) or "").strip() if original else ""
        _validate_row(mapped)
        rows.append(mapped)
    return rows


def _validate_row(row: Mapping[str, str]) -> None:
    if not _DATE_RE.fullmatch(row["date"]):
        raise AscError("报表 Date 不是 YYYY-MM-DD。")
    try:
        date.fromisoformat(row["date"])
    except ValueError:
        raise AscError("报表 Date 不是有效日期。") from None
    if not re.fullmatch(r"\d+", row["counts"]):
        raise AscError("Counts 不是非负整数。")


def _empty_metrics(kind: str) -> dict[str, object]:
    slot: dict[str, object] = {key: None for key in METRIC_KEYS[kind]}
    slot["app_name"] = None
    slot["_conflict"] = False
    return slot


def _add_count(current: int | None, value: int) -> int:
    if current is None:
        return value
    return current + value


def _add_name(slot: dict[str, object], name: str) -> None:
    cleaned = name.strip()
    if not cleaned or slot.get("_conflict"):
        return
    if slot.get("app_name") is None:
        slot["app_name"] = cleaned
    elif slot["app_name"] != cleaned:
        slot["app_name"] = None
        slot["_conflict"] = True


def aggregate_rows(kind: str, rows: list[dict[str, str]], app_id: str) -> dict[str, dict[str, object]]:
    dates: dict[str, dict[str, object]] = {}
    for row in rows:
        if row["app_id"] != app_id:
            continue
        slot = dates.setdefault(row["date"], _empty_metrics(kind))
        _add_name(slot, row.get("app_name") or "")
        count = int(row["counts"])
        if kind == "downloads":
            metric = COUNTED_DOWNLOAD_TYPES.get(canon_name(row["download_type"]))
            if metric:
                slot[metric] = _add_count(slot[metric], count)  # type: ignore[arg-type]
        else:
            event = canon_name(row["event"])
            page_type = canon_name(row["page_type"])
            if event == "impression":
                slot["impressions"] = _add_count(slot["impressions"], count)  # type: ignore[arg-type]
            elif event == "page view" and page_type == "product page":
                slot["product_page_views"] = _add_count(slot["product_page_views"], count)  # type: ignore[arg-type]
    return {day: _public_metrics(slot) for day, slot in dates.items()}


def _public_metrics(slot: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in slot.items() if key != "_conflict"}


def _same_name(left: object, right: object) -> str | None:
    if not isinstance(left, str) or not left:
        return right if isinstance(right, str) else None
    if not isinstance(right, str) or not right or left == right:
        return left
    return None


def _add_optional(left: object, right: object) -> int | None:
    if left is None:
        return right if isinstance(right, int) else None
    if right is None:
        return left if isinstance(left, int) else None
    if isinstance(left, int) and isinstance(right, int):
        return left + right
    raise AscError("报表汇总遇到非整数。")


def merge_date_maps(
    left: dict[str, dict[str, object]],
    right: dict[str, dict[str, object]],
) -> dict[str, dict[str, object]]:
    merged = {day: dict(values) for day, values in left.items()}
    for day, values in right.items():
        if day not in merged:
            merged[day] = dict(values)
            continue
        slot = dict(merged[day])
        for key, value in values.items():
            if key == "app_name":
                slot["app_name"] = _same_name(slot.get("app_name"), value)
            else:
                slot[key] = _add_optional(slot.get(key), value)
        merged[day] = slot
    return merged


def aggregate_segments(
    kind: str,
    blobs: list[bytes],
    app_id: str,
    *,
    limit: int = MAX_REPORT_BYTES,
) -> dict[str, dict[str, object]]:
    combined: dict[str, dict[str, object]] | None = None
    for blob in blobs:
        try:
            text = gunzip_limited(blob, limit).decode("utf-8-sig")
        except UnicodeDecodeError:
            raise AscError("报表不是 UTF-8。") from None
        part = aggregate_rows(kind, parse_tsv(text, kind), app_id)
        combined = part if combined is None else merge_date_maps(combined, part)
    return combined or {}


def instance_sort_key(processing_date: str, access_type: str, instance_id: str) -> tuple[date, int, str]:
    if not instance_id:
        raise AscError("报表实例缺少 id。")
    try:
        processed = date.fromisoformat(processing_date)
    except ValueError:
        raise AscError("报表实例的 processingDate 不是有效日期。") from None
    return (processed, ACCESS_RANK.get(access_type, 0), instance_id)


def select_dates(instances: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    """Pick one instance per event date.

    The sort key is processing date, then ONGOING over ONE_TIME_SNAPSHOT,
    then instance id. Checksums are intentionally not an input.
    """

    winners: dict[str, dict[str, object]] = {}
    keys: dict[str, tuple[date, int, str]] = {}
    for instance in instances:
        key = instance_sort_key(
            str(instance["processing_date"]),
            str(instance["access_type"]),
            str(instance["id"]),
        )
        dates = instance.get("dates")
        if not isinstance(dates, dict):
            raise AscError("实例汇总结构无法识别。")
        for day, metrics in dates.items():
            if not isinstance(metrics, dict):
                raise AscError("实例汇总结构无法识别。")
            if day not in winners or key > keys[day]:
                winners[str(day)] = {
                    "instance_id": instance["id"],
                    "processing_date": instance["processing_date"],
                    "access_type": instance["access_type"],
                    "metrics": metrics,
                }
                keys[str(day)] = key
    return winners


def latest_common_day(left: Mapping[str, object], right: Mapping[str, object]) -> str | None:
    common = set(left) & set(right)
    if not common:
        return None
    return max(common, key=date.fromisoformat)


def build_summary(
    app_id: str,
    day: str,
    downloads_win: Mapping[str, object],
    discovery_win: Mapping[str, object],
) -> dict[str, object]:
    downloads = downloads_win["metrics"]
    discovery = discovery_win["metrics"]
    if not isinstance(downloads, dict) or not isinstance(discovery, dict):
        raise AscError("实例汇总结构无法识别。")
    names = []
    for metrics in (downloads, discovery):
        name = metrics.get("app_name")
        if isinstance(name, str) and name:
            names.append(name)
    app_name = names[0] if len(set(names)) == 1 else None
    return {
        "event_date": day,
        "app_id": app_id,
        "app_name": app_name,
        "first_time_downloads": downloads.get("first_time_downloads"),
        "redownloads": downloads.get("redownloads"),
        "impressions": discovery.get("impressions"),
        "product_page_views": discovery.get("product_page_views"),
        "conversion": None,
        "downloads_processing_date": downloads_win["processing_date"],
        "discovery_processing_date": discovery_win["processing_date"],
        "downloads_instance_id": downloads_win["instance_id"],
        "discovery_instance_id": discovery_win["instance_id"],
    }


def summary_fingerprint(summary: Mapping[str, object]) -> str:
    visible = {
        "event_date": summary["event_date"],
        "app_id": summary["app_id"],
        "first_time_downloads": summary["first_time_downloads"],
        "redownloads": summary["redownloads"],
        "impressions": summary["impressions"],
        "product_page_views": summary["product_page_views"],
    }
    raw = json.dumps(visible, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _shown(value: object) -> str:
    if value is None:
        return "不可用"
    return str(value)


def format_summary(summary: Mapping[str, object]) -> str:
    lines = [
        f"事件日 {summary['event_date']}（报表 Date，UTC 日历日）",
        (
            "处理日 下载 "
            f"{summary['downloads_processing_date']} / 发现 {summary['discovery_processing_date']}"
        ),
        f"首次下载 {_shown(summary['first_time_downloads'])}（事件次数）",
        f"重新下载 {_shown(summary['redownloads'])}（事件次数）",
    ]
    if summary["first_time_downloads"] is not None and summary["redownloads"] is not None:
        total = int(summary["first_time_downloads"]) + int(summary["redownloads"])  # type: ignore[arg-type]
        lines.append(f"下载合计 {total}（事件次数）")
    else:
        lines.append("下载合计 不可用")
    lines.append(f"展示次数 {_shown(summary['impressions'])}（事件次数，不含浏览）")
    lines.append(f"产品页浏览 {_shown(summary['product_page_views'])}（事件次数）")
    lines.append("转化率 不可用")
    return "\n".join(lines)


def format_title(summary: Mapping[str, object]) -> str:
    name = summary.get("app_name")
    if isinstance(name, str) and name:
        return f"{name} {summary['event_date']}"
    return f"App {summary['app_id']} {summary['event_date']}"


def has_any_metric(summary: Mapping[str, object]) -> bool:
    return any(
        summary[key] is not None
        for key in ("first_time_downloads", "redownloads", "impressions", "product_page_views")
    )


def now_iso() -> str:
    try:
        tz: timezone | ZoneInfo = ZoneInfo("Asia/Shanghai")
    except Exception:
        tz = timezone.utc
    return datetime.now(tz).isoformat(timespec="seconds")


def acquire_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise AscError("已有 asc-bark 正在运行，本次退出。") from None
    except OSError:
        handle.close()
        raise AscError("无法锁定数据目录。", code=2) from None
    return handle


def release_lock(handle) -> None:
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


@contextmanager
def locked(data_dir: Path) -> Iterator[None]:
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise AscError("无法创建数据目录。", code=2) from None
    handle = acquire_lock(data_dir / "asc-bark.lock")
    try:
        yield
    finally:
        release_lock(handle)


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"version": STATE_VERSION, "instances": {}, "pushed": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise AscError("状态文件无法读取，未覆盖它。") from None
    if (
        not isinstance(data, dict)
        or data.get("version") != STATE_VERSION
        or not isinstance(data.get("instances"), dict)
        or not isinstance(data.get("pushed"), dict)
    ):
        raise AscError("状态文件版本无法识别，未覆盖它。")
    return data


def save_state(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    payload = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise AscError("无法写入状态文件。") from None
    os.chmod(path, 0o600)


def checksum_matches(blob: bytes, checksum: str) -> bool:
    if not re.fullmatch(r"[0-9a-fA-F]{32}", checksum):
        raise AscError("分段 checksum 不是 32 位十六进制。")
    digest = hashlib.md5(blob).hexdigest()
    return digest.lower() == checksum.lower()


def push_bark(
    session: requests.Session,
    server: str,
    key: str,
    title: str,
    body: str,
    sleeper: Callable[[float], None] = time.sleep,
) -> None:
    url = server.rstrip("/") + "/push"
    payload = {"device_key": key, "title": title, "body": body, "group": "asc-bark"}
    headers = {"Content-Type": "application/json; charset=utf-8", "User-Agent": USER_AGENT}
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = session.post(url, json=payload, headers=headers, timeout=TIMEOUT)
        except requests.RequestException:
            if attempt == MAX_ATTEMPTS:
                raise AscError("Bark 推送失败，未记录为已推送。") from None
            sleeper(backoff_seconds(attempt))
            continue
        if response.status_code == 200:
            try:
                data = response.json()
            except ValueError:
                data = None
            if isinstance(data, dict) and data.get("code") == 200:
                return
            raise AscError("Bark 推送失败，未记录为已推送。")
        if response.status_code in RETRY_STATUS and attempt < MAX_ATTEMPTS:
            sleeper(backoff_seconds(attempt, response.headers.get("Retry-After")))
            continue
        raise AscError("Bark 推送失败，未记录为已推送。")
    raise AscError("Bark 推送失败，未记录为已推送。")


class AppleClient:
    def __init__(
        self,
        session: requests.Session,
        *,
        api_base: str,
        app_id: str,
        issuer_id: str,
        key_id: str,
        private_key: str,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.session = session
        self.api_base = api_base.rstrip("/")
        self.app_id = app_id
        self.issuer_id = issuer_id
        self.key_id = key_id
        self.private_key = private_key
        self.sleeper = sleeper
        self.clock = clock
        self._token = ""
        self._token_exp = 0

    def list_requests(self) -> list[dict[str, object]]:
        items = self._pages(
            f"/v1/apps/{self.app_id}/analyticsReportRequests",
            {
                "limit": "200",
                "fields[analyticsReportRequests]": "accessType,stoppedDueToInactivity",
            },
        )
        found = []
        for item in items:
            if item.get("type") not in (None, "analyticsReportRequests"):
                continue
            attributes = item.get("attributes") or {}
            access_type = attributes.get("accessType")
            if access_type not in ("ONGOING", "ONE_TIME_SNAPSHOT"):
                raise AscError("报表请求的 accessType 无法识别。")
            if not item.get("id"):
                raise AscError("报表请求缺少 id。")
            found.append(
                {
                    "id": item["id"],
                    "access_type": access_type,
                    "stopped": bool(attributes.get("stoppedDueToInactivity")),
                }
            )
        return found

    def create_request(self, access_type: str) -> str | None:
        body = {
            "data": {
                "type": "analyticsReportRequests",
                "attributes": {"accessType": access_type},
                "relationships": {"app": {"data": {"type": "apps", "id": self.app_id}}},
            }
        }
        response = self._send(
            "POST",
            self._api_url("/v1/analyticsReportRequests"),
            params=None,
            json_body=body,
            ok=(200, 201),
            also=(409,),
        )
        if response.status_code == 409:
            return None
        try:
            payload = response.json()
            created = payload["data"]["id"]
        except (ValueError, KeyError, TypeError):
            raise AscError("Apple 创建报表请求的响应无法识别。") from None
        if not created:
            raise AscError("Apple 创建报表请求的响应无法识别。")
        return str(created)

    def list_reports(self, request_id: str, category: str) -> list[dict[str, str]]:
        items = self._pages(
            f"/v1/analyticsReportRequests/{request_id}/reports",
            {
                "limit": "200",
                "filter[category]": category,
                "fields[analyticsReports]": "name,category",
            },
        )
        reports = []
        for item in items:
            if item.get("type") not in (None, "analyticsReports"):
                continue
            attributes = item.get("attributes") or {}
            name = attributes.get("name")
            if not name or not item.get("id"):
                raise AscError("报表缺少 name 或 id。")
            reports.append(
                {
                    "id": str(item["id"]),
                    "name": str(name),
                    "category": str(attributes.get("category") or ""),
                }
            )
        return reports

    def list_instances(self, report_id: str) -> list[dict[str, str]]:
        items = self._pages(
            f"/v1/analyticsReports/{report_id}/instances",
            {
                "limit": "200",
                "filter[granularity]": "DAILY",
                "fields[analyticsReportInstances]": "granularity,processingDate",
            },
        )
        instances = []
        for item in items:
            if item.get("type") not in (None, "analyticsReportInstances"):
                continue
            attributes = item.get("attributes") or {}
            granularity = attributes.get("granularity")
            processing = attributes.get("processingDate")
            if not item.get("id") or not granularity or not processing:
                raise AscError("报表实例缺少 id、granularity 或 processingDate。")
            instance_sort_key(str(processing), "ONGOING", str(item["id"]))
            instances.append(
                {
                    "id": str(item["id"]),
                    "granularity": str(granularity),
                    "processing_date": str(processing),
                }
            )
        return instances

    def list_segments(self, instance_id: str) -> list[dict]:
        items = self._pages(
            f"/v1/analyticsReportInstances/{instance_id}/segments",
            {
                "limit": "200",
                "fields[analyticsReportSegments]": "checksum,sizeInBytes,url",
            },
        )
        segments = []
        for item in items:
            if item.get("type") not in (None, "analyticsReportSegments"):
                continue
            attributes = item.get("attributes") or {}
            segments.append(
                {
                    "id": str(item.get("id") or ""),
                    "checksum": attributes.get("checksum") or "",
                    "size": attributes.get("sizeInBytes"),
                    "url": attributes.get("url") or "",
                }
            )
        return segments

    def download_verified(self, segment: Mapping[str, object]) -> bytes:
        url = str(segment["url"])
        checksum = str(segment["checksum"])
        size = int(segment["size"])  # type: ignore[arg-type]
        if not url.startswith("https://"):
            raise AscError("分段下载地址不是 https。")
        if not re.fullmatch(r"[0-9a-fA-F]{32}", checksum):
            raise AscError("分段 checksum 不是 32 位十六进制。")
        headers = {"User-Agent": USER_AGENT}
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = self.session.get(url, headers=headers, timeout=TIMEOUT)
            except requests.RequestException:
                if attempt == MAX_ATTEMPTS:
                    raise AscError("下载报表分段失败。") from None
                self.sleeper(backoff_seconds(attempt))
                continue
            if response.status_code != 200:
                if response.status_code in RETRY_STATUS and attempt < MAX_ATTEMPTS:
                    self.sleeper(backoff_seconds(attempt, response.headers.get("Retry-After")))
                    continue
                raise AscError("下载报表分段失败。")
            blob = response.content
            if len(blob) == size and checksum_matches(blob, checksum):
                return blob
            if attempt == MAX_ATTEMPTS:
                raise AscError("分段校验未通过，本次不推送。")
            self.sleeper(backoff_seconds(attempt))
        raise AscError("下载报表分段失败。")

    def _pages(self, path: str, params: dict[str, str]) -> list[dict]:
        url: str | None = self._api_url(path)
        first = True
        items: list[dict] = []
        while url:
            self._assert_api_url(url)
            response = self._send("GET", url, params=params if first else None, json_body=None, ok=(200,), also=())
            first = False
            try:
                body = response.json()
            except ValueError:
                raise AscError("Apple 返回的不是 JSON。") from None
            data = body.get("data") if isinstance(body, dict) else None
            if not isinstance(data, list):
                raise AscError("Apple 响应缺少 data 列表。")
            items.extend(data)
            link = body.get("links") if isinstance(body, dict) else None
            nxt = link.get("next") if isinstance(link, dict) else None
            url = str(nxt) if nxt else None
        return items

    def _send(self, method, url, *, params, json_body, ok: tuple[int, ...], also: tuple[int, ...]):
        headers = {
            "Authorization": f"Bearer {self._bearer()}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                if method == "GET":
                    response = self.session.get(url, params=params, headers=headers, timeout=TIMEOUT)
                else:
                    response = self.session.post(url, json=json_body, headers=headers, timeout=TIMEOUT)
            except requests.RequestException:
                if attempt == MAX_ATTEMPTS:
                    raise AscError("连接 App Store Connect 失败。") from None
                self.sleeper(backoff_seconds(attempt))
                continue
            if response.status_code in ok or response.status_code in also:
                return response
            if response.status_code in RETRY_STATUS and attempt < MAX_ATTEMPTS:
                self.sleeper(backoff_seconds(attempt, response.headers.get("Retry-After")))
                continue
            raise self._http_error(response)
        raise AscError("连接 App Store Connect 失败。")

    def _http_error(self, response) -> AscError:
        if response.status_code in (401, 403):
            return AscError(
                "Apple 拒绝了 API 密钥。init 需要 Admin；下载需要 Admin、Sales and Reports 或 Finance。",
                code=1,
            )
        return AscError(f"App Store Connect 返回 HTTP {response.status_code}。")

    def _bearer(self) -> str:
        now = int(self.clock())
        if not self._token or now >= self._token_exp - TOKEN_REFRESH_MARGIN_SECONDS:
            self._token = make_token(self.issuer_id, self.key_id, self.private_key, now=now)
            self._token_exp = now + TOKEN_TTL_SECONDS
        return self._token

    def _api_url(self, path: str) -> str:
        if path.startswith("https://") or path.startswith("http://"):
            return path
        return self.api_base + "/" + path.lstrip("/")

    def _assert_api_url(self, url: str) -> None:
        if not url.startswith(self.api_base + "/"):
            raise AscError("分页链接不属于 App Store Connect API。")


def _validate_bark_server(server: str) -> str:
    parsed = urlparse(server)
    if parsed.scheme not in ("https", "http") or not parsed.netloc:
        raise AscError("BARK_SERVER 必须是 http(s) 根地址。", code=2)
    if parsed.query or parsed.params or parsed.fragment or parsed.path not in ("", "/"):
        raise AscError("BARK_SERVER 只填写根地址，不要包含路径、查询参数或设备密钥。", code=2)
    return server


def _read_private_key(path_value: str) -> str:
    path = Path(path_value)
    if not path.is_file():
        raise AscError("找不到 ASC_PRIVATE_KEY_PATH 指向的 p8 文件。", code=2)
    try:
        size = path.stat().st_size
    except OSError:
        raise AscError("无法读取 p8 文件。", code=2) from None
    if size > 100_000:
        raise AscError("私钥文件过大。", code=2)
    try:
        pem = path.read_text(encoding="utf-8")
    except OSError:
        raise AscError("无法读取 p8 文件。", code=2) from None
    if "PRIVATE KEY" not in pem:
        raise AscError("私钥文件不是 PEM。", code=2)
    return pem


def _required(env: Mapping[str, str], name: str) -> str:
    value = (env.get(name) or "").strip()
    if not value:
        raise AscError(f"缺少 {name}。", code=2)
    return value


def load_config(env: Mapping[str, str], *, need_apple: bool, need_bark: bool) -> Config:
    data_dir = Path((env.get("ASC_DATA_DIR") or "/data").strip() or "/data")
    bark_server = _validate_bark_server((env.get("BARK_SERVER") or DEFAULT_BARK_SERVER).strip())
    bark_key = (env.get("BARK_KEY") or "").strip() or None
    if need_bark and not bark_key:
        raise AscError("缺少 BARK_KEY。", code=2)
    if not need_apple:
        return Config(
            issuer_id="",
            key_id="",
            app_id="",
            private_key="",
            key_path="",
            bark_key=bark_key,
            bark_server=bark_server,
            data_dir=data_dir,
        )
    issuer_id = _required(env, "ASC_ISSUER_ID")
    key_id = _required(env, "ASC_KEY_ID")
    app_id = _required(env, "ASC_APP_ID")
    if not re.fullmatch(r"\d{1,20}", app_id):
        raise AscError("ASC_APP_ID 必须是数字形式的 Apple ID。", code=2)
    if any(char.isspace() for char in issuer_id + key_id):
        raise AscError("ASC_ISSUER_ID 和 ASC_KEY_ID 不能包含空白。", code=2)
    key_path = _required(env, "ASC_PRIVATE_KEY_PATH")
    private_key = _read_private_key(key_path)
    make_token(issuer_id, key_id, private_key)
    return Config(
        issuer_id=issuer_id,
        key_id=key_id,
        app_id=app_id,
        private_key=private_key,
        key_path=key_path,
        bark_key=bark_key,
        bark_server=bark_server,
        data_dir=data_dir,
    )


def _client(config: Config, session: requests.Session, sleeper: Callable[[float], None]) -> AppleClient:
    return AppleClient(
        session,
        api_base=config.api_base,
        app_id=config.app_id,
        issuer_id=config.issuer_id,
        key_id=config.key_id,
        private_key=config.private_key,
        sleeper=sleeper,
    )


def _names(reports: list[dict[str, str]]) -> str:
    values = sorted({report["name"] for report in reports})
    if not values:
        return "（没有报表）"
    shown = "、".join(values[:30])
    if len(values) > 30:
        shown += "…"
    return shown


def _inspect_segments(segments: list[dict]) -> tuple[str, list[dict]]:
    if not segments:
        return "pending", []
    ready = []
    for segment in segments:
        url = str(segment.get("url") or "")
        checksum = str(segment.get("checksum") or "")
        size = segment.get("size")
        if not url:
            return "pending", []
        if size is None or checksum == "":
            raise AscError("分段缺少 checksum 或 sizeInBytes。")
        if not url.startswith("https://"):
            raise AscError("分段下载地址不是 https。")
        try:
            size_int = int(size)
        except (TypeError, ValueError):
            raise AscError("分段 sizeInBytes 不是整数。") from None
        if size_int < 0:
            raise AscError("分段 sizeInBytes 不是整数。")
        ready.append(
            {
                "id": str(segment.get("id") or ""),
                "url": url,
                "checksum": checksum,
                "size": size_int,
            }
        )
    ready.sort(key=lambda item: item["id"])
    return "ready", ready


def _cache_dates(cached: object, kind: str, checksums: list[str]) -> dict[str, dict[str, object]] | None:
    if not isinstance(cached, dict):
        return None
    if cached.get("report") != kind or cached.get("checksums") != checksums:
        return None
    dates = cached.get("dates")
    if not isinstance(dates, dict):
        return None
    return dates


def cmd_init(
    config: Config,
    session: requests.Session,
    *,
    snapshot: bool,
    sleeper: Callable[[float], None] = time.sleep,
) -> Result:
    with locked(config.data_dir):
        client = _client(config, session, sleeper)
        existing = client.list_requests()
        lines: list[str] = []
        active = [item for item in existing if item["access_type"] == "ONGOING" and not item["stopped"]]
        stopped = [item for item in existing if item["access_type"] == "ONGOING" and item["stopped"]]
        if active:
            lines.append(f"已存在 ONGOING 请求 {active[0]['id']}，未重复创建。")
        else:
            created = client.create_request("ONGOING")
            if created is None:
                existing = client.list_requests()
                active = [item for item in existing if item["access_type"] == "ONGOING" and not item["stopped"]]
                if not active:
                    raise AscError("创建 ONGOING 请求时发生冲突，但没有找到可用请求。")
                lines.append(f"ONGOING 请求已存在 {active[0]['id']}，未重复创建。")
            else:
                if stopped:
                    lines.append("已有因长时间未下载而停止的 ONGOING 请求，已另外创建新的请求。")
                lines.append(f"已创建 ONGOING 请求 {created}。第一份报表通常需要 1–2 天。")
        if snapshot:
            snaps = [item for item in existing if item["access_type"] == "ONE_TIME_SNAPSHOT"]
            if snaps:
                lines.append(
                    f"已存在 ONE_TIME_SNAPSHOT 请求 {snaps[0]['id']}，未重复创建。"
                    "每月只能发起一次；本工具不删除旧请求。"
                )
            else:
                created = client.create_request("ONE_TIME_SNAPSHOT")
                if created is None:
                    refreshed = client.list_requests()
                    snaps = [item for item in refreshed if item["access_type"] == "ONE_TIME_SNAPSHOT"]
                    if not snaps:
                        raise AscError("创建 ONE_TIME_SNAPSHOT 时发生冲突，但没有找到已有请求。")
                    lines.append(
                        f"已存在 ONE_TIME_SNAPSHOT 请求 {snaps[0]['id']}，未重复创建。"
                        "每月只能发起一次；本工具不删除旧请求。"
                    )
                else:
                    lines.append(
                        f"已创建 ONE_TIME_SNAPSHOT 请求 {created}。"
                        "快照只覆盖请求当时能取得的历史，不会继续生成之后的新数据。"
                    )
        else:
            lines.append("未请求 ONE_TIME_SNAPSHOT。需要历史报表时再执行 init --snapshot。")
        lines.append("run 只读取和下载，不会创建请求。")
        return Result(0, "\n".join(lines))


def _matching_reports(reports: list[dict[str, str]], spec: dict[str, str]) -> list[dict[str, str]]:
    expected = canon_name(spec["name"])
    return [
        report
        for report in reports
        if canon_name(report["name"]) == expected and report["category"] == spec["category"]
    ]


def cmd_run(
    config: Config,
    session: requests.Session,
    *,
    dry_run: bool,
    sleeper: Callable[[float], None] = time.sleep,
) -> Result:
    with locked(config.data_dir):
        state_path = config.data_dir / "state.json"
        state = load_state(state_path)
        changed = False
        try:
            result = _run_locked(config, session, state, dry_run=dry_run, sleeper=sleeper, mark_changed=lambda: _mark(state))
            changed = True
        except AscError:
            if state.get("_dirty"):
                state.pop("_dirty", None)
                save_state(state_path, state)
            raise
        state.pop("_dirty", None)
        if changed:
            save_state(state_path, state)
        return result


def _mark(state: dict) -> None:
    state["_dirty"] = True


def _run_locked(
    config: Config,
    session: requests.Session,
    state: dict,
    *,
    dry_run: bool,
    sleeper: Callable[[float], None],
    mark_changed: Callable[[], None],
) -> Result:
    client = _client(config, session, sleeper)
    requests_found = client.list_requests()
    if not requests_found:
        return Result(0, "还没有分析报表请求。请先执行 init。本次不推送。")

    found: dict[str, list[dict[str, object]]] = {spec["kind"]: [] for spec in REPORT_SPECS}
    category_reports: dict[str, list[dict[str, str]]] = {spec["kind"]: [] for spec in REPORT_SPECS}
    for request in requests_found:
        for spec in REPORT_SPECS:
            reports = client.list_reports(str(request["id"]), spec["category"])
            category_reports[spec["kind"]].extend(reports)
            for report in _matching_reports(reports, spec):
                instances = client.list_instances(report["id"])
                for instance in instances:
                    if instance["granularity"] != "DAILY":
                        continue
                    found[spec["kind"]].append(
                        {
                            "id": instance["id"],
                            "processing_date": instance["processing_date"],
                            "access_type": request["access_type"],
                            "kind": spec["kind"],
                        }
                    )

    for spec in REPORT_SPECS:
        reports = category_reports[spec["kind"]]
        if reports and not found[spec["kind"]] and not _matching_reports(reports, spec):
            raise AscError(
                f"未找到报表“{spec['name']}”（{spec['category']}）。"
                f"该分类返回的报表：{_names(reports)}。"
                "没有改用 Detailed 报表。"
            )
    if any(not category_reports[spec["kind"]] for spec in REPORT_SPECS):
        return Result(0, "报表请求已存在，但标准日报还没有列出。首次请求后通常需要 1–2 天。本次不推送。")
    if any(not found[spec["kind"]] for spec in REPORT_SPECS):
        return Result(0, "标准日报已找到，但还没有 DAILY 实例。首次请求后通常需要 1–2 天。本次不推送。")

    notes: list[str] = []
    usable: list[dict[str, object]] = []
    for spec in REPORT_SPECS:
        group = found[spec["kind"]]
        newest = max(date.fromisoformat(str(item["processing_date"])) for item in group)
        newest_group = [item for item in group if date.fromisoformat(str(item["processing_date"])) == newest]
        prepared = []
        for item in group:
            segments = client.list_segments(str(item["id"]))
            status, ready = _inspect_segments(segments)
            prepared.append((item, status, ready))
        if any(status != "ready" for item, status, _ready in prepared if item in newest_group):
            return Result(0, "最新的每日实例还没有可下载分段。报表生成会晚于事件日，本次不推送。")
        skipped_old = False
        for item, status, ready in prepared:
            if status != "ready":
                skipped_old = True
                continue
            item["segments"] = ready
            usable.append(item)
        if skipped_old:
            notes.append("部分较旧实例还没有下载地址，已跳过；没有把这些缺口补成 0。")

    seen: set[str] = set()
    for item in usable:
        instance_id = str(item["id"])
        seen.add(instance_id)
        kind = str(item["kind"])
        segments = list(item["segments"])  # type: ignore[arg-type]
        checksums = sorted(str(segment["checksum"]).lower() for segment in segments)
        cached = _cache_dates(state["instances"].get(instance_id), kind, checksums)
        if cached is None:
            blobs = [client.download_verified(segment) for segment in segments]
            cached = aggregate_segments(kind, blobs, config.app_id)
            mark_changed()
        state["instances"][instance_id] = {
            "report": kind,
            "processing_date": item["processing_date"],
            "access_type": item["access_type"],
            "checksums": checksums,
            "dates": cached,
        }
        item["dates"] = cached

    state["instances"] = {key: value for key, value in state["instances"].items() if key in seen}
    winners = {
        spec["kind"]: select_dates([item for item in usable if item["kind"] == spec["kind"]])
        for spec in REPORT_SPECS
    }
    day = latest_common_day(winners["downloads"], winners["discovery"])
    prefix = ("\n".join(notes) + "\n") if notes else ""
    if day is None:
        return Result(
            0,
            prefix
            + "没有共同事件日。"
            + f"下载报表有 {len(winners['downloads'])} 个事件日，发现报表有 {len(winners['discovery'])} 个事件日。"
            + "本次不推送，也不把缺失的一天补成 0。",
        )
    summary = build_summary(config.app_id, day, winners["downloads"][day], winners["discovery"][day])
    if not has_any_metric(summary):
        return Result(
            0,
            prefix
            + f"事件日 {day} 在两份日报里都有行，但没有可汇总的首次下载、重新下载、展示或产品页浏览。"
            + "缺失不是 0。本次不推送。",
        )
    fingerprint = summary_fingerprint(summary)
    pushed = state["pushed"].get(day)
    if isinstance(pushed, dict) and pushed.get("fingerprint") == fingerprint:
        return Result(0, prefix + f"事件日 {day} 的汇总已推送，内容没有变化。本次不推送。")
    body = format_summary(summary)
    title = format_title(summary)
    if dry_run:
        return Result(0, prefix + "dry-run：未调用 Bark。\n" + body)
    if not config.bark_key:
        raise AscError("缺少 BARK_KEY。", code=2)
    try:
        push_bark(session, config.bark_server, config.bark_key, title, body, sleeper)
    except AscError:
        mark_changed()
        raise
    state["pushed"][day] = {"fingerprint": fingerprint, "pushed_at": now_iso()}
    mark_changed()
    return Result(0, prefix + "已推送。\n" + title + "\n" + body)


def cmd_test_notification(
    config: Config,
    session: requests.Session,
    *,
    sleeper: Callable[[float], None] = time.sleep,
) -> Result:
    if not config.bark_key:
        raise AscError("缺少 BARK_KEY。", code=2)
    push_bark(
        session,
        config.bark_server,
        config.bark_key,
        "asc-bark 测试",
        "这是一条测试通知，不包含 App Store 数据。",
        sleeper,
    )
    return Result(0, "已发送测试通知。")


def _known_secrets(env: Mapping[str, str]) -> list[str]:
    secrets = []
    bark = (env.get("BARK_KEY") or "").strip()
    if len(bark) >= 8:
        secrets.append(bark)
    path_value = (env.get("ASC_PRIVATE_KEY_PATH") or "").strip()
    if path_value:
        path = Path(path_value)
        try:
            if path.is_file() and path.stat().st_size <= 100_000:
                text = path.read_text(encoding="utf-8")
                if "PRIVATE KEY" in text and len(text) >= 8:
                    secrets.append(text)
        except OSError:
            pass
    return secrets


@dataclass(frozen=True)
class Schedule:
    """A five-field cron schedule.

    ``day_any`` and ``weekday_any`` are true only when that field is ``*``.
    When both day-of-month and day-of-week are restricted, a date matches if
    either field matches (standard cron). ``weekdays`` uses Sunday = 0.
    """

    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    day_any: bool
    weekday_any: bool
    expression: str


def _parse_cron_field(token: str, low: int, high: int) -> frozenset[int]:
    values: set[int] = set()
    for part in token.split(","):
        match = re.fullmatch(r"(\*|\d{1,2}(?:-\d{1,2})?)(?:/(\d{1,4}))?", part)
        if not match:
            raise AscError(f"ASC_SCHEDULE 无法解析“{token}”。{SCHEDULE_HINT}", code=2)
        base, step_text = match.group(1), match.group(2)
        step = 1
        if step_text is not None:
            step = int(step_text)
            if step <= 0:
                raise AscError("ASC_SCHEDULE 的步长必须大于 0。", code=2)
        if base == "*":
            start, end = low, high
        elif "-" in base:
            left, right = base.split("-", 1)
            start, end = int(left), int(right)
            if start > end:
                raise AscError(f"ASC_SCHEDULE 的范围无效：{part}。", code=2)
        else:
            start = end = int(base)
            if step_text is not None:
                end = high
        if start < low or end > high:
            raise AscError(f"ASC_SCHEDULE 字段超出 {low}-{high}：{part}。", code=2)
        values.update(range(start, end + 1, step))
    if not values:
        raise AscError(f"ASC_SCHEDULE 字段没有可用的值：{token}。", code=2)
    return frozenset(values)


def parse_schedule(value: str) -> Schedule:
    text = " ".join(value.strip().split())
    if not text:
        raise AscError(f"ASC_SCHEDULE 不能为空。{SCHEDULE_HINT}", code=2)
    daily = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if daily:
        hour, minute = int(daily.group(1)), int(daily.group(2))
        if hour > 23 or minute > 59:
            raise AscError(f"ASC_SCHEDULE 的时刻无效：{text}。{SCHEDULE_HINT}", code=2)
        fields = [str(minute), str(hour), "*", "*", "*"]
    else:
        fields = text.split(" ")
        if len(fields) != 5:
            raise AscError(SCHEDULE_HINT, code=2)
    weekdays = set(_parse_cron_field(fields[4], 0, 7))
    if 7 in weekdays:
        weekdays.discard(7)
        weekdays.add(0)
    return Schedule(
        minutes=_parse_cron_field(fields[0], 0, 59),
        hours=_parse_cron_field(fields[1], 0, 23),
        days=_parse_cron_field(fields[2], 1, 31),
        months=_parse_cron_field(fields[3], 1, 12),
        weekdays=frozenset(weekdays),
        day_any=fields[2] == "*",
        weekday_any=fields[4] == "*",
        expression=" ".join(fields),
    )


def schedule_from_env(env: Mapping[str, str]) -> Schedule:
    raw = (env.get("ASC_SCHEDULE") or "").strip()
    return parse_schedule(raw or DEFAULT_SCHEDULE)


def timezone_from_env(env: Mapping[str, str]) -> ZoneInfo:
    name = (env.get("TZ") or "").strip() or DEFAULT_TIMEZONE
    try:
        return ZoneInfo(name)
    except Exception:
        raise AscError(f"时区无法识别：{name}。", code=2) from None


def _cron_weekday(day: date) -> int:
    """Cron weekday: Sunday is 0 and Saturday is 6."""

    return (day.weekday() + 1) % 7


def _calendar_day_matches(schedule: Schedule, day: date) -> bool:
    dom_ok = day.day in schedule.days
    dow_ok = _cron_weekday(day) in schedule.weekdays
    if schedule.day_any and schedule.weekday_any:
        return True
    if schedule.day_any:
        return dow_ok
    if schedule.weekday_any:
        return dom_ok
    return dom_ok or dow_ok


def _is_real_wall_time(candidate: datetime) -> bool:
    tz = candidate.tzinfo
    if tz is None:
        return False
    try:
        back = candidate.astimezone(timezone.utc).astimezone(tz)
    except (OSError, OverflowError, ValueError):
        return False
    return (
        back.year == candidate.year
        and back.month == candidate.month
        and back.day == candidate.day
        and back.hour == candidate.hour
        and back.minute == candidate.minute
        and back.fold == candidate.fold
    )


def _wall_instants(year: int, month: int, day: int, hour: int, minute: int, tz: ZoneInfo) -> list[datetime]:
    found: list[datetime] = []
    seen: set[float] = set()
    for fold in (0, 1):
        try:
            candidate = datetime(year, month, day, hour, minute, tzinfo=tz, fold=fold)
        except (OverflowError, ValueError):
            continue
        if not _is_real_wall_time(candidate):
            continue
        stamp = candidate.timestamp()
        if stamp in seen:
            continue
        seen.add(stamp)
        found.append(candidate)
    return found


def next_occurrence(schedule: Schedule, now: datetime, tz: ZoneInfo) -> datetime:
    """Return the next scheduled instant strictly after ``now``.

    Missing local times (daylight-saving gaps) are skipped. A repeated local
    time yields both instants, and the earlier one still in the future wins.
    """

    if now.tzinfo is None:
        local_now = now.replace(tzinfo=tz)
    else:
        local_now = now.astimezone(tz)
    cursor = local_now.date()
    try:
        horizon = cursor.replace(year=cursor.year + SCHEDULE_HORIZON_YEARS)
    except ValueError:
        horizon = date(cursor.year + SCHEDULE_HORIZON_YEARS, 3, 1)
    while cursor <= horizon:
        if cursor.month in schedule.months and _calendar_day_matches(schedule, cursor):
            instants: list[datetime] = []
            for hour in sorted(schedule.hours):
                for minute in sorted(schedule.minutes):
                    instants.extend(
                        _wall_instants(cursor.year, cursor.month, cursor.day, hour, minute, tz)
                    )
            instants.sort(key=lambda item: item.timestamp())
            # Same tzinfo compares wall time and ignores fold, so use timestamps.
            now_stamp = local_now.timestamp()
            for instant in instants:
                if instant.timestamp() > now_stamp:
                    return instant
        cursor += timedelta(days=1)
    raise AscError("ASC_SCHEDULE 在未来 8 年内没有匹配的时间。", code=2)


def install_shutdown(
    poll_seconds: float = SHUTDOWN_POLL_SECONDS,
) -> tuple[Callable[[], bool], Callable[[float], bool], Callable[[], None]]:
    """Catch SIGTERM and SIGINT. ``wait`` returns True when stopping.

    The wait is polled so ``docker stop`` is noticed even when the main thread
    is blocked. Handlers are restored by the returned function.
    """

    flag = threading.Event()

    def handler(signum: int, frame: object) -> None:
        flag.set()

    previous: list[tuple[signal.Signals, signal.Handlers]] = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        previous.append((sig, signal.getsignal(sig)))
        signal.signal(sig, handler)

    def restore() -> None:
        for sig, old in previous:
            signal.signal(sig, old)

    def wait(seconds: float) -> bool:
        if seconds < 0:
            seconds = 0.0
        deadline = time.monotonic() + seconds
        while True:
            if flag.is_set():
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if flag.wait(min(poll_seconds, remaining)):
                return True

    return flag.is_set, wait, restore


def serve_loop(
    schedule: Schedule,
    tz: ZoneInfo,
    run_once: Callable[[], Result],
    *,
    clock: Callable[[], datetime],
    wait: Callable[[float], bool],
    secrets: list[str] | None = None,
) -> int:
    """Wait for the schedule, run once, and repeat until ``wait`` asks to stop.

    A successful result, a non-zero result, ``AscError``, and any other
    exception are logged. None of them ends the loop. The data-directory lock
    inside a normal run still rejects a second fetch while one is in progress.
    This function returns 0 only after shutdown.
    """

    hidden = secrets or []
    while True:
        now = clock()
        nxt = next_occurrence(schedule, now, tz)
        anchor = now.replace(tzinfo=tz) if now.tzinfo is None else now.astimezone(tz)
        # Subtraction keeps the wall clock when both values share a tzinfo,
        # which collapses a repeated daylight-saving hour to a zero delay.
        delay = nxt.timestamp() - anchor.timestamp()
        if delay < 0:
            delay = 0.0
        emit(f"下次运行：{nxt.isoformat(timespec='seconds')}", hidden)
        if wait(delay):
            return 0
        try:
            result = run_once()
        except AscError as exc:
            emit(f"本次运行失败，将等到下一个日程。{exc}", hidden, err=True)
            continue
        except Exception:
            emit("本次运行失败，将等到下一个日程。", hidden, err=True)
            emit(redact(traceback.format_exc(), hidden), hidden, err=True)
            continue
        emit(result.message, hidden, err=result.code != 0)
        if result.code != 0:
            emit("本次未成功，将等到下一个日程。", hidden, err=True)


def cmd_serve(
    config: Config,
    session: requests.Session,
    *,
    schedule: Schedule,
    tz: ZoneInfo,
    sleeper: Callable[[float], None] = time.sleep,
    clock: Callable[[], datetime] | None = None,
    wait: Callable[[float], bool] | None = None,
    runner: Callable[[], Result] | None = None,
) -> Result:
    def default_clock() -> datetime:
        return datetime.now(tz)

    def default_run() -> Result:
        return cmd_run(config, session, dry_run=False, sleeper=sleeper)

    use_clock = clock or default_clock
    use_run = runner or default_run
    # Reject a schedule that can never fire before sleeping.
    next_occurrence(schedule, use_clock(), tz)
    emit(
        f"按日程运行：{schedule.expression}，时区 {tz.key}。"
        "启动后先等待下一次，不会立刻抓取。"
        "成功或失败都会继续等待；停止容器时，进行中的这一轮会做完再退出。"
    )

    def noop() -> None:
        return None

    restore = noop
    use_wait = wait
    if use_wait is None:
        _stopped, use_wait, restore = install_shutdown()
    try:
        serve_loop(
            schedule,
            tz,
            use_run,
            clock=use_clock,
            wait=use_wait,
            secrets=config.secret_values(),
        )
    finally:
        restore()
    return Result(0, "已停止。")


def main(
    argv: list[str] | None = None,
    environ: Mapping[str, str] | None = None,
    session: requests.Session | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    *,
    clock: Callable[[], datetime] | None = None,
    wait: Callable[[float], bool] | None = None,
    runner: Callable[[], Result] | None = None,
) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    env = os.environ if environ is None else environ
    secrets = _known_secrets(env)
    http = session if session is not None else requests.Session()
    try:
        if args.command == "serve":
            schedule = schedule_from_env(env)
            tz = timezone_from_env(env)
            config = load_config(env, need_apple=True, need_bark=True)
            result = cmd_serve(
                config,
                http,
                schedule=schedule,
                tz=tz,
                sleeper=sleeper,
                clock=clock,
                wait=wait,
                runner=runner,
            )
        elif args.command == "test-notification":
            config = load_config(env, need_apple=False, need_bark=True)
            result = cmd_test_notification(config, http, sleeper=sleeper)
        else:
            dry_run = bool(getattr(args, "dry_run", False))
            config = load_config(env, need_apple=True, need_bark=args.command == "run" and not dry_run)
            if args.command == "init":
                result = cmd_init(config, http, snapshot=bool(args.snapshot), sleeper=sleeper)
            else:
                result = cmd_run(config, http, dry_run=dry_run, sleeper=sleeper)
        secrets = list(dict.fromkeys([*secrets, *config.secret_values()]))
    except AscError as exc:
        emit(str(exc), secrets, err=True)
        return exc.code
    except Exception:
        emit(redact(traceback.format_exc(), secrets), secrets, err=True)
        return 1
    emit(result.message, secrets, err=result.code != 0)
    return result.code


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="asc-bark",
        description="读取 App Store Connect 分析报表的最近一个共同事件日，并推送到 Bark。",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="创建 ONGOING 请求；可用 --snapshot 追加一次性历史快照")
    init.add_argument("--snapshot", action="store_true", help="若还没有 ONE_TIME_SNAPSHOT，则一并创建")
    run = commands.add_parser("run", help="读取最新共同事件日；汇总有变化时推送 Bark")
    run.add_argument("--dry-run", action="store_true", help="只读取并打印汇总，不推送 Bark")
    commands.add_parser(
        "serve",
        help="按 ASC_SCHEDULE 常驻运行；到点执行与 run 相同的抓取和推送",
    )
    commands.add_parser("test-notification", help="只发送一条 Bark 测试通知，不读取 Apple")
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
