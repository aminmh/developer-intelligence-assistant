"""Equivalence review: @.archcore/regression/regression-check.spec.md clauses 4-5, failure clause 7."""

from __future__ import annotations

import json
import zipfile
from io import BytesIO

from regression.equivalence import review, symbol_bodies
from regression.model import (CHANGED_EXPECTED, JUDGMENT_LABEL, NOT_COVERED, SUSPECTED_REGRESSION, UNCHANGED)

BASE_SRC = "def find(id):\n    return db.get(id)\n\ndef total(xs):\n    return sum(xs)\n"
CAND_SRC = "def find(id):\n    row = db.get(id)\n    return row\n\ndef total(xs):\n    return sum(xs) + 1\n"
BASE_DEFS = {"s-find": {"file": "app/repo.py", "line": 1, "end_line": 2},
             "s-total": {"file": "app/repo.py", "line": 4, "end_line": 5},
             "s-old": {"file": "app/repo.py", "line": 4, "end_line": 5}}
CAND_DEFS = {"s-find": {"file": "app/repo.py", "line": 1, "end_line": 3},
             "s-total": {"file": "app/repo.py", "line": 5, "end_line": 6},
             "s-new": {"file": "app/repo.py", "line": 5, "end_line": 6}}


def _zip(text):
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("app/repo.py", text)
    return buffer.getvalue()


def _report():
    symbols = [{"canonical_symbol_id": sid, "fqn": fqn, "language": "python", "change": change, "verdict": None,
                "evidence": []}
               for sid, fqn, change in [("s-find", "repo.find", "modified"), ("s-total", "repo.total", "modified"),
                                        ("s-new", "repo.new", "added"), ("s-old", "repo.old", "removed")]]
    impacted = [{"fqn": "views.show", "path": "app/views.py", "line": 7, "depth": 1, "via": ["views.show", "repo.find"]},
                {"fqn": "views.list", "path": "app/views.py", "line": 12, "depth": 1, "via": ["views.list", "repo.old"]}]
    return {"symbols": symbols, "impacted": impacted}


class Model:
    def __init__(self, answers):
        self.answers = answers
        self.prompts = []

    def __call__(self, prompt, **kwargs):
        self.prompts.append((json.loads(prompt), kwargs))
        answer = self.answers[json.loads(prompt)["symbol"]]
        if isinstance(answer, Exception):
            raise answer
        return json.dumps(answer), "test-model", kwargs["provider"]


def _run(model, **kwargs):
    report = _report()
    summary = review("proj", report, base_definitions=BASE_DEFS, candidate_definitions=CAND_DEFS,
                     base_archive=lambda: _zip(BASE_SRC), candidate_archive=lambda: _zip(CAND_SRC),
                     model_call=model, provider="local", **kwargs)
    return {row["fqn"]: row for row in report["symbols"]}, summary


def test_symbol_bodies_follow_line_ranges():
    bodies = symbol_bodies(_zip(CAND_SRC), CAND_DEFS, ["s-find", "missing"])
    assert bodies["s-find"]["body"] == "def find(id):\n    row = db.get(id)\n    return row"
    assert "missing" not in bodies


def test_verdict_cited_labelled_budgeted():
    model = Model({"repo.find": {"verdict": "same", "reasons": []},
                   "repo.total": {"verdict": "different", "reasons": [
                       {"aspect": "return_value", "text": "adds 1 to the sum", "side": "candidate", "line": 6}]}})
    rows, summary = _run(model)
    assert rows["repo.find"]["verdict"] == UNCHANGED
    total = rows["repo.total"]
    assert total["verdict"] == SUSPECTED_REGRESSION
    evidence = total["evidence"][0]
    assert evidence["label"] == JUDGMENT_LABEL
    assert evidence["reasons"] == [{"aspect": "return_value", "text": "adds 1 to the sum", "side": "candidate",
                                    "path": "app/repo.py", "line": 6}]
    # The prompt carries both bodies and the direct callers, with the language as data.
    sent = model.prompts[0][0]
    assert sent["base"]["body"].startswith("def find") and sent["candidate"]["line"] == 1
    assert sent["callers"] == [{"fqn": "views.show", "path": "app/views.py", "line": 7}]
    assert sent["language"] == "python"
    # Added and removed symbols need no model call.
    assert rows["repo.new"]["verdict"] == CHANGED_EXPECTED
    assert rows["repo.old"]["verdict"] == SUSPECTED_REGRESSION
    assert len(model.prompts) == 2 and summary["reviewed"] == 2


def test_budget_leaves_the_rest_not_covered():
    model = Model({"repo.find": {"verdict": "same"}, "repo.total": {"verdict": "same"}})
    rows, summary = _run(model, max_reviewed=1)
    assert rows["repo.find"]["verdict"] == UNCHANGED
    assert rows["repo.total"]["verdict"] == NOT_COVERED
    assert rows["repo.total"]["evidence"][0]["reason"] == "budget"
    assert summary == {"reviewed": 1, "skipped": 1, "reason": "budget"}


def test_model_failure_and_unclear_answers_stay_not_covered():
    model = Model({"repo.find": {"verdict": "maybe"}, "repo.total": RuntimeError("down")})
    rows, summary = _run(model)
    assert rows["repo.find"]["verdict"] == NOT_COVERED  # an invalid verdict is never "unchanged"
    assert rows["repo.find"]["evidence"][0]["model_verdict"] == "maybe"
    assert rows["repo.total"]["evidence"][0]["reason"] == "model_unavailable"
    assert summary["reason"] == "model_unavailable"


def test_missing_source_is_not_covered():
    report = _report()
    review("proj", report, base_definitions=BASE_DEFS, candidate_definitions=CAND_DEFS,
           base_archive=lambda: b"not a zip", candidate_archive=lambda: _zip(CAND_SRC),
           model_call=Model({}), provider="local")
    assert {row["verdict"] for row in report["symbols"]} == {NOT_COVERED}
