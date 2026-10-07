"""Reranking opcional: puntuación léxica, reordenación determinista y activación por ajustes."""

import pytest
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.embeddings.store import SimilarChunk
from app.evaluation.retrieval import choose_rerank
from app.features.documents.models import Document, DocumentChunk
from app.retrieval.rerank import RerankPolicy, lexical_score, rerank, stems
from app.retrieval.search import SearchLimits, search_chunks, search_with_report
from tests.test_embedding_store import index_document
from tests.test_search import TEXTS, ask

DB = "postgresql+psycopg://x/y"


def hit(text: str, score: float, ordinal: int, document: str = "doc.md") -> SimilarChunk:
    chunk = DocumentChunk(text=text, ordinal=ordinal)
    return SimilarChunk(chunk, Document(filename=document), 1 - score)


class TestLexicalScore:
    def test_stems_ignore_accents_stopwords_and_short_words(self) -> None:
        assert stems("¿Cuántos DÍAS puede teletrabajar la plantilla?") == {
            "dias",
            "teletr",
            "planti",
        }

    def test_numbers_are_kept_whole(self) -> None:
        assert "30" in stems("Tiene 30 días")

    def test_inflections_match_through_the_stem(self) -> None:
        question = stems("teletrabajar")
        assert lexical_score(question, "El teletrabajo es voluntario") == 1.0

    def test_the_score_is_the_share_of_question_words_found(self) -> None:
        question = stems("contrato alquiler duración")
        assert lexical_score(question, "El contrato de alquiler es anual") == pytest.approx(2 / 3)

    def test_a_question_without_content_words_scores_zero(self) -> None:
        assert lexical_score(stems("¿Y eso?"), "cualquier texto") == 0.0


class TestRerank:
    def test_a_lexical_match_can_overtake_a_closer_vector(self) -> None:
        far = hit("El contrato de alquiler dura doce meses.", 0.30, 0)
        near = hit("Receta de paella con arroz.", 0.40, 1)
        result = rerank("¿Cuánto dura el contrato de alquiler?", [near, far], RerankPolicy())
        assert [h.chunk.ordinal for h in result.ordered] == [0, 1]
        assert result.moved == 2

    def test_the_vector_score_of_each_hit_is_untouched(self) -> None:
        far, near = hit("contrato de alquiler", 0.30, 0), hit("otra cosa", 0.40, 1)
        result = rerank("contrato de alquiler", [near, far], RerankPolicy())
        assert [round(h.score, 6) for h in result.ordered] == [0.3, 0.4]

    def test_entries_keep_the_origin_and_both_scores(self) -> None:
        far, near = hit("contrato de alquiler", 0.30, 7, "a.md"), hit("otra cosa", 0.40, 2, "b.md")
        first, second = rerank(
            "contrato de alquiler", [near, far], RerankPolicy(weight=0.5)
        ).entries
        assert (first.filename, first.ordinal) == ("a.md", 7)
        assert (first.original_rank, first.final_rank) == (2, 1)
        assert first.vector_score == pytest.approx(0.3)
        assert first.lexical_score == 1.0
        assert first.combined_score == pytest.approx(0.65)
        assert (second.original_rank, second.final_rank) == (1, 2)
        assert first.to_dict()["combined_score"] == 0.65

    def test_weight_zero_keeps_the_vector_order_and_one_ignores_it(self) -> None:
        a, b = hit("nada que ver", 0.9, 0), hit("contrato de alquiler", 0.1, 1)
        only_vector = rerank("contrato de alquiler", [a, b], RerankPolicy(weight=0.0))
        only_text = rerank("contrato de alquiler", [a, b], RerankPolicy(weight=1.0))
        assert [h.chunk.ordinal for h in only_vector.ordered] == [0, 1]
        assert [h.chunk.ordinal for h in only_text.ordered] == [1, 0]

    def test_ties_keep_the_original_order(self) -> None:
        hits = [hit("texto neutro", 0.5, ordinal) for ordinal in range(4)]
        result = rerank("pregunta distinta", hits, RerankPolicy())
        assert [h.chunk.ordinal for h in result.ordered] == [0, 1, 2, 3]
        assert result.moved == 0

    def test_it_is_deterministic(self) -> None:
        hits = [hit(f"contrato número {n}", 0.5 - n / 100, n) for n in range(5)]
        assert rerank("contrato", hits, RerankPolicy()) == rerank("contrato", hits, RerankPolicy())

    @pytest.mark.parametrize(("weight", "pool"), [(-0.1, 3), (1.1, 3), (0.5, 0)])
    def test_invalid_policies_are_rejected(self, weight: float, pool: int) -> None:
        with pytest.raises(ValueError, match=r"weight|pool"):
            RerankPolicy(weight=weight, pool=pool)

    def test_the_policy_describes_itself(self) -> None:
        assert RerankPolicy(weight=0.4, pool=2).describe() == {
            "key": "lexical/v1",
            "strategy": "lexical",
            "weight": 0.4,
            "pool": 2,
        }


class TestSearch:
    def test_it_is_off_by_default(self, db_session: Session) -> None:
        assert SearchLimits().rerank is None
        assert SearchLimits.from_settings(Settings(database_url=DB)).rerank is None
        document, _ = index_document(db_session, TEXTS)
        outcome = search_with_report(
            db_session,
            ask("los gatos duermen en el sofá"),
            owner_id=document.owner_id,
            limits=SearchLimits(3, 5),
        )
        assert outcome.reranking is None

    def test_it_is_enabled_by_configuration(self) -> None:
        settings = Settings(
            database_url=DB, search_rerank="lexical", search_rerank_weight=0.7, search_rerank_pool=4
        )
        policy = SearchLimits.from_settings(settings).rerank
        assert policy == RerankPolicy("lexical", 0.7, 4)

    def test_the_weight_one_puts_the_best_textual_match_first(self, db_session: Session) -> None:
        document, _ = index_document(db_session, TEXTS)
        limits = SearchLimits(1, 10, rerank=RerankPolicy(weight=1.0, pool=10), dedup=None)
        hits = search_chunks(
            db_session,
            ask("¿Cuánto dura el contrato de alquiler?"),
            owner_id=document.owner_id,
            limits=limits,
            k=1,
        )
        assert [h.chunk.text for h in hits] == [TEXTS[3]]

    def test_the_candidate_pool_is_wider_than_k_and_the_result_is_cut_to_k(
        self, db_session: Session
    ) -> None:
        document, _ = index_document(db_session, TEXTS)
        limits = SearchLimits(2, 10, rerank=RerankPolicy(pool=3), dedup=None)
        outcome = search_with_report(
            db_session, ask("el contrato de alquiler"), owner_id=document.owner_id, limits=limits
        )
        assert outcome.reranking is not None
        assert len(outcome.reranking.entries) == 6
        assert len(outcome.hits) == 2
        assert [e.final_rank for e in outcome.reranking.entries] == [1, 2, 3, 4, 5, 6]

    def test_with_the_policy_off_the_result_is_the_vector_search(self, db_session: Session) -> None:
        document, _ = index_document(db_session, TEXTS)
        question = ask("los gatos duermen en el sofá")
        plain = search_chunks(
            db_session, question, owner_id=document.owner_id, limits=SearchLimits(3, 5)
        )
        weightless = search_chunks(
            db_session,
            question,
            owner_id=document.owner_id,
            limits=SearchLimits(3, 5, rerank=RerankPolicy(weight=0.0, pool=1)),
        )
        assert [h.chunk.id for h in weightless] == [h.chunk.id for h in plain]

    def test_it_works_after_the_redundancy_reduction(self, db_session: Session) -> None:
        document, _ = index_document(db_session, [*TEXTS, TEXTS[3]])
        outcome = search_with_report(
            db_session,
            ask("el contrato de alquiler"),
            owner_id=document.owner_id,
            limits=SearchLimits.from_settings(Settings(database_url=DB, search_rerank="lexical")),
        )
        assert outcome.reduction is not None and outcome.reranking is not None
        texts = [h.chunk.text for h in outcome.hits]
        assert texts.count(TEXTS[3]) == 1


class TestCommandLineChoice:
    def test_without_a_request_the_application_setting_rules(self) -> None:
        base = RerankPolicy(weight=0.3, pool=5)
        assert choose_rerank(None, None, None) is None
        assert choose_rerank(base, None, None) == base

    def test_off_disables_and_lexical_enables_with_a_weight(self) -> None:
        assert choose_rerank(RerankPolicy(), "off", None) is None
        assert choose_rerank(None, "lexical", 0.8) == RerankPolicy(weight=0.8)
        assert choose_rerank(RerankPolicy(pool=5), None, 0.2) == RerankPolicy(weight=0.2, pool=5)
