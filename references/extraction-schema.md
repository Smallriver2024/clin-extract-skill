# Extraction Schema Design Guide

This reference covers how to design the extraction schema — the JSON structure that defines
what fields the LLM extracts from each article. A well-designed schema is the single most
important factor in extraction quality.

## Schema structure

Every extraction schema has two levels:

```
{
  "article_level": { ... },   // one set per paper
  "entity_level": [ ... ],    // one or more per paper
  "normalization": { ... },   // confidence scores
  "quality_flags": { ... }    // review flags
}
```

### Article-level fields

These describe the paper as a whole. They are extracted once per article.

| Field | Type | Description | Required |
|-------|------|-------------|----------|
| `pmid` | string | PubMed ID | Yes |
| `title` | string | Article title | Yes |
| `journal` | string | Journal name | Yes |
| `pub_date` | string | Publication date (YYYY-MM-DD) | Yes |
| `study_type` | string | Study type classification | Yes |
| `study_design` | string | e.g., retrospective, prospective, RCT | Yes |
| `data_source_type` | string | e.g., registry, EHR, trial, survey | Yes |
| `country_or_region` | string | Study country/region | Recommended |
| `target_population_summary` | string | Brief population description | Recommended |
| `primary_disease_raw` | string | Disease as stated in paper | Yes |
| `primary_disease_standard` | string | Standardized disease name | Yes |
| `primary_subspecialty` | string | Clinical subspecialty (controlled vocabulary) | Yes |
| `article_has_external_validation` | boolean | Any external validation reported | Recommended |
| `article_has_internal_validation` | boolean | Any internal validation reported | Recommended |
| `study_quality_score` | object | Quality score with sub-dimensions | Recommended |
| `overall_notes` | string | Extractor notes | Optional |

### Entity-level fields

These describe individual entities within a paper (models, arms, tests, factors).
A single paper can have multiple entities.

| Field | Type | Description | Required |
|-------|------|-------------|----------|
| `entity_id` | int | 1-based index within article | Yes |
| `entity_name` | string | Name/identifier of the entity | Recommended |
| `entity_stage` | string | e.g., development, validation, update | Recommended |
| `target_condition_raw` | string | Condition as stated | Yes |
| `target_condition_standard` | string | Standardized condition name | Yes |
| `outcome_raw` | string | Outcome as stated | Yes |
| `outcome_standard` | string | Standardized outcome | Recommended |
| `outcome_group` | string | Outcome category (controlled vocabulary) | Recommended |
| `time_horizon` | string | Prediction/time horizon | Optional |
| `sample_size` | int | Total sample size | Recommended |
| `events` | int | Number of events (for time-to-event) | Optional |
| `predictors_raw` | string[] | Predictor names as stated | Recommended |
| `predictor_domains` | string[] | e.g., demographics, labs, imaging | Recommended |
| `method_raw` | string | Method as stated | Yes |
| `method_group` | string | Method category (controlled vocabulary) | Yes |
| `validation_type` | string | Validation approach (controlled vocabulary) | Recommended |
| `has_external_validation` | boolean | External validation performed | Recommended |
| `has_internal_validation` | boolean | Internal validation performed | Recommended |
| `performance_metrics` | object | AUC, C-index, sensitivity, specificity, etc. | Recommended |
| `entity_quality_score` | object | Quality score with sub-dimensions | Recommended |
| `notes` | string | Extractor notes | Optional |

## Controlled vocabularies

Define controlled vocabularies for categorical fields. The LLM must choose from these
options (or output `"other"` / `"unclear"`).

### Clinical subspecialties

```
cardiology, oncology, neurology, respiratory_medicine, gastroenterology,
hepatology, nephrology, endocrinology, hematology, rheumatology,
infectious_disease, critical_care, emergency_medicine, surgery,
anesthesiology_perioperative, obstetrics_gynecology, pediatrics, psychiatry,
radiology, rehabilitation, public_health, general_medicine, other
```

### Outcome groups

```
mortality, survival, recurrence, complication, treatment_response,
functional_outcome, diagnosis, event_risk, hospitalization_utilization, other
```

### Method groups (prediction models)

```
logistic_regression, cox_regression, lasso_cox, lasso_logistic,
linear_regression, tree_based_ml, svm, neural_network, deep_learning,
nomogram, risk_score, ensemble_model, signature_model,
statistical_model_other, unclear
```

### Method groups (alternative: RCT interventions)

```
pharmacological, surgical, behavioral, device, diagnostic_strategy,
screening_program, rehabilitation, lifestyle, dietary, combination, other
```

### Validation types

```
none_reported, internal_split, cross_validation, bootstrap,
temporal_validation, external_validation, independent_validation_cohort, unclear
```

## Quality scoring rubric

Both article-level and entity-level should have quality scores with these sub-dimensions:

| Sub-score | Range | What to evaluate |
|-----------|-------|-----------------|
| `sample_size_score` | 0-20 | Sample size adequacy for the research question. <100 = 0-5, 100-500 = 5-10, 500-2000 = 10-15, >2000 = 15-20 |
| `validation_rigor_score` | 0-20 | Presence and quality of validation. No validation = 0-5, internal only = 5-12, external = 12-20 |
| `method_score` | 0-20 | Methodological appropriateness. Unclear method = 0-5, basic = 5-12, advanced/ensemble = 12-20 |
| `performance_score` | 0-20 | Completeness of reported metrics. No metrics = 0, AUC only = 5-10, discrimination+calibration = 10-18, full report = 18-20 |
| `clinical_applicability_score` | 0-20 | Real-world usability. No applicability discussion = 0-5, model presentation = 5-12, decision analysis + implementation = 12-20 |
| `total_score` | 0-100 | Sum of the five sub-scores |

## Schema adaptation for different study types

### Clinical prediction model → Diagnostic accuracy study

| Prediction model field | Diagnostic study field |
|------------------------|----------------------|
| `model_id` | `test_id` |
| `model_name` | `index_test_name` |
| `predictors_raw` | `index_test_description` |
| `model_method_group` | `test_modality` (lab, imaging, clinical, etc.) |
| AUC | sensitivity, specificity, PPV, NPV |
| — | `reference_standard` (new field) |

### Clinical prediction model → RCT

| Prediction model field | RCT field |
|------------------------|-----------|
| `model_id` | `arm_id` |
| `model_name` | `intervention_name` |
| `model_method_group` | `intervention_type` |
| `predictors_raw` | — (replace with `inclusion_criteria`) |
| — | `comparator`, `primary_endpoint`, `allocation` (new fields) |

## Schema design checklist

Before finalizing a schema, verify:

- [ ] Every categorical field has a defined controlled vocabulary
- [ ] Standardized fields are paired with raw-text counterparts
- [ ] Quality scores have clear scoring guidance
- [ ] Entity-level fields cover one entity per list item
- [ ] The schema can handle papers with zero, one, or multiple entities
- [ ] Ambiguity flags exist for fields that commonly require judgment
- [ ] Required vs. optional fields are clearly marked
