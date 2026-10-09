"""Contextual BM25 + optional local dense retrieval/reranking. No Mongo vector feature required."""

import json
import math
import re
from collections import Counter
from hashlib import sha256

from .requirements import KEYWORDS, requirements

STOP = {
    "a",
    "an",
    "the",
    "and",
    "or",
    "of",
    "to",
    "in",
    "for",
    "with",
    "all",
    "only",
    "stated",
    "applicable",
    "selected",
    "including",
    "every",
}

# Datasheet vocabulary supplements the human checklist; these are generic engineering aliases,
# not answers or tuning to a reference PDF.
QUERY_ALIASES = {
    "1.functions": "pin functions input output driver receiver functional modes",
    "9.specific": "electrical characteristics feature description functional modes driver receiver",
    "16.inventory": "typical characteristics application curves waveform timing graphs figures",
    "16.conditions": "typical characteristics application curves waveform timing graph conditions",
    "2.components": "capacitor resistor typical application component values",
    "5.decoupling": "bypass decoupling supply capacitor power recommendations",
    "7.orientation": "pin 1 index dot notch top view bottom view package outline",
    "11.metrics": "thermal information theta psi Rtheta junction ambient",
    "15.revision": "revision history changes revisions",
    "17.reflow": "package option addendum peak reflow temperature solder",
    "17.assembly": "package option addendum moisture sensitivity MSL level peak temp stencil",
    "17.compliance": "package option addendum RoHS REACH environmental lead finish eco plan",
}


def tokens(text):
    words = [t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in STOP]
    return [t[:-1] if len(t) > 4 and t.endswith("s") and not t.endswith("ss") else t for t in words]


class Retriever:
    def __init__(self, store, manifest, chunks, config):
        self.chunks = chunks
        self.by_id = {c["id"]: c for c in chunks}
        self.config = config
        self.texts = [c["context"] + "\n" + c["text"] for c in chunks]
        self.terms = [Counter(tokens(t)) for t in self.texts]
        self.df = Counter(t for terms in self.terms for t in terms)
        self.avg_len = sum(sum(t.values()) for t in self.terms) / max(1, len(chunks))
        self.encoder = None
        self.reranker = None
        self.vectors = None
        if config.get("embedding_model"):
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as e:
                raise ValueError("Install uv sync --extra retrieval to use local embeddings") from e
            self.encoder = SentenceTransformer(
                config["embedding_model"], local_files_only=config.get("local_only", True)
            )
            key = sha256(json.dumps([manifest["id"], config["embedding_model"]]).encode()).hexdigest()
            try:
                record = store.get("embedding_indexes", key)
                self.vectors = json.loads(store.read_artifact(record["artifact_id"]))
            except KeyError:
                self.vectors = self.encoder.encode(self.texts, normalize_embeddings=True).tolist()
                blob = store.artifact(json.dumps(self.vectors).encode(), "application/json", "vectors.json")
                store.put(
                    "embedding_indexes",
                    {
                        "id": key,
                        "index_id": manifest["id"],
                        "artifact_id": blob,
                        "model": config["embedding_model"],
                    },
                )
        if config.get("reranker_model"):
            try:
                from sentence_transformers import CrossEncoder
            except ImportError as e:
                raise ValueError("Install uv sync --extra retrieval to use local reranking") from e
            self.reranker = CrossEncoder(
                config["reranker_model"], local_files_only=config.get("local_only", True)
            )

    def rank(self, query, limit):
        query_terms = set(tokens(query))
        scores = []
        n = len(self.chunks)
        for i, terms in enumerate(self.terms):
            length = sum(terms.values())
            score = sum(
                math.log(1 + (n - self.df[t] + 0.5) / (self.df[t] + 0.5))
                * terms[t]
                * 2.5
                / (terms[t] + 1.5 * (0.25 + 0.75 * length / max(1, self.avg_len)))
                for t in query_terms
                if terms[t]
            )
            if score > 0:
                scores.append((i, score))
        lexical = sorted(scores, key=lambda x: (-x[1], x[0]))
        fused = Counter({i: 1 / (60 + pos) for pos, (i, _) in enumerate(lexical, 1)})
        if self.encoder is not None:
            q = self.encoder.encode([query], normalize_embeddings=True)[0]
            dense = sorted(
                [
                    (i, sum(float(a) * b for a, b in zip(q, v, strict=True)))
                    for i, v in enumerate(self.vectors)
                ],
                key=lambda x: (-x[1], x[0]),
            )
            for pos, (i, _) in enumerate(dense, 1):
                fused[i] += 1 / (60 + pos)
        ranked = sorted(fused, key=lambda i: (-fused[i], i))
        if self.reranker is not None and ranked:
            pool = ranked[: max(limit * 3, 20)]
            rs = self.reranker.predict([(query, self.texts[i]) for i in pool])
            ranked = [i for i, _ in sorted(zip(pool, rs, strict=True), key=lambda x: (-x[1], x[0]))]
        return [(self.chunks[i]["id"], round(fused[i], 6)) for i in ranked[:limit]]

    def select(self, requirement_id, target, *, complement=False, expanded=False):
        sid = int(requirement_id.split(".")[0])
        text = requirements()[requirement_id]
        # Complementary search uses the explicit task vocabulary instead of the initial description.
        query = (" ".join(KEYWORDS[sid]) + " " + requirement_id.split(".")[1]) if complement else text
        query += " " + QUERY_ALIASES.get(requirement_id, "")
        # Exact part names recur in headers/order tables and must not dominate topical relevance.
        ranked = self.rank(query, self.config["top_k"] * (3 if expanded else 1))
        seeds = {cid for cid, _ in ranked}
        if sid in {7, 11, 14, 17}:
            seeds.update(
                c["id"]
                for c in self.chunks
                if set(c["covered_region_ids"]) & set(target.get("evidence_ids", []))
            )
        if sid == 2:
            seeds.update(cid for cid, _ in self.rank("pin configuration pin functions numbering", 2))
        # Never silently lose image-only/unknown pages from a text-only index.
        seeds |= {c["id"] for c in self.chunks if c["unknown_visual"]}
        selected = self.expand(seeds, target)
        return selected, {
            cid: {
                "route": "seed" if cid in seeds else "reference_or_condition",
                "rank_score": dict(ranked).get(cid),
            }
            for cid in selected
        }

    def expand(self, seeds, target):
        selected = set(seeds)
        frontier = set(seeds)
        # Two hops preserve referenced objects plus their source-page conditions. Unrestricted
        # traversal through every reference in a neighbor recreates the entire-document packet.
        for _ in range(2):
            frontier = {linked for cid in frontier for linked in self.by_id[cid]["links"]} - selected
            selected.update(frontier)
        package = target.get("package_code")
        return {
            cid
            for cid in selected
            if not package
            or not self.by_id[cid]["package_hint"]
            or self.by_id[cid]["package_hint"] == package
        }
