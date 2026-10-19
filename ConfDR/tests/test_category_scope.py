"""Keep category boundaries explicit without silently accepting model rejections."""

from test_review_protocol import ReplayTransport, verdict

from confdr.formalization.generation import generate_rules
from confdr.shared.review import SCOPE_POLICY


class ScopeTransport(ReplayTransport):
    def __init__(self, catalog, *, reject=False):
        super().__init__(catalog)
        self.reject = reject
        self.scopes = []
        self.reference_payloads = []

    def complete(self, task, system, payload, schema):
        if task in {"independent_spec", "generate_rules", "semantic_review"}:
            scope = payload["category_scope"]
            assert scope["requirement_verbatim"] == payload["taxonomy"]["categories"][0]["description"]
            assert SCOPE_POLICY in system
            self.scopes.append(scope)
        if task == "independent_spec":
            assert "fallible model" in system
            self.reference_payloads.append(payload)
        result = super().complete(task, system, payload, schema)
        if task == "semantic_review" and self.reject:
            return verdict([{"kind": "rule_error", "message": "Missing unrelated topology checks."}])
        return result


def test_generation_and_review_share_verbatim_scope(catalog):
    catalog["taxonomy"]["categories"][0]["description"] = "Every assign must satisfy selector_ok."
    transport = ScopeTransport(catalog)
    result = generate_rules(transport, catalog["taxonomy"], verifier=transport)
    assert result["validation"]["status"] == "empirically_validated"
    assert len(transport.scopes) == 4
    assert all(s == transport.scopes[0] for s in transport.scopes)
    assert set(transport.scopes[0]["explicitly_named_atoms"]) == {"assign", "selector_ok"}


def test_scope_guidance_never_overrides_blocking_review(catalog):
    transport = ScopeTransport(catalog, reject=True)
    result = generate_rules(transport, catalog["taxonomy"], verifier=transport, max_rounds=2)
    assert transport.candidate_calls == 0
    assert result["validation"]["status"] == "rejected"
    assert (
        transport.reference_payloads[1]["specification_feedback"]["semantic_review"]["issues"][0]["kind"]
        == "rule_error"
    )
    assert "previous_candidate" not in transport.reference_payloads[1]
    assert all(a["verification"]["status"] == "rejected" for a in result["generation"]["reference_attempts"])
