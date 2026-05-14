# Prompt Templates

This reference provides prompt patterns for the two LLM stages: prescreen filtering
and structured extraction. Customize these with the user's domain, schema, and
controlled vocabularies.

## Prescreen prompt

The prescreen stage filters out non-target study types. The prompt must be strict
(high precision) because false positives waste extraction quota.

### System prompt pattern

```
You are a [domain] literature screening assistant.

Your task is to determine whether a PubMed article should be included
in a database of [target study type].

Rules:
1. Use only the title, abstract, and publication types.
2. INCLUDE only if the paper describes [inclusion criteria].
3. EXCLUDE: reviews, meta-analyses, systematic reviews, case reports,
   editorials, letters, comments, guidelines, protocols, [other exclusions].
4. EXCLUDE papers that only [exclusion scenarios].
5. When uncertain, default to EXCLUDE (conservative screening).
6. Output JSON only.
```

### User prompt pattern

```
Determine whether this article should be included.

PMID: {pmid}
Title: {title}
Publication types: {pub_types}
Abstract: {abstract}

Return JSON:
{
  "decision": "include" | "exclude" | "uncertain",
  "reason": "brief reason in [language]",
  "is_original_research": true | false,
  "study_type": "brief classification"
}
```

### Local pre-filters (before API call)

Apply these regex/simple checks to reduce API calls:

```python
EXCLUDE_PUB_TYPES = {
    'review', 'meta-analysis', 'systematic review', 'case report',
    'editorial', 'letter', 'comment', 'news', 'guideline', 'protocol',
    'retracted publication', 'published erratum', 'interview',
    'congresses', 'lecture', 'video-audio media', 'patient education handout',
}

EXCLUDE_TITLE_PATTERNS = [
    r'\b(review|meta.analysis|systematic review)\b',
    r'\b(case report|case study)\b',
    r'\b(protocol|guideline|consensus)\b',
    r'\b(bioinformatics|genomic|transcriptomic|proteomic)\b',  # domain-specific
]
```

## Extraction prompt

The extraction stage produces structured JSON. The prompt must be precise about
the JSON schema, field definitions, and standardization rules.

### System prompt pattern (long version)

```
You are a [domain] information extraction assistant.

Your task is to extract structured information from PubMed metadata,
title, and abstract for papers that describe [target study type].

Rules:
1. Use only the provided title, abstract, and metadata.
2. Do not infer information that is not stated.
3. If a field is unclear or not reported, return null, [], or "unclear".
4. One article may contain multiple [entities]; split them into separate
   items in "[entity_level]".
5. Standardize [key concepts] conservatively.
6. Assign one primary [classification] for each entity.
7. Extract [validation/quality markers] at both article and entity levels.
8. Score [study/entity] quality with a total of 100, using sub-scores (0-20 each):
   [list sub-score dimensions].
9. Output valid JSON only. No explanations.

[Controlled vocabulary options...]

[Standardization rules...]

Be conservative, precise, and schema-compliant.
```

### System prompt pattern (short version, for cheaper models)

```
You extract structured information for a [domain] database from PubMed
title, abstract, and metadata.

Rules:
- Use only provided information.
- Do not hallucinate.
- If unclear, return null, [], or "unclear".
- One article may contain multiple [entities]; split them in "[entity_level]".
- Standardize [key concepts] conservatively.
- Assign one primary [classification] per entity.
- Extract [validation/quality markers].
- Score quality with total 100 using sub-scores (0-20 each): [sub-score list].
- Output JSON only.

[Controlled vocabularies - condensed...]
```

### User prompt template

```
Extract structured information from the following article.

Return JSON only.

PMID: {pmid}
Title: {title}
Journal: {journal}
Publication date: {pub_date}
Publication types: {pub_types}
Keywords: {keywords}
MeSH terms: {mesh_terms}
Abstract:
{abstract}

Return this JSON structure:

{
  "article_level": {
    ...field placeholders...
  },
  "entity_level": [
    {
      ...field placeholders...
    }
  ],
  "normalization": {
    "..._confidence": 0,
    "overall_extraction_confidence": 0
  },
  "quality_flags": {
    "..._ambiguous": false,
    "requires_human_review": false
  }
}
```

## Language adaptation

### Chinese-language output

When the user is Chinese-speaking or requests Chinese output:

- System prompt and rules stay in English (LLMs follow English instructions better)
- Field descriptions in the user prompt template can be bilingual
- `notes`, `reason` fields output in Chinese
- Standardized disease/outcome names in English, raw text preserved as-is

### Multilingual PubMed records

- Chinese-language PubMed records: keep raw text in Chinese, standardize to English
- Non-English abstracts: flag `abstract_language` in article-level fields
- MeSH terms are always English; use them for standardization

## Prompt quality checklist

- [ ] System prompt states "use only provided information" rule
- [ ] System prompt lists the complete controlled vocabulary
- [ ] User prompt includes the full JSON skeleton
- [ ] Fields that accept `null` are explicitly noted
- [ ] Standardization rules are concrete, not vague
- [ ] The "Output JSON only" constraint is stated in both system and user prompts
