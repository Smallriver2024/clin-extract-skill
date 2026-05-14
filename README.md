# clin-extract

A Claude skill for AI-driven structured data extraction from clinical literature.
Covers the full pipeline: extraction schema design → PubMed search → LLM-based
prescreening → structured field extraction with quality scoring → arbitration of
low-confidence records → post-extraction analysis.

This skill is domain-agnostic within clinical medicine. While it was developed for
clinical prediction model extraction, the schema-driven design generalizes to RCT
data extraction, diagnostic accuracy studies, prognostic factor mining, prevalence
surveys, and other systematic clinical literature chart abstraction tasks.

---

## Installation

### Natural language (via Claude Code)

1. Install [Claude Code](https://docs.anthropic.com/en/docs/claude-code).
2. Clone this repository or copy the `clin-extract/` directory into your project:
   ```bash
   git clone https://github.com/Smallriver2024/clin-extract-skill.git
   ```
3. Move the `clin-extract/` folder into your working directory. Claude Code will
   automatically discover and load `SKILL.md` when you trigger the skill with
   keywords such as "clinical data extraction", "structured literature mining",
   "prediction model extraction", or "build an extraction pipeline".
4. In your Claude Code session, describe your extraction task. The skill will
   guide you through schema design, collect your API credentials, and generate
   customized Python scripts.

### Script-based installation (standalone)

If you prefer to run the template scripts directly without Claude Code:

```bash
# 1. Clone the repository
git clone https://github.com/Smallriver2024/clin-extract-skill.git
cd clin-extract-skill

# 2. Install Python dependencies
pip install requests

# 3. Set required environment variables
export LLM_API_KEYS="sk-your-key-1,sk-your-key-2"
export LLM_BASE_URL="https://dashscope.aliyuncs.com/compatible-mode/v1"
export LLM_MODEL="qwen-flash"                      # for prescreen
export LLM_PRIMARY_MODEL="qwen3.5-plus"            # for main extraction
export LLM_ARBITRATION_MODEL="qwen3-max"           # for arbitration
export PUBMED_EMAIL="your-email@institution.edu"

# 4. Customize the prompts and controlled vocabularies in the scripts
#    to match your domain, then run the pipeline (see Quick Start below).
```

> **Note:** The scripts in `scripts/` are templates. They work out of the box for
> clinical prediction model extraction, but the real power comes from customizing
> them to your domain via Claude Code's guided workflow. API keys are always read
> from environment variables — never hardcoded.

---

## How it works

### AI-assisted prescreening

Candidate records first pass through local heuristic pre-filters to reduce
unnecessary LLM calls. Pre-filtering rules operate on titles, abstracts, and
publication types, automatically excluding:

- Records with missing titles or abstracts
- Review-type publication types (reviews, meta-analyses, systematic reviews,
  editorials, letters, comments, guidelines, case reports, etc.)
- Records whose titles explicitly indicate reviews or case reports
- Records lacking prediction-model keywords or clinical/patient context cues

Pre-filtering keywords cover prediction-model-related expressions such as
*prediction model*, *risk model*, *nomogram*, *risk score*, *machine learning*,
*deep learning*, *classifier*, *model*, and *score*, while also requiring the
presence of clinical context terms such as *patient*, *clinical*, *hospital*,
*cohort*, *disease*, *diagnosis*, or *prognosis*. Records suspected of being
"validation-only, comparison-only, or benchmark-only" are excluded at the local
stage if they do not also contain model-building terms such as *develop*,
*derive*, *build*, *construct*, *train*, *establish*, *create*, or *update*.

Records that pass local pre-filtering enter Stage 1 LLM screening. This stage
uses the **qwen-flash** model via the DashScope OpenAI-compatible API. The prompt
instructs the model to determine, based solely on PMID, title, abstract, and
publication types, whether a record should be included in a clinical prediction
model original research database, and to output a controlled JSON object. Output
fields include `include`, `is_original_article`, `is_clinical_prediction_model`,
`has_model_building`, `exclude_reason`, and `confidence`. A record proceeds to
structured extraction only when `include`, `is_original_article`,
`is_clinical_prediction_model`, and `has_model_building` are all `true`. The
prescreening program supports multi-threaded concurrency, API key rotation,
global request rate limiting, retry with exponential backoff, sharded output,
and checkpoint/resume.

<details>
<summary><b>Prescreen system prompt (click to expand)</b></summary>

```
You are a clinical literature screening assistant.

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
6. Output JSON only.

Return this JSON:
{
  "include": true/false,
  "is_original_article": true/false,
  "is_clinical_prediction_model": true/false,
  "has_model_building": true/false,
  "exclude_reason": "brief reason if excluded",
  "confidence": 0-100
}
```
</details>

### AI-driven structured information extraction

Stage 2 structured extraction operates on the `screened_*.jsonl` files produced
by Stage 1. We developed an AI-driven literature information extraction Agent
Skill for clinical prediction model studies, packaging literature screening rules,
field definitions, controlled classification systems, JSON schema, quality scoring
rules, error handling, and review flagging into a reusable workflow. The complete
prompts, field definitions, and run examples for this Agent Skill are open-sourced
in this repository.

Structured extraction uses the **qwen3.5-plus** model. For low-confidence records
or those flagged for human review, the program supports a second-pass arbitration
using **qwen3-max**, a higher-capability model. All model calls use `temperature=0`
and request `json_object` format to improve output stability and parsability. The
prompt explicitly instructs the model to:

- Use only the provided title, abstract, and metadata
- Not infer information from full text or general knowledge
- Return `null`, empty list `[]`, or `"unclear"` for ambiguous fields
- Split multiple models or outcomes into separate items under `model_level`
- Conservatively standardize disease names, outcomes, clinical subspecialties,
  modeling methods, and validation types

<details>
<summary><b>Extraction system prompt — long form (click to expand)</b></summary>

```
You are a medical prediction-model information extraction assistant.

Your task is to extract structured information from PubMed metadata, title, and
abstract for papers that describe original clinical prediction model studies.

Rules:
1. Use only the provided title, abstract, and metadata.
2. Do not infer information that is not stated.
3. If a field is unclear or not reported, return null, [], or "unclear".
4. One article may contain multiple models. Split them into separate items in
   "model_level".
5. Standardize disease names and outcomes conservatively.
6. Assign one primary clinical subspecialty for each model.
7. Extract whether external/internal validation is reported at both article and
   model levels.
8. Score study and model quality (total 100) using five sub-scores (0-20 each):
   sample_size_score, validation_rigor_score, method_score, performance_score,
   clinical_applicability_score.
9. Output valid JSON only. No explanations.

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
temporal_validation, external_validation, independent_validation_cohort,
unclear

Be conservative, precise, and schema-compliant.
```
</details>

### Extraction schema

The extraction schema comprises two main structures: **article-level** and
**model-level**.

**Article-level fields** include: PMID, title, journal, publication date,
prediction type, study design, data source type, country or region, primary
disease (raw), standardized disease name, primary clinical subspecialty,
presence of external validation, presence of internal validation, whether the
article is a multi-model study, number of models described, study quality
score, and overall notes.

**Model-level fields** include: model ID within article, model name, prediction
type, target disease (raw and standardized), clinical subspecialty, outcome
(raw and standardized), outcome group, prediction time horizon, sample size,
number of events, predictors (raw list), predictor domains, modeling method
(raw and standardized method group), validation type, presence of external
validation, presence of internal validation, performance metrics, comparator
model, model quality score, and notes.

**Performance metrics** include: AUC, C-index, sensitivity, specificity,
positive predictive value, negative predictive value, and whether calibration,
decision curve analysis (DCA), and net reclassification improvement (NRI) are
reported.

<details>
<summary><b>Full extraction JSON schema (click to expand)</b></summary>

```json
{
  "article_level": {
    "pmid": "",
    "title": "",
    "journal": "",
    "pub_date": "",
    "prediction_type": "",
    "article_model_type": "",
    "clinical_use_case": "",
    "target_population_summary": "",
    "study_design": "",
    "data_source_type": "",
    "country_or_region": "",
    "primary_disease_raw": "",
    "primary_disease_standard": "",
    "primary_subspecialty": "",
    "article_has_external_validation": null,
    "article_has_internal_validation": null,
    "is_multimodel_article": false,
    "number_of_models_described": 0,
    "study_quality_score": {
      "sample_size_score": null,
      "validation_rigor_score": null,
      "method_score": null,
      "performance_score": null,
      "clinical_applicability_score": null,
      "total_score": null
    },
    "overall_notes": ""
  },
  "model_level": [
    {
      "model_id_within_article": 1,
      "model_name": "",
      "model_stage": "",
      "prediction_type": "",
      "target_disease_raw": "",
      "target_disease_standard": "",
      "disease_subspecialty": "",
      "outcome_raw": "",
      "outcome_standard": "",
      "outcome_group": "",
      "time_horizon": "",
      "sample_size": null,
      "events": null,
      "predictors_raw": [],
      "predictor_domains": [],
      "model_method_raw": "",
      "model_method_group": "",
      "validation_type": "",
      "has_external_validation": null,
      "has_internal_validation": null,
      "performance_metrics": {
        "auc": null,
        "c_index": null,
        "sensitivity": null,
        "specificity": null,
        "ppv": null,
        "npv": null,
        "calibration_reported": null,
        "dca_reported": null,
        "nri_reported": null
      },
      "model_quality_score": {
        "sample_size_score": null,
        "validation_rigor_score": null,
        "method_score": null,
        "performance_score": null,
        "clinical_applicability_score": null,
        "total_score": null
      },
      "comparator": "",
      "notes": ""
    }
  ],
  "normalization": {
    "disease_standardization_confidence": 0,
    "subspecialty_classification_confidence": 0,
    "overall_extraction_confidence": 0
  },
  "quality_flags": {
    "disease_ambiguous": false,
    "outcome_ambiguous": false,
    "method_ambiguous": false,
    "requires_human_review": false
  }
}
```
</details>

### Controlled classification systems

The extraction schema enforces four controlled vocabularies:

| Category | Options |
|---|---|
| **Clinical subspecialties** (21) | cardiology, oncology, neurology, respiratory_medicine, gastroenterology, hepatology, nephrology, endocrinology, hematology, rheumatology, infectious_disease, critical_care, emergency_medicine, surgery, anesthesiology_perioperative, obstetrics_gynecology, pediatrics, psychiatry, radiology, rehabilitation, public_health, general_medicine, other |
| **Outcome groups** (10) | mortality, survival, recurrence, complication, treatment_response, functional_outcome, diagnosis, event_risk, hospitalization_utilization, other |
| **Method groups** (15) | logistic_regression, cox_regression, lasso_cox, lasso_logistic, linear_regression, tree_based_ml, svm, neural_network, deep_learning, nomogram, risk_score, ensemble_model, signature_model, statistical_model_other, unclear |
| **Validation types** (8) | none_reported, internal_split, cross_validation, bootstrap, temporal_validation, external_validation, independent_validation_cohort, unclear |

Fields that cannot be mapped to any preset option are set to `"other"` or
`"unclear"` and recorded as field standardization issues.

### Quality scoring

To enable large-scale literature surveillance, the extraction pipeline produces
semi-quantitative quality scores at both the study level and the model level.
Each score totals 100 points across five sub-dimensions (0–20 each):

| Sub-dimension | What it assesses |
|---|---|
| **sample_size_score** | Adequacy of sample size and number of events |
| **validation_rigor_score** | Presence and quality of internal/external validation |
| **method_score** | Appropriateness of modeling methodology |
| **performance_score** | Completeness of reported performance metrics |
| **clinical_applicability_score** | Real-world usability and implementation readiness |

The study-level score summarizes the overall methodological and reporting quality
of an article. The model-level score evaluates individual models on sample size,
validation approach, modeling method, reported performance, and potential clinical
utility. The program performs local range validation on all score fields and
recalculates the total when all five sub-scores are present. If the model-reported
total does not match the sum of sub-scores, the mismatch is recorded as a
structural issue and the record is flagged for review.

> **Important note:** These quality scores are designed for large-scale monitoring
> and cross-sectional comparison. They are not equivalent to manual PROBAST or
> PROBAST+AI risk-of-bias assessments. They should be interpreted as descriptive
> indicators rather than formal bias risk judgments.

### Validation, normalization & review flagging

All LLM responses undergo local JSON parsing, structural validation, and field
normalization:

- **JSON parsing**: code fence markers are stripped; the first valid JSON object
  is extracted via brace-matching
- **Structural validation**: the presence of `article_level`, `model_level`,
  `normalization`, and `quality_flags` is checked; missing or malformed
  structures are replaced with default empty templates
- **Field normalization**: numeric fields are coerced to `int` or `float`;
  boolean fields to `true`, `false`, or `null`; list fields to string arrays;
  controlled-vocabulary fields are mapped to preset enumerations
- **Validation inference**: model-level `has_external_validation` and
  `has_internal_validation` are inferred from `validation_type` when missing
  (`external_validation` and `independent_validation_cohort` → external;
  `internal_split`, `cross_validation`, `bootstrap`, `temporal_validation` →
  internal); article-level validation flags are aggregated from model-level data
- **Multi-model handling**: model entries are re-indexed sequentially; articles
  with multiple models are marked as `is_multimodel_article`

An automatic review-flagging mechanism marks records as `requires_human_review`
when: disease, outcome, or method ambiguity is detected; the overall extraction
confidence falls below the configured threshold; model-level entries are missing;
all model quality scores are absent; or local structural validation identifies
field issues. Flagged records can enter a second-pass arbitration workflow using
a higher-capability model (e.g., **qwen3-max**), with results written to a
separate adjudicated JSONL file for subsequent manual inspection or sensitivity
analysis.

---

## Pipeline

```
PubMed Search ──→ Stage 1: Prescreen ──→ Stage 2: Extract ──→ Analysis
   (E-utilities)    (local pre-filter      (structured fields    (figures, tables,
                     + qwen-flash LLM)       + quality scores      manuscript export)
                                              + qwen3.5-plus)
                                    ↓
                              Flagged records
                                    ↓
                              Arbitration
                              (qwen3-max)
```

---

## When to use

- Building a database of clinical prediction models from PubMed
- Extracting structured data from clinical trial reports
- Mining diagnostic accuracy studies for meta-analysis
- Systematic chart abstraction from medical literature
- Any task requiring structured field extraction from hundreds to thousands of
  PubMed articles

---

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

---

## Quick start

After configuring your environment variables and customizing the scripts:

```bash
# 1. PubMed search
nohup python3 scripts/pubmed_search.py > pubmed_search.log 2>&1 &

# 2. Prescreen (after search completes)
nohup python3 scripts/prescreen_filter.py > prescreen_filter.log 2>&1 &

# 3. Main extraction (after prescreen completes)
nohup python3 scripts/extract_main.py \
  --mode extract \
  --input-dir outputs/prescreen/screened_jsonl \
  --input-glob "screened_*.jsonl" \
  --output-dir outputs/extract \
  --prompt-style long \
  --primary-model qwen3.5-plus \
  --max-workers 15 \
  --global-rps 8 \
  --review-confidence-threshold 60 \
  > extract_main.log 2>&1 &

# 4. Arbitration of flagged records (after step 3)
nohup python3 scripts/extract_main.py \
  --mode rerun_flagged \
  --input-dir outputs/prescreen/screened_jsonl \
  --input-glob "screened_*.jsonl" \
  --output-dir outputs/extract \
  --prompt-style long \
  --arbitration-model qwen3-max \
  --max-workers 15 \
  --global-rps 6 \
  --review-confidence-threshold 60 \
  > extract_rerun.log 2>&1 &
```

---

## Design principles

1. **Schema-first** — The extraction schema is the single source of truth that
   drives prompt design, validation logic, and output structure.
2. **Generalizable** — Change the entity definition and controlled vocabularies,
   and the same pipeline works for diagnostic studies, RCTs, or prognostic factor
   extraction.
3. **Conservative extraction** — Extract only from provided metadata. Mark
   unclear fields as `null`, `[]`, or `"unclear"`. Never hallucinate.
4. **Quality-aware** — Semi-quantitative scoring enables large-scale monitoring.
   Flagged records get a second pass with a stronger model.
5. **Reproducible** — Every script supports checkpoint/resume, deterministic
   output ordering, and logged parameters.
6. **Secure by default** — API keys are read from environment variables. No
   credentials are ever hardcoded.

---

## Notes

- The skill generates code customized to your schema and API. The scripts in
  `scripts/` are production-tested templates for clinical prediction model
  extraction; customize them for other domains.
- All scripts support checkpoint/resume — if a run is interrupted, re-running
  the same command picks up where it left off.
- The quality scores are descriptive monitoring indicators, not formal risk-of-bias
  assessments. For formal appraisal, use PROBAST or PROBAST+AI.
