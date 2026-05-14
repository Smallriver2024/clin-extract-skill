#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Stage 2: Structured information extraction via LLM.

Two modes:
  1. extract — Primary extraction from prescreened articles
  2. rerun_flagged — Arbitration of low-confidence/flagged records

Features:
- Configurable extraction schema via environment variables
- Multi-key concurrent API calls with rate limiting
- Local JSON validation and normalization
- Checkpoint/resume support
- Separate output, review, and audit streams

Usage:
    # Primary extraction
    python3 extract_main.py --mode extract \
      --input-dir outputs/prescreen/screened_jsonl \
      --output-dir outputs/extract \
      --primary-model gpt-4o \
      --max-workers 15 --global-rps 8

    # Arbitration
    python3 extract_main.py --mode rerun_flagged \
      --input-dir outputs/prescreen/screened_jsonl \
      --output-dir outputs/extract \
      --arbitration-model gpt-4o \
      --max-workers 10 --global-rps 5
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from itertools import cycle
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import requests


# =========================
# Configuration
# =========================
SCRIPT_DIR = Path(__file__).resolve().parent

# API keys from environment
_API_KEYS_STR = os.environ.get("LLM_API_KEYS", "")
DEFAULT_API_KEYS = [k.strip() for k in _API_KEYS_STR.split(",") if k.strip()]

DEFAULT_BASE_URL = os.environ.get(
    "LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
)
DEFAULT_CHAT_COMPLETIONS_URL = f"{DEFAULT_BASE_URL}/chat/completions"

# Default paths
DEFAULT_INPUT_DIR = SCRIPT_DIR / "outputs" / "prescreen" / "screened_jsonl"
DEFAULT_INPUT_GLOB = "screened_*.jsonl"
DEFAULT_OUTPUT_DIR = SCRIPT_DIR / "outputs" / "extract"

# Model defaults
DEFAULT_PRIMARY_MODEL = os.environ.get("LLM_PRIMARY_MODEL", "gpt-4o")
DEFAULT_ARBITRATION_MODEL = os.environ.get("LLM_ARBITRATION_MODEL", "gpt-4o")

# Concurrency defaults
DEFAULT_MAX_WORKERS = int(os.environ.get("LLM_MAX_WORKERS", "15"))
DEFAULT_GLOBAL_RPS = float(os.environ.get("LLM_GLOBAL_RPS", "8.0"))
DEFAULT_REQUEST_TIMEOUT = 180
DEFAULT_MAX_RETRIES = 6
DEFAULT_BACKOFF_BASE = 2.0
DEFAULT_BACKOFF_MAX = 60.0
DEFAULT_WRITE_BUFFER_SIZE = 5
DEFAULT_REVIEW_CONF_THRESHOLD = 60


# =========================
# Controlled vocabularies (customize for your domain)
# =========================
# These are the default vocabularies for clinical prediction model extraction.
# Replace with your domain's vocabularies.

SUBSPECIALTIES = [
    "cardiology", "oncology", "neurology", "respiratory_medicine",
    "gastroenterology", "hepatology", "nephrology", "endocrinology",
    "hematology", "rheumatology", "infectious_disease", "critical_care",
    "emergency_medicine", "surgery", "anesthesiology_perioperative",
    "obstetrics_gynecology", "pediatrics", "psychiatry", "radiology",
    "rehabilitation", "public_health", "general_medicine", "other",
]

OUTCOME_GROUPS = [
    "mortality", "survival", "recurrence", "complication",
    "treatment_response", "functional_outcome", "diagnosis",
    "event_risk", "hospitalization_utilization", "other",
]

METHOD_GROUPS = [
    "logistic_regression", "cox_regression", "lasso_cox", "lasso_logistic",
    "linear_regression", "tree_based_ml", "svm", "neural_network",
    "deep_learning", "nomogram", "risk_score", "ensemble_model",
    "signature_model", "statistical_model_other", "unclear",
]

VALIDATION_TYPES = [
    "none_reported", "internal_split", "cross_validation", "bootstrap",
    "temporal_validation", "external_validation",
    "independent_validation_cohort", "unclear",
]

ALLOWED_SUBSPECIALTIES = set(SUBSPECIALTIES)
ALLOWED_OUTCOME_GROUPS = set(OUTCOME_GROUPS)
ALLOWED_METHOD_GROUPS = set(METHOD_GROUPS)
ALLOWED_VALIDATION_TYPES = set(VALIDATION_TYPES)

SCORE_SUB_KEYS = [
    "sample_size_score",
    "validation_rigor_score",
    "method_score",
    "performance_score",
    "clinical_applicability_score",
]

EXTERNAL_VALIDATION_TYPES = {"external_validation", "independent_validation_cohort"}
INTERNAL_VALIDATION_TYPES = {
    "internal_split", "cross_validation", "bootstrap", "temporal_validation",
}


# =========================
# Prompts (customize for your domain)
# =========================
# Set these via environment variables to override for different domains.

LONG_SYSTEM_PROMPT = os.environ.get(
    "EXTRACT_LONG_SYSTEM_PROMPT",
    """You are a medical prediction-model information extraction assistant.

Your task is to extract structured information from PubMed metadata, title, and
abstract for papers that describe original clinical prediction model studies.

Rules:
1. Use only the provided title, abstract, and metadata.
2. Do not infer information that is not stated.
3. If a field is unclear or not reported, return null, [], or "unclear".
4. One article may contain multiple models. Split them into separate items in "model_level".
5. Standardize disease names and outcomes conservatively.
6. Assign one primary clinical subspecialty for each model.
7. Extract whether external/internal validation is reported at article and model levels.
8. Score study and model quality (total 100) using sub-scores (0-20 each):
   sample_size_score, validation_rigor_score, method_score, performance_score,
   clinical_applicability_score.
9. Output valid JSON only, no explanation.

Clinical subspecialties:
cardiology, oncology, neurology, respiratory_medicine, gastroenterology,
hepatology, nephrology, endocrinology, hematology, rheumatology,
infectious_disease, critical_care, emergency_medicine, surgery,
anesthesiology_perioperative, obstetrics_gynecology, pediatrics, psychiatry,
radiology, rehabilitation, public_health, general_medicine, other

Outcome groups:
mortality, survival, recurrence, complication, treatment_response,
functional_outcome, diagnosis, event_risk, hospitalization_utilization, other

Method groups:
logistic_regression, cox_regression, lasso_cox, lasso_logistic,
linear_regression, tree_based_ml, svm, neural_network, deep_learning,
nomogram, risk_score, ensemble_model, signature_model,
statistical_model_other, unclear

Validation types:
none_reported, internal_split, cross_validation, bootstrap,
temporal_validation, external_validation, independent_validation_cohort, unclear

Be conservative, precise, and schema-compliant."""
)

LONG_USER_TEMPLATE = """Extract structured information from the following article.

Return JSON only.

PMID: <<PMID>>
Title: <<TITLE>>
Journal: <<JOURNAL>>
Publication date: <<PUB_DATE>>
Publication types: <<PUBLICATION_TYPES>>
Keywords: <<KEYWORDS>>
MeSH terms: <<MESH_TERMS>>
Abstract:
<<ABSTRACT>>

Return this JSON structure:

{
  "article_level": {
    "pmid": "", "title": "", "journal": "", "pub_date": "",
    "prediction_type": "", "article_model_type": "",
    "clinical_use_case": "", "target_population_summary": "",
    "study_design": "", "data_source_type": "", "country_or_region": "",
    "primary_disease_raw": "", "primary_disease_standard": "",
    "primary_subspecialty": "",
    "article_has_external_validation": null,
    "article_has_internal_validation": null,
    "is_multimodel_article": false, "number_of_models_described": 0,
    "study_quality_score": {
      "sample_size_score": null, "validation_rigor_score": null,
      "method_score": null, "performance_score": null,
      "clinical_applicability_score": null, "total_score": null
    },
    "overall_notes": ""
  },
  "model_level": [
    {
      "model_id_within_article": 1, "model_name": "", "model_stage": "",
      "prediction_type": "", "target_disease_raw": "",
      "target_disease_standard": "", "disease_subspecialty": "",
      "outcome_raw": "", "outcome_standard": "", "outcome_group": "",
      "time_horizon": "", "sample_size": null, "events": null,
      "predictors_raw": [], "predictor_domains": [],
      "model_method_raw": "", "model_method_group": "",
      "validation_type": "",
      "has_external_validation": null, "has_internal_validation": null,
      "performance_metrics": {
        "auc": null, "c_index": null, "sensitivity": null,
        "specificity": null, "ppv": null, "npv": null,
        "calibration_reported": null, "dca_reported": null,
        "nri_reported": null
      },
      "model_quality_score": {
        "sample_size_score": null, "validation_rigor_score": null,
        "method_score": null, "performance_score": null,
        "clinical_applicability_score": null, "total_score": null
      },
      "comparator": "", "notes": ""
    }
  ],
  "normalization": {
    "disease_standardization_confidence": 0,
    "subspecialty_classification_confidence": 0,
    "overall_extraction_confidence": 0
  },
  "quality_flags": {
    "disease_ambiguous": false, "outcome_ambiguous": false,
    "method_ambiguous": false, "requires_human_review": false
  }
}
"""

SHORT_SYSTEM_PROMPT = os.environ.get(
    "EXTRACT_SHORT_SYSTEM_PROMPT",
    """You extract structured information for a clinical prediction model
database from PubMed title, abstract, and metadata.

Rules:
- Use only provided information. Do not hallucinate.
- If unclear, return null, [], or "unclear".
- One article may contain multiple models; split in "model_level".
- Standardize disease names conservatively.
- Score quality (total 100) with sub-scores (0-20 each): sample_size_score,
  validation_rigor_score, method_score, performance_score,
  clinical_applicability_score.
- Output JSON only.

Subspecialties: cardiology, oncology, neurology, respiratory_medicine,
gastroenterology, hepatology, nephrology, endocrinology, hematology,
rheumatology, infectious_disease, critical_care, emergency_medicine,
surgery, anesthesiology_perioperative, obstetrics_gynecology, pediatrics,
psychiatry, radiology, rehabilitation, public_health, general_medicine, other

Outcome groups: mortality, survival, recurrence, complication,
treatment_response, functional_outcome, diagnosis, event_risk,
hospitalization_utilization, other

Method groups: logistic_regression, cox_regression, lasso_cox,
lasso_logistic, linear_regression, tree_based_ml, svm, neural_network,
deep_learning, nomogram, risk_score, ensemble_model, signature_model,
statistical_model_other, unclear

Validation types: none_reported, internal_split, cross_validation,
bootstrap, temporal_validation, external_validation,
independent_validation_cohort, unclear"""
)

SHORT_USER_TEMPLATE = """Extract structured JSON from this article.

PMID: <<PMID>>
Title: <<TITLE>>
Journal: <<JOURNAL>>
Publication date: <<PUB_DATE>>
Publication types: <<PUBLICATION_TYPES>>
Keywords: <<KEYWORDS>>
MeSH terms: <<MESH_TERMS>>
Abstract:
<<ABSTRACT>>

Return JSON with article_level, model_level[], normalization, quality_flags.
Include validation presence fields and quality scores.
Use null / [] / "unclear" if not reported.
Return JSON only."""


# =========================
# Runtime configuration
# =========================
@dataclass
class RuntimeConfig:
    mode: str
    input_dir: Path
    input_glob: str
    output_dir: Path
    base_url: str
    chat_url: str
    prompt_style: str
    primary_model: str
    arbitration_model: str
    max_workers: int
    global_rps: float
    request_timeout: int
    max_retries: int
    backoff_base: float
    backoff_max: float
    write_buffer_size: int
    review_confidence_threshold: int
    enable_inline_second_pass: bool
    force: bool


# =========================
# Thread-safe utilities
# =========================
print_lock = threading.Lock()
rate_lock = threading.Lock()
last_request_ts = 0.0
thread_local = threading.local()


def log(msg: str) -> None:
    with print_lock:
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{now}] {msg}", flush=True)


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def norm_text(x: Any) -> str:
    if x is None:
        return ""
    return " ".join(str(x).split()).strip()


def maybe_text(x: Any) -> Optional[str]:
    s = norm_text(x)
    return s if s else None


def read_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if isinstance(obj, dict):
                yield obj


def append_jsonl(path: Path, rows: List[Dict[str, Any]]) -> None:
    if not rows:
        return
    ensure_dir(path.parent)
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def save_json(path: Path, obj: Dict[str, Any]) -> None:
    ensure_dir(path.parent)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def load_json(path: Path) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def parse_shard_id(path: Path) -> str:
    m = re.search(r"(\d+)", path.stem)
    return m.group(1) if m else path.stem


def normalize_label(s: Optional[str]) -> str:
    if not s:
        return ""
    s = norm_text(s).lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s


# =========================
# Type coercion helpers
# =========================
def to_bool(v: Any, default: bool = False) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        x = v.strip().lower()
        if x in {"true", "1", "yes", "y"}:
            return True
        if x in {"false", "0", "no", "n"}:
            return False
    return default


def to_bool_or_null(v: Any) -> Optional[bool]:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        x = v.strip().lower()
        if x in {"true", "1", "yes", "y"}:
            return True
        if x in {"false", "0", "no", "n"}:
            return False
    return None


def to_int_or_null(v: Any) -> Optional[int]:
    if v is None:
        return None
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    if isinstance(v, str):
        s = v.strip().replace(",", "")
        if not s:
            return None
        m = re.search(r"-?\d+", s)
        if m:
            try:
                return int(m.group(0))
            except Exception:
                return None
    return None


def to_float_or_null(v: Any) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip().replace(",", "")
        if not s:
            return None
        m = re.search(r"-?\d+(?:\.\d+)?", s)
        if m:
            try:
                return float(m.group(0))
            except Exception:
                return None
    return None


def to_list_str(v: Any) -> List[str]:
    if v is None:
        return []
    out: List[str] = []
    if isinstance(v, list):
        for item in v:
            s = norm_text(item)
            if s:
                out.append(s)
        return out
    s = norm_text(v)
    if not s:
        return []
    if ";" in s:
        parts = [norm_text(x) for x in s.split(";")]
    elif "," in s:
        parts = [norm_text(x) for x in s.split(",")]
    else:
        parts = [s]
    return [p for p in parts if p]


def clamp_0_100(v: Any, default: int = 0) -> int:
    n = to_int_or_null(v)
    if n is None:
        n = default
    return max(0, min(100, n))


def clamp_int_or_null(v: Any, min_v: int, max_v: int) -> Optional[int]:
    n = to_int_or_null(v)
    if n is None:
        return None
    return max(min_v, min(max_v, n))


def coerce_enum(v: Any, allowed: Set[str], default: str) -> str:
    s = norm_text(v)
    if not s:
        return default
    if s in allowed:
        return s
    ns = normalize_label(s)
    for x in allowed:
        if normalize_label(x) == ns:
            return x
    return default


def stringify_keywords(x: Any) -> str:
    if isinstance(x, list):
        vals = [norm_text(v) for v in x if norm_text(v)]
        return "; ".join(vals)
    return norm_text(x)


def stringify_mesh_terms(x: Any) -> str:
    if not isinstance(x, list):
        return norm_text(x)
    parts: List[str] = []
    for item in x:
        if isinstance(item, dict):
            d = norm_text(item.get("descriptor"))
            qs = item.get("qualifiers")
            if isinstance(qs, list):
                q_list = [norm_text(q) for q in qs if norm_text(q)]
            else:
                q_list = []
            if d and q_list:
                parts.append(f"{d} [{'; '.join(q_list)}]")
            elif d:
                parts.append(d)
        else:
            s = norm_text(item)
            if s:
                parts.append(s)
    return "; ".join(parts)


def normalize_score_block(
    raw: Any, issue_prefix: str, issues: List[str]
) -> Dict[str, Optional[int]]:
    obj = raw if isinstance(raw, dict) else {}
    subscores: Dict[str, Optional[int]] = {}
    for key in SCORE_SUB_KEYS:
        subscores[key] = clamp_int_or_null(obj.get(key), 0, 20)

    provided_total = clamp_int_or_null(obj.get("total_score"), 0, 100)
    non_null_subscores = [v for v in subscores.values() if v is not None]
    all_present = len(non_null_subscores) == len(SCORE_SUB_KEYS)

    if all_present:
        calculated_total = sum(non_null_subscores)
        if provided_total is not None and provided_total != calculated_total:
            issues.append(f"{issue_prefix}.quality_score_total_mismatch")
        total = calculated_total
    else:
        total = provided_total
        if total is None and non_null_subscores:
            total = sum(non_null_subscores)
            if total > 100:
                total = 100

    out: Dict[str, Optional[int]] = dict(subscores)
    out["total_score"] = total
    return out


# =========================
# Key rotation + rate limiting + sessions
# =========================
class KeyRotator:
    def __init__(self, keys: List[str]):
        keys = [k.strip() for k in keys if k and k.strip()]
        if not keys:
            raise ValueError("No API keys provided. Set LLM_API_KEYS environment variable.")
        self._lock = threading.Lock()
        self._iter = cycle(keys)

    def next_key(self) -> str:
        with self._lock:
            return next(self._iter)


def throttle(global_rps: float) -> None:
    global last_request_ts
    if global_rps <= 0:
        return
    min_interval = 1.0 / global_rps
    with rate_lock:
        now = time.monotonic()
        wait = min_interval - (now - last_request_ts)
        if wait > 0:
            time.sleep(wait)
        last_request_ts = time.monotonic()


def get_session(pool_size: int = 8) -> requests.Session:
    session = getattr(thread_local, "session", None)
    if session is None:
        session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=pool_size, pool_maxsize=pool_size
        )
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        thread_local.session = session
    return session


# =========================
# Prompt assembly
# =========================
def render_template(tpl: str, mapping: Dict[str, str]) -> str:
    out = tpl
    for k, v in mapping.items():
        out = out.replace(f"<<{k}>>", v)
    return out


def build_messages(
    record: Dict[str, Any], prompt_style: str
) -> List[Dict[str, str]]:
    pmid = norm_text(record.get("pmid"))
    title = norm_text(record.get("title"))
    journal = norm_text(record.get("journal"))
    pub_date = norm_text(record.get("pub_date"))
    pub_types = stringify_keywords(record.get("publication_types"))
    keywords = stringify_keywords(record.get("keywords"))
    mesh_terms = stringify_mesh_terms(record.get("mesh_terms"))
    abstract = norm_text(record.get("abstract"))

    mapping = {
        "PMID": pmid,
        "TITLE": title,
        "JOURNAL": journal,
        "PUB_DATE": pub_date,
        "PUBLICATION_TYPES": pub_types,
        "KEYWORDS": keywords,
        "MESH_TERMS": mesh_terms,
        "ABSTRACT": abstract,
    }

    if prompt_style == "long":
        system_prompt = LONG_SYSTEM_PROMPT
        user_prompt = render_template(LONG_USER_TEMPLATE, mapping)
    else:
        system_prompt = SHORT_SYSTEM_PROMPT
        user_prompt = render_template(SHORT_USER_TEMPLATE, mapping)

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


# =========================
# JSON parsing
# =========================
def strip_code_fence(text: str) -> str:
    x = text.strip()
    if x.startswith("```"):
        x = re.sub(r"^```(?:json)?\s*", "", x, flags=re.I)
        x = re.sub(r"\s*```$", "", x)
    return x.strip()


def extract_first_json_object(text: str) -> Optional[str]:
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def safe_json_loads(text: str) -> Dict[str, Any]:
    x = strip_code_fence(text)
    try:
        obj = json.loads(x)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    candidate = extract_first_json_object(x)
    if candidate:
        obj = json.loads(candidate)
        if isinstance(obj, dict):
            return obj
    raise ValueError("Model response could not be parsed as JSON object")


# =========================
# LLM call
# =========================
def call_llm_json(
    messages: List[Dict[str, str]],
    model: str,
    key_rotator: KeyRotator,
    cfg: RuntimeConfig,
) -> Dict[str, Any]:
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }

    last_error: Optional[str] = None

    for attempt in range(1, cfg.max_retries + 1):
        api_key = key_rotator.next_key()
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        try:
            throttle(cfg.global_rps)
            resp = get_session().post(
                cfg.chat_url,
                headers=headers,
                json=payload,
                timeout=cfg.request_timeout,
            )

            if resp.status_code == 200:
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                return safe_json_loads(content)

            if resp.status_code in {429, 500, 502, 503, 504}:
                last_error = f"HTTP {resp.status_code}: {resp.text[:500]}"
                sleep_s = min(
                    cfg.backoff_base ** attempt + random.uniform(0, 1),
                    cfg.backoff_max,
                )
                time.sleep(sleep_s)
                continue

            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:1000]}")

        except Exception as e:
            last_error = str(e)
            sleep_s = min(
                cfg.backoff_base ** attempt + random.uniform(0, 1),
                cfg.backoff_max,
            )
            time.sleep(sleep_s)

    raise RuntimeError(f"LLM call failed, model={model}, error={last_error}")


# =========================
# Schema normalization & validation
# =========================
def empty_extraction_from_source(record: Dict[str, Any]) -> Dict[str, Any]:
    """Create an empty extraction template populated with source metadata."""
    pmid = norm_text(record.get("pmid"))
    title = norm_text(record.get("title"))
    journal = norm_text(record.get("journal"))
    pub_date = norm_text(record.get("pub_date"))
    country = norm_text(record.get("country"))

    return {
        "article_level": {
            "pmid": pmid,
            "title": title,
            "journal": journal,
            "pub_date": pub_date,
            "prediction_type": "unclear",
            "article_model_type": "unclear",
            "clinical_use_case": "unclear",
            "target_population_summary": "unclear",
            "study_design": "unclear",
            "data_source_type": "unclear",
            "country_or_region": country if country else "unclear",
            "primary_disease_raw": None,
            "primary_disease_standard": "unclear",
            "primary_subspecialty": "other",
            "article_has_external_validation": None,
            "article_has_internal_validation": None,
            "is_multimodel_article": False,
            "number_of_models_described": 0,
            "study_quality_score": {
                "sample_size_score": None,
                "validation_rigor_score": None,
                "method_score": None,
                "performance_score": None,
                "clinical_applicability_score": None,
                "total_score": None,
            },
            "overall_notes": "",
        },
        "model_level": [],
        "normalization": {
            "disease_standardization_confidence": 0,
            "subspecialty_classification_confidence": 0,
            "overall_extraction_confidence": 0,
        },
        "quality_flags": {
            "disease_ambiguous": False,
            "outcome_ambiguous": False,
            "method_ambiguous": False,
            "requires_human_review": False,
        },
    }


def normalize_article_level(
    raw: Dict[str, Any], source: Dict[str, Any], issues: List[str]
) -> Dict[str, Any]:
    """Normalize and validate article-level fields."""
    pmid_src = norm_text(source.get("pmid"))
    title_src = norm_text(source.get("title"))
    journal_src = norm_text(source.get("journal"))
    pub_date_src = norm_text(source.get("pub_date"))
    country_src = norm_text(source.get("country"))

    pmid = norm_text(raw.get("pmid")) or pmid_src
    title = norm_text(raw.get("title")) or title_src
    journal = norm_text(raw.get("journal")) or journal_src
    pub_date = norm_text(raw.get("pub_date")) or pub_date_src

    primary_subspecialty = coerce_enum(
        raw.get("primary_subspecialty"), ALLOWED_SUBSPECIALTIES, "other"
    )
    if norm_text(raw.get("primary_subspecialty")) and primary_subspecialty == "other":
        issues.append("article.primary_subspecialty_out_of_set")

    return {
        "pmid": pmid,
        "title": title,
        "journal": journal,
        "pub_date": pub_date,
        "prediction_type": norm_text(raw.get("prediction_type")) or "unclear",
        "article_model_type": norm_text(raw.get("article_model_type")) or "unclear",
        "clinical_use_case": norm_text(raw.get("clinical_use_case")) or "unclear",
        "target_population_summary": maybe_text(raw.get("target_population_summary")) or "unclear",
        "study_design": norm_text(raw.get("study_design")) or "unclear",
        "data_source_type": norm_text(raw.get("data_source_type")) or "unclear",
        "country_or_region": norm_text(raw.get("country_or_region"))
        or (country_src if country_src else "unclear"),
        "primary_disease_raw": maybe_text(raw.get("primary_disease_raw")),
        "primary_disease_standard": norm_text(raw.get("primary_disease_standard")) or "unclear",
        "primary_subspecialty": primary_subspecialty,
        "article_has_external_validation": to_bool_or_null(
            raw.get("article_has_external_validation")
        ),
        "article_has_internal_validation": to_bool_or_null(
            raw.get("article_has_internal_validation")
        ),
        "is_multimodel_article": to_bool(raw.get("is_multimodel_article"), default=False),
        "number_of_models_described": to_int_or_null(raw.get("number_of_models_described")),
        "study_quality_score": normalize_score_block(
            raw.get("study_quality_score"), "article", issues
        ),
        "overall_notes": norm_text(raw.get("overall_notes")),
    }


def normalize_model_item(
    item: Dict[str, Any], idx: int, issues: List[str]
) -> Dict[str, Any]:
    """Normalize and validate a single model-level item."""
    disease_subspecialty = coerce_enum(
        item.get("disease_subspecialty"), ALLOWED_SUBSPECIALTIES, "other"
    )
    if norm_text(item.get("disease_subspecialty")) and disease_subspecialty == "other":
        issues.append(f"model[{idx}].disease_subspecialty_out_of_set")

    outcome_group = coerce_enum(
        item.get("outcome_group"), ALLOWED_OUTCOME_GROUPS, "other"
    )
    if norm_text(item.get("outcome_group")) and outcome_group == "other":
        issues.append(f"model[{idx}].outcome_group_out_of_set")

    method_group = coerce_enum(
        item.get("model_method_group"), ALLOWED_METHOD_GROUPS, "unclear"
    )
    if norm_text(item.get("model_method_group")) and method_group == "unclear":
        issues.append(f"model[{idx}].method_group_out_of_set")

    validation_type = coerce_enum(
        item.get("validation_type"), ALLOWED_VALIDATION_TYPES, "unclear"
    )
    if norm_text(item.get("validation_type")) and validation_type == "unclear":
        issues.append(f"model[{idx}].validation_type_out_of_set")

    pm_raw = (
        item.get("performance_metrics")
        if isinstance(item.get("performance_metrics"), dict)
        else {}
    )

    return {
        "model_id_within_article": to_int_or_null(
            item.get("model_id_within_article")
        )
        or (idx + 1),
        "model_name": maybe_text(item.get("model_name")),
        "model_stage": maybe_text(item.get("model_stage")),
        "prediction_type": norm_text(item.get("prediction_type")) or "unclear",
        "target_disease_raw": maybe_text(item.get("target_disease_raw")),
        "target_disease_standard": norm_text(item.get("target_disease_standard")) or "unclear",
        "disease_subspecialty": disease_subspecialty,
        "outcome_raw": maybe_text(item.get("outcome_raw")),
        "outcome_standard": norm_text(item.get("outcome_standard")) or "unclear",
        "outcome_group": outcome_group,
        "time_horizon": maybe_text(item.get("time_horizon")),
        "sample_size": to_int_or_null(item.get("sample_size")),
        "events": to_int_or_null(item.get("events")),
        "predictors_raw": to_list_str(item.get("predictors_raw")),
        "predictor_domains": to_list_str(item.get("predictor_domains")),
        "model_method_raw": maybe_text(item.get("model_method_raw")),
        "model_method_group": method_group,
        "validation_type": validation_type,
        "has_external_validation": to_bool_or_null(item.get("has_external_validation")),
        "has_internal_validation": to_bool_or_null(item.get("has_internal_validation")),
        "performance_metrics": {
            "auc": to_float_or_null(pm_raw.get("auc")),
            "c_index": to_float_or_null(pm_raw.get("c_index")),
            "sensitivity": to_float_or_null(pm_raw.get("sensitivity")),
            "specificity": to_float_or_null(pm_raw.get("specificity")),
            "ppv": to_float_or_null(pm_raw.get("ppv")),
            "npv": to_float_or_null(pm_raw.get("npv")),
            "calibration_reported": to_bool_or_null(pm_raw.get("calibration_reported")),
            "dca_reported": to_bool_or_null(pm_raw.get("dca_reported")),
            "nri_reported": to_bool_or_null(pm_raw.get("nri_reported")),
        },
        "model_quality_score": normalize_score_block(
            item.get("model_quality_score"), f"model[{idx}]", issues
        ),
        "comparator": maybe_text(item.get("comparator")),
        "notes": norm_text(item.get("notes")),
    }


def normalize_extraction(
    raw_obj: Dict[str, Any], source: Dict[str, Any]
) -> Tuple[Dict[str, Any], List[str]]:
    """Full normalization of an LLM extraction response."""
    issues: List[str] = []
    base = empty_extraction_from_source(source)

    if not isinstance(raw_obj, dict):
        issues.append("top_level_not_object")
        base["quality_flags"]["requires_human_review"] = True
        return base, issues

    article_raw = (
        raw_obj.get("article_level")
        if isinstance(raw_obj.get("article_level"), dict)
        else {}
    )
    if not isinstance(raw_obj.get("article_level"), dict):
        issues.append("missing_article_level")

    model_raw = (
        raw_obj.get("model_level")
        if isinstance(raw_obj.get("model_level"), list)
        else []
    )
    if not isinstance(raw_obj.get("model_level"), list):
        issues.append("missing_model_level_array")

    norm_raw = (
        raw_obj.get("normalization")
        if isinstance(raw_obj.get("normalization"), dict)
        else {}
    )
    quality_raw = (
        raw_obj.get("quality_flags")
        if isinstance(raw_obj.get("quality_flags"), dict)
        else {}
    )

    article_level = normalize_article_level(article_raw, source, issues)

    model_level: List[Dict[str, Any]] = []
    for i, item in enumerate(model_raw):
        if not isinstance(item, dict):
            issues.append(f"model[{i}]_not_object")
            continue
        model_level.append(normalize_model_item(item, i, issues))

    # Re-index model IDs sequentially
    for i, m in enumerate(model_level, start=1):
        m["model_id_within_article"] = i

    # Infer validation flags from validation_type if not explicitly set
    for m in model_level:
        vt = m.get("validation_type")
        if m.get("has_external_validation") is None:
            if vt in EXTERNAL_VALIDATION_TYPES:
                m["has_external_validation"] = True
            elif vt in INTERNAL_VALIDATION_TYPES or vt in {"none_reported", "unclear"}:
                m["has_external_validation"] = False
        if m.get("has_internal_validation") is None:
            if vt in INTERNAL_VALIDATION_TYPES:
                m["has_internal_validation"] = True
            elif vt in EXTERNAL_VALIDATION_TYPES or vt in {"none_reported", "unclear"}:
                m["has_internal_validation"] = False

    # Article-level validation from model-level aggregation
    n_models = to_int_or_null(article_level.get("number_of_models_described"))
    if n_models is None:
        n_models = len(model_level)
    if n_models < 0:
        issues.append("number_of_models_negative")
        n_models = len(model_level)

    article_level["number_of_models_described"] = n_models
    article_level["is_multimodel_article"] = bool(n_models > 1)

    ext_vals = [m.get("has_external_validation") for m in model_level]
    ext_known = [v for v in ext_vals if v is not None]
    if article_level.get("article_has_external_validation") is None and ext_known:
        article_level["article_has_external_validation"] = any(ext_known)

    int_vals = [m.get("has_internal_validation") for m in model_level]
    int_known = [v for v in int_vals if v is not None]
    if article_level.get("article_has_internal_validation") is None and int_known:
        article_level["article_has_internal_validation"] = any(int_known)

    normalization = {
        "disease_standardization_confidence": clamp_0_100(
            norm_raw.get("disease_standardization_confidence"), default=0
        ),
        "subspecialty_classification_confidence": clamp_0_100(
            norm_raw.get("subspecialty_classification_confidence"), default=0
        ),
        "overall_extraction_confidence": clamp_0_100(
            norm_raw.get("overall_extraction_confidence"), default=0
        ),
    }

    quality_flags = {
        "disease_ambiguous": to_bool(quality_raw.get("disease_ambiguous"), default=False),
        "outcome_ambiguous": to_bool(quality_raw.get("outcome_ambiguous"), default=False),
        "method_ambiguous": to_bool(quality_raw.get("method_ambiguous"), default=False),
        "requires_human_review": to_bool(
            quality_raw.get("requires_human_review"), default=False
        ),
    }

    if issues:
        quality_flags["requires_human_review"] = True

    return {
        "article_level": article_level,
        "model_level": model_level,
        "normalization": normalization,
        "quality_flags": quality_flags,
    }, issues


def extraction_needs_review(
    extraction: Dict[str, Any], confidence_threshold: int
) -> bool:
    """Check if an extraction needs human review."""
    q = extraction.get("quality_flags") or {}
    norm = extraction.get("normalization") or {}
    models = extraction.get("model_level") or []

    if to_bool(q.get("requires_human_review"), False):
        return True
    if to_bool(q.get("disease_ambiguous"), False):
        return True
    if to_bool(q.get("outcome_ambiguous"), False):
        return True
    if to_bool(q.get("method_ambiguous"), False):
        return True

    overall_conf = clamp_0_100(norm.get("overall_extraction_confidence"), default=0)
    if overall_conf < confidence_threshold:
        return True

    if not isinstance(models, list) or len(models) == 0:
        return True

    score_totals: List[Optional[int]] = []
    for m in models:
        if not isinstance(m, dict):
            continue
        score = (
            m.get("model_quality_score")
            if isinstance(m.get("model_quality_score"), dict)
            else {}
        )
        score_totals.append(to_int_or_null(score.get("total_score")))
    if score_totals and all(v is None for v in score_totals):
        return True

    return False


def add_error_note(extraction: Dict[str, Any], msg: str) -> Dict[str, Any]:
    """Add an error note to an extraction and mark for review."""
    x = dict(extraction)
    art = dict(x.get("article_level") or {})
    old = norm_text(art.get("overall_notes"))
    short = norm_text(msg)
    if len(short) > 300:
        short = short[:300]
    art["overall_notes"] = f"{old}; {short}".strip("; ") if old else short
    x["article_level"] = art

    q = dict(x.get("quality_flags") or {})
    q["requires_human_review"] = True
    x["quality_flags"] = q

    n = dict(x.get("normalization") or {})
    n["overall_extraction_confidence"] = min(
        clamp_0_100(n.get("overall_extraction_confidence"), 0), 20
    )
    x["normalization"] = n
    return x


# =========================
# Single record processing
# =========================
def process_record_extract(
    record: Dict[str, Any],
    key_rotator: KeyRotator,
    cfg: RuntimeConfig,
) -> Tuple[Dict[str, Any], Dict[str, Any], bool]:
    """Process one record in extract mode."""
    pmid = norm_text(record.get("pmid"))
    messages = build_messages(record, cfg.prompt_style)

    audit: Dict[str, Any] = {
        "pmid": pmid,
        "mode": "extract",
        "primary_model": cfg.primary_model,
        "inline_second_pass": False,
        "second_pass_model": None,
        "status": "ok",
        "issues": [],
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    }

    try:
        raw1 = call_llm_json(messages, cfg.primary_model, key_rotator, cfg)
        extraction1, issues1 = normalize_extraction(raw1, record)
        audit["issues"].extend(issues1)

        final = extraction1
        if cfg.enable_inline_second_pass and extraction_needs_review(
            extraction1, cfg.review_confidence_threshold
        ):
            audit["inline_second_pass"] = True
            audit["second_pass_model"] = cfg.arbitration_model
            try:
                raw2 = call_llm_json(messages, cfg.arbitration_model, key_rotator, cfg)
                extraction2, issues2 = normalize_extraction(raw2, record)
                audit["issues"].extend([f"second_pass:{x}" for x in issues2])
                final = extraction2
            except Exception as e2:
                audit["issues"].append(f"second_pass_failed:{norm_text(e2)}")
                final = add_error_note(final, f"second_pass_failed: {e2}")

        needs_review = extraction_needs_review(final, cfg.review_confidence_threshold)
        return final, audit, needs_review

    except Exception as e:
        err = norm_text(e)
        audit["status"] = "error"
        audit["issues"].append(f"primary_failed:{err}")

        fallback = empty_extraction_from_source(record)
        fallback = add_error_note(fallback, f"primary_failed: {err}")
        return fallback, audit, True


def process_record_rerun(
    record: Dict[str, Any],
    key_rotator: KeyRotator,
    cfg: RuntimeConfig,
) -> Tuple[Dict[str, Any], Dict[str, Any], bool]:
    """Process one record in rerun_flagged mode."""
    pmid = norm_text(record.get("pmid"))
    messages = build_messages(record, cfg.prompt_style)

    audit: Dict[str, Any] = {
        "pmid": pmid,
        "mode": "rerun_flagged",
        "model": cfg.arbitration_model,
        "status": "ok",
        "issues": [],
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    }

    try:
        raw = call_llm_json(messages, cfg.arbitration_model, key_rotator, cfg)
        extraction, issues = normalize_extraction(raw, record)
        audit["issues"].extend(issues)
        needs_review = extraction_needs_review(extraction, cfg.review_confidence_threshold)
        return extraction, audit, needs_review

    except Exception as e:
        err = norm_text(e)
        audit["status"] = "error"
        audit["issues"].append(f"rerun_failed:{err}")

        fallback = empty_extraction_from_source(record)
        fallback = add_error_note(fallback, f"rerun_failed: {err}")
        return fallback, audit, True


# =========================
# File-level processing
# =========================
def load_processed_pmids(path: Path) -> Set[str]:
    """Load PMIDs already written to an output file."""
    out: Set[str] = set()
    if not path.exists():
        return out
    for row in read_jsonl(path):
        pmid = ""
        if isinstance(row.get("article_level"), dict):
            pmid = norm_text(row["article_level"].get("pmid"))
        if not pmid:
            pmid = norm_text(row.get("pmid"))
        if pmid:
            out.add(pmid)
    return out


def collect_flagged_pmids(
    extracted_file: Path, confidence_threshold: int
) -> Set[str]:
    """Collect PMIDs flagged for review from extracted output."""
    flagged: Set[str] = set()
    if not extracted_file.exists():
        return flagged
    for row in read_jsonl(extracted_file):
        try:
            if extraction_needs_review(row, confidence_threshold):
                pmid = ""
                if isinstance(row.get("article_level"), dict):
                    pmid = norm_text(row["article_level"].get("pmid"))
                if pmid:
                    flagged.add(pmid)
        except Exception:
            continue
    return flagged


def process_one_file_extract(
    input_file: Path,
    out_file: Path,
    review_file: Path,
    audit_file: Path,
    done_file: Path,
    key_rotator: KeyRotator,
    cfg: RuntimeConfig,
) -> Dict[str, Any]:
    """Process one input JSONL file in extract mode."""
    log(f"Processing {input_file.name}")

    records = list(read_jsonl(input_file))
    total = len(records)
    if total == 0:
        ensure_dir(done_file.parent)
        done_file.write_text("OK\n", encoding="utf-8")
        log(f"{input_file.name} is empty, skipping")
        return {"file": input_file.name, "total": 0, "scheduled": 0,
                "ok": 0, "review": 0, "errors": 0}

    processed_pmids = load_processed_pmids(out_file)

    to_process: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for row in records:
        pmid = norm_text(row.get("pmid"))
        if not pmid:
            to_process.append(row)
            continue
        if pmid in processed_pmids or pmid in seen:
            continue
        seen.add(pmid)
        to_process.append(row)

    scheduled = len(to_process)
    if scheduled == 0:
        ensure_dir(done_file.parent)
        done_file.write_text("OK\n", encoding="utf-8")
        log(f"{input_file.name} already fully processed, skipping")
        return {"file": input_file.name, "total": total, "scheduled": 0,
                "ok": 0, "review": 0, "errors": 0}

    out_buffer: List[Dict[str, Any]] = []
    review_buffer: List[Dict[str, Any]] = []
    audit_buffer: List[Dict[str, Any]] = []
    review_pmids = load_processed_pmids(review_file)

    done = 0
    ok = 0
    review_n = 0
    errors = 0

    with ThreadPoolExecutor(max_workers=cfg.max_workers) as ex:
        fut_map = {
            ex.submit(process_record_extract, row, key_rotator, cfg): norm_text(row.get("pmid"))
            for row in to_process
        }

        for fut in as_completed(fut_map):
            done += 1
            pmid = fut_map[fut]
            try:
                extraction, audit, needs_review = fut.result()
                out_buffer.append(extraction)
                audit_buffer.append(audit)

                if audit.get("status") != "ok":
                    errors += 1
                else:
                    ok += 1

                if needs_review:
                    out_pmid = norm_text(
                        (extraction.get("article_level") or {}).get("pmid")
                    )
                    if out_pmid and out_pmid not in review_pmids:
                        review_buffer.append(extraction)
                        review_pmids.add(out_pmid)
                        review_n += 1
                    elif not out_pmid:
                        review_buffer.append(extraction)
                        review_n += 1

                if len(out_buffer) >= cfg.write_buffer_size:
                    append_jsonl(out_file, out_buffer)
                    out_buffer.clear()
                if len(review_buffer) >= cfg.write_buffer_size:
                    append_jsonl(review_file, review_buffer)
                    review_buffer.clear()
                if len(audit_buffer) >= cfg.write_buffer_size:
                    append_jsonl(audit_file, audit_buffer)
                    audit_buffer.clear()

            except Exception as e:
                errors += 1
                audit_buffer.append({
                    "pmid": pmid, "mode": "extract",
                    "status": "worker_exception", "error": norm_text(e),
                    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                })

            if done % 100 == 0 or done == scheduled:
                log(f"  {input_file.name}: {done}/{scheduled} done, "
                    f"ok={ok}, review={review_n}, errors={errors}")

    if out_buffer:
        append_jsonl(out_file, out_buffer)
    if review_buffer:
        append_jsonl(review_file, review_buffer)
    if audit_buffer:
        append_jsonl(audit_file, audit_buffer)

    ensure_dir(done_file.parent)
    done_file.write_text("OK\n", encoding="utf-8")
    log(f"Done {input_file.name}: total={total}, scheduled={scheduled}, "
        f"ok={ok}, review={review_n}, errors={errors}")

    return {"file": input_file.name, "total": total, "scheduled": scheduled,
            "ok": ok, "review": review_n, "errors": errors}


def process_one_file_rerun(
    input_file: Path,
    extracted_file: Path,
    out_file: Path,
    audit_file: Path,
    done_file: Path,
    key_rotator: KeyRotator,
    cfg: RuntimeConfig,
) -> Dict[str, Any]:
    """Process one input file in rerun_flagged (arbitration) mode."""
    log(f"Arbitrating {input_file.name}")

    flagged_pmids = collect_flagged_pmids(
        extracted_file, confidence_threshold=cfg.review_confidence_threshold
    )
    if not flagged_pmids:
        ensure_dir(done_file.parent)
        done_file.write_text("OK\n", encoding="utf-8")
        log(f"{input_file.name}: no flagged records found")
        return {"file": input_file.name, "flagged": 0, "scheduled": 0,
                "ok": 0, "still_review": 0, "errors": 0}

    done_pmids = load_processed_pmids(out_file)

    source_rows: List[Dict[str, Any]] = []
    for row in read_jsonl(input_file):
        pmid = norm_text(row.get("pmid"))
        if pmid and pmid in flagged_pmids and pmid not in done_pmids:
            source_rows.append(row)

    scheduled = len(source_rows)
    if scheduled == 0:
        ensure_dir(done_file.parent)
        done_file.write_text("OK\n", encoding="utf-8")
        log(f"{input_file.name}: all flagged records already adjudicated")
        return {"file": input_file.name, "flagged": len(flagged_pmids),
                "scheduled": 0, "ok": 0, "still_review": 0, "errors": 0}

    out_buffer: List[Dict[str, Any]] = []
    audit_buffer: List[Dict[str, Any]] = []

    done = 0
    ok = 0
    still_review = 0
    errors = 0

    with ThreadPoolExecutor(max_workers=cfg.max_workers) as ex:
        fut_map = {
            ex.submit(process_record_rerun, row, key_rotator, cfg): norm_text(row.get("pmid"))
            for row in source_rows
        }

        for fut in as_completed(fut_map):
            done += 1
            pmid = fut_map[fut]
            try:
                extraction, audit, needs_review = fut.result()
                out_buffer.append(extraction)
                audit_buffer.append(audit)

                if audit.get("status") != "ok":
                    errors += 1
                else:
                    ok += 1

                if needs_review:
                    still_review += 1

                if len(out_buffer) >= cfg.write_buffer_size:
                    append_jsonl(out_file, out_buffer)
                    out_buffer.clear()
                if len(audit_buffer) >= cfg.write_buffer_size:
                    append_jsonl(audit_file, audit_buffer)
                    audit_buffer.clear()

            except Exception as e:
                errors += 1
                audit_buffer.append({
                    "pmid": pmid, "mode": "rerun_flagged",
                    "status": "worker_exception", "error": norm_text(e),
                    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                })

            if done % 100 == 0 or done == scheduled:
                log(f"  {input_file.name}: {done}/{scheduled} done, "
                    f"ok={ok}, still_review={still_review}, errors={errors}")

    if out_buffer:
        append_jsonl(out_file, out_buffer)
    if audit_buffer:
        append_jsonl(audit_file, audit_buffer)

    ensure_dir(done_file.parent)
    done_file.write_text("OK\n", encoding="utf-8")
    log(f"Done arbitrating {input_file.name}: flagged={len(flagged_pmids)}, "
        f"scheduled={scheduled}, ok={ok}, still_review={still_review}, errors={errors}")

    return {"file": input_file.name, "flagged": len(flagged_pmids),
            "scheduled": scheduled, "ok": ok,
            "still_review": still_review, "errors": errors}


# =========================
# Entry points
# =========================
def build_runtime_config(args: argparse.Namespace) -> RuntimeConfig:
    base_url = args.base_url.rstrip("/")
    chat_url = f"{base_url}/chat/completions"
    return RuntimeConfig(
        mode=args.mode,
        input_dir=Path(args.input_dir).resolve(),
        input_glob=args.input_glob,
        output_dir=Path(args.output_dir).resolve(),
        base_url=base_url,
        chat_url=chat_url,
        prompt_style=args.prompt_style,
        primary_model=args.primary_model,
        arbitration_model=args.arbitration_model,
        max_workers=max(1, int(args.max_workers)),
        global_rps=float(args.global_rps),
        request_timeout=max(10, int(args.request_timeout)),
        max_retries=max(1, int(args.max_retries)),
        backoff_base=max(1.2, float(args.backoff_base)),
        backoff_max=max(1.0, float(args.backoff_max)),
        write_buffer_size=max(1, int(args.write_buffer_size)),
        review_confidence_threshold=max(0, min(100, int(args.review_confidence_threshold))),
        enable_inline_second_pass=bool(args.enable_inline_second_pass),
        force=bool(args.force),
    )


def run_extract(cfg: RuntimeConfig, key_rotator: KeyRotator) -> None:
    """Run primary extraction on all input files."""
    if not cfg.input_dir.exists():
        raise FileNotFoundError(f"Input directory not found: {cfg.input_dir}")

    input_files = sorted(cfg.input_dir.glob(cfg.input_glob))
    if not input_files:
        raise FileNotFoundError(f"No files matching {cfg.input_glob} in {cfg.input_dir}")

    extracted_dir = cfg.output_dir / "extracted_jsonl"
    review_dir = cfg.output_dir / "review_jsonl"
    audit_dir = cfg.output_dir / "audit"
    done_dir = cfg.output_dir / "done_extract"
    checkpoint_file = cfg.output_dir / "checkpoint_extract.json"

    for d in [extracted_dir, review_dir, audit_dir, done_dir]:
        ensure_dir(d)

    checkpoint: Dict[str, Any] = {
        "mode": "extract",
        "input_dir": str(cfg.input_dir),
        "output_dir": str(cfg.output_dir),
        "prompt_style": cfg.prompt_style,
        "primary_model": cfg.primary_model,
        "arbitration_model": cfg.arbitration_model,
        "enable_inline_second_pass": cfg.enable_inline_second_pass,
        "max_workers": cfg.max_workers,
        "global_rps": cfg.global_rps,
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "finished_files": [],
    }
    if checkpoint_file.exists():
        try:
            old = load_json(checkpoint_file)
            if isinstance(old, dict):
                checkpoint.update(old)
        except Exception:
            pass

    finished_files = set(checkpoint.get("finished_files", []))
    rows: List[Dict[str, Any]] = []

    for input_file in input_files:
        shard_id = parse_shard_id(input_file)
        done_file = done_dir / f"{shard_id}.done"
        out_file = extracted_dir / f"extracted_{shard_id}.jsonl"
        review_file = review_dir / f"review_{shard_id}.jsonl"
        audit_file = audit_dir / f"extract_audit_{shard_id}.jsonl"

        if not cfg.force and (done_file.exists() or input_file.name in finished_files):
            log(f"Skipping completed shard: {input_file.name}")
            continue

        row = process_one_file_extract(
            input_file=input_file, out_file=out_file,
            review_file=review_file, audit_file=audit_file,
            done_file=done_file, key_rotator=key_rotator, cfg=cfg,
        )
        rows.append(row)

        finished_files.add(input_file.name)
        checkpoint["finished_files"] = sorted(finished_files)
        checkpoint["last_finished_file"] = input_file.name
        checkpoint["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        save_json(checkpoint_file, checkpoint)

    checkpoint["finished_files"] = sorted(finished_files)
    checkpoint["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_json(checkpoint_file, checkpoint)

    total = sum(r.get("total", 0) for r in rows)
    scheduled = sum(r.get("scheduled", 0) for r in rows)
    ok = sum(r.get("ok", 0) for r in rows)
    review_n = sum(r.get("review", 0) for r in rows)
    errors = sum(r.get("errors", 0) for r in rows)

    log("Primary extraction complete")
    log(f"  Shards: {len(input_files)}")
    log(f"  Total records: {total}")
    log(f"  Sent to model: {scheduled}")
    log(f"  Successful: {ok}")
    log(f"  Flagged for review: {review_n}")
    log(f"  Errors: {errors}")
    log(f"  Output: {cfg.output_dir}")


def run_rerun_flagged(cfg: RuntimeConfig, key_rotator: KeyRotator) -> None:
    """Run arbitration on flagged records."""
    if not cfg.input_dir.exists():
        raise FileNotFoundError(f"Input directory not found: {cfg.input_dir}")

    input_files = sorted(cfg.input_dir.glob(cfg.input_glob))
    if not input_files:
        raise FileNotFoundError(f"No files matching {cfg.input_glob} in {cfg.input_dir}")

    extracted_dir = cfg.output_dir / "extracted_jsonl"
    adjudicated_dir = cfg.output_dir / "adjudicated_jsonl"
    audit_dir = cfg.output_dir / "audit"
    done_dir = cfg.output_dir / "done_rerun"
    checkpoint_file = cfg.output_dir / "checkpoint_rerun.json"

    for d in [extracted_dir, adjudicated_dir, audit_dir, done_dir]:
        ensure_dir(d)

    checkpoint: Dict[str, Any] = {
        "mode": "rerun_flagged",
        "input_dir": str(cfg.input_dir),
        "output_dir": str(cfg.output_dir),
        "prompt_style": cfg.prompt_style,
        "arbitration_model": cfg.arbitration_model,
        "max_workers": cfg.max_workers,
        "global_rps": cfg.global_rps,
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "finished_files": [],
    }
    if checkpoint_file.exists():
        try:
            old = load_json(checkpoint_file)
            if isinstance(old, dict):
                checkpoint.update(old)
        except Exception:
            pass

    finished_files = set(checkpoint.get("finished_files", []))
    rows: List[Dict[str, Any]] = []

    for input_file in input_files:
        shard_id = parse_shard_id(input_file)
        done_file = done_dir / f"{shard_id}.done"
        extracted_file = extracted_dir / f"extracted_{shard_id}.jsonl"
        out_file = adjudicated_dir / f"adjudicated_{shard_id}.jsonl"
        audit_file = audit_dir / f"rerun_audit_{shard_id}.jsonl"

        if not extracted_file.exists():
            log(f"Skipping {input_file.name}: missing extracted file {extracted_file.name}")
            continue

        if not cfg.force and (done_file.exists() or input_file.name in finished_files):
            log(f"Skipping completed arbitration shard: {input_file.name}")
            continue

        row = process_one_file_rerun(
            input_file=input_file, extracted_file=extracted_file,
            out_file=out_file, audit_file=audit_file,
            done_file=done_file, key_rotator=key_rotator, cfg=cfg,
        )
        rows.append(row)

        finished_files.add(input_file.name)
        checkpoint["finished_files"] = sorted(finished_files)
        checkpoint["last_finished_file"] = input_file.name
        checkpoint["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        save_json(checkpoint_file, checkpoint)

    checkpoint["finished_files"] = sorted(finished_files)
    checkpoint["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_json(checkpoint_file, checkpoint)

    flagged = sum(r.get("flagged", 0) for r in rows)
    scheduled = sum(r.get("scheduled", 0) for r in rows)
    ok = sum(r.get("ok", 0) for r in rows)
    still_review = sum(r.get("still_review", 0) for r in rows)
    errors = sum(r.get("errors", 0) for r in rows)

    log("Arbitration complete")
    log(f"  Shards: {len(input_files)}")
    log(f"  Flagged total: {flagged}")
    log(f"  Adjudicated: {scheduled}")
    log(f"  Successful: {ok}")
    log(f"  Still needs human review: {still_review}")
    log(f"  Errors: {errors}")
    log(f"  Output: {cfg.output_dir}")


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Stage 2: Structured clinical literature extraction"
    )
    p.add_argument("--mode", choices=["extract", "rerun_flagged"], default="extract")
    p.add_argument("--input-dir", default=str(DEFAULT_INPUT_DIR))
    p.add_argument("--input-glob", default=DEFAULT_INPUT_GLOB)
    p.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    p.add_argument("--base-url", default=DEFAULT_BASE_URL)
    p.add_argument("--prompt-style", choices=["long", "short"], default="short")
    p.add_argument("--primary-model", default=DEFAULT_PRIMARY_MODEL)
    p.add_argument("--arbitration-model", default=DEFAULT_ARBITRATION_MODEL)
    p.add_argument("--max-workers", type=int, default=DEFAULT_MAX_WORKERS)
    p.add_argument("--global-rps", type=float, default=DEFAULT_GLOBAL_RPS)
    p.add_argument("--request-timeout", type=int, default=DEFAULT_REQUEST_TIMEOUT)
    p.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES)
    p.add_argument("--backoff-base", type=float, default=DEFAULT_BACKOFF_BASE)
    p.add_argument("--backoff-max", type=float, default=DEFAULT_BACKOFF_MAX)
    p.add_argument("--write-buffer-size", type=int, default=DEFAULT_WRITE_BUFFER_SIZE)
    p.add_argument("--review-confidence-threshold", type=int, default=DEFAULT_REVIEW_CONF_THRESHOLD)
    p.add_argument("--enable-inline-second-pass", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--api-keys-file", default="")
    return p


def load_api_keys(args: argparse.Namespace) -> List[str]:
    """Load API keys from multiple sources."""
    if args.api_keys_file:
        p = Path(args.api_keys_file).expanduser().resolve()
        if not p.exists():
            raise FileNotFoundError(f"API keys file not found: {p}")
        keys = [
            line.strip()
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines()
        ]
        keys = [k for k in keys if k]
        if keys:
            return keys

    env_keys = os.getenv("LLM_API_KEYS", "")
    if env_keys.strip():
        tmp = re.split(r"[,;\n\r\t ]+", env_keys.strip())
        keys = [x.strip() for x in tmp if x.strip()]
        if keys:
            return keys

    return [k.strip() for k in DEFAULT_API_KEYS if k and k.strip()]


def main() -> None:
    args = build_arg_parser().parse_args()
    cfg = build_runtime_config(args)

    api_keys = load_api_keys(args)
    key_rotator = KeyRotator(api_keys)

    log(
        f"Starting: mode={cfg.mode}, prompt_style={cfg.prompt_style}, "
        f"primary_model={cfg.primary_model}, arbitration_model={cfg.arbitration_model}, "
        f"workers={cfg.max_workers}, rps={cfg.global_rps}"
    )
    log(f"Input: {cfg.input_dir}")
    log(f"Output: {cfg.output_dir}")

    if cfg.mode == "extract":
        run_extract(cfg, key_rotator)
    else:
        run_rerun_flagged(cfg, key_rotator)


if __name__ == "__main__":
    main()
