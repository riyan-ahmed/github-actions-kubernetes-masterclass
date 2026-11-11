import shutil

from aiops.agent.retrieval import RunbookIndex, tokenize
from aiops.tests.conftest import ROOT

IDX = RunbookIndex.from_dir(ROOT / "runbooks")


def top(q):
    return IDX.best_runbook(q)["doc"]


def test_each_runbook_is_retrievable_from_its_symptoms():
    assert top("container terminated OOMKilled exit code 137 memory limit exceeded") == "oomkilled.md"
    assert top("CrashLoopBackOff back-off restarting failed container exit code 1 unknown port") == "crashloopbackoff.md"
    assert top("Readiness probe failed HTTP probe statuscode 404 pod running not ready no endpoints") == "readiness-failure.md"


def test_ranking_is_content_based_not_filename_based(tmp_path):
    for p in (ROOT / "runbooks").glob("*.md"):
        shutil.copy(p, tmp_path / f"zz-{p.stem[::-1]}.md")      # scrambled file names
    idx = RunbookIndex.from_dir(tmp_path)
    assert idx.best_runbook("OOMKilled memory limit exit code 137")["title"] == "OOMKilled runbook"


def test_returns_sections_with_scores_and_none_for_gibberish():
    r = IDX.best_runbook("kubectl rollout undo previous ReplicaSet crash")
    assert r["sections"] and all(s["score"] > 0 for s in r["sections"])
    assert IDX.best_runbook("zzzz qqqq xxxx") is None


def test_bm25_rare_terms_outweigh_common_ones():
    hits = IDX.search("OOMKilled", 3)
    assert hits[0]["doc"] == "oomkilled.md"
    assert hits[0]["score"] > (hits[1]["score"] if len(hits) > 1 else 0)


def test_tokenizer_drops_stopwords_and_plurals():
    assert tokenize("The pods are restarting") == ["pod", "restarting"]
