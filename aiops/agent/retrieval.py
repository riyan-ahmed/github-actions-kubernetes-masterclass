"""Runbook retrieval: BM25 over heading-delimited chunks of Markdown files.

This is lexical retrieval (term statistics), not semantic/embedding RAG. It is real,
deterministic and unit-tested; swapping in embeddings would only change `score()`.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = frozenset("a an and are as at be by for from has have in is it its of on or that the this to was were will with not no".split())


def tokenize(text: str) -> list[str]:
    toks = []
    for t in _TOKEN.findall(text.lower()):
        if t in _STOP:
            continue
        if len(t) > 3 and t.endswith("s") and not t.endswith(("ss", "us")):
            t = t[:-1]
        toks.append(t)
    return toks


@dataclass
class Chunk:
    doc: str          # file name
    title: str        # document title (H1)
    heading: str
    text: str
    tokens: list[str]


def load_chunks(directory: str | Path) -> list[Chunk]:
    chunks: list[Chunk] = []
    for path in sorted(Path(directory).glob("*.md")):
        title, heading, buf = path.stem, "Overview", []

        def flush():
            body = "\n".join(buf).strip()
            if body:
                # title and heading are indexed too, so section names carry weight
                chunks.append(Chunk(path.name, title, heading, body, tokenize(f"{title} {heading} {body}")))
            buf.clear()

        for line in path.read_text().splitlines():
            if line.startswith("# "):
                title = line[2:].strip()
            elif line.startswith("## "):
                flush()
                heading = line[3:].strip()
            else:
                buf.append(line)
        flush()
    return chunks


class RunbookIndex:
    def __init__(self, chunks: list[Chunk], k1: float = 1.5, b: float = 0.75):
        self.chunks, self.k1, self.b = chunks, k1, b
        self.n = len(chunks)
        self.avgdl = sum(len(c.tokens) for c in chunks) / max(1, self.n)
        df: Counter = Counter()
        for c in chunks:
            df.update(set(c.tokens))
        self.idf = {t: math.log(1 + (self.n - d + 0.5) / (d + 0.5)) for t, d in df.items()}
        self.tf = [Counter(c.tokens) for c in chunks]

    @classmethod
    def from_dir(cls, directory: str | Path) -> "RunbookIndex":
        return cls(load_chunks(directory))

    def score(self, query: str, i: int) -> float:
        tf, dl, s = self.tf[i], len(self.chunks[i].tokens), 0.0
        for t in set(tokenize(query)):
            f = tf.get(t, 0)
            if f:
                s += self.idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * dl / self.avgdl))
        return s

    def search(self, query: str, k: int = 3) -> list[dict]:
        ranked = sorted(((self.score(query, i), i) for i in range(self.n)), reverse=True)
        return [{"score": round(s, 3), "doc": self.chunks[i].doc, "title": self.chunks[i].title,
                 "heading": self.chunks[i].heading, "text": self.chunks[i].text}
                for s, i in ranked[:k] if s > 0]

    def best_runbook(self, query: str, k: int = 4) -> dict | None:
        """Pick the document whose top chunks score highest; return it with its best sections."""
        hits = self.search(query, k * 3)
        if not hits:
            return None
        per_doc: Counter = Counter()
        for h in hits:
            per_doc[h["doc"]] += h["score"]
        doc = per_doc.most_common(1)[0][0]
        sections = [h for h in hits if h["doc"] == doc][:k]
        return {"doc": doc, "title": sections[0]["title"], "score": round(per_doc[doc], 3), "sections": sections}


def build_query(classification: dict, evidence: dict) -> str:
    """Query text from observed evidence (not from the alert name)."""
    parts = [classification["incident_type"].replace("_", " "), classification["root_cause"].replace("_", " ")]
    parts += [s["signal"] for s in classification["signals"]]
    for p in evidence.get("k8s", {}).get("pods", []):
        for c in p["containers"]:
            for st in (c.get("state"), c.get("last_state")):
                if st:
                    parts += [str(st.get("reason", "")), f"exit code {st.get('exit_code', '')}"]
    parts += [f"{e['reason']} {e['message']}" for e in evidence.get("k8s", {}).get("events", [])[:5]]
    return " ".join(parts)
