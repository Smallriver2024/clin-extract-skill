#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
PubMed search and download script for clinical literature extraction.

This script:
1. Searches PubMed via E-utilities API with a configurable query
2. Splits large searches into date-range chunks to stay under the 10K result limit
3. Downloads article metadata (title, abstract, publication types, MeSH, keywords)
4. Saves results as JSONL shards for downstream processing

Usage:
    python3 pubmed_search.py

Configuration is read from environment variables or defaults.
"""

from __future__ import annotations

import json
import math
import time
import calendar
import urllib.parse
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
import os
from pathlib import Path
from datetime import datetime, date


# =========================
# Configuration
# =========================
# PubMed E-utilities requires an email and tool name
EMAIL = os.environ.get("PUBMED_EMAIL", "your-email@example.com")
TOOL = os.environ.get("PUBMED_TOOL", "clin_extract_downloader")
API_KEY = os.environ.get("PUBMED_API_KEY", "")  # Optional, increases rate limit

# Search query (customize this for your domain)
# Example: clinical prediction models 2010-2026
RAW_QUERY = os.environ.get(
    "PUBMED_QUERY",
    '( "clinical prediction model*"[tiab] OR "prediction model*"[tiab] OR '
    '"predictive model*"[tiab] OR "risk model*"[tiab] OR "risk prediction"[tiab] OR '
    '"prognostic model*"[tiab] OR "diagnostic model*"[tiab] OR nomogram*[tiab] OR '
    '"risk score*"[tiab] ) AND '
    '( clinic*[tiab] OR patient*[tiab] OR disease*[tiab] OR hospital*[tiab] OR medical[tiab] ) AND '
    '2010:2026[dp]'
)

# Exclude review/meta-analysis at the PubMed level
EXCLUDE_REVIEWS = True

# Date range for chunking
SEARCH_START_YEAR = int(os.environ.get("SEARCH_START_YEAR", "2010"))
SEARCH_END_YEAR = int(os.environ.get("SEARCH_END_YEAR", "2026"))

# Output directory (relative to script location)
SCRIPT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = SCRIPT_DIR / "outputs" / "pubmed_search"
JSONL_RECORDS_PER_FILE = int(os.environ.get("JSONL_RECORDS_PER_FILE", "5000"))
MAX_SUBQUERY_COUNT = int(os.environ.get("MAX_SUBQUERY_COUNT", "9800"))

# Rate limiting (conservative defaults for no API key)
BATCH_SIZE = 200
REQUEST_INTERVAL = 0.34  # Seconds between requests (no API key: ~3/sec)
REQUEST_TIMEOUT = 120
MAX_RETRIES = 5
RETRY_BACKOFF_BASE = 3

# PubMed E-utilities base URL
BASE_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

_last_request_time = 0.0


# =========================
# Utilities
# =========================
def log(msg: str) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {msg}", flush=True)


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def save_json(obj, path: Path) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def load_json(path: Path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def rate_limit() -> None:
    """Enforce request interval."""
    global _last_request_time
    elapsed = time.time() - _last_request_time
    if elapsed < REQUEST_INTERVAL:
        time.sleep(REQUEST_INTERVAL - elapsed)
    _last_request_time = time.time()


def safe_request(url: str, timeout: int = REQUEST_TIMEOUT) -> bytes:
    """Make HTTP request with retries and backoff."""
    for attempt in range(MAX_RETRIES):
        try:
            rate_limit()
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = RETRY_BACKOFF_BASE ** attempt
                log(f"HTTP 429, waiting {wait}s (attempt {attempt + 1}/{MAX_RETRIES})")
                time.sleep(wait)
            else:
                log(f"HTTP error {e.code} for URL: {url[:120]}")
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_BACKOFF_BASE ** attempt)
                else:
                    raise
        except Exception as e:
            log(f"Request error: {e} (attempt {attempt + 1}/{MAX_RETRIES})")
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_BACKOFF_BASE ** attempt)
            else:
                raise
    raise RuntimeError(f"Max retries exceeded for URL: {url[:120]}")


# =========================
# PubMed API
# =========================
def build_query(base_query: str) -> str:
    """Optionally add review/meta-analysis exclusion."""
    if EXCLUDE_REVIEWS:
        return f'({base_query}) NOT review[pt] NOT meta-analysis[pt] NOT systematic[sb]'
    return base_query


def get_count(query: str) -> int:
    """Get the total hit count for a query."""
    params = {
        "db": "pubmed",
        "term": query,
        "rettype": "count",
        "retmode": "json",
        "tool": TOOL,
        "email": EMAIL,
    }
    if API_KEY:
        params["api_key"] = API_KEY
    url = f"{BASE_URL}/esearch.fcgi?{urllib.parse.urlencode(params)}"
    data = safe_request(url)
    result = json.loads(data)
    return int(result.get("esearchresult", {}).get("count", "0"))


def search_pmids(query: str, retstart: int = 0, retmax: int = 10000) -> list[str]:
    """Search PubMed and return PMIDs."""
    params = {
        "db": "pubmed",
        "term": query,
        "retstart": retstart,
        "retmax": min(retmax, 10000),
        "retmode": "json",
        "sort": "pub_date",
        "tool": TOOL,
        "email": EMAIL,
    }
    if API_KEY:
        params["api_key"] = API_KEY
    url = f"{BASE_URL}/esearch.fcgi?{urllib.parse.urlencode(params)}"
    data = safe_request(url)
    result = json.loads(data)
    id_list = result.get("esearchresult", {}).get("idlist", [])
    return id_list


def fetch_articles(pmids: list[str]) -> list[dict]:
    """Fetch article metadata for a batch of PMIDs."""
    if not pmids:
        return []

    params = {
        "db": "pubmed",
        "id": ",".join(pmids),
        "retmode": "xml",
        "tool": TOOL,
        "email": EMAIL,
    }
    if API_KEY:
        params["api_key"] = API_KEY
    url = f"{BASE_URL}/efetch.fcgi?{urllib.parse.urlencode(params)}"
    data = safe_request(url)
    root = ET.fromstring(data)

    articles = []
    for article_elem in root.findall(".//PubmedArticle"):
        article = parse_article(article_elem)
        if article:
            articles.append(article)
    return articles


def parse_article(article_elem: ET.Element) -> dict | None:
    """Parse a PubmedArticle XML element into a flat dict."""
    try:
        medline = article_elem.find(".//MedlineCitation")
        if medline is None:
            return None

        pmid_elem = medline.find("PMID")
        pmid = pmid_elem.text if pmid_elem is not None else ""

        article_info = medline.find("Article")
        if article_info is None:
            return None

        title_elem = article_info.find("ArticleTitle")
        title = title_elem.text or "" if title_elem is not None else ""

        abstract_parts = []
        abstract_elem = article_info.find("Abstract")
        if abstract_elem is not None:
            for abt in abstract_elem.findall("AbstractText"):
                label = abt.get("Label", "")
                text = abt.text or ""
                if label:
                    abstract_parts.append(f"{label}: {text}")
                else:
                    abstract_parts.append(text)
        abstract = " ".join(abstract_parts)

        journal_elem = article_info.find("Journal")
        journal_title = ""
        pub_date_str = ""
        if journal_elem is not None:
            j_title = journal_elem.find("Title")
            if j_title is not None and j_title.text:
                journal_title = j_title.text
            # Parse publication date
            j_date = journal_elem.find("JournalIssue/PubDate")
            if j_date is not None:
                year = j_date.findtext("Year", "")
                month = j_date.findtext("Month", "Jan")
                day = j_date.findtext("Day", "01")
                try:
                    month_num = list(calendar.month_abbr).index(month[:3])
                    pub_date_str = f"{year}-{month_num:02d}-{int(day):02d}"
                except (ValueError, AttributeError):
                    pub_date_str = year

        # Publication types
        pub_types = []
        pub_type_list = article_info.find("PublicationTypeList")
        if pub_type_list is not None:
            for pt in pub_type_list.findall("PublicationType"):
                if pt.text:
                    pub_types.append(pt.text)

        # Keywords
        keywords = []
        kw_list = medline.find("KeywordList")
        if kw_list is not None:
            for kw in kw_list.findall("Keyword"):
                if kw.text:
                    keywords.append(kw.text)

        # MeSH terms
        mesh_terms = []
        mesh_list = medline.find("MeshHeadingList")
        if mesh_list is not None:
            for mesh in mesh_list.findall("MeshHeading"):
                desc = mesh.find("DescriptorName")
                if desc is not None and desc.text:
                    mesh_terms.append(desc.text)

        # Authors
        authors = []
        author_list = article_info.find("AuthorList")
        if author_list is not None:
            for auth in author_list.findall("Author"):
                last = auth.findtext("LastName", "")
                first = auth.findtext("ForeName", "")
                if last:
                    authors.append(f"{last} {first}".strip())

        doi = ""
        eid_list = article_info.findall(".//ELocationID")
        for eid in eid_list:
            if eid.get("EIdType") == "doi" and eid.text:
                doi = eid.text
                break

        return {
            "pmid": pmid,
            "title": title,
            "abstract": abstract,
            "journal": journal_title,
            "pub_date": pub_date_str,
            "publication_types": pub_types,
            "keywords": keywords,
            "mesh_terms": mesh_terms,
            "authors": authors,
            "doi": doi,
        }

    except Exception as e:
        log(f"Error parsing article: {e}")
        return None


# =========================
# Main download logic
# =========================
def search_and_download() -> None:
    """Main entry point: search PubMed and download all results."""
    query = build_query(RAW_QUERY)
    log(f"Query: {query[:200]}...")

    total = get_count(query)
    log(f"Total hits: {total}")

    if total == 0:
        log("No results found. Check your query.")
        return

    if total > MAX_SUBQUERY_COUNT:
        log(f"Total ({total}) > {MAX_SUBQUERY_COUNT}, splitting by year/month...")
        download_by_chunks(query, total)
    else:
        log(f"Total ({total}) <= {MAX_SUBQUERY_COUNT}, downloading directly...")
        download_all(query, total)


def download_all(query: str, total: int) -> None:
    """Download all results when under the chunk threshold."""
    ensure_dir(OUTPUT_DIR)
    jsonl_dir = OUTPUT_DIR / "jsonl"
    ensure_dir(jsonl_dir)

    all_pmids = []
    for start in range(0, total, 10000):
        pmids = search_pmids(query, retstart=start, retmax=10000)
        all_pmids.extend(pmids)
        log(f"Retrieved PMIDs: {len(all_pmids)}/{total}")

    save_as_jsonl(all_pmids, jsonl_dir)


def download_by_chunks(query: str, total: int) -> None:
    """Split search by year-month chunks to stay under the result limit."""
    ensure_dir(OUTPUT_DIR)
    jsonl_dir = OUTPUT_DIR / "jsonl"
    ensure_dir(jsonl_dir)

    all_pmids = set()
    for year in range(SEARCH_START_YEAR, SEARCH_END_YEAR + 1):
        for month in range(1, 13):
            chunk_query = f'{query} AND {year}/{month:02d}[dp]'
            try:
                count = get_count(chunk_query)
                if count == 0:
                    continue
                log(f"  {year}-{month:02d}: {count} hits")
                for start in range(0, min(count, MAX_SUBQUERY_COUNT), 10000):
                    pmids = search_pmids(chunk_query, retstart=start, retmax=10000)
                    for p in pmids:
                        all_pmids.add(p)
            except Exception as e:
                # Try even smaller chunks: by day for overflow months
                if "10000" in str(e) or count > MAX_SUBQUERY_COUNT:
                    log(f"  {year}-{month:02d} overflow, splitting by day...")
                    days = calendar.monthrange(year, month)[1]
                    for day in range(1, days + 1):
                        day_query = f'{query} AND {year}/{month:02d}/{day:02d}[dp]'
                        try:
                            day_count = get_count(day_query)
                            if day_count == 0:
                                continue
                            for start in range(0, day_count, 10000):
                                pmids = search_pmids(day_query, retstart=start, retmax=10000)
                                for p in pmids:
                                    all_pmids.add(p)
                        except Exception as e2:
                            log(f"  Error {year}-{month:02d}-{day:02d}: {e2}")
                else:
                    log(f"  Error {year}-{month:02d}: {e}")

            time.sleep(0.1)

    log(f"Total unique PMIDs after chunked search: {len(all_pmids)}")
    save_as_jsonl(sorted(all_pmids), jsonl_dir)


def save_as_jsonl(pmids: list[str], output_dir: Path) -> None:
    """Download article metadata for PMIDs and save as JSONL shards."""
    total = len(pmids)
    shard_idx = 0
    record_idx = 0
    shard_records = []

    for batch_start in range(0, total, BATCH_SIZE):
        batch = pmids[batch_start:batch_start + BATCH_SIZE]
        articles = fetch_articles(batch)
        log(f"Fetched {len(articles)} articles ({batch_start + len(batch)}/{total})")

        for art in articles:
            shard_records.append(art)
            record_idx += 1

            if len(shard_records) >= JSONL_RECORDS_PER_FILE:
                write_shard(shard_records, output_dir, shard_idx)
                shard_records = []
                shard_idx += 1

    # Write final shard
    if shard_records:
        write_shard(shard_records, output_dir, shard_idx)

    log(f"Done. {record_idx} records saved to {shard_idx + 1} shard(s) in {output_dir}")


def write_shard(records: list[dict], output_dir: Path, shard_idx: int) -> None:
    """Write a shard of records as a JSONL file."""
    filename = output_dir / f"articles_{shard_idx:05d}.jsonl"
    with open(filename, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    log(f"  Wrote {len(records)} records to {filename.name}")


if __name__ == "__main__":
    if EMAIL == "your-email@example.com":
        log("WARNING: Set PUBMED_EMAIL environment variable to your email address.")
        log("Example: export PUBMED_EMAIL=your-email@institution.edu")
    search_and_download()
