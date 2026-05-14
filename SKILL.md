---
name: clin-extract
description: >-
  Clinical literature structured data extraction pipeline. Design extraction schemas, generate
  PubMed search + prescreen + LLM-extraction Python scripts, and guide post-extraction analysis.
  Use when the user asks to extract structured fields from clinical/medical literature,
  build a clinical prediction model database, mine clinical trial data, extract diagnostic
  test characteristics, or any systematic clinical literature data extraction task.
  Trigger keywords: "临床数据抽提", "文献信息提取", "预测模型提取", "临床文献挖掘",
  "结构化提取", "literature extraction", "clinical data mining", "chart abstraction".
---

# Clinical Literature Structured Extraction

Use this skill to help users go from a clinical research question to a structured,
analysis-ready dataset extracted from PubMed literature via LLM.

## What this skill does

1. **Schema design** — Work with the user to define extraction fields (article-level
   and entity-level), controlled vocabularies, and quality scoring rubrics.
2. **Code generation** — Write three Python scripts customized to the user's schema,
   API endpoint, and concurrency requirements.
3. **Run guide** — Provide exact shell commands to execute the pipeline end-to-end.
4. **Analysis handoff** — Deliver analysis starter code so the user can immediately
   explore the extracted dataset.

## Pipeline overview

```
PubMed Search (pubmed_search.py)
    │
    ▼
Stage 1 ─ Prescreen (prescreen_filter.py)
    │  LLM filters: is this an original clinical
    │  prediction model / target study type?
    │  Excludes reviews, meta-analyses, editorials...
    ▼
Stage 2 ─ Structured extraction (extract_main.py)
    │  LLM extracts article_level + entity_level fields
    │  Quality scoring + validation flags
    │  Flagged-record review (arbitration model)
    ▼
Output JSONL → analysis scripts → figures & tables
```

## Step-by-step workflow

### Step 1: Understand the extraction goal

Ask the user:

1. What clinical domain? (e.g., cardiovascular, oncology, neurology, general)
2. What study type? (e.g., clinical prediction models, RCTs, diagnostic accuracy studies)
3. What is the output used for? (systematic review, database build, meta-analysis, landscape scan)

### Step 2: Design the extraction schema

Work with the user to define extraction fields at two levels:

**Article-level fields** (one set per paper):
- Bibliographic metadata (PMID, title, journal, pub_date)
- Study design, population, data source
- Primary disease/condition (raw + standardized)
- Quality scores with sub-dimensions

**Entity-level fields** (one or more per paper, e.g., per model, per arm, per outcome):
- Entity identifier, name, stage
- Target condition, outcome, time horizon
- Predictors/features, method
- Performance metrics
- Validation details
- Entity-level quality scores

Load `references/extraction-schema.md` for the full schema design guide with
controlled vocabularies, scoring rubrics, and field definitions.

**Key principle**: Design the schema so it generalizes. A well-designed schema for
clinical prediction models can be adapted to diagnostic studies, prognostic factor
studies, or intervention reviews by changing the entity definition and controlled
vocabularies.

### Step 3: Collect API configuration

Ask the user for:

| Item | Example | Required |
|------|---------|----------|
| API base URL | `https://dashscope.aliyuncs.com/compatible-mode/v1` | Yes |
| API key(s) | `sk-xxx...` (comma-separated or one per line) | Yes |
| Primary model | `qwen3.5-plus` or `gpt-4o` or `deepseek-chat` | Yes |
| Arbitration model | `qwen3-max` or `gpt-4o` (higher-capability for flagged records) | Recommended |

**Security note**: Never write API keys into generated scripts as defaults.
Always use environment variables or a separate config file (see `references/api-config.md`).

**Model selection guidance**:
- Prescreen: use a fast, cheap model (e.g., `qwen-flash`, `gpt-4o-mini`)
- Main extraction: use a balanced model (e.g., `qwen3.5-plus`, `gpt-4o`, `deepseek-chat`)
- Arbitration: use the strongest available model (e.g., `qwen3-max`, `gpt-4o`)

### Step 4: Generate the three scripts

Load the script templates from `scripts/` and customize them with the user's:

- Extraction schema (JSON structure in the prompt)
- Controlled vocabularies (subspecialties, outcome groups, method groups, etc.)
- API endpoint and model names
- PubMed search query (if needed)
- Output directory structure
- Concurrency settings

**Script 1: `pubmed_search.py`**
- PubMed E-utilities search with date range and filters
- Downloads articles as JSONL shards
- Handles rate limiting and retries
- Load template from `scripts/pubmed_search.py`

**Script 2: `prescreen_filter.py`**
- Reads JSONL shards
- LLM-based prescreening: keep only target study type
- Multi-key concurrency, checkpoint/resume
- Load template from `scripts/prescreen_filter.py`

**Script 3: `extract_main.py`**
- Two modes: `--mode extract` (primary) and `--mode rerun_flagged` (arbitration)
- Extracts article_level + entity_level fields
- Local JSON validation and normalization
- Quality scoring with review flags
- Load template from `scripts/extract_main.py`

### Step 5: Write the run guide

After generating the scripts, provide the user with a run guide:

```bash
# Step 1: PubMed search
nohup python3 pubmed_search.py > pubmed_search.log 2>&1 &

# Step 2: Prescreen (after search completes)
nohup python3 prescreen_filter.py > prescreen_filter.log 2>&1 &

# Step 3: Main extraction (after prescreen completes)
nohup python3 extract_main.py \
  --mode extract \
  --input-dir outputs/prescreen/screened_jsonl \
  --input-glob "screened_*.jsonl" \
  --output-dir outputs/extract \
  --primary-model <model> \
  --max-workers 15 \
  --global-rps 8 \
  --review-confidence-threshold 60 \
  > extract_main.log 2>&1 &

# Step 4: Arbitration (after main extraction)
nohup python3 extract_main.py \
  --mode rerun_flagged \
  --input-dir outputs/prescreen/screened_jsonl \
  --input-glob "screened_*.jsonl" \
  --output-dir outputs/extract \
  --arbitration-model <stronger-model> \
  --max-workers 15 \
  --global-rps 6 \
  --review-confidence-threshold 60 \
  > extract_rerun.log 2>&1 &
```

### Step 6: Analysis handoff

Generate an analysis starter script that:

- Loads the extracted JSONL files
- Produces summary statistics (counts, distributions)
- Creates basic visualizations (bar charts, heatmaps, temporal trends)
- Exports structured tables for manuscript

Load `references/analysis-guide.md` for standard analysis patterns.

## Schema customization guide

When the user's domain differs from clinical prediction models, adapt the schema:

| Study type | Entity | Key entity fields |
|------------|--------|-------------------|
| Prediction model | model | predictors, method, AUC, validation |
| RCT | arm | intervention, comparator, N, primary outcome |
| Diagnostic study | test | index test, reference standard, sens/spec |
| Prognostic study | factor | biomarker, cut-off, HR, endpoint |
| Prevalence study | subgroup | population, N, prevalence, CI |

The article-level structure stays largely the same; the entity-level structure changes.

## Quality scoring

Every schema should include quality scores (article-level + entity-level) with 4-5 sub-dimensions:

| Dimension | What it captures | Range |
|-----------|-----------------|-------|
| sample_size_score | Adequacy of sample/events | 0-20 |
| validation_rigor_score | Internal/external validation | 0-20 |
| method_score | Appropriateness of methods | 0-20 |
| performance_score | Reported performance metrics | 0-20 |
| clinical_applicability_score | Real-world usability | 0-20 |
| total_score | Sum of above | 0-100 |

## Related files

| File | Open when |
|------|-----------|
| [references/extraction-schema.md](references/extraction-schema.md) | Designing the extraction schema, controlled vocabularies, and field definitions |
| [references/prompt-templates.md](references/prompt-templates.md) | Writing LLM prompts for prescreen and extraction stages |
| [references/api-config.md](references/api-config.md) | Configuring API keys, endpoints, model selection, and concurrency |
| [references/analysis-guide.md](references/analysis-guide.md) | Post-extraction analysis: loading data, summary stats, figures, export |
| [scripts/pubmed_search.py](scripts/pubmed_search.py) | Template for PubMed E-utilities search and download |
| [scripts/prescreen_filter.py](scripts/prescreen_filter.py) | Template for LLM-based first-stage screening |
| [scripts/extract_main.py](scripts/extract_main.py) | Template for LLM-based structured extraction with arbitration |

## Principles

1. **Primary sources over guesswork** — Extract only from provided title/abstract/metadata.
   Do not fabricate fields. Mark unclear fields as `null`, `[]`, or `"unclear"`.
2. **Schema-driven** — The extraction schema defines the output. Spend time on schema
   design before writing code.
3. **Conservative standardization** — Standardize disease names, outcomes, and methods
   conservatively. Keep raw text alongside standardized values.
4. **Quality over quantity** — Flag low-confidence extractions for human review.
   Use a stronger model for arbitration.
5. **Reproducible by design** — Every script supports checkpoint/resume, deterministic
   output ordering, and logged parameters.
6. **Security-first** — Never hardcode API keys. Use environment variables or config files.
