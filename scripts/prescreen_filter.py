#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Stage 1: LLM-based prescreening filter.

Reads PubMed JSONL shards and uses an LLM to filter out non-target study types.
Only papers passing the prescreen are saved to the output directory.

Features:
- Local pre-filtering (regex-based) to reduce API calls
- Multi-key concurrent API calls with rate limiting
- Checkpoint/resume support
- Saves only screened-in (include) records

Usage:
    python3 prescreen_filter.py

Configuration via environment variables:
    LLM_API_KEYS: comma-separated API keys
    LLM_BASE_URL: OpenAI-compatible base URL
    LLM_MODEL: model name (default: gpt-4o-mini)
    LLM_MAX_WORKERS: concurrent threads (default: 15)
    LLM_GLOBAL_RPS: requests per second (default: 5.0)
"""

from __future__ import annotations

import json
import os
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from itertools import cycle
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

import requests


# =========================
# Configuration
# =========================
SCRIPT_DIR = Path(__file__).resolve().parent

# API keys from environment (comma-separated)
_API_KEYS_STR = os.environ.get("LLM_API_KEYS", "")
DEFAULT_API_KEYS = [k.strip() for k in _API_KEYS_STR.split(",") if k.strip()]

BASE_URL = os.environ.get("LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
CHAT_COMPLETIONS_URL = f"{BASE_URL}/chat/completions"
MODEL = os.environ.get("LLM_MODEL", "gpt-4o-mini")

# Input: where PubMed JSONL shards are
INPUT_DIR = SCRIPT_DIR / "outputs" / "pubmed_search" / "jsonl"
INPUT_GLOB = "articles_*.jsonl"

# Output
OUTPUT_DIR = SCRIPT_DIR / "outputs" / "prescreen"
SCREENED_SUBDIR = "screened_jsonl"
DONE_SUBDIR = "done"

# Concurrency and rate limiting
MAX_WORKERS = int(os.environ.get("LLM_MAX_WORKERS", "15"))
GLOBAL_RPS = float(os.environ.get("LLM_GLOBAL_RPS", "5.0"))
REQUEST_TIMEOUT = 120
MAX_RETRIES = 6
BACKOFF_BASE = 2.0
BACKOFF_MAX = 60.0
WRITE_BUFFER_SIZE = 100
SESSION_POOL = 8


# =========================
# Local pre-filters
# =========================
EXCLUDE_PUB_TYPES: Set[str] = {
    "review", "meta-analysis", "systematic review", "case report",
    "editorial", "letter", "comment", "news", "guideline", "protocol",
    "retracted publication", "published erratum", "interview",
    "congresses", "lecture", "video-audio media", "patient education handout",
    "biography", "historical article", "newspaper article", "autobiography",
    "bibliography", "directory", "festschrift", "legal case", "legislation",
    "portrait", "consensus development conference", "consensus development conference, nih",
    "practice guideline", "clinical conference", "scientific integrity review",
    "twin study", "technical report", "interactive tutorial", "dataset",
}


def is_likely_excluded(pub_types: List[str], title: str = "", abstract: str = "") -> Tuple[bool, str]:
    """Local pre-filter to reduce API calls. Returns (excluded, reason)."""
    pts_lower = {pt.lower() for pt in pub_types}

    # Check publication types
    for exclude_pt in EXCLUDE_PUB_TYPES:
        for pt in pts_lower:
            if exclude_pt in pt:
                return True, f"Publication type: {exclude_pt}"

    # Check title patterns
    exclude_title_patterns = [
        (r'\b(review|meta.analysis|systematic review)\b', "review"),
        (r'\b(case report|case study)\b', "case report"),
        (r'\b(protocol|guideline|consensus)\b', "protocol/guideline"),
        (r'\b(genome.wide|transcriptom|proteom|metabolom)\b', "omics-focused"),
    ]
    title_lower = title.lower()
    for pattern, reason in exclude_title_patterns:
        if re.search(pattern, title_lower):
            return True, f"Title match: {reason}"

    # Skip articles without abstracts
    if not abstract or len(abstract.strip()) < 50:
        return True, "No abstract or too short"

    return False, ""


# =========================
# Prompt (customize for your domain)
# =========================
SYSTEM_PROMPT = os.environ.get(
    "PRESCREEN_SYSTEM_PROMPT",
    """You are a clinical literature screening assistant.

Your task is to determine whether a PubMed article should be included in a
database of original clinical prediction model studies.

Rules:
1. Use only the title, abstract, and publication types.
2. INCLUDE only if the paper describes building, developing, training, or
   validating a clinical prediction model.
3. EXCLUDE: reviews, meta-analyses, systematic reviews, case reports,
   editorials, letters, comments, guidelines, protocols, method-only papers
   without a concrete model, external-validation-only studies without model
   building, purely bioinformatics/genomics studies without clinical application.
4. EXCLUDE biomarker-only studies that do not build a prediction model.
5. When uncertain, default to EXCLUDE (conservative screening).
6. Output JSON only. No explanation.

Return this JSON:
{"decision": "include|uncertain|exclude", "reason": "brief reason in English"}"""
)


def make_user_prompt(article: dict) -> str:
    title = article.get("title", "")
    pub_types = ", ".join(article.get("publication_types", []))
    abstract = article.get("abstract", "")
    pmid = article.get("pmid", "")
    return (
        f"PMID: {pmid}\n"
        f"Title: {title}\n"
        f"Publication types: {pub_types}\n"
        f"Abstract: {abstract[:3000]}"  # Truncate very long abstracts
    )


# =========================
# API Client
# =========================
class APIClient:
    """Thread-safe API client with session pool and rate limiting."""

    def __init__(self, api_keys: List[str]):
        self._api_keys = api_keys if api_keys else ["no-key"]
        self._key_cycle = cycle(api_keys)
        self._lock = threading.Lock()
        self._sessions: Dict[str, requests.Session] = {}
        self._last_request_time = 0.0
        self._rate_lock = threading.Lock()

        for key in self._api_keys:
            session = requests.Session()
            session.headers.update({
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            })
            self._sessions[key] = session

    def _next_key(self) -> str:
        with self._lock:
            return next(self._key_cycle)

    def _wait_rate_limit(self):
        with self._rate_lock:
            elapsed = time.time() - self._last_request_time
            min_interval = 1.0 / GLOBAL_RPS if GLOBAL_RPS > 0 else 0
            if elapsed < min_interval:
                time.sleep(min_interval - elapsed + random.uniform(0, 0.05))
            self._last_request_time = time.time()

    def call(self, system_prompt: str, user_prompt: str) -> dict:
        """Call LLM with retry logic."""
        for attempt in range(MAX_RETRIES):
            key = self._next_key()
            session = self._sessions[key]
            self._wait_rate_limit()

            try:
                resp = session.post(
                    CHAT_COMPLETIONS_URL,
                    json={
                        "model": MODEL,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt},
                        ],
                        "temperature": 0.1,
                        "max_tokens": 200,
                    },
                    timeout=REQUEST_TIMEOUT,
                )

                if resp.status_code == 429:
                    wait = min(BACKOFF_BASE ** attempt + random.uniform(0, 1), BACKOFF_MAX)
                    time.sleep(wait)
                    continue

                if resp.status_code in (502, 503):
                    time.sleep(BACKOFF_BASE ** attempt)
                    continue

                resp.raise_for_status()
                data = resp.json()
                content = data["choices"][0]["message"]["content"]

                # Try to parse JSON from response
                return _parse_json_response(content)

            except (requests.RequestException, (KeyError, IndexError, json.JSONDecodeError)) as e:
                if attempt < MAX_RETRIES - 1:
                    time.sleep(BACKOFF_BASE ** attempt + random.uniform(0, 0.5))
                else:
                    return {"decision": "error", "reason": str(e)}

        return {"decision": "error", "reason": "max retries exceeded"}


def _parse_json_response(text: str) -> dict:
    """Extract JSON from LLM response (handles markdown code blocks)."""
    text = text.strip()
    # Remove code fences
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.startswith("```")]
        text = "\n".join(lines).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to find JSON object in the text
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            return json.loads(match.group())
        return {"decision": "error", "reason": f"JSON parse failed: {text[:100]}"}


# =========================
# Main pipeline
# =========================
def run_prescreen() -> None:
    """Main entry point."""
    # Validate configuration
    if not DEFAULT_API_KEYS:
        print("ERROR: Set LLM_API_KEYS environment variable.")
        print("Example: export LLM_API_KEYS='sk-key1,sk-key2'")
        return

    input_dir = INPUT_DIR
    if not input_dir.exists():
        print(f"ERROR: Input directory not found: {input_dir}")
        print("Run pubmed_search.py first.")
        return

    output_dir = OUTPUT_DIR
    screened_dir = output_dir / SCREENED_SUBDIR
    done_dir = output_dir / DONE_SUBDIR

    # Checkpoint
    checkpoint_path = output_dir / "checkpoint.json"
    checkpoint = load_checkpoint(checkpoint_path)

    screened_dir.mkdir(parents=True, exist_ok=True)
    done_dir.mkdir(parents=True, exist_ok=True)

    # Find input files
    input_files = sorted(input_dir.glob(INPUT_GLOB))
    if not input_files:
        print(f"ERROR: No files matching {INPUT_GLOB} in {input_dir}")
        return

    client = APIClient(DEFAULT_API_KEYS)
    print(f"Model: {MODEL}, Workers: {MAX_WORKERS}, RPS: {GLOBAL_RPS}")
    print(f"Input dir: {input_dir}, {len(input_files)} file(s)")

    for fpath in input_files:
        done_marker = done_dir / f"{fpath.stem}.done"
        if done_marker.exists():
            continue

        if fpath.stem in checkpoint.get("completed_files", []):
            done_marker.touch()
            continue

        print(f"\nProcessing: {fpath.name}")
        process_file(fpath, screened_dir, client, checkpoint)

        done_marker.touch()
        checkpoint.setdefault("completed_files", []).append(fpath.stem)
        save_checkpoint(checkpoint_path, checkpoint)

    print("\nPrescreening complete.")


def process_file(fpath: Path, screened_dir: Path, client: APIClient, checkpoint: dict) -> None:
    """Process one JSONL file."""
    # Load articles
    articles = []
    with open(fpath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                articles.append(json.loads(line))

    # Resume from last processed PMID
    processed_pmids = set(checkpoint.get("processed_pmids", []))
    articles = [a for a in articles if a.get("pmid") not in processed_pmids]

    included_count = 0
    excluded_local = 0
    excluded_llm = 0

    output_path = screened_dir / f"screened_{fpath.stem}.jsonl"
    write_buffer = []
    buffer_lock = threading.Lock()

    def flush_buffer():
        with buffer_lock:
            if write_buffer:
                with open(output_path, "a", encoding="utf-8") as f:
                    for rec in write_buffer:
                        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                write_buffer.clear()

    # Process with thread pool
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {}
        for art in articles:
            # Local pre-filter
            pub_types = art.get("publication_types", [])
            title = art.get("title", "")
            abstract = art.get("abstract", "")
            excluded, reason = is_likely_excluded(pub_types, title, abstract)
            if excluded:
                excluded_local += 1
                continue

            fut = executor.submit(client.call, SYSTEM_PROMPT, make_user_prompt(art))
            futures[fut] = art

        for i, fut in enumerate(as_completed(futures)):
            art = futures[fut]
            try:
                result = fut.result()
                decision = result.get("decision", "error")

                if decision == "include":
                    included_count += 1
                    with buffer_lock:
                        write_buffer.append(art)
                        if len(write_buffer) >= WRITE_BUFFER_SIZE:
                            flush_buffer()
                else:
                    excluded_llm += 1

            except Exception as e:
                print(f"  Error processing PMID {art.get('pmid')}: {e}")

            if (i + 1) % 100 == 0:
                print(f"  Progress: {i + 1}/{len(futures)} "
                      f"(included: {included_count}, excluded_local: {excluded_local}, "
                      f"excluded_llm: {excluded_llm})")

        flush_buffer()

    print(f"  Results for {fpath.name}:")
    print(f"    Included: {included_count}")
    print(f"    Excluded (local): {excluded_local}")
    print(f"    Excluded (LLM): {excluded_llm}")


def load_checkpoint(path: Path) -> dict:
    if path.exists():
        with open(path, "r") as f:
            return json.load(f)
    return {}


def save_checkpoint(path: Path, data: dict) -> None:
    with open(path, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    run_prescreen()
