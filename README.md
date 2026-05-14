# `clin-extract` skill

A Claude skill for building clinical literature structured data extraction pipelines.
Covers the full workflow: schema design → PubMed search → LLM prescreening → structured
extraction → arbitration → analysis.

This skill is domain-agnostic within clinical medicine. While it was born from clinical
prediction model extraction, the schema-driven design generalizes to RCT data extraction,
diagnostic test characteristic mining, prognostic factor studies, prevalence surveys,
and any systematic clinical literature chart abstraction.

## What it does

1. **Schema design** — Helps the user define extraction fields at article-level and
   entity-level, with controlled vocabularies and quality scoring rubrics.
2. **Code generation** — Writes three customized Python scripts (PubMed search, LLM
   prescreen, LLM structured extraction) based on the user's schema, API endpoint,
   and model choices.
3. **Run guide** — Provides exact shell commands and execution order.
4. **Analysis handoff** — Generates analysis starter code for summary statistics,
   visualizations, and table export.

## Pipeline

```
PubMed Search ──→ Stage 1: Prescreen ──→ Stage 2: Extract ──→ Analysis
                    (filter non-target     (structured fields    (figures, tables,
                     study types via LLM)   + quality scores)     manuscript export)
```

## When to use

- Building a database of clinical prediction models from PubMed
- Extracting structured data from clinical trial reports
- Mining diagnostic accuracy studies for meta-analysis
- Systematic chart abstraction from medical literature
- Any task requiring structured field extraction from hundreds to thousands of PubMed articles

## File structure

```
clin-extract/
├── SKILL.md                           # Skill rules + workflow (loaded by Claude)
├── README.md                          # This file
├── references/
│   ├── extraction-schema.md           # Schema design guide + field definitions
│   ├── prompt-templates.md            # LLM prompt patterns for prescreen & extract
│   ├── api-config.md                  # API key, endpoint, model, concurrency guidance
│   └── analysis-guide.md             # Post-extraction analysis patterns
├── scripts/
│   ├── pubmed_search.py               # PubMed E-utilities search template
│   ├── prescreen_filter.py            # LLM prescreening filter template
│   └── extract_main.py                # LLM structured extraction template
└── .gitignore
```

## Design intent

The skill is designed to be **schema-first**: the extraction schema is the single source
of truth that drives prompt design, validation logic, and output structure. A well-designed
schema makes the code nearly write itself.

It is also designed to be **generalizable**. Change the entity definition and controlled
vocabularies, and the same pipeline extracts diagnostic test data, RCT characteristics,
or prognostic factor information instead of prediction model details.

## Reference map

- `extraction-schema.md` — Complete field catalog, controlled vocabularies, scoring rubrics, and schema adaptation guide for non-prediction-model domains.
- `prompt-templates.md` — System/user prompt patterns for prescreen and extraction stages, with examples of prompt engineering for clinical tasks.
- `api-config.md` — Supported LLM providers (OpenAI, DashScope, DeepSeek, etc.), model selection by task, concurrency tuning, and security best practices.
- `analysis-guide.md` — Loading JSONL output, computing summary statistics, generating publication-ready figures, and exporting structured tables.
- `scripts/pubmed_search.py` — Production-tested PubMed E-utilities downloader with date-range chunking, rate limiting, and JSONL sharding.
- `scripts/prescreen_filter.py` — Multi-key concurrent LLM prescreener with local pre-filters, checkpoint/resume, and configurable study-type targeting.
- `scripts/extract_main.py` — Two-pass LLM extractor (primary extraction + arbitration of flagged records) with JSON validation, normalization, and quality scoring.

## Quick start (for the user)

After Claude generates your scripts, run them in order:

```bash
# 1. PubMed search
python3 pubmed_search.py

# 2. Prescreen
python3 prescreen_filter.py

# 3. Main extraction
nohup python3 extract_main.py \
  --mode extract \
  --input-dir outputs/prescreen/screened_jsonl \
  --input-glob "screened_*.jsonl" \
  --output-dir outputs/extract \
  --primary-model <your-model> \
  --max-workers 15 \
  --global-rps 8 \
  > extract_main.log 2>&1 &

# 4. Arbitration (after step 3)
nohup python3 extract_main.py \
  --mode rerun_flagged \
  --input-dir outputs/prescreen/screened_jsonl \
  --input-glob "screened_*.jsonl" \
  --output-dir outputs/extract \
  --arbitration-model <your-strongest-model> \
  --max-workers 15 \
  --global-rps 6 \
  > extract_rerun.log 2>&1 &
```

## Notes

- The skill generates code customized to your schema and API. It does not ship pre-built
  extraction code for any specific clinical domain.
- All generated scripts support checkpoint/resume — if a run is interrupted, re-running
  the same command picks up where it left off.
- API keys are handled via environment variables or a separate config file, never
  hardcoded into scripts.
