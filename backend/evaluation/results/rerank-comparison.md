# Baseline vs reranked retrieval

Dataset `atlas-qa@1.0.0` (28 questions, sha256 `63b4e926e405`). Both arms share one index (embedding `fake/text-embedding-3-small/1536/v1`, chunking `v1:size=1000,overlap=150`, top-k 5). The candidate adds `lexical/v1` (weight 0.5, pool 3).

## Caveats

- Single annotator and a small dataset: differences of a single question are not statistically meaningful; read the per-question changes.
- Fake embeddings: the vector score is noise, so the reranking effect is overstated and says nothing about a real embedding model.
- Fake LLM (extractive baseline): faithfulness is trivially 1.0 and the answer metrics describe the heuristic, not a real model.

## Quality

| Metric | Baseline | Reranked | Delta | |
| --- | ---: | ---: | ---: | --- |
| `retrieval.overall.recall` | 0.8788 | 0.9242 | +0.0455 | better |
| `retrieval.overall.mrr` | 0.6848 | 0.9773 | +0.2924 | better |
| `retrieval.overall.precision` | 0.2000 | 0.2091 | +0.0091 | better |
| `retrieval.overall.source_success` | 1.0000 | 1.0000 | +0.0000 |  |
| `retrieval.answerable.recall` | 0.9375 | 1.0000 | +0.0625 | better |
| `retrieval.answerable.mrr` | 0.7417 | 1.0000 | +0.2583 | better |
| `retrieval.ambiguous.recall` | 0.7222 | 0.7222 | +0.0000 |  |
| `retrieval.ambiguous.mrr` | 0.5333 | 0.9167 | +0.3833 | better |
| `answers.correct_rate` | 0.5455 | 0.5909 | +0.0455 | better |
| `answers.key_fact_recall` | 0.8438 | 0.8056 | -0.0382 | **worse** |
| `answers.faithfulness` | 1.0000 | 1.0000 | +0.0000 |  |
| `answers.citation_precision` | 1.0000 | 0.9444 | -0.0556 | **worse** |
| `answers.citation_recall` | 0.9375 | 0.8889 | -0.0486 | **worse** |
| `answers.valid_abstention_rate` | 0.8333 | 0.8333 | +0.0000 |  |
| `answers.unnecessary_abstention_rate` | 0.2727 | 0.1818 | -0.0909 | better |
| `answers.hallucination_rate` | 0.0357 | 0.0357 | +0.0000 |  |

Retrieval per question: 10 improved, 0 worsened, 12 unchanged.

| Question | Kind | Recall | Reciprocal rank |
| --- | --- | --- | --- |
| a002 | ambiguous | 0.333333 → 0.333333 | 0.25 → 0.5 |
| a003 | ambiguous | 1.0 → 1.0 | 0.5 → 1.0 |
| a005 | ambiguous | 1.0 → 1.0 | 0.25 → 1.0 |
| a006 | ambiguous | 0.5 → 0.5 | 0.2 → 1.0 |
| q001 | answerable | 1.0 → 1.0 | 0.5 → 1.0 |
| q002 | answerable | 1.0 → 1.0 | 0.333333 → 1.0 |
| q004 | answerable | 1.0 → 1.0 | 0.333333 → 1.0 |
| q011 | answerable | 1.0 → 1.0 | 0.5 → 1.0 |
| q015 | answerable | 1.0 → 1.0 | 0.2 → 1.0 |
| q016 | answerable | 0.0 → 1.0 | 0.0 → 1.0 |

| Question | Kind | Verdict |
| --- | --- | --- |
| a002 | ambiguous | unnecessary_abstention → incorrect |
| q016 | answerable | unnecessary_abstention → correct |

## Cost

| | Baseline | Reranked |
| --- | ---: | ---: |
| Answer input tokens | 16037 | 16180 |
| Answer output tokens | 341 | 366 |

Extra model calls per question for reranking: 0.

## Latency

10 repeats after a warm-up pass, 280 samples per arm, alternating order. Milliseconds, measured on one machine: they change from run to run, only the order of magnitude is stable.

| | Baseline mean / p50 / p95 | Reranked mean / p50 / p95 | Overhead p95 |
| --- | --- | --- | ---: |
| Retrieval | 3.087 / 2.921 / 4.451 | 5.057 / 5.071 / 7.07 | +2.619 |
| End to end | 4.183 / 3.908 / 7.858 | 6.394 / 6.299 / 8.678 | +0.82 |

## Decision

**Keep the reranking.**

- [x] retrieval.overall.recall does not regress (delta +0.045454)
- [x] retrieval.overall.mrr does not regress (delta +0.292425)
- [x] answers.correct_rate does not regress (delta +0.045454)
- [x] answers.hallucination_rate does not regress (delta +0.0)
- [x] at least one guarded metric improves
- [x] p95 retrieval overhead <= 20.0 ms (delta +2.619)

Metrics outside the rule that got worse (they did not decide, but look at them): `answers.key_fact_recall`, `answers.citation_precision`, `answers.citation_recall`.

The rule was fixed before looking at the results: no regression in recall, MRR, correct rate or hallucination rate, at least one of them improves, and the p95 retrieval overhead stays within 20.0 ms.

