"""Continuous, conservative county lead ingestion. See README before enabling."""
from __future__ import annotations

import json
import logging
import os
import random
import re
import signal
import threading
import time
import unicodedata
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup
from supabase import create_client

LOG = logging.getLogger("lead_agent")
STOP = threading.Event()


def address_key(address: str) -> str:
    # Intentionally retain unit numbers and street suffixes; false merges lose leads.
    return re.sub(r"[^a-z0-9]+", " ", unicodedata.normalize("NFKC", address).casefold()).strip()


def eligible(row: dict, horizon: int, today: date | None = None) -> bool:
    today = today or date.today()
    if row.get("use", "").strip().casefold() not in {"commercial", "industrial", "mixed use", "mixed-use"}:
        return False
    if not row.get("address", "").strip() or not row.get("record_id", "").strip():
        return False
    if row.get("kind") == "violation":
        return row.get("status", "").strip().casefold() in {"active", "open"}
    if row.get("kind") == "backflow":
        try:
            return 0 <= (date.fromisoformat(row["deadline"]) - today).days <= horizon
        except (KeyError, TypeError, ValueError):
            return False
    return False


class Portal:
    def __init__(self, config: dict, client: httpx.Client):
        self.config, self.client = config, client
        self.last_request = 0.0
        self.robots: dict[str, RobotFileParser] = {}
        self.agent = random.choice([
            "CommercialLeadMonitor/1.0 (+county-public-records; profile=A)",
            "CommercialLeadMonitor/1.0 (+county-public-records; profile=B)",
        ])

    def request(self, url: str, robots: bool = True) -> httpx.Response:
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.hostname not in self.config["allowed_hosts"] or parts.username or parts.password:
            raise ValueError("Source URL must use HTTPS and an explicitly allowed host")
        origin = f"https://{parts.netloc}"
        if robots:
            if origin not in self.robots:
                response = self.request(origin + "/robots.txt", robots=False)
                rules = RobotFileParser()
                rules.parse(response.text.splitlines() if response.status_code != 404 else [])
                self.robots[origin] = rules
            if not self.robots[origin].can_fetch("CommercialLeadMonitor", url):
                raise RuntimeError("Source disallows automated access in robots.txt")
        for attempt in range(4):
            delay = max(0, float(self.config.get("request_delay_seconds", 5)) - (time.monotonic() - self.last_request))
            if STOP.wait(delay):
                raise InterruptedError("Shutdown requested")
            self.last_request = time.monotonic()
            try:
                response = self.client.get(url, headers={"User-Agent": self.agent})
                if response.status_code == 404 and not robots:
                    return response
                if response.status_code in {401, 403}:
                    raise RuntimeError("Portal denied access; manual access review required")
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt == 3:
                        response.raise_for_status()
                    retry = response.headers.get("Retry-After", "")
                    delay = min(float(retry), 900) if retry.isdigit() else 2 ** attempt * 5 + random.random()
                    if STOP.wait(delay):
                        raise InterruptedError("Shutdown requested")
                    continue
                response.raise_for_status()
                return response
            except (httpx.TimeoutException, httpx.NetworkError):
                if attempt == 3:
                    raise
                if STOP.wait(2 ** attempt * 5 + random.random()):
                    raise InterruptedError("Shutdown requested")
        raise RuntimeError("Request retries exhausted")

    def rows(self):
        url = self.config["url"]
        visited = set()
        for _ in range(int(self.config.get("max_pages", 100))):
            if url in visited:
                raise RuntimeError("Portal pagination loop detected")
            visited.add(url)
            response = self.request(url)
            if self.config["format"] == "json":
                payload = response.json()
                items = payload[self.config["items_key"]]
                if not isinstance(items, list):
                    raise ValueError("Expected a JSON list; source schema may have changed")
                for item in items:
                    yield {key: str(item.get(column) or "").strip() for key, column in self.config["fields"].items()}
                next_url = payload.get(self.config.get("next_key", "next"))
            else:
                soup = BeautifulSoup(response.text, "html.parser")
                if not soup.select_one(self.config["container_selector"]):
                    raise ValueError("Expected result container absent; portal markup may have changed")
                for item in soup.select(self.config["row_selector"]):
                    yield {key: (node.get_text(" ", strip=True) if (node := item.select_one(selector)) else "") for key, selector in self.config["fields"].items()}
                node = soup.select_one(self.config["next_selector"]) if self.config.get("next_selector") else None
                next_url = node.get("href") if node else None
            if not next_url:
                return
            url = urljoin(url, next_url)
        raise RuntimeError("Page limit reached; collection incomplete")


class Store:
    def __init__(self, client):
        self.client = client

    def insert_new(self, row: dict, source: str) -> bool:
        key = address_key(row["address"])
        if not key:
            return False
        existing = self.client.table("commercial_leads").select("id").eq("address_key", key).limit(1).execute()
        if existing.data:
            return False
        # Atomic unique-key conflict handling protects against concurrent collectors.
        result = self.client.table("commercial_leads").upsert({
            "address": row["address"], "address_key": key, "status": "New",
            "source": source, "source_record_id": row["record_id"],
            "lead_type": row["kind"], "deadline": row.get("deadline") or None,
        }, on_conflict="address_key", ignore_duplicates=True).execute()
        return bool(result.data)


def flight(config: dict, store: Store, client: httpx.Client) -> dict:
    counts = {"sources_ok": 0, "sources_failed": 0, "examined": 0, "eligible": 0, "inserted": 0}
    for source in config["sources"]:
        try:
            for row in Portal(source, client).rows():
                counts["examined"] += 1
                if eligible(row, config.get("deadline_horizon_days", 90)):
                    counts["eligible"] += 1
                    counts["inserted"] += int(store.insert_new(row, source["name"]))
            counts["sources_ok"] += 1
        except Exception as exc:
            # Never log response bodies, addresses, URLs, or credentials.
            counts["sources_failed"] += 1
            LOG.error("source_failed name=%s error_type=%s", source["name"], type(exc).__name__)
    return counts


def load_config(path: str) -> dict:
    raw = os.environ.get("PORTAL_CONFIG_JSON")
    config = json.loads(raw if raw else Path(path).read_text())
    sources = config.get("sources", [])
    if {s.get("name") for s in sources} != {"code_enforcement", "building_permits"} or len(sources) != 2:
        raise ValueError("Exactly code_enforcement and building_permits sources are required")
    for source in sources:
        if not source.get("verified"):
            raise ValueError("Live portal adapter must be verified before enabling collection")
        if source.get("format") not in {"json", "html"}:
            raise ValueError("Source format must be json or html")
        if not {"address", "record_id", "kind", "use", "status"} <= source.get("fields", {}).keys():
            raise ValueError("Source fields must include address, record_id, kind, use, status")
    return config


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = load_config(os.environ.get("PORTAL_CONFIG", "portals.json"))
    url = os.environ["SUPABASE_URL"]
    if urlsplit(url).scheme != "https":
        raise ValueError("SUPABASE_URL must use HTTPS")
    store = Store(create_client(url, os.environ["SUPABASE_SERVICE_ROLE_KEY"]))
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: STOP.set())
    state = Path(os.environ.get("STATE_DIR", "state"))
    state.mkdir(parents=True, exist_ok=True)
    interval = max(300, int(os.environ.get("POLL_INTERVAL_SECONDS", "21600")))
    with httpx.Client(timeout=httpx.Timeout(60, connect=15), follow_redirects=False) as client:
        while not STOP.is_set():
            counts = flight(config, store, client)
            status = {"finished_at": datetime.now(timezone.utc).isoformat(), **counts}
            temp = state / "health.tmp"
            temp.write_text(json.dumps(status))
            temp.replace(state / "health.json")
            LOG.info("flight_complete %s", json.dumps(counts))
            if os.environ.get("RUN_ONCE") == "1":
                return 1 if counts["sources_failed"] else 0
            STOP.wait(interval + random.uniform(0, min(60, interval * .05)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
