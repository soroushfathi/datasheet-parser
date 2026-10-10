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

# Generic source-title routes. They boost actual headings/captions, not occurrences of
# common electrical words in a long requirement or answers from a reference fixture.
SOURCE_TOPICS = {
    1: r"pin (?:configuration|function)",
    2: r"application information|standard application|typical application|design requirements|design procedure",
    3: r"absolute maximum|esd ratings",
    4: r"recommended operating conditions",
    5: r"power supply recommendations|power requirements|application information|design requirements",
    6: r"component selection|design procedure|application information|design requirements",
    7: r"package outline|land pattern|board layout|mechanical dimensions|generic package view",
    8: r"layout guidelines|^\d+ layout$",
    9: r"electrical characteristics",
    10: r"communication interface|feature description|overview|device functional modes",
    11: r"thermal information|thermal characteristics|thermal metric",
    12: r"switching characteristics|timing characteristics|parameter measurement",
    13: r"esd ratings|protection|features",
    14: r"ordering information|orderable part|device information|packaging information|revision history",
    15: r"revision history|errata|notes",
    16: r"typical characteristics|application curves",
    17: r"package option addendum|packaging information|stencil design|solder|reflow|msl|rohs",
}


SOURCE_REQUIREMENT_TOPICS = {
    "1.functions": r"pin (?:configuration|function)|feature description|device functional modes",
    "1.connections": r"pin (?:configuration|function)|application information|standard application|design procedure",
    "2.components": r"standard application|typical application|design requirements|design procedure|component selection",
    "4.logic": r"recommended operating conditions|electrical characteristics",
    "5.decoupling": r"power supply recommendations|standard application|application information|layout guidelines",
    "6.types": r"component selection|standard application|application information|design procedure",
    "10.protocols": r"communication interface|overview|features|design requirements",
    "10.address": r"address|pin configuration|device functional modes|overview",
    "10.levels": r"recommended operating conditions|electrical characteristics|interface",
    "11.layout": r"layout guidelines|layout example|board layout|land pattern|thermal layout",
    "11.protection": r"thermal protection|thermal shutdown|thermal characteristics|thermal information|derating",
    "13.features": r"feature description|electrical characteristics|esd ratings|protection|features",
    "13.behavior": r"feature description|electrical characteristics|esd ratings|protection",
    "16.inventory": r"typical characteristics|application curves|parameter measurement|timing waveform",
    "16.conditions": r"typical characteristics|application curves|parameter measurement|timing waveform",
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
        bounded = self.config.get("packet_version") == "budget-v1"
        limit = self.config["top_k"] * (3 if expanded else 1)
        ranked = (
            self._bounded_rank(query, requirement_id, target, limit) if bounded else self.rank(query, limit)
        )
        seeds = {cid for cid, _ in ranked}
        if sid in {7, 11, 14, 17}:
            seeds.update(
                c["id"]
                for c in self.chunks
                if set(c["covered_region_ids"]) & set(target.get("evidence_ids", []))
            )
        if sid == 2:
            seeds.update(cid for cid, _ in self.rank("pin configuration pin functions numbering", 2))
        # Old runs preserve their original universal visual selection. Budgeted runs route
        # pages by indexed text/captions and expose unclassified pages as coverage gaps.
        # An unknown image with no useful text cannot establish a source-absence decision.
        if not bounded:
            seeds |= {c["id"] for c in self.chunks if c["unknown_visual"]}
        selected = self.expand(seeds, target)
        mandatory = self.required_closure(seeds) if bounded else set()
        return selected, {
            cid: {
                "route": "seed"
                if cid in seeds
                else "required_condition"
                if cid in mandatory
                else "reference_or_condition",
                "rank_score": dict(ranked).get(cid),
                **({"requirement_id": requirement_id} if bounded else {}),
            }
            for cid in selected
        }

    def _bounded_rank(self, query, requirement_id, target, limit):
        sid = int(requirement_id.split(".")[0])
        lexical = dict(self.rank(query, len(self.chunks)))
        scored = []
        package = target.get("package_code")
        source_topic = SOURCE_REQUIREMENT_TOPICS.get(requirement_id, SOURCE_TOPICS[sid])
        for chunk in self.chunks:
            if package and chunk["package_hint"] and chunk["package_hint"] != package:
                continue
            heading = chunk.get("heading_candidate", chunk["context"]).lower()
            text = chunk["text"].lower()
            topic = bool(re.search(source_topic, heading))
            # Unnumbered package captions/order-table headers do not have a numbered
            # heading. Read their source labels as routing metadata as well.
            if sid in {7, 14, 17}:
                topic |= bool(re.search(source_topic, text))
            if sid == 3:
                topic = bool(
                    re.search(
                        "esd ratings" if requirement_id.endswith(".esd") else "absolute maximum", heading
                    )
                )
            if sid == 14 and not requirement_id.endswith(".revision_lifecycle"):
                topic = bool(
                    re.search(
                        r"ordering information|orderable part|device information|packaging information",
                        heading + "\n" + text,
                    )
                )
            legal_notice = bool(re.search(r"^\s*important notice|^\s*disclaimer", text, re.MULTILINE))
            protection_detail = (
                sid == 13
                and not legal_notice
                and bool(
                    re.search(
                        r"short[ -]?circuit|short(?:s|ed|ing)? to ground|over[ -]?voltage|under[ -]?voltage|current limit|fault|latch[ -]?up",
                        text,
                    )
                )
            )
            if protection_detail:
                topic = True
            if requirement_id == "15.footnotes":
                topic = bool(
                    re.search(
                        r"^\s*(?:\(\d+\)|NOTES?\s*[:.]|[A-Z]\.\s+)|\b(?:test conditions|unless otherwise (?:noted|specified))\b",
                        text,
                        re.MULTILINE | re.IGNORECASE,
                    )
                )
            score = lexical.get(chunk["id"], 0) + (10 if topic else 0)
            if protection_detail:
                score += 3
            if sid == 7 and chunk["package_hint"] == package and chunk["visual"]:
                score += 10
            # An unclassified image surrounded by package appendix pages is a relevant
            # mechanical search candidate, never an inferred package identification.
            if chunk["unknown_visual"] and sid in {7, 17} and "package" in heading:
                score += 8
            if (
                requirement_id == "15.footnotes"
                and topic
                and re.search(r"characteristics|rating|operating|measurement", heading)
            ):
                score += 2
            if requirement_id == "15.footnotes" and topic and "package appendix" in heading:
                score += 1
            if requirement_id == "15.footnotes" and chunk["unknown_visual"] and "package" in heading:
                score += 11
            if sid == 14 and target.get("part_number") and target["part_number"].lower() in text and topic:
                score += 3
            if score > 0:
                scored.append((chunk["id"], score, chunk["ordinal"]))
        ranked = sorted(scored, key=lambda r: (-r[1], r[2]))
        # Cover distinct relevant source titles before selecting many fragments under
        # one title. A chosen heading still brings its complete atomic table closure.
        first, repeated, seen = [], [], set()
        for row in ranked:
            chunk = self.by_id[row[0]]
            heading = (
                chunk["page"]
                if requirement_id == "15.footnotes"
                else chunk.get("heading_candidate", chunk["context"])
            )
            if row[1] >= 8 and heading not in seen:
                first.append(row)
                seen.add(heading)
            else:
                repeated.append(row)
        return [(cid, round(score, 6)) for cid, score, _ in (first + repeated)[:limit]]

    def expand(self, seeds, target):
        if self.config.get("packet_version") == "budget-v1":
            # Required condition/footnote closure is transitive. Optional references are
            # followed once; recursively walking their references recreates the document.
            selected = self.required_closure(seeds)
            optional = {
                edge["chunk_id"]
                for cid in seeds
                if cid in self.by_id
                for edge in self.by_id[cid].get("typed_links", [])
                if not edge["required"]
            }
            selected |= self.required_closure(optional)
            package = target.get("package_code")
            return {
                cid
                for cid in selected
                if not package
                or not self.by_id[cid]["package_hint"]
                or self.by_id[cid]["package_hint"] == package
            }
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

    def required_closure(self, seeds):
        selected = set(seeds) & set(self.by_id)
        frontier = set(selected)
        while frontier:
            linked = {
                edge["chunk_id"]
                for cid in frontier
                for edge in self.by_id[cid].get("typed_links", [])
                if edge["required"] and edge["chunk_id"] in self.by_id
            } - selected
            selected.update(linked)
            frontier = linked
        return selected
