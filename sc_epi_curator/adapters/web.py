"""Bounded, allowlisted, robots-aware HTML crawler for explicit seed sites."""

from __future__ import annotations

import json
import urllib.parse
import urllib.robotparser
from collections import deque
from html.parser import HTMLParser

from ..crawl_models import CrawlError, DiscoveryRecord, stable_id
from ..http import FetchError, HttpClient, validate_url
from ..models import EvidenceStrength
from .common import (
    collapse_space,
    evidence_for_values,
    extract_identifiers,
    merge_identifiers,
)


NON_HTML_SUFFIXES = {
    ".7z",
    ".bam",
    ".bed",
    ".csv",
    ".fastq",
    ".fq",
    ".gz",
    ".h5",
    ".h5ad",
    ".h5mu",
    ".jpg",
    ".jpeg",
    ".pdf",
    ".png",
    ".tar",
    ".tsv",
    ".zip",
}


class PageParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self.links: list[str] = []
        self.meta: dict[str, str] = {}
        self.json_ld: list[str] = []
        self.canonical_url = ""
        self._hidden_depth = 0
        self._in_title = False
        self._in_json_ld = False
        self._json_ld_parts: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        values = {key.lower(): value or "" for key, value in attrs}
        lowered = tag.lower()
        if lowered in {"script", "style", "noscript", "svg"}:
            self._hidden_depth += 1
        if lowered == "title":
            self._in_title = True
        if (
            lowered == "script"
            and values.get("type", "").lower() == "application/ld+json"
        ):
            self._in_json_ld = True
            self._json_ld_parts = []
        if lowered == "a" and values.get("href"):
            self.links.append(urllib.parse.urljoin(self.base_url, values["href"]))
        if lowered == "link" and "canonical" in values.get("rel", "").lower():
            self.canonical_url = urllib.parse.urljoin(
                self.base_url, values.get("href", "")
            )
        if lowered == "meta":
            key = (
                values.get("name")
                or values.get("property")
                or values.get("http-equiv")
            ).lower()
            content = values.get("content", "")
            if key and content:
                self.meta[key] = content

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered == "title":
            self._in_title = False
        if lowered == "script" and self._in_json_ld:
            value = "".join(self._json_ld_parts).strip()
            if value:
                self.json_ld.append(value)
            self._in_json_ld = False
            self._json_ld_parts = []
        if lowered in {"script", "style", "noscript", "svg"}:
            self._hidden_depth = max(0, self._hidden_depth - 1)

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
        if self._in_json_ld:
            self._json_ld_parts.append(data)
        if self._hidden_depth == 0:
            value = collapse_space(data)
            if value:
                self.text_parts.append(value)


class RobotsGate:
    def __init__(self, client: HttpClient, *, fail_open: bool = False) -> None:
        self.client = client
        self.fail_open = fail_open
        self._parsers: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    def allowed(self, url: str) -> bool:
        parsed = urllib.parse.urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self._parsers:
            robots_url = origin + "/robots.txt"
            try:
                text, _receipt = self.client.get_text(
                    robots_url, accept="text/plain"
                )
                parser = urllib.robotparser.RobotFileParser()
                parser.set_url(robots_url)
                parser.parse(text.splitlines())
                self._parsers[origin] = parser
            except FetchError:
                self._parsers[origin] = None
        parser = self._parsers[origin]
        if parser is None:
            return self.fail_open
        return parser.can_fetch(self.client.policy.user_agent, url)


class WebPageAdapter:
    name = "web"

    def __init__(
        self,
        client: HttpClient,
        *,
        seed_urls: list[str],
        max_depth: int = 0,
        robots_fail_open: bool = False,
    ) -> None:
        if not seed_urls:
            raise ValueError("at least one seed URL is required")
        for url in seed_urls:
            validate_url(url, client.policy.allowed_hosts)
        if max_depth < 0 or max_depth > 3:
            raise ValueError("max_depth must be between 0 and 3")
        self.client = client
        self.seed_urls = [self._canonicalize(url) for url in seed_urls]
        self.max_depth = max_depth
        self.robots = RobotsGate(client, fail_open=robots_fail_open)
        self.errors: list[CrawlError] = []

    def discover(self, query: str, limit: int) -> list[DiscoveryRecord]:
        if limit < 1:
            return []
        queue = deque((url, 0) for url in self.seed_urls)
        seen: set[str] = set()
        records: list[DiscoveryRecord] = []
        while queue and len(records) < limit:
            url, depth = queue.popleft()
            if url in seen:
                continue
            seen.add(url)
            if not self.robots.allowed(url):
                continue
            try:
                text, receipt = self.client.get_text(url, accept="text/html")
            except FetchError as error:
                self.errors.append(
                    CrawlError(
                        source=self.name,
                        stage="fetch_page",
                        target=url,
                        error_type=type(error).__name__,
                        message=str(error),
                    )
                )
                continue
            if "html" not in receipt.content_type.lower() and "<html" not in text[:500].lower():
                continue
            parser = PageParser(url)
            parser.feed(text)
            title = collapse_space(" ".join(parser.title_parts)) or url
            visible_text = collapse_space(" ".join(parser.text_parts))[:100_000]
            description = collapse_space(
                parser.meta.get("description")
                or parser.meta.get("og:description")
                or visible_text[:1000]
            )
            identifiers = extract_identifiers(
                title,
                description,
                visible_text,
                *parser.json_ld,
            )
            json_ld_identifiers: dict[str, list[str]] = {}
            for item in parser.json_ld:
                try:
                    parsed_json_ld = json.loads(item)
                except json.JSONDecodeError:
                    continue
                json_ld_identifiers = merge_identifiers(
                    json_ld_identifiers,
                    extract_identifiers(json.dumps(parsed_json_ld)),
                )
            identifiers = merge_identifiers(identifiers, json_ld_identifiers)
            canonical_url = parser.canonical_url or url
            try:
                validate_url(canonical_url, self.client.policy.allowed_hosts)
            except FetchError:
                canonical_url = url
            record_id = stable_id(
                "record", {"source": self.name, "source_id": canonical_url}
            )
            mentioned = [
                value for values in identifiers.values() for value in values
            ]
            evidence = evidence_for_values(
                record_id=record_id,
                claim_key="mentioned_identifier",
                values=mentioned,
                strength=EvidenceStrength.DECLARATIVE,
                source_type="allowlisted_web_page",
                source_ref=canonical_url,
                source_locator="html",
                receipt=receipt,
                method="bounded_html_accession_extraction",
            )
            if not query or query.lower() in visible_text.lower():
                records.append(
                    DiscoveryRecord(
                        record_id=record_id,
                        source=self.name,
                        source_id=canonical_url,
                        canonical_url=canonical_url,
                        title=title,
                        summary=description,
                        identifiers=identifiers,
                        metadata={
                            "depth": depth,
                            "content_type": receipt.content_type,
                            "outbound_link_count": len(parser.links),
                        },
                        evidence=evidence,
                        receipts=[receipt],
                    )
                )
            if depth >= self.max_depth:
                continue
            for link in parser.links:
                candidate = self._crawlable_link(link, url)
                if candidate and candidate not in seen:
                    queue.append((candidate, depth + 1))
        return records

    def _crawlable_link(self, link: str, parent: str) -> str | None:
        parsed = urllib.parse.urlsplit(link)
        if parsed.scheme != "https" or not parsed.hostname:
            return None
        parent_host = urllib.parse.urlsplit(parent).hostname
        if parsed.hostname.lower() != (parent_host or "").lower():
            return None
        try:
            validate_url(link, self.client.policy.allowed_hosts)
        except FetchError:
            return None
        suffix = parsed.path.lower()
        if any(suffix.endswith(item) for item in NON_HTML_SUFFIXES):
            return None
        return self._canonicalize(link)

    @staticmethod
    def _canonicalize(url: str) -> str:
        parsed = urllib.parse.urlsplit(url)
        query = urllib.parse.urlencode(
            sorted(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
        )
        return urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path or "/", query, "")
        )
