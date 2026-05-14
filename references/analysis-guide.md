# Post-Extraction Analysis Guide

This reference covers standard analysis patterns for extracted clinical literature data.
Generate an analysis script customized to the user's extraction schema.

## Data loading

```python
import json
import pandas as pd
from pathlib import Path
from glob import glob

def load_extracted_data(output_dir: str, pattern: str = "extracted_*.jsonl"):
    """Load all extracted JSONL files into DataFrames."""
    records = []
    for f in sorted(glob(f"{output_dir}/{pattern}")):
        with open(f) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    records.append(json.loads(line))

    # Split into article-level and entity-level
    articles = []
    entities = []
    for r in records:
        art = r.get("article_level", {})
        art["_source_pmid"] = art.get("pmid")
        articles.append(art)
        for ent in r.get("entity_level", []):
            ent["_source_pmid"] = art.get("pmid")
            entities.append(ent)

    df_art = pd.DataFrame(articles)
    df_ent = pd.DataFrame(entities)
    return df_art, df_ent
```

## Summary statistics

### Basic counts

```python
print(f"Total articles extracted: {len(df_art)}")
print(f"Total entities extracted: {len(df_ent)}")
print(f"Articles with entities: {df_art['pmid'].nunique()}")
print(f"Mean entities per article: {len(df_ent) / len(df_art):.2f}")
```

### Subspecialty distribution

```python
subspecialty_counts = df_art['primary_subspecialty'].value_counts()
print(subspecialty_counts)
```

### Temporal trends

```python
df_art['year'] = pd.to_datetime(df_art['pub_date']).dt.year
yearly = df_art.groupby('year').size()
yearly.plot(kind='bar', title='Articles per year')
```

### Method distribution

```python
method_counts = df_ent['method_group'].value_counts()
method_counts.plot(kind='barh', title='Methods used')
```

### Outcome distribution

```python
outcome_counts = df_ent['outcome_group'].value_counts()
outcome_counts.plot(kind='pie', autopct='%1.1f%%', title='Outcome types')
```

## Quality analysis

### Quality score distribution

```python
# Article-level quality
quality_cols = ['sample_size_score', 'validation_rigor_score', 'method_score',
                'performance_score', 'clinical_applicability_score', 'total_score']

for col in quality_cols:
    col_name = f"study_{col}"
    if col_name in df_art.columns:
        print(f"\n{col}:")
        print(df_art[col_name].describe())

# Or extract from nested JSON
def extract_quality_scores(df, score_col, prefix):
    """Extract quality sub-scores from nested JSON column."""
    for sub in ['sample_size_score', 'validation_rigor_score', 'method_score',
                'performance_score', 'clinical_applicability_score', 'total_score']:
        df[f"{prefix}_{sub}"] = df[score_col].apply(
            lambda x: x.get(sub) if isinstance(x, dict) else None
        )
    return df
```

### Quality by subspecialty

```python
quality_by_specialty = df_art.groupby('primary_subspecialty')['study_total_score'].mean()
quality_by_specialty.sort_values().plot(kind='barh')
```

### Validation reporting rates

```python
ext_val_rate = df_art['article_has_external_validation'].mean() * 100
int_val_rate = df_art['article_has_internal_validation'].mean() * 100
print(f"Articles with external validation: {ext_val_rate:.1f}%")
print(f"Articles with internal validation: {int_val_rate:.1f}%")
```

## Visualization recipes

### Figure 1: Publication trend by subspecialty

```python
import matplotlib.pyplot as plt

fig, ax = plt.subplots(figsize=(10, 6))
year_spec = df_art.groupby(['year', 'primary_subspecialty']).size().unstack(fill_value=0)
year_spec.plot(kind='bar', stacked=True, ax=ax, colormap='tab20')
ax.set_title('Clinical prediction model publications by year and subspecialty')
ax.set_xlabel('Year')
ax.set_ylabel('Number of articles')
ax.legend(bbox_to_anchor=(1.05, 1), loc='upper left')
plt.tight_layout()
fig.savefig('fig1_publication_trend.svg', format='svg')
```

### Figure 2: Method × subspecialty heatmap

```python
import seaborn as sns

method_spec = pd.crosstab(df_ent['disease_subspecialty'], df_ent['method_group'])
plt.figure(figsize=(14, 8))
sns.heatmap(method_spec, annot=True, fmt='d', cmap='YlOrRd')
plt.title('Methods used by clinical subspecialty')
plt.tight_layout()
plt.savefig('fig2_method_heatmap.svg', format='svg')
```

### Figure 3: Quality radar by subspecialty

```python
import numpy as np

quality_dims = ['sample_size_score', 'validation_rigor_score', 'method_score',
                'performance_score', 'clinical_applicability_score']

# Compute mean scores per subspecialty (top 5)
top5 = df_art['primary_subspecialty'].value_counts().head(5).index
means = df_art[df_art['primary_subspecialty'].isin(top5)].groupby(
    'primary_subspecialty')[quality_dims].mean()

# Radar plot
angles = np.linspace(0, 2 * np.pi, len(quality_dims), endpoint=False).tolist()
angles += angles[:1]

fig, ax = plt.subplots(figsize=(8, 8), subplot_kw=dict(polar=True))
for spec in top5:
    values = means.loc[spec].values.tolist()
    values += values[:1]
    ax.plot(angles, values, 'o-', label=spec)
ax.set_xticks(angles[:-1])
ax.set_xticklabels(quality_dims)
ax.legend(loc='upper right', bbox_to_anchor=(1.3, 1.1))
plt.tight_layout()
fig.savefig('fig3_quality_radar.svg', format='svg')
```

## Export for manuscript

### Table 1: Study characteristics

```python
table1 = df_art.groupby('primary_subspecialty').agg(
    n_articles=('pmid', 'nunique'),
    n_models=('pmid', 'count'),
    external_validation_pct=('article_has_external_validation', lambda x: x.mean() * 100),
    mean_quality=('study_total_score', 'mean'),
    median_year=('year', 'median'),
).reset_index()
table1.to_csv('table1_study_characteristics.csv', index=False)
```

### Supplementary table: All extracted data

```python
# Merge article and entity data for full export
full_export = df_ent.merge(
    df_art[['pmid', 'title', 'journal', 'pub_date', 'primary_subspecialty']],
    left_on='_source_pmid', right_on='pmid', suffixes=('_entity', '_article')
)
full_export.to_excel('supplementary_extracted_data.xlsx', index=False)
```

## Analysis checklist

- [ ] Load all JSONL files and verify record counts match expectations
- [ ] Check for missing/null values in key fields
- [ ] Validate controlled vocabulary compliance (no unexpected categories)
- [ ] Cross-check quality scores against validation flags
- [ ] Identify and describe low-confidence records (requires_human_review = true)
- [ ] Generate temporal trends, method distributions, outcome distributions
- [ ] Produce quality score summaries by subspecialty
- [ ] Export Table 1 and supplementary table
