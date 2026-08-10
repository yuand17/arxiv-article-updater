"""Bounded, SSRF-safe discovery of official journal update endpoints."""

import ipaddress
import json
import re
import socket
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from urllib.parse import urljoin, urlparse

import feedparser
import httpx
from bs4 import BeautifulSoup

from ..journal_network import get_journal_network
from ..sources.journals import JournalAdapter, JournalFeed, parse_journal_feed
from .article_classification import classify_journal_candidate, infer_journal_scope

DISCOVERY_VERSION = "journal-discovery-v1"
MAX_RESPONSE_BYTES = 2_000_000
MAX_DISCOVERY_PAGES = 20
MAX_DISCOVERY_DEPTH = 2
CROSSREF_JOURNALS_URL = "https://api.crossref.org/journals"
LINK_HINTS = ("rss", "atom", "feed", "latest", "recent", "articles", "research")


class JournalDiscoveryError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class DiscoveredEndpoint:
    kind: str
    url: str
    priority: int


@dataclass(frozen=True, slots=True)
class PreviewPaper:
    title: str
    authors: str
    published_at: str


@dataclass(slots=True)
class JournalDiscoveryPreview:
    token: str
    name: str
    homepage_url: str
    canonical_domain: str
    issn_online: str
    issn_print: str
    scope_kind: str
    endpoints: list[DiscoveredEndpoint]
    scanned_count: int
    nonresearch_filtered: int
    nonphysics_filtered: int
    papers: list[PreviewPaper]
    warnings: list[str] = field(default_factory=list)
    version: str = DISCOVERY_VERSION


def _host_addresses(
    hostname: str,
    resolver=socket.getaddrinfo,
) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        records = resolver(hostname, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise JournalDiscoveryError("期刊官网域名无法解析。") from exc
    addresses = {ipaddress.ip_address(record[4][0]) for record in records}
    if not addresses or any(not address.is_global for address in addresses):
        raise JournalDiscoveryError("期刊官网不能指向本机或私有网络。")
    return addresses


def validate_public_https(
    value: str,
    *,
    resolver=socket.getaddrinfo,
) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise JournalDiscoveryError("期刊官网必须是公开的 HTTPS URL。")
    if parsed.port not in {None, 443}:
        raise JournalDiscoveryError("期刊官网不能使用非标准端口。")
    _host_addresses(parsed.hostname, resolver)
    return parsed._replace(fragment="").geturl()


def _safe_get(
    client: httpx.Client,
    url: str,
    *,
    resolver=socket.getaddrinfo,
) -> httpx.Response:
    current_url = validate_public_https(url, resolver=resolver)
    for _redirect in range(6):
        try:
            response = client.get(
                current_url,
                headers={"User-Agent": "arxiv-updater/0.2 (personal research library)"},
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            raise JournalDiscoveryError(
                "无法连接期刊官网或官方来源，请检查网址后重试。"
            ) from exc
        if response.is_redirect:
            location = response.headers.get("location")
            if not location:
                raise JournalDiscoveryError("期刊来源返回了无目标的重定向。")
            current_url = validate_public_https(
                urljoin(current_url, location), resolver=resolver
            )
            continue
        response.raise_for_status()
        try:
            declared = int(response.headers.get("content-length") or 0)
        except ValueError as exc:
            raise JournalDiscoveryError("期刊来源返回了无效的响应长度。") from exc
        if declared > MAX_RESPONSE_BYTES or len(response.content) > MAX_RESPONSE_BYTES:
            raise JournalDiscoveryError("期刊官网或来源响应过大。")
        return response
    raise JournalDiscoveryError("期刊来源重定向次数过多。")


def _json_ld_objects(soup: BeautifulSoup) -> list[dict]:
    objects: list[dict] = []
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(node.string or "")
        except (TypeError, json.JSONDecodeError):
            continue
        values = payload if isinstance(payload, list) else [payload]
        for value in values:
            if isinstance(value, dict):
                objects.append(value)
                graph = value.get("@graph")
                if isinstance(graph, list):
                    objects.extend(item for item in graph if isinstance(item, dict))
    return objects


def _page_metadata(
    soup: BeautifulSoup, fallback_name: str, homepage_url: str
) -> tuple[str, str, str, str]:
    canonical = soup.find("link", rel=lambda value: value and "canonical" in value)
    canonical_url = (
        urljoin(homepage_url, str(canonical.get("href") or ""))
        if canonical
        else homepage_url
    )
    name = fallback_name.strip()
    issns: list[str] = []
    for payload in _json_ld_objects(soup):
        payload_name = payload.get("name") or payload.get("headline")
        if payload_name and not name:
            name = str(payload_name).strip()
        raw_issn = payload.get("issn")
        if isinstance(raw_issn, list):
            issns.extend(str(value) for value in raw_issn)
        elif raw_issn:
            issns.append(str(raw_issn))
    for node in soup.select('meta[name*="issn" i]'):
        issns.append(str(node.get("content") or ""))
    normalized = []
    for value in issns:
        match = re.search(r"\b\d{4}-[\dXx]{4}\b", value)
        if match and match.group(0).upper() not in normalized:
            normalized.append(match.group(0).upper())
    fallback = name or urlparse(homepage_url).hostname or "期刊"
    online, print_issn = (normalized + ["", ""])[:2]
    return fallback, canonical_url, online, print_issn


def _publisher_endpoints(name: str, homepage_url: str) -> list[DiscoveredEndpoint]:
    parsed = urlparse(homepage_url)
    host = (parsed.hostname or "").lower()
    path_parts = [part for part in parsed.path.split("/") if part]
    endpoints: list[DiscoveredEndpoint] = []
    if host.endswith("nature.com"):
        slug = path_parts[0] if path_parts else "nature"
        endpoints.append(
            DiscoveredEndpoint("rss", f"https://www.nature.com/{slug}.rss", 10)
        )
    normalized_name = " ".join(name.lower().split())
    if host.endswith("aps.org") and (
        "physical review letters" in normalized_name or "prl" in path_parts
    ):
        endpoints.append(
            DiscoveredEndpoint("rss", "https://feeds.aps.org/rss/recent/prl.xml", 10)
        )
    return endpoints


def _html_endpoint_links(soup: BeautifulSoup, homepage_url: str) -> list[DiscoveredEndpoint]:
    results: list[DiscoveredEndpoint] = []
    for node in soup.find_all(["link", "a"], href=True):
        href = urljoin(homepage_url, str(node.get("href") or ""))
        parsed = urlparse(href)
        if parsed.hostname != urlparse(homepage_url).hostname:
            continue
        rel = " ".join(node.get("rel") or [])
        media_type = str(node.get("type") or "").lower()
        text = f"{href} {node.get_text(' ')} {rel} {media_type}".lower()
        if not any(hint in text for hint in LINK_HINTS):
            continue
        kind = "atom" if "atom" in text else "rss"
        priority = 20 if "alternate" in rel or "rss" in media_type or "atom" in media_type else 50
        results.append(DiscoveredEndpoint(kind, href, priority))
    return results


def _bounded_endpoint_candidates(
    client: httpx.Client,
    homepage_url: str,
    homepage_soup: BeautifulSoup,
    *,
    resolver=socket.getaddrinfo,
) -> list[DiscoveredEndpoint]:
    """Follow only hinted same-domain links, with hard depth and page-count bounds."""

    results: list[DiscoveredEndpoint] = []
    queue: list[tuple[str, BeautifulSoup, int]] = [(homepage_url, homepage_soup, 0)]
    visited = {homepage_url}
    while queue:
        page_url, soup, depth = queue.pop(0)
        hinted = _html_endpoint_links(soup, page_url)
        results.extend(hinted)
        if depth >= MAX_DISCOVERY_DEPTH:
            continue
        for endpoint in hinted:
            if endpoint.url in visited or len(visited) >= MAX_DISCOVERY_PAGES:
                continue
            visited.add(endpoint.url)
            try:
                response = _safe_get(client, endpoint.url, resolver=resolver)
            except (httpx.HTTPError, JournalDiscoveryError):
                continue
            if feedparser.parse(response.content).entries:
                results.append(
                    DiscoveredEndpoint(endpoint.kind, str(response.url), endpoint.priority)
                )
            elif "html" in response.headers.get("content-type", "").lower():
                queue.append(
                    (
                        str(response.url),
                        BeautifulSoup(response.text, "html.parser"),
                        depth + 1,
                    )
                )
    return results


def _crossref_match(client: httpx.Client, name: str) -> tuple[str, list[str]] | None:
    try:
        response = client.get(
            CROSSREF_JOURNALS_URL,
            params={"query": name, "rows": 5, "select": "title,ISSN"},
            headers={"User-Agent": "arxiv-updater/0.2 (mailto:local@localhost)"},
        )
        response.raise_for_status()
        items = response.json().get("message", {}).get("items", [])
    except (httpx.HTTPError, ValueError, AttributeError):
        return None
    best: tuple[float, str, list[str]] | None = None
    for item in items:
        title = str(item.get("title") or "").strip()
        score = SequenceMatcher(None, name.lower(), title.lower()).ratio()
        issns = [str(value) for value in item.get("ISSN") or []]
        if best is None or score > best[0]:
            best = score, title, issns
    if best and best[0] >= 0.65:
        return best[1], best[2]
    return None


def discover_journal(
    name: str,
    homepage_url: str,
    *,
    client: httpx.Client | None = None,
    resolver=None,
) -> JournalDiscoveryPreview:
    if not name.strip():
        raise JournalDiscoveryError("请填写期刊名称。")
    if client is None and resolver is None:
        network = get_journal_network()
        client = network.client
        resolver = network.resolver
    else:
        client = client or httpx.Client(timeout=20, follow_redirects=True, trust_env=False)
        resolver = resolver or socket.getaddrinfo
    homepage_url = validate_public_https(homepage_url, resolver=resolver)
    homepage = _safe_get(client, homepage_url, resolver=resolver)
    content_type = homepage.headers.get("content-type", "").lower()
    if "html" not in content_type and not feedparser.parse(homepage.content).entries:
        raise JournalDiscoveryError("期刊官网没有返回可识别的 HTML 或 feed。")
    soup = BeautifulSoup(homepage.text, "html.parser")
    canonical_name, canonical_url, issn_online, issn_print = _page_metadata(
        soup, name, homepage_url
    )
    canonical_url = validate_public_https(canonical_url, resolver=resolver)
    canonical_domain = (urlparse(canonical_url).hostname or "").lower()
    scope_kind = infer_journal_scope(canonical_name)

    publisher_candidates = _publisher_endpoints(canonical_name, canonical_url)
    candidates = list(publisher_candidates)
    if feedparser.parse(homepage.content).entries:
        candidates.append(DiscoveredEndpoint("rss", str(homepage.url), 5))
    deduplicated: dict[str, DiscoveredEndpoint] = {}
    for endpoint in candidates[:MAX_DISCOVERY_PAGES]:
        current = deduplicated.get(endpoint.url)
        if current is None or endpoint.priority < current.priority:
            deduplicated[endpoint.url] = endpoint

    warnings: list[str] = []
    crossref = _crossref_match(client, canonical_name)
    if crossref:
        crossref_name, crossref_issns = crossref
        if SequenceMatcher(None, canonical_name.lower(), crossref_name.lower()).ratio() < 0.65:
            raise JournalDiscoveryError("Crossref 期刊名称与官网明显冲突。")
        if (issn_online or issn_print) and crossref_issns and not (
            {issn_online, issn_print} & set(crossref_issns)
        ):
            raise JournalDiscoveryError("Crossref ISSN 与官网明显冲突。")
        missing_issns = [
            value for value in crossref_issns if value not in {issn_online, issn_print}
        ]
        if not issn_online and missing_issns:
            issn_online = missing_issns.pop(0)
        if not issn_print and missing_issns:
            issn_print = missing_issns.pop(0)
        crossref_issn = issn_online or issn_print
        if crossref_issn:
            crossref_url = f"https://api.crossref.org/journals/{crossref_issn}/works"
            deduplicated.setdefault(
                crossref_url,
                DiscoveredEndpoint("crossref", crossref_url, 80),
            )
    else:
        warnings.append("Crossref 暂时无法完成交叉验证，已保留官网证据。")

    accepted_endpoints: list[DiscoveredEndpoint] = []
    accepted_papers: list[PreviewPaper] = []
    parsed_any = False
    scanned = nonresearch = nonphysics = 0
    attempted_endpoints: set[str] = set()

    def inspect_endpoints(endpoints: list[DiscoveredEndpoint]) -> None:
        nonlocal parsed_any, scanned, nonresearch, nonphysics
        for endpoint in sorted(endpoints, key=lambda item: item.priority):
            if endpoint.url in attempted_endpoints:
                continue
            attempted_endpoints.add(endpoint.url)
            try:
                feed = JournalFeed(
                    canonical_name,
                    endpoint.url,
                    issn_online or issn_print,
                    endpoint.kind,
                )
                if endpoint.kind == "crossref":
                    parsed = JournalAdapter(feeds=[feed], client=client).fetch(
                        datetime.now(UTC) - timedelta(days=14)
                    )
                else:
                    response = _safe_get(client, endpoint.url, resolver=resolver)
                    parsed = [
                        candidate
                        for candidate in parse_journal_feed(response.text, feed)
                        if candidate.published_at
                        and (candidate.doi or candidate.canonical_url)
                    ]
            except (httpx.HTTPError, JournalDiscoveryError, ValueError):
                continue
            if not parsed:
                continue
            parsed_any = True
            endpoint_papers: list[PreviewPaper] = []
            scanned += len(parsed)
            for candidate in parsed:
                result = classify_journal_candidate(
                    candidate, journal_name=canonical_name, scope_kind=scope_kind
                )
                if not result.is_original_research:
                    nonresearch += 1
                elif not result.is_physics:
                    nonphysics += 1
                else:
                    endpoint_papers.append(
                        PreviewPaper(
                            candidate.title,
                            ", ".join(candidate.authors),
                            candidate.published_at.date().isoformat()
                            if candidate.published_at
                            else "日期未知",
                        )
                    )
            if endpoint_papers:
                accepted_endpoints.append(endpoint)
                accepted_papers.extend(endpoint_papers)
                return

    inspect_endpoints(publisher_candidates)
    if not parsed_any and not accepted_papers:
        discovered_candidates = _bounded_endpoint_candidates(
            client,
            canonical_url,
            soup,
            resolver=resolver,
        )
        for endpoint in discovered_candidates:
            current = deduplicated.get(endpoint.url)
            if current is None or endpoint.priority < current.priority:
                deduplicated[endpoint.url] = endpoint
        inspect_endpoints(list(deduplicated.values()))
    if not parsed_any:
        raise JournalDiscoveryError("没有发现可解析且字段完整的官方期刊来源。")
    if not accepted_papers:
        raise JournalDiscoveryError("已找到来源，但近期条目中没有可确认的物理原创研究论文。")
    return JournalDiscoveryPreview(
        token=str(uuid.uuid4()),
        name=canonical_name,
        homepage_url=canonical_url,
        canonical_domain=canonical_domain,
        issn_online=issn_online,
        issn_print=issn_print,
        scope_kind=scope_kind,
        endpoints=accepted_endpoints,
        scanned_count=scanned,
        nonresearch_filtered=nonresearch,
        nonphysics_filtered=nonphysics,
        papers=accepted_papers[:3],
        warnings=warnings,
    )
