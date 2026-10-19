"""Category merges preserve evidence and never promote stale model reviews."""

import copy

import pytest
from test_review_protocol import ReplayTransport, verdict

from confdr.cli import parser
from confdr.formalization.generation import generate_rules
from confdr.formalization.merge import merge_bundles
from confdr.formalization.service import revalidate
from confdr.shared.catalog import read_bundle, save_bundle
from confdr.shared.contracts import verification_base
from confdr.shared.io import read, write
from confdr.shared.review import review_rules


class Reviewer:
    def __init__(self):
        self.seen = []
        self.calls = []

    def complete(self, task, system, payload, schema):
        assert task == "semantic_review"
        self.seen.append((payload["taxonomy"]["categories"][0]["id"], payload["review_subject"]))
        return verdict()


@pytest.fixture
def bundles(catalog, tmp_path):
    results = []
    for cat in ("A", "B"):
        item = copy.deepcopy(catalog)
        item["taxonomy"]["categories"][0]["id"] = cat
        item["rules"][0].update(id=f"{cat}/rule", category_id=cat, source_ids=[f"taxonomy:{cat}"])
        transport = ReplayTransport(item)
        results.append(
            generate_rules(
                transport,
                item["taxonomy"],
                verifier=transport,
                knowledge=[{"id": "shared", "categories": [cat], "text": "Shared context"}],
            )
        )
    base, patch = copy.deepcopy(results[0]), results[1]
    base["taxonomy"]["categories"] += copy.deepcopy(patch["taxonomy"]["categories"])
    base["knowledge"][0]["categories"].append("B")
    base["knowledge"] += copy.deepcopy(patch["knowledge"][1:])
    base["rules"] += copy.deepcopy(patch["rules"])
    for key in ("reviews", "reference_reviews"):
        base["verification_material"][key].update(copy.deepcopy(patch["verification_material"][key]))
    base["verification_material"]["reference"]["categories"].update(
        copy.deepcopy(patch["verification_material"]["reference"]["categories"])
    )
    base["verification_material"]["tests"] += copy.deepcopy(patch["verification_material"]["tests"])
    for cat in base["taxonomy"]["categories"]:
        ctx = verification_base(base["taxonomy"], cat, base["knowledge"])
        for key, subject, rules in (
            ("reviews", "candidate", [r for r in base["rules"] if r["category_id"] == cat["id"]]),
            (
                "reference_reviews",
                "reference",
                base["verification_material"]["reference"]["categories"][cat["id"]],
            ),
        ):
            base["verification_material"][key][cat["id"]] = review_rules(
                Reviewer(), ctx, rules, subject=subject
            )
    # The base has a missing category, like the real E1 failure.
    base["rules"] = [r for r in base["rules"] if r["category_id"] != "B"]
    save_bundle(tmp_path / "base", base)
    assert save_bundle(tmp_path / "patch", patch)["status"] == "empirically_validated"
    return tmp_path / "base/rules.yaml", tmp_path / "patch/rules.yaml", tmp_path / "merged"


def test_merge_and_targeted_rereview(bundles):
    base, patch, output = bundles
    before = {p: p.read_bytes() for parent in (base.parent, patch.parent) for p in parent.iterdir()}
    original_review = read(patch.with_name("verification.json"))["verification_material"]["reviews"]["B"]
    report = merge_bundles(base, patch, output, category_ids=["B"])
    assert report["status"] == "unverified"
    assert report["category_status"] == {"A": "empirically_validated", "B": "unverified"}
    merged = read_bundle(output / "rules.yaml", require_current=False)
    assert merged["verification_material"]["reviews"]["B"] == original_review
    assert [r["category_id"] for r in merged["rules"]] == ["A", "B"]
    assert len(merged["verification_material"]["tests"]) == 8
    reviewer = Reviewer()
    report = revalidate(output / "rules.yaml", verifier=reviewer)
    assert report["status"] == "empirically_validated"
    assert reviewer.seen == [("B", "reference"), ("B", "candidate")]
    assert revalidate(output / "rules.yaml")["status"] == "empirically_validated"
    assert all(p.read_bytes() == content for p, content in before.items())
    assert read_bundle(output / "rules.yaml")["validation"]["status"] == "empirically_validated"


def test_same_context_merge_needs_no_rereview(bundles):
    _, patch, output = bundles
    assert merge_bundles(patch, patch, output, category_ids=["B"])["status"] == "empirically_validated"


@pytest.mark.parametrize("change", ["stale", "rejected", "knowledge", "taxonomy", "semantics"])
def test_invalid_input_cannot_be_promoted(bundles, change):
    base, patch, output = bundles
    p = read_bundle(patch, require_current=False)
    if change == "semantics":
        evidence = read(patch.with_name("verification.json"))
        evidence["semantics_version"] = "old"
        write(patch.with_name("verification.json"), evidence)
    elif change == "stale":
        source = read(patch)
        source["rules"][0]["title"] = "Changed after validation"
        write(patch, __import__("yaml").safe_dump(source))
    else:
        if change == "rejected":
            p["rules"][0]["formula"] = {"bool": True}
        elif change == "knowledge":
            p["knowledge"][0]["text"] = "Different source text"
        else:
            p["taxonomy"]["categories"][0]["description"] = "Different category"
        # Recompute file hash, without pretending model evidence became valid.
        import yaml

        from confdr.shared.catalog import save_validation, source_content

        write(patch, yaml.safe_dump(source_content(p)))
        save_validation(patch, p)
    with pytest.raises(ValueError):
        merge_bundles(base, patch, output, category_ids=["B"])
    assert not output.exists()


def test_output_must_be_new_and_category_must_exist(bundles):
    base, patch, output = bundles
    with pytest.raises(ValueError, match="exist in both"):
        merge_bundles(base, patch, output, category_ids=["unknown"])
    with pytest.raises(ValueError, match="already exists"):
        merge_bundles(base, patch, base.parent, category_ids=["B"])


def test_merge_cli_arguments():
    args = parser().parse_args(
        [
            "merge-rules",
            "--base",
            "base/rules.yaml",
            "--patch",
            "patch/rules.yaml",
            "--categories",
            "S3",
            "E1",
            "--output",
            "merged",
        ]
    )
    assert args.categories == ["S3", "E1"]


def test_append_new_category_keeps_old_formulas_and_revalidates(bundles, tmp_path):
    base_path, patch, output = bundles
    base = read_bundle(base_path, require_current=False)
    base["taxonomy"]["categories"] = [c for c in base["taxonomy"]["categories"] if c["id"] == "A"]
    base["knowledge"] = [{**s, "categories": ["A"]} for s in base["knowledge"] if "A" in s["categories"]]
    base["taxonomy"]["taxonomy_version"] = "before-addition"
    save_bundle(tmp_path / "single", base)
    base_path = tmp_path / "single/rules.yaml"
    before = base_path.read_bytes()
    with pytest.raises(ValueError, match="exist in both"):
        merge_bundles(base_path, patch, output, category_ids=["B"])
    report = merge_bundles(base_path, patch, output, category_ids=["B"], allow_new_categories=True)
    assert report["status"] == "unverified"  # Changed contexts are not promoted.
    merged = read_bundle(output / "rules.yaml", require_current=False)
    assert [c["id"] for c in merged["taxonomy"]["categories"]] == ["A", "B"]
    assert [r for r in merged["rules"] if r["category_id"] == "A"] == base["rules"]
    assert merged["generation"]["merge"]["added_categories"] == ["B"]
    assert merged["verification_material"]["reference"]["categories"]["B"]
    assert all(key in merged["verification_material"] for key in ["tests", "reviews", "reference_reviews"])
    assert next(s for s in merged["knowledge"] if s["id"] == "shared")["categories"] == ["A", "B"]
    assert revalidate(output / "rules.yaml", verifier=Reviewer())["status"] == "empirically_validated"
    assert base_path.read_bytes() == before


def test_add_category_rejects_conflicting_shared_knowledge(bundles, tmp_path):
    base_path, patch, output = bundles
    base = read_bundle(base_path, require_current=False)
    base["taxonomy"]["categories"] = [c for c in base["taxonomy"]["categories"] if c["id"] == "A"]
    base["knowledge"] = [{**s, "categories": ["A"]} for s in base["knowledge"] if "A" in s["categories"]]
    base["knowledge"][0]["text"] = "Changed incompatible shared source"
    save_bundle(tmp_path / "single", base)
    with pytest.raises(ValueError, match="Knowledge source definition differs"):
        merge_bundles(
            tmp_path / "single/rules.yaml", patch, output, category_ids=["B"], allow_new_categories=True
        )
    assert not output.exists()


def test_cli_explicit_new_category_switch():
    args = parser().parse_args(
        [
            "merge-rules",
            "--base",
            "base/rules.yaml",
            "--patch",
            "patch/rules.yaml",
            "--output",
            "new",
            "--categories",
            "R5",
            "--allow-new-categories",
        ]
    )
    assert args.allow_new_categories and args.categories == ["R5"]
