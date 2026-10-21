"""Conservative variant scope uses fictional review text without model calls."""

import unittest
import json
from unittest.mock import Mock

from llm_analysis import LLMEngine
from llm_provider import ProviderRegistry
from research import _focused_wire, retrieve


def review(identity, body, title=""):
    return {"asin": "B0TEST0001", "review_id": identity, "rating": "4",
            "review_title": title, "review_body": body}


class VariantRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.engine = LLMEngine(ProviderRegistry(environ={}), None,
            transport=lambda payload: self.fail("Variant retrieval must stay local"))

    def search(self, question, *rows, **options):
        canonical, _, _, _ = self.engine._prepare(rows, "demo", "")
        selected, exposed, metadata = retrieve(self.engine, canonical, question, **options)
        return {item["review_id"] for item in exposed.values()}, metadata, selected, exposed

    def test_explicit_color_excludes_wrong_and_unknown_variants(self):
        identities, meta, _, _ = self.search("How is battery on the blue variant?",
            review("a", "The blue variant battery lasts a session."),
            review("b", "The red variant battery died quickly."),
            review("c", "Battery lasts a session."))
        self.assertEqual(identities, {"a"})
        self.assertEqual({key: value for key, value in meta["variant_scope"].items() if key != "evidence"}, {"constraints": ["Color: blue"], "eligible_reviews": 1,
            "excluded_unknown": 1, "excluded_conflicting": 1, "method": "explicit_review_text", "ambiguous": False})
        self.assertEqual(meta["matched_reviews"], 1)
        self.assertEqual(meta["variant_scope"]["evidence"][0]["review_id"], "a")

    def test_color_on_an_object_does_not_establish_product_variant(self):
        identities, meta, _, _ = self.search("Does the blue variant overheat?",
            review("a", "The case is blue and the device overheats."),
            review("b", "The blue light is on. No overheating."),
            review("c", "The indicator color is blue. The device overheats."),
            review("d", "The accessory colour: blue. The device overheats."))
        self.assertEqual(identities, set())
        self.assertEqual(meta["variant_scope"]["excluded_unknown"], 4)

    def test_product_color_label_is_distinct_from_an_indicator(self):
        identities, meta, _, _ = self.search("Does the blue variant have a red indicator?",
            review("a", "Blue variant. The indicator is red."),
            review("b", "The product color is blue. The indicator is red."))
        self.assertEqual(identities, {"a", "b"})
        self.assertFalse(meta["variant_scope"]["ambiguous"])
        self.assertEqual(meta["variant_scope"]["constraints"], ["Color: blue"])

    def test_unsupported_bare_color_query_does_not_claim_variant_filter(self):
        _, meta, _, _ = self.search("Does the blue light work?", review("a", "The blue light works."))
        self.assertIsNone(meta["variant_scope"])

    def test_mixed_color_and_negated_identity_are_excluded(self):
        identities, meta, _, _ = self.search("How is battery on the blue variant?",
            review("a", "I ordered the blue variant but received red. Battery is weak."),
            review("b", "Not the blue variant. Battery is weak."),
            review("c", "The blue variant battery is better than the red variant."))
        self.assertEqual(identities, set())
        self.assertEqual(meta["variant_scope"]["excluded_conflicting"], 3)

    def test_negated_query_is_ambiguous_instead_of_inverted(self):
        for question in ("How is battery on variants other than the blue variant?",
                "How is battery on anything except the blue variant?"):
            with self.subTest(question=question):
                identities, meta, _, _ = self.search(question,
                    review("a", "Blue variant battery lasts."), review("b", "Red variant battery died."))
                self.assertEqual(identities, set())
                self.assertTrue(meta["variant_scope"]["ambiguous"])
                result = self.engine.ask([review("a", "Blue variant battery lasts.")], question,
                    dataset="demo", asin="", allow_external=False)
                self.assertIn("Ask about one explicit variant", result["answer"])

    def test_contracted_and_long_form_negated_review_identity_is_excluded(self):
        for phrase in ("This isn't the blue variant", "This wasn't a blue variant", "This was not the blue variant",
                "This isn\u2019t the blue variant"):
            with self.subTest(phrase=phrase):
                identities, meta, _, _ = self.search("How is battery on the blue variant?",
                    review("a", phrase + ". Battery lasts."))
                self.assertEqual(identities, set())
                self.assertEqual(meta["variant_scope"]["excluded_conflicting"], 1)

    def test_title_and_body_disagreement_is_not_variant_proof(self):
        identities, meta, _, _ = self.search("How is battery on the blue variant?",
            review("a", "Red variant battery died.", title="Blue variant"))
        self.assertEqual(identities, set())
        self.assertEqual(meta["variant_scope"]["excluded_conflicting"], 1)

    def test_ambiguous_query_does_not_choose_one_requested_color(self):
        identities, meta, _, _ = self.search("How is battery on the blue or red variant?",
            review("a", "The red variant battery is good."))
        self.assertEqual(identities, set())
        self.assertTrue(meta["variant_scope"]["ambiguous"])

    def test_equivalent_capacities_are_normalized_without_modifying_sources(self):
        source = "The 0.5 L bottle leaks near the lid."
        identities, meta, selected, exposed = self.search("Any leaks from the 500ml bottle?",
            review("a", source), review("b", "The 1 litre bottle leaks near the lid."),
            review("c", "The bottle leaks near the lid."))
        self.assertEqual(identities, {"a"})
        self.assertEqual(meta["variant_scope"]["constraints"], ["Capacity: 500 ml"])
        self.assertEqual(meta["variant_scope"]["excluded_conflicting"], 1)
        self.assertEqual(meta["variant_scope"]["excluded_unknown"], 1)
        self.assertEqual(selected[0]["body"], source)
        key = selected[0]["key"]
        self.engine._validate_answer({"answer": "A selected source reports a leak.",
            "evidence": [{"key": key, "quote": source}], "limitations": []}, exposed)

    def test_capacity_label_decimal_and_mixed_quantities(self):
        identities, meta, _, _ = self.search("How is cleaning for the .5 L size?",
            review("a", "Capacity: 500 millilitres. Cleaning is easy."),
            review("b", "The 500 ml bottle replaced my 1 litre bottle. Cleaning is easy."),
            review("c", "I use 500ml of water when cleaning."))
        self.assertEqual(identities, {"a"})
        self.assertEqual(meta["variant_scope"]["excluded_conflicting"], 1)
        self.assertEqual(meta["variant_scope"]["excluded_unknown"], 1)

    def test_capacity_values_cannot_be_parsed_from_comma_suffix(self):
        identities, _, _, _ = self.search("Does the 500 ml bottle leak?",
            review("a", "The 1,500ml bottle leaks."))
        self.assertEqual(identities, set())

    def test_usage_quantity_in_question_is_not_a_product_identity(self):
        identities, meta, _, _ = self.search("Can it blend 500 ml of milk?",
            review("a", "The blender can blend milk."))
        self.assertEqual(identities, {"a"})
        self.assertIsNone(meta["variant_scope"])

    def test_explicit_size_supports_case_and_equivalent_name(self):
        identities, meta, _, _ = self.search("Is size XL comfortable?",
            review("a", "Size: extra large. Comfortable for short use."),
            review("b", "Size: medium. Comfortable for short use."),
            review("c", "A large pocket. Comfortable fabric."))
        self.assertEqual(identities, {"a"})
        self.assertEqual(meta["variant_scope"]["constraints"], ["Size: XL"])
        self.assertEqual(meta["variant_scope"]["excluded_unknown"], 1)

    def test_all_requested_attributes_must_be_explicit(self):
        identities, meta, _, _ = self.search("How is cleaning on the blue variant 500 ml bottle?",
            review("a", "Blue variant, capacity: 0.5 L. Cleaning is easy."),
            review("b", "Blue variant. Cleaning is easy."),
            review("c", "Red variant, 500 ml bottle. Cleaning is easy."))
        self.assertEqual(identities, {"a"})
        self.assertEqual(meta["variant_scope"]["eligible_reviews"], 1)

    def test_size_query_alternatives_and_order_mixups_are_ambiguous(self):
        identities, meta, _, _ = self.search("Is size XL or M comfortable?",
            review("a", "Size XL. Comfortable."))
        self.assertEqual(identities, set())
        self.assertTrue(meta["variant_scope"]["ambiguous"])
        identities, meta, _, _ = self.search("Is size XL comfortable?",
            review("a", "Ordered size XL, received M. Comfortable."))
        self.assertEqual(identities, set())
        self.assertEqual(meta["variant_scope"]["excluded_conflicting"], 1)
        for ending in ("The replacement is M.", "I received a medium."):
            with self.subTest(ending=ending):
                identities, meta, _, _ = self.search("Is size XL comfortable?",
                    review("a", "Ordered size XL. " + ending + " Comfortable."))
                self.assertEqual(identities, set())
                self.assertEqual(meta["variant_scope"]["excluded_conflicting"], 1)

    def test_variant_words_alone_are_not_topical_evidence(self):
        identities, meta, _, _ = self.search("Does the blue variant overheat?",
            review("a", "The blue variant has a sturdy stand."))
        self.assertEqual(identities, set())
        self.assertEqual(meta["variant_scope"]["eligible_reviews"], 1)
        self.assertEqual(meta["matched_reviews"], 0)
        identities, meta, _, _ = self.search("Does the 500ml bottle overheat?",
            review("a", "The 500ml bottle has a sturdy lid."))
        self.assertEqual(identities, set())
        self.assertEqual(meta["variant_scope"]["eligible_reviews"], 1)

    def test_general_scoped_query_samples_only_eligible_reviews(self):
        identities, meta, _, _ = self.search("Summarize the blue variant.",
            review("a", "Blue variant. A stable stand."), review("b", "Red variant. A quiet motor."))
        self.assertEqual(identities, {"a"})
        self.assertTrue(meta["general_sample"])

    def test_unscoped_questions_preserve_all_relevant_variants(self):
        identities, meta, _, _ = self.search("How is battery?",
            review("a", "Blue variant battery lasts."), review("b", "Red variant battery died."))
        self.assertEqual(identities, {"a", "b"})
        self.assertIsNone(meta["variant_scope"])

    def test_agent_candidate_lookup_does_not_silently_apply_focused_filter(self):
        identities, meta, _, _ = self.search("How is battery on the blue variant?",
            review("a", "Red variant battery died."), apply_variant_scope=False)
        self.assertEqual(identities, {"a"})
        self.assertIsNone(meta["variant_scope"])

    def test_scoped_empty_result_is_local_and_discloses_filter(self):
        result = self.engine.ask([review("a", "Battery lasts a session.")],
            "How is battery on the blue variant?", dataset="demo", asin="", allow_external=False)
        self.assertEqual(result["method"], "local_evidence")
        self.assertEqual(result["evidence"], [])
        self.assertEqual(result["usage"]["calls"], 0)
        self.assertEqual(result["scope"]["variant_scope"]["excluded_unknown"], 1)
        self.assertTrue(any("not verified product metadata" in limit for limit in result["limitations"]))

    def test_gray_and_grey_are_equivalent(self):
        identities, _, _, _ = self.search("How is battery on the gray model?",
            review("a", "Colour: grey. Battery lasts a session."))
        self.assertEqual(identities, {"a"})

    def test_local_quote_ranking_prefers_topic_over_repeated_variant_words(self):
        body = "Blue variant. " + "A blue variant with a stable stand. " * 5 + "No overheating in my use."
        result = self.engine.ask([review("a", body)], "Does the blue variant overheat?",
            dataset="demo", asin="", allow_external=False)
        self.assertIn("No overheating", result["evidence"][0]["quote"])
        self.assertIn(result["evidence"][0]["quote"], body)

    def test_distant_identity_context_is_bounded_exact_private_and_citable(self):
        body = "Blue variant. " + "Stable stand. " * 100 + "No overheating in my use."
        _, meta, selected, exposed = self.search("Does the blue variant overheat?",
            review("PRIVATE-ORIGINAL-ID", body))
        self.assertNotIn("Blue variant", selected[0]["body"])
        contexts = selected[0]["variant_context"]
        self.assertGreater(len(contexts), 0)
        self.assertLessEqual(len(contexts), 3)
        self.assertTrue(all(len(context["text"]) <= 160 and context["text"] in body for context in contexts))
        self.assertTrue(all(item["quote"] in body for item in meta["variant_scope"]["evidence"]))
        wire, citations = _focused_wire(selected)
        self.assertEqual(len(wire["reviews"]), 1)
        self.assertNotIn("PRIVATE-ORIGINAL-ID", json.dumps(wire))
        self.assertNotIn("B0TEST0001", json.dumps(wire))
        self.assertNotIn("variant_context", json.dumps(wire))
        identity = next(value for value in citations.values() if "Blue variant" in value["quote"])
        self.engine._validate_answer({"answer": "The cited review names the blue variant.",
            "evidence": [identity], "limitations": []}, exposed)

    def test_cached_focused_answer_keeps_current_identity_scope(self):
        body = "Blue variant. No overheating in my use."
        self.engine.registry.configure({"api_key": "fictional-key"})
        self.engine.store = Mock()
        sent = []
        def reply(messages, schema, usage, identity, **options):
            sent.append(messages)
            wire = json.loads(messages[-1]["content"])["review_evidence"]["reviews"][0]
            label = next(iter(wire["body"]))
            return {"tool_calls": [], "model": "fictional/test:free",
                "content": json.dumps({"answer": "A selected review names the blue variant.", "evidence": [label], "limitations": []})}
        self.engine._call = reply
        self.engine.store.get_chunk.return_value = None
        first = self.engine.ask([review("a", body)], "Does the blue variant overheat?", dataset="demo", asin="")
        self.engine.store.get_chunk.return_value = self.engine.store.put_chunk.call_args.args[1]
        again = self.engine.ask([review("a", body)], "Does the blue variant overheat?", dataset="demo", asin="")
        self.assertTrue(again["cache_hit"])
        self.assertEqual(first["scope"]["variant_scope"], again["scope"]["variant_scope"])
        self.assertEqual(len(sent), 1)


if __name__ == "__main__":
    unittest.main()
