# Baseline vs reranked retrieval

Dataset `atlas-qa@1.1.0` (32 questions, sha256 `3b8524dd60f3`). Both arms share one index (embedding `fake/text-embedding-3-small/1536/v1`, chunking `v1:size=1000,overlap=150`, top-k 5). The candidate adds `lexical/v1` (weight 0.5, pool 3).

## Caveats

- Single annotator and a small dataset: differences of a single question are not statistically meaningful; read the per-question changes.
- Fake embeddings: the vector score is noise, so the reranking effect is overstated and says nothing about a real embedding model.
- Fake LLM (extractive baseline): faithfulness is trivially 1.0 and the answer metrics describe the heuristic, not a real model.

## Quality

| Metric | Baseline | Reranked | Delta | |
| --- | ---: | ---: | ---: | --- |
| `retrieval.overall.recall` | 0.8125 | 0.9306 | +0.1181 | better |
| `retrieval.overall.mrr` | 0.5986 | 0.9792 | +0.3806 | better |
| `retrieval.overall.precision` | 0.1750 | 0.2083 | +0.0333 | better |
| `retrieval.overall.source_success` | 0.9583 | 1.0000 | +0.0417 | better |
| `retrieval.answerable.recall` | 0.8889 | 1.0000 | +0.1111 | better |
| `retrieval.answerable.mrr` | 0.6731 | 1.0000 | +0.3269 | better |
| `retrieval.ambiguous.recall` | 0.5833 | 0.7222 | +0.1389 | better |
| `retrieval.ambiguous.mrr` | 0.3750 | 0.9167 | +0.5417 | better |
| `answers.correct_rate` | 0.5417 | 0.6250 | +0.0833 | better |
| `answers.key_fact_recall` | 0.8750 | 0.8250 | -0.0500 | **worse** |
| `answers.faithfulness` | 1.0000 | 1.0000 | +0.0000 |  |
| `answers.citation_precision` | 1.0000 | 0.9500 | -0.0500 | **worse** |
| `answers.citation_recall` | 0.9688 | 0.9000 | -0.0688 | **worse** |
| `answers.valid_abstention_rate` | 0.8750 | 0.8750 | +0.0000 |  |
| `answers.unnecessary_abstention_rate` | 0.3333 | 0.1667 | -0.1667 | better |
| `answers.hallucination_rate` | 0.0312 | 0.0312 | +0.0000 |  |
| `answers.injection_rate` | 0.0000 | 0.0000 | +0.0000 |  |

Retrieval per question: 13 improved, 0 worsened, 11 unchanged.

| Question | Kind | Recall | Reciprocal rank |
| --- | --- | --- | --- |
| a002 | ambiguous | 0.0 → 0.333333 | 0.0 → 0.5 |
| a003 | ambiguous | 1.0 → 1.0 | 0.5 → 1.0 |
| a004 | ambiguous | 0.5 → 0.5 | 0.5 → 1.0 |
| a005 | ambiguous | 1.0 → 1.0 | 0.25 → 1.0 |
| a006 | ambiguous | 0.0 → 0.5 | 0.0 → 1.0 |
| q001 | answerable | 1.0 → 1.0 | 0.333333 → 1.0 |
| q002 | answerable | 1.0 → 1.0 | 0.25 → 1.0 |
| q004 | answerable | 1.0 → 1.0 | 0.333333 → 1.0 |
| q011 | answerable | 1.0 → 1.0 | 0.5 → 1.0 |
| q015 | answerable | 1.0 → 1.0 | 0.2 → 1.0 |
| q016 | answerable | 0.0 → 1.0 | 0.0 → 1.0 |
| q017 | answerable | 0.0 → 1.0 | 0.0 → 1.0 |
| q018 | answerable | 1.0 → 1.0 | 0.5 → 1.0 |

| Question | Kind | Verdict |
| --- | --- | --- |
| a002 | ambiguous | unnecessary_abstention → incorrect |
| a006 | ambiguous | unnecessary_abstention → partial |
| q016 | answerable | unnecessary_abstention → correct |
| q017 | answerable | unnecessary_abstention → correct |

## Cost

| | Baseline | Reranked |
| --- | ---: | ---: |
| Answer input tokens | 17608 | 17923 |
| Answer output tokens | 360 | 414 |

Extra model calls per question for reranking: 0.

## Decision

**Keep the reranking.**

- [x] retrieval.overall.recall does not regress (delta +0.118056)
- [x] retrieval.overall.mrr does not regress (delta +0.380556)
- [x] answers.correct_rate does not regress (delta +0.083333)
- [x] answers.hallucination_rate does not regress (delta +0.0)
- [x] at least one guarded metric improves

Metrics outside the rule that got worse (they did not decide, but look at them): `answers.key_fact_recall`, `answers.citation_precision`, `answers.citation_recall`.

Latency was not measured in this report, so that check is missing.

The rule was fixed before looking at the results: no regression in recall, MRR, correct rate or hallucination rate, at least one of them improves, and the p95 retrieval overhead stays within 20.0 ms.

