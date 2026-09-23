"""owl4_core: the four WiseOwl core metrics (Describe, Define, Connection, Flat).

Copied from WiseOwl 0.10.0 (https://github.com/aryand1/WiseOwl):
ontology/vocab.py, ontology/index.py, ontology/loader.py, ontology/identity.py,
compute/embeddings.py, metrics/describe.py, define.py, connection.py, flat.py
and the core part of reporting.py.

The score math is unchanged. The additions only record more detail:
per-metric counts, per-entity values, timings, and memory use.

Version 1.1 adds (strict WiseOwl scores are unchanged):
* define_bp: a second Define score that also reads each ontology's own
  definition property (from BioPortal metadata, or detected by name, such as
  NCIT's "DEFINITION" property P97). The strict Define is still reported.
* owlready2 fallback parser for OWL/XML and for RDF/XML that rdflib rejects.
* Every parse attempt's error is recorded.

Two engineering changes, neither of which changes the formulas:
1. Define streams the definition vectors batch by batch and compares each
   batch with its label vectors, so very large ontologies do not need all
   vectors on the GPU at once. Batch order is the same length-sorted order
   WiseOwl uses.
2. The parser tries the format detected from the file content first, then
   falls back to WiseOwl's own format list.
"""

from __future__ import annotations

import gc
import hashlib
import json
import logging
import math
import os
import re
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, NamedTuple, Union

import numpy as np
import rdflib
from rdflib import BNode, Literal, URIRef
from rdflib.namespace import DCTERMS, OWL, RDF, RDFS, SKOS, Namespace

log = logging.getLogger("owl4.core")

WISEOWL_SOURCE_VERSION = "0.10.0"
CORE4_VERSION = "core4-1.1.1"

Node = Union[URIRef, BNode, Literal]
Pair = tuple

# =============================================================================
# vocab.py (copied)
# =============================================================================

OBOINOWL = Namespace("http://www.geneontology.org/formats/oboInOwl#")
IAO = Namespace("http://purl.obolibrary.org/obo/")
SKOSXL = Namespace("http://www.w3.org/2008/05/skos-xl#")
IAO_DEFINITION: URIRef = IAO["IAO_0000115"]

CLASS_TYPES: tuple = (OWL.Class, RDFS.Class)

DESCRIBE_SIGNALS: frozenset = frozenset(
    {
        RDFS.label, SKOS.prefLabel, SKOS.altLabel, RDFS.comment, DCTERMS.description,
        DCTERMS.title, SKOS.definition, IAO_DEFINITION, OBOINOWL.hasExactSynonym,
        OBOINOWL.hasRelatedSynonym, OBOINOWL.hasBroadSynonym, OBOINOWL.hasNarrowSynonym,
    }
)

DEFINE_DEFINITION_PROPS: tuple = (
    RDFS.comment, DCTERMS.description, SKOS.definition, IAO_DEFINITION, OBOINOWL.hasDefinition,
)

LABEL_PROPS: tuple = (SKOS.prefLabel, RDFS.label)

CONNECTION_ANNOTATION_PROPS: frozenset = frozenset(
    {
        RDFS.label, RDFS.comment, RDFS.seeAlso, RDFS.isDefinedBy, DCTERMS.title,
        DCTERMS.creator, DCTERMS.description, DCTERMS.abstract, DCTERMS.subject,
        DCTERMS.identifier, SKOS.prefLabel, SKOS.altLabel, SKOS.hiddenLabel,
        SKOS.definition, SKOS.note, SKOS.scopeNote, SKOS.changeNote, SKOS.editorialNote,
        SKOS.historyNote, OBOINOWL.hasExactSynonym, OBOINOWL.hasRelatedSynonym,
        OBOINOWL.hasBroadSynonym, OBOINOWL.hasNarrowSynonym,
    }
)

STRUCTURAL_PREDICATES: frozenset = frozenset(
    {
        RDF.type, RDFS.subClassOf, RDFS.subPropertyOf, RDFS.domain, RDFS.range,
        OWL.equivalentClass, OWL.disjointWith, OWL.unionOf, OWL.intersectionOf,
        OWL.complementOf, OWL.oneOf, OWL.sameAs, OWL.differentFrom, OWL.hasKey,
        OWL.onProperty, OWL.someValuesFrom, OWL.allValuesFrom, OWL.propertyChainAxiom,
        OWL.inverseOf, OWL.equivalentProperty,
    }
)

RESTRICTION_FILLER_PROPS: tuple = (OWL.someValuesFrom, OWL.allValuesFrom)

DEFINE_STOPWORDS: frozenset = frozenset(
    "a an the and or of for in on with by to from is are was were be been being "
    "this that these those it its as at".split()
)


def local_name(iri: str) -> str:
    if "#" in iri:
        return iri.rsplit("#", 1)[-1]
    if "/" in iri:
        return iri.rsplit("/", 1)[-1]
    return iri


# =============================================================================
# index.py (copied; Python 3.12 "type" aliases replaced for wider compatibility)
# =============================================================================

_EMPTY: tuple = ()


@dataclass(frozen=True, slots=True)
class OntologyIndex:
    triple_count: int
    _spo: Mapping
    _by_predicate: Mapping
    _by_type: Mapping
    classes: frozenset
    named_classes: frozenset
    individuals: frozenset
    annotation_properties: frozenset

    @classmethod
    def from_graph(cls, graph: rdflib.Graph) -> "OntologyIndex":
        spo: dict = {}
        by_predicate: dict = {}
        by_type: dict = {}
        count = 0
        for s, p, o in graph:
            count += 1
            spo.setdefault(s, {}).setdefault(p, []).append(o)
            by_predicate.setdefault(p, []).append((s, o))
            if p == RDF.type:
                by_type.setdefault(o, set()).add(s)

        frozen_spo = {s: {p: tuple(os_) for p, os_ in ps.items()} for s, ps in spo.items()}
        del spo
        frozen_by_predicate = {p: tuple(pairs) for p, pairs in by_predicate.items()}
        del by_predicate
        frozen_by_type = {t: frozenset(ss) for t, ss in by_type.items()}

        classes = _collect_classes(frozen_by_type, frozen_by_predicate)
        individuals = frozenset(
            s for s, o in frozen_by_predicate.get(RDF.type, _EMPTY)
            if o in classes and s not in classes
        )
        return cls(
            triple_count=count,
            _spo=frozen_spo,
            _by_predicate=frozen_by_predicate,
            _by_type=frozen_by_type,
            classes=classes,
            named_classes=classes - {OWL.Thing, OWL.Nothing},
            individuals=individuals,
            annotation_properties=frozen_by_type.get(OWL.AnnotationProperty, frozenset()),
        )

    def objects(self, subject, predicate) -> tuple:
        return self._spo.get(subject, {}).get(predicate, _EMPTY)

    def predicates(self, subject) -> Iterable:
        return self._spo.get(subject, {}).keys()

    def has(self, subject, predicate) -> bool:
        return predicate in self._spo.get(subject, {})

    def pairs(self, predicate) -> tuple:
        return self._by_predicate.get(predicate, _EMPTY)

    def subjects_of_type(self, rdf_type) -> frozenset:
        return self._by_type.get(rdf_type, frozenset())

    def predicate_groups(self) -> Iterator:
        yield from self._by_predicate.items()

    def rdf_list(self, head) -> list:
        items: list = []
        seen: set = set()
        node = head
        while node != RDF.nil and node not in seen:
            seen.add(node)
            firsts = self.objects(node, RDF.first)
            if not firsts:
                break
            items.append(firsts[0])
            rests = self.objects(node, RDF.rest)
            if not rests:
                break
            node = rests[0]
        return items

    @property
    def entities(self) -> frozenset:
        return self.classes | self.individuals

    def texts(self, subject, predicate) -> list:
        return [str(o) for o in self.objects(subject, predicate)]

    def first_label(self, entity) -> str:
        for prop in LABEL_PROPS:
            values = self.texts(entity, prop)
            if values:
                return values[0]
        return str(entity).split("#")[-1].split("/")[-1]

    def definition(self, entity, props: tuple, *, strip_each: bool) -> str:
        parts: list = []
        for prop in props:
            for value in self.texts(entity, prop):
                if strip_each:
                    text = value.strip()
                    if text:
                        parts.append(text)
                else:
                    parts.append(value)
        joined = " ".join(parts)
        return joined if strip_each else joined.strip()


def _collect_classes(by_type: Mapping, by_predicate: Mapping) -> frozenset:
    classes: set = set()
    for class_type in (*CLASS_TYPES, SKOS.Concept):
        classes.update(s for s in by_type.get(class_type, ()) if isinstance(s, URIRef))
    for child, parent in by_predicate.get(RDFS.subClassOf, ()):
        if isinstance(child, URIRef):
            classes.add(child)
        if isinstance(parent, URIRef):
            classes.add(parent)
    return frozenset(classes)


# =============================================================================
# loader.py (copied) + content sniffing
# =============================================================================

PARSE_FORMATS: Sequence = (None, "xml", "turtle", "n3", "nt", "trig", "trix")
RDFLIB_FORMATS = {"xml", "turtle", "n3", "nt", "trig", "trix", "json-ld", "nquads"}
UNSUPPORTED_FORMATS = {"obo", "ofn", "omn", "html", "zip", "gzip", "empty", "office_document"}
NOT_AN_ONTOLOGY_FORMATS = {"html", "office_document"}


class OntologyParseError(ValueError):
    def __init__(self, message: str, attempts: list | None = None) -> None:
        super().__init__(message)
        self.attempts = attempts or []


def parse_with_owlready2(path: str, kind: str) -> rdflib.Graph:
    """Convert RDF/XML or OWL/XML with owlready2's standalone parsers (no imports loaded)."""
    if kind == "owlxml":
        from owlready2.owlxml_2_ntriples import parse as o2_parse
    else:
        from owlready2.rdfxml_2_ntriples import parse as o2_parse
    graph = rdflib.Graph()
    add = graph.add
    bnodes: dict = {}

    def node(x: str):
        if x.startswith("_:"):
            b = bnodes.get(x)
            if b is None:
                b = bnodes[x] = BNode()
            return b
        return URIRef(x)

    def on_obj(s, p, o):
        add((node(s), URIRef(p), node(o)))

    def on_data(s, p, o, d):
        if d and d.startswith("@"):
            lit = Literal(o, lang=d[1:])
        elif d:
            lit = Literal(o, datatype=URIRef(d))
        else:
            lit = Literal(o)
        add((node(s), URIRef(p), lit))

    with open(path, "rb") as fh:
        o2_parse(fh, on_prepare_obj=on_obj, on_prepare_data=on_data)
    if len(graph) == 0:
        raise ValueError("owlready2 read 0 triples")
    return graph


def sniff_format(path: str, head_bytes: int = 65536) -> str | None:
    """Guess the serialization from the first bytes of the file."""
    with open(path, "rb") as fh:
        raw = fh.read(head_bytes)
    if not raw.strip():
        return "empty"
    if raw[:2] == b"PK":
        try:
            import zipfile
            with zipfile.ZipFile(path) as zf:
                if "[Content_Types].xml" in zf.namelist():
                    return "office_document"
        except Exception:  # noqa: BLE001
            pass
        return "zip"
    if raw[:2] == b"\x1f\x8b":
        return "gzip"
    text = raw.decode("utf-8", errors="ignore").lstrip("\ufeff").lstrip()
    low = text.lower()
    if "<rdf:rdf" in low:
        return "xml"
    if low.startswith("<!doctype html") or low.startswith("<html"):
        return "html"
    if "<ontology" in low and "www.w3.org/2002/07/owl" in low:
        return "owlxml"
    if low.startswith("<?xml"):
        return "trix" if "<trix" in low else "xml"
    if low.startswith("format-version:") or "\n[term]" in low or low.startswith("[term]"):
        return "obo"
    first_lines = [ln.strip() for ln in low.splitlines()[:200] if ln.strip() and not ln.strip().startswith("#")]
    if first_lines and (first_lines[0].startswith("prefix(") or first_lines[0].startswith("ontology(")):
        return "ofn"
    if first_lines and (first_lines[0].startswith("prefix:") or first_lines[0].startswith("ontology:")):
        return "omn"
    if low.startswith("{") or low.startswith("["):
        return "json-ld"
    if any(ln.startswith(("@prefix", "@base", "prefix ", "base ")) for ln in first_lines[:50]):
        return "turtle"
    if first_lines and all(ln.startswith(("<", "_:")) and ln.endswith(".") for ln in first_lines[:20]):
        return "nt"
    return None


def parse_graph(path: str, first_format: str | None = None, *, use_owlready2: bool = True) -> tuple:
    """Parse the file. Returns (graph, format_used, attempts).

    Tries ``first_format`` (from sniffing) first, then WiseOwl's own list,
    then owlready2 (RDF/XML or OWL/XML) if rdflib could not read the file.
    """
    path = os.fspath(path)
    attempts: list = []
    if first_format == "owlxml":
        order: list = []
    else:
        order = []
        if first_format in RDFLIB_FORMATS:
            order.append(first_format)
        order.extend(f for f in PARSE_FORMATS if f not in order)
    last_error: Exception | None = None
    for fmt in order:
        graph = rdflib.Graph()
        t0 = time.perf_counter()
        try:
            graph.parse(path, format=fmt)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            attempts.append({"format": fmt or "auto", "ok": False,
                             "seconds": round(time.perf_counter() - t0, 3),
                             "error": f"{type(exc).__name__}: {str(exc)[:300]}"})
            del graph
            gc.collect()
            continue
        attempts.append({"format": fmt or "auto", "ok": True,
                         "seconds": round(time.perf_counter() - t0, 3)})
        return graph, fmt or "auto", attempts
    if use_owlready2 and first_format in ("xml", "owlxml", None, "trix"):
        kind = "owlxml" if first_format == "owlxml" else "rdfxml"
        t0 = time.perf_counter()
        try:
            graph = parse_with_owlready2(path, kind)
            attempts.append({"format": f"owlready2:{kind}", "ok": True,
                             "seconds": round(time.perf_counter() - t0, 3)})
            return graph, f"owlready2:{kind}", attempts
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            attempts.append({"format": f"owlready2:{kind}", "ok": False,
                             "seconds": round(time.perf_counter() - t0, 3),
                             "error": f"{type(exc).__name__}: {str(exc)[:300]}"})
    tried = ", ".join(a["format"] for a in attempts)
    raise OntologyParseError(
        f"unsupported format (tried {tried}). Last error: {last_error}", attempts)


# =============================================================================
# identity.py (copied)
# =============================================================================

class OntologyId(NamedTuple):
    ontology_iri: str
    version_iri: str


def file_sha256(path: str, *, length: int | None = 32) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    h = digest.hexdigest()
    return h[:length] if length else h


def ontology_id(graph: rdflib.Graph, path: str) -> OntologyId:
    ontology_iri = None
    version_iri = None
    for subject in graph.subjects(RDF.type, OWL.Ontology):
        ontology_iri = str(subject)
        for version in graph.objects(subject, OWL.versionIRI):
            version_iri = str(version)
        break
    content_hash = None
    if ontology_iri is None or version_iri is None:
        try:
            content_hash = file_sha256(path)
        except OSError:
            content_hash = None
    if ontology_iri is None:
        ontology_iri = f"file:hash:{content_hash}" if content_hash else "anonymous"
    if version_iri is None:
        version_iri = f"filehash:{content_hash}" if content_hash else "filehash:unknown"
    return OntologyId(ontology_iri, version_iri)


# =============================================================================
# embeddings.py (copied ClsEncoder) + streaming paired cosine
# =============================================================================

def _torch():
    import torch
    return torch


class ClsEncoder:
    """Encode texts to [CLS] vectors (copied from WiseOwl compute/embeddings.py)."""

    def __init__(self, tokenizer, model, device, *, max_length: int, batch_size: int,
                 mixed_precision: bool = False, vectors_on_device_max_gb: float = 4.0) -> None:
        torch = _torch()
        self._tokenizer = tokenizer
        self._model = model.to(device).eval()
        self._device = torch.device(device)
        self._max_length = max_length
        self._batch_size = max(1, batch_size)
        self._mixed_precision = mixed_precision
        self._vectors_on_device_max_bytes = int(vectors_on_device_max_gb * (1 << 30))

    @property
    def device(self):
        return self._device

    @property
    def batch_size(self) -> int:
        return self._batch_size

    @property
    def max_length(self) -> int:
        return self._max_length

    def _autocast(self):
        import contextlib
        torch = _torch()
        if self._mixed_precision and self._device.type == "cuda":
            dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
            return torch.autocast(device_type="cuda", dtype=dtype)
        return contextlib.nullcontext()

    def _batches(self, texts: Sequence[str]):
        """Yield (indices, cls_vectors_on_device, token_lengths) in WiseOwl's length-sorted order."""
        torch = _torch()
        order = sorted(range(len(texts)), key=lambda i: len(texts[i]), reverse=True)
        for start in range(0, len(order), self._batch_size):
            batch_idx = order[start: start + self._batch_size]
            batch = self._tokenizer(
                [texts[i] for i in batch_idx],
                return_tensors="pt",
                padding=True,
                truncation=True,
                max_length=self._max_length,
            ).to(self._device)
            with torch.inference_mode(), self._autocast():
                output = self._model(**batch)
            vectors = output.last_hidden_state[:, 0, :].float()
            lengths = batch["attention_mask"].sum(dim=1).cpu().numpy()
            yield batch_idx, vectors, lengths

    def encode(self, texts: Sequence[str]):
        """Same result as WiseOwl ClsEncoder.encode: (n, hidden) float32, input order."""
        torch = _torch()
        hidden = self._model.config.hidden_size
        n = len(texts)
        on_device = n * hidden * 4 <= self._vectors_on_device_max_bytes
        target = self._device if on_device else torch.device("cpu")
        out = torch.empty((n, hidden), dtype=torch.float32, device=target)
        lengths = np.zeros(n, dtype=np.int32)
        for batch_idx, vectors, lens in self._batches(texts):
            idx = torch.tensor(batch_idx, dtype=torch.long, device=target)
            out[idx] = vectors.to(target)
            lengths[batch_idx] = lens
        return out, lengths

    def paired_cosine(self, a_texts: Sequence[str], b_texts: Sequence[str]) -> dict:
        """cosine(CLS(a[i]), CLS(b[i])) for every i, computed on the device.

        Equivalent to WiseOwl's ``rowwise_cosine(encode(a), encode(b))`` but the
        b-vectors are never all held at once.
        """
        torch = _torch()
        import torch.nn.functional as F
        n = len(a_texts)
        t0 = time.perf_counter()
        a_vecs, a_len = self.encode(a_texts)
        t_a = time.perf_counter() - t0
        cos = np.zeros(n, dtype=np.float64)
        b_len = np.zeros(n, dtype=np.int32)
        t1 = time.perf_counter()
        for batch_idx, b_vecs, lens in self._batches(b_texts):
            idx = torch.tensor(batch_idx, dtype=torch.long, device=a_vecs.device)
            a_part = a_vecs[idx].to(self._device)
            sims = F.cosine_similarity(a_part, b_vecs, dim=1)
            cos[batch_idx] = sims.cpu().numpy().astype(np.float64)
            b_len[batch_idx] = lens
        t_b = time.perf_counter() - t1
        return {
            "cosine": cos,
            "a_token_len": a_len,
            "b_token_len": b_len,
            "seconds_encode_a": round(t_a, 3),
            "seconds_encode_b_and_cosine": round(t_b, 3),
            "a_vectors_device": str(a_vecs.device),
        }


def load_encoder(bert_name: str, device: str, *, max_length: int, batch_size: int,
                 mixed_precision: bool, vectors_on_device_max_gb: float) -> ClsEncoder:
    from transformers import AutoModel, AutoTokenizer
    tokenizer = AutoTokenizer.from_pretrained(bert_name)
    model = AutoModel.from_pretrained(bert_name)
    model = model.float()
    return ClsEncoder(tokenizer, model, device, max_length=max_length, batch_size=batch_size,
                      mixed_precision=mixed_precision,
                      vectors_on_device_max_gb=vectors_on_device_max_gb)


# =============================================================================
# Metrics (formulas copied; details added)
# =============================================================================

_WORD = re.compile(r"\w+")
_TRUE_STRINGS = {"true", "1"}


def _short(node) -> str:
    return str(node)


# ---------------------------------------------------------------- Describe

def _has_skosxl_alt_label(index: OntologyIndex, entity) -> bool:
    return any(
        index.has(label_node, SKOSXL.literalForm)
        for label_node in index.objects(entity, SKOSXL.altLabel)
    )


def compute_describe(index: OntologyIndex, entities: list) -> tuple:
    """Describe = 10 * described / entities (WiseOwl metrics/describe.py)."""
    n = len(entities)
    details: dict = {"entities": n}
    if not entities:
        details.update(described=0, reason="no entities")
        return 0.0, details, None
    signals = frozenset(DESCRIBE_SIGNALS) | index.annotation_properties
    described = np.zeros(n, dtype=bool)
    n_signals = np.zeros(n, dtype=np.int32)
    signal_use: Counter = Counter()
    skosxl_only = 0
    for i, entity in enumerate(entities):
        present = [p for p in index.predicates(entity) if p in signals]
        sk = False
        if not present:
            sk = _has_skosxl_alt_label(index, entity)
            if sk:
                skosxl_only += 1
        for p in present:
            signal_use[p] += 1
        n_signals[i] = len(present) + (1 if sk else 0)
        described[i] = bool(present) or sk
    count = int(described.sum())
    score = round(10.0 * count / n, 2)
    builtin = {str(p): signal_use.get(p, 0) for p in sorted(DESCRIBE_SIGNALS, key=str)}
    declared = {str(p): c for p, c in signal_use.most_common() if p not in DESCRIBE_SIGNALS}
    details.update(
        described=count,
        undescribed=n - count,
        described_share=count / n,
        described_by_skosxl_altlabel_only=skosxl_only,
        declared_annotation_properties=len(index.annotation_properties),
        signal_usage_builtin=builtin,
        signal_usage_declared_annotation_properties_top50=dict(list(declared.items())[:50]),
    )
    return score, details, {"described": described, "describe_signal_count": n_signals}


# ---------------------------------------------------------------- Define

def adequacy_parts(text: str, min_tokens: int = 12) -> tuple:
    """Returns (adequacy, completeness, quality, token_count); adequacy as in WiseOwl."""
    tokens = _WORD.findall(text.lower())
    if not tokens:
        return 0.0, 0.0, 0.0, 0
    completeness = min(1.0, len(tokens) / min_tokens)
    quality = 1.0 - sum(t in DEFINE_STOPWORDS for t in tokens) / len(tokens)
    return max(0.0, min(1.0, 0.4 * completeness + 0.6 * quality)), completeness, quality, len(tokens)


def _label_source(index: OntologyIndex, entity) -> str:
    for prop, name in ((SKOS.prefLabel, "skos:prefLabel"), (RDFS.label, "rdfs:label")):
        if index.objects(entity, prop):
            return name
    return "iri_local_name"


def _stats(arr) -> dict:
    if arr is None or len(arr) == 0:
        return {"n": 0}
    a = np.asarray(arr, dtype=np.float64)
    return {
        "n": int(a.size), "mean": float(a.mean()), "std": float(a.std()),
        "min": float(a.min()), "p05": float(np.percentile(a, 5)),
        "p25": float(np.percentile(a, 25)), "median": float(np.median(a)),
        "p75": float(np.percentile(a, 75)), "p95": float(np.percentile(a, 95)),
        "max": float(a.max()),
    }


def _definition_text(index: OntologyIndex, entity, props: tuple, literal_only: frozenset) -> str:
    """Same joining as OntologyIndex.definition(strip_each=False); props in literal_only keep text values only."""
    parts: list = []
    for prop in props:
        for o in index.objects(entity, prop):
            if prop in literal_only and not isinstance(o, Literal):
                continue
            parts.append(str(o))
    return " ".join(parts).strip()


def compute_define(index: OntologyIndex, entities: list, encoder: ClsEncoder, *,
                   min_tokens: int = 12,
                   fallback_encoder: Callable[[], ClsEncoder] | None = None,
                   props: tuple = DEFINE_DEFINITION_PROPS,
                   literal_only: frozenset = frozenset()) -> tuple:
    """Define = 10 * mean(0.4*match + 0.6*adequacy) (WiseOwl metrics/define.py).

    ``props`` is WiseOwl's definition property list for the strict score; the
    BioPortal-aware score passes a longer list.
    """
    n = len(entities)
    details: dict = {"entities": n}
    if not entities:
        details.update(defined=0, reason="no entities")
        return 0.0, details, None

    if literal_only:
        definitions = [_definition_text(index, e, props, literal_only) for e in entities]
    else:
        definitions = [index.definition(e, props, strip_each=False) for e in entities]
    defined_idx = [i for i, d in enumerate(definitions) if d.strip()]
    source_use = Counter()
    for i in defined_idx:
        for prop in props:
            if index.objects(entities[i], prop):
                source_use[str(prop)] += 1
    if not defined_idx:
        details.update(defined=0, reason="no definitions")
        return 0.0, details, {"has_definition": np.zeros(n, dtype=bool)}

    labels = [index.first_label(entities[i]) for i in defined_idx]
    texts = [definitions[i] for i in defined_idx]
    label_sources = Counter(_label_source(index, entities[i]) for i in defined_idx)

    used_device = str(encoder.device)
    fallback_reason = None
    try:
        pc = encoder.paired_cosine(labels, texts)
    except Exception as exc:  # noqa: BLE001
        is_oom = "out of memory" in str(exc).lower() or type(exc).__name__ == "OutOfMemoryError"
        if not (is_oom and fallback_encoder is not None):
            raise
        torch = _torch()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        log.warning("Define: GPU out of memory, retrying on CPU")
        fallback_reason = f"{type(exc).__name__}: {str(exc)[:200]}"
        enc2 = fallback_encoder()
        used_device = str(enc2.device)
        pc = enc2.paired_cosine(labels, texts)

    cosines = pc["cosine"]
    mu = float(cosines.mean())
    sd = float(cosines.std())
    sd_raw = sd
    if sd == 0.0:
        sd = 1.0
    match = 1.0 / (1.0 + np.exp(-(cosines - mu) / sd))
    parts = [adequacy_parts(t, min_tokens) for t in texts]
    adequacy = np.fromiter((p[0] for p in parts), dtype=np.float64, count=len(parts))
    completeness = np.fromiter((p[1] for p in parts), dtype=np.float64, count=len(parts))
    quality = np.fromiter((p[2] for p in parts), dtype=np.float64, count=len(parts))
    word_tokens = np.fromiter((p[3] for p in parts), dtype=np.int64, count=len(parts))

    per_entity = np.zeros(n, dtype=np.float64)
    per_entity[defined_idx] = 0.4 * match + 0.6 * adequacy
    score = round(10.0 * float(per_entity.mean()), 2)

    max_len = encoder.max_length
    details.update(
        defined=len(defined_idx),
        undefined=n - len(defined_idx),
        defined_share=len(defined_idx) / n,
        mean_value_over_defined=float((0.4 * match + 0.6 * adequacy).mean()),
        cosine_mean_used_for_zscore=mu,
        cosine_std_used_for_zscore=sd_raw,
        cosine_stats=_stats(cosines),
        match_stats=_stats(match),
        adequacy_stats=_stats(adequacy),
        completeness_stats=_stats(completeness),
        quality_stats=_stats(quality),
        definition_word_tokens_stats=_stats(word_tokens),
        definitions_shorter_than_min_tokens=int((word_tokens < min_tokens).sum()),
        definitions_truncated_at_bert_max_length=int((pc["b_token_len"] >= max_len).sum()),
        labels_truncated_at_bert_max_length=int((pc["a_token_len"] >= max_len).sum()),
        bert_max_length=max_len,
        min_tokens=min_tokens,
        label_source_counts=dict(label_sources),
        definition_source_usage=dict(source_use),
        device=used_device,
        gpu_oom_fallback=fallback_reason,
        batch_size=encoder.batch_size,
        label_vectors_device=pc["a_vectors_device"],
        seconds_encode_labels=pc["seconds_encode_a"],
        seconds_encode_definitions_and_cosine=pc["seconds_encode_b_and_cosine"],
    )
    ent = {
        "has_definition": np.zeros(n, dtype=bool),
        "definition_chars": np.zeros(n, dtype=np.int64),
        "definition_word_tokens": np.zeros(n, dtype=np.int64),
        "definition_bert_tokens": np.zeros(n, dtype=np.int32),
        "label_bert_tokens": np.zeros(n, dtype=np.int32),
        "define_cosine": np.full(n, np.nan),
        "define_match": np.full(n, np.nan),
        "define_adequacy": np.full(n, np.nan),
        "define_value": per_entity,
    }
    di = np.asarray(defined_idx)
    ent["has_definition"][di] = True
    ent["definition_chars"][di] = [len(t) for t in texts]
    ent["definition_word_tokens"][di] = word_tokens
    ent["definition_bert_tokens"][di] = pc["b_token_len"]
    ent["label_bert_tokens"][di] = pc["a_token_len"]
    ent["define_cosine"][di] = cosines
    ent["define_match"][di] = match
    ent["define_adequacy"][di] = adequacy
    return score, details, ent


_DEFINITION_NAMES = {"definition", "def", "defn", "textualdefinition", "definitions"}


def detect_definition_properties(index: OntologyIndex) -> dict:
    """Properties named or labelled "definition" (e.g. NCIT P97, efo:definition) outside WiseOwl's list."""
    found: dict = {}
    for p, pairs in index.predicate_groups():
        if p in DEFINE_DEFINITION_PROPS or not isinstance(p, URIRef):
            continue
        names = [local_name(str(p))] + index.texts(p, RDFS.label) + index.texts(p, SKOS.prefLabel)
        norm = {re.sub(r"[^a-z]", "", n.lower()) for n in names}
        if not norm & _DEFINITION_NAMES:
            continue
        sample = pairs[:1000]
        literals = sum(1 for _, o in sample if isinstance(o, Literal))
        if sample and literals >= 0.8 * len(sample):
            found[p] = len(pairs)
    return found


# Properties that point to where a term is defined, not to definition text.
NOT_DEFINITION_TEXT = frozenset({RDFS.isDefinedBy, RDFS.seeAlso, OWL.sameAs})


def bp_definition_props(index: OntologyIndex, extra_iris: Iterable | None) -> tuple:
    """WiseOwl's list + BioPortal's declared definition property + detected ones.

    Returns (props, sources). Extra properties only contribute text values
    (see compute_define literal_only); rdfs:isDefinedBy and similar are ignored.
    """
    props = list(DEFINE_DEFINITION_PROPS)
    sources: dict = {}
    for iri in extra_iris or ():
        u = URIRef(str(iri))
        if u in NOT_DEFINITION_TEXT:
            sources[str(u)] = "ignored: links to a defining document, not definition text"
            continue
        if u not in props:
            props.append(u)
            sources[str(u)] = "bioportal_definitionProperty"
    for p, n in detect_definition_properties(index).items():
        if p not in props:
            props.append(p)
            sources[str(p)] = f"detected_by_name ({n} uses)"
        elif str(p) in sources:
            sources[str(p)] += " + detected_by_name"
    return tuple(props), sources


# ---------------------------------------------------------------- Connection

_MAX_DISTINCT_PROPS = 5.0
_RICHNESS_LOG_BASE = 11


def _object_properties(index: OntologyIndex) -> tuple:
    annotation_props = frozenset(CONNECTION_ANNOTATION_PROPS) | index.annotation_properties
    object_like: set = set()
    for predicate, pairs in index.predicate_groups():
        if predicate in STRUCTURAL_PREDICATES or predicate in annotation_props:
            continue
        if any(not isinstance(o, Literal) for _, o in pairs):
            object_like.add(predicate)
    declared = index.subjects_of_type(OWL.ObjectProperty)
    result = frozenset((declared | object_like) - annotation_props)
    return result, declared, object_like


def compute_connection(index: OntologyIndex, entities: list) -> tuple:
    """Connection = 10*(0.7 coverage + 0.2 diversity + 0.1 richness) (metrics/connection.py)."""
    n = len(entities)
    details: dict = {"entities": n}
    if not entities:
        details.update(reason="no entities")
        return 0.0, details, None
    entity_set = index.entities
    object_properties, declared, object_like = _object_properties(index)
    distinct_props: dict = defaultdict(set)
    link_counts: dict = defaultdict(int)
    prop_links: Counter = Counter()
    triple_links = 0
    for prop in object_properties:
        for subject, obj in index.pairs(prop):
            if subject in entity_set:
                distinct_props[subject].add(prop)
                link_counts[subject] += 1
                prop_links[prop] += 1
                triple_links += 1
            if obj in entity_set:
                distinct_props[obj].add(prop)
                link_counts[obj] += 1
                prop_links[prop] += 1
                triple_links += 1

    children_of: dict = defaultdict(list)
    for child, parent in index.pairs(RDFS.subClassOf):
        children_of[parent].append(child)
    restriction_links = 0
    restrictions_on_object_props = 0
    for restriction in index.subjects_of_type(OWL.Restriction):
        for prop in index.objects(restriction, OWL.onProperty):
            if prop not in object_properties:
                continue
            restrictions_on_object_props += 1
            for cls in children_of.get(restriction, ()):
                if cls in index.classes:
                    distinct_props[cls].add(prop)
                    link_counts[cls] += 1
                    prop_links[prop] += 1
                    restriction_links += 1

    connected = sum(1 for e in entities if distinct_props.get(e))
    coverage = connected / n
    diversity = sum(min(len(distinct_props.get(e, ())) / _MAX_DISTINCT_PROPS, 1.0) for e in entities) / n
    richness = sum(
        min(math.log(link_counts[e] + 1, _RICHNESS_LOG_BASE), 1.0) if link_counts.get(e) else 0.0
        for e in entities
    ) / n
    score = round(10.0 * (0.7 * coverage + 0.2 * diversity + 0.1 * richness), 2)
    details.update(
        coverage=coverage, diversity=diversity, richness=richness,
        connected_entities=connected, total_entities=n,
        object_properties_used=len(object_properties),
        object_properties_declared=len(declared),
        object_like_predicates_detected=len(object_like),
        links_from_triples=triple_links,
        links_from_restrictions=restriction_links,
        restrictions_on_object_properties=restrictions_on_object_props,
        owl_restrictions_total=len(index.subjects_of_type(OWL.Restriction)),
        top_properties_by_links=[{"property": str(p), "links": c} for p, c in prop_links.most_common(30)],
    )
    ent = {
        "connection_links": np.fromiter((link_counts.get(e, 0) for e in entities), dtype=np.int64, count=n),
        "connection_distinct_properties": np.fromiter(
            (len(distinct_props.get(e, ())) for e in entities), dtype=np.int64, count=n),
    }
    return score, details, ent


# ---------------------------------------------------------------- Flat

def _restriction_fillers(index: OntologyIndex, node) -> Iterator:
    for prop in RESTRICTION_FILLER_PROPS:
        yield from index.objects(node, prop)


def _expression_parents(index: OntologyIndex, expression) -> Iterator:
    yield from _restriction_fillers(index, expression)
    for list_head in index.objects(expression, OWL.intersectionOf):
        for item in index.rdf_list(list_head):
            if isinstance(item, BNode):
                yield from _restriction_fillers(index, item)
            else:
                yield item


def build_taxonomy(index: OntologyIndex) -> dict:
    classes = index.classes
    parent_to_children: dict = {}

    def add_edge(parent, child) -> None:
        if parent in classes and child in classes and parent != OWL.Thing:
            parent_to_children.setdefault(parent, set()).add(child)

    for child, parent in index.pairs(RDFS.subClassOf):
        if isinstance(parent, BNode):
            for implied in _expression_parents(index, parent):
                add_edge(implied, child)
        else:
            add_edge(parent, child)
    for cls in classes:
        for expression in index.objects(cls, OWL.equivalentClass):
            if isinstance(expression, BNode):
                for implied in _expression_parents(index, expression):
                    add_edge(implied, cls)
            else:
                add_edge(expression, cls)
    for parent, children in parent_to_children.items():
        children.discard(parent)
    return parent_to_children


def max_taxonomy_depth(parent_to_children: dict, deterministic: bool = False) -> tuple:
    """Longest downward path (in nodes), cycle-safe. Also returns back-edge count.

    WiseOwl visits classes in Python set order, which changes between processes,
    so an ontology with subclass cycles can get a different depth on each run.
    The batch worker fixes this by starting Python with a fixed hash seed, which
    keeps WiseOwl's exact algorithm (default deterministic=False). deterministic=True
    visits classes in sorted IRI order instead; it is stable but can differ from
    WiseOwl on ontologies with cycles, so it is off by default.
    """
    depth: dict = {}
    back_edges = [0]

    def depth_of(root) -> int:
        if root in depth:
            return depth[root]
        stack: list = [(root, False)]
        on_stack: set = set()
        while stack:
            node, children_done = stack.pop()
            if children_done:
                depth[node] = 1 + max((depth.get(c, 1) for c in parent_to_children.get(node, ())), default=0)
                on_stack.discard(node)
                continue
            if node in depth:
                continue
            if node in on_stack:
                depth[node] = 1
                back_edges[0] += 1
                continue
            on_stack.add(node)
            children = parent_to_children.get(node, ())
            if not children:
                depth[node] = 1
                on_stack.discard(node)
                continue
            stack.append((node, True))
            if deterministic:
                stack.extend((c, False) for c in sorted(children, key=str, reverse=True) if c not in depth)
            else:
                stack.extend((c, False) for c in children if c not in depth)
        return depth.get(root, 1)

    parents = sorted(parent_to_children, key=str) if deterministic else parent_to_children
    max_depth = max((depth_of(p) for p in parents), default=0)
    return max_depth, back_edges[0]


def compute_flat(index: OntologyIndex, entities: list, *, depth_target: int = 5,
                 branch_target: int = 3, deterministic: bool = False) -> tuple:
    """Flat = round((depth score + breadth score) / 2) (metrics/flat.py)."""
    taxonomy = build_taxonomy(index)
    max_depth, back_edges = max_taxonomy_depth(taxonomy, deterministic)
    avg_branch = sum(len(c) for c in taxonomy.values()) / len(taxonomy) if taxonomy else 0.0
    depth_score = min(max_depth / depth_target, 1.0) * 10
    breadth_score = min(avg_branch / branch_target, 1.0) * 10
    score = int(round((depth_score + breadth_score) / 2))
    children_counts = [len(c) for c in taxonomy.values()]
    all_children = set()
    for c in taxonomy.values():
        all_children |= c
    roots = [p for p in taxonomy if p not in all_children]
    details = dict(
        max_depth=max_depth, avg_branching=avg_branch, depth_score=depth_score,
        breadth_score=breadth_score, depth_target=depth_target, branch_target=branch_target,
        parents_with_children=len(taxonomy), taxonomy_edges=sum(children_counts),
        classes_in_taxonomy=len(set(taxonomy) | all_children),
        root_parents=len(roots), cycle_back_edges_cut=back_edges,
        children_per_parent_stats=_stats(children_counts),
        classes_total=len(index.classes),
    )
    n = len(entities)
    ent = {"taxonomy_children": np.fromiter((len(taxonomy.get(e, ())) for e in entities),
                                            dtype=np.int64, count=n)}
    return score, details, ent


# =============================================================================
# reporting.py (core part, copied wording)
# =============================================================================

CORE_LABELS = {"describe_score": "Describe", "define_score": "Define",
               "connection_score": "Connection", "flat_score": "Flat"}
_CORE_REASONS = {
    "describe_score": "the annotation properties and labels do not have a relatable description",
    "define_score": "the annotations lack semantic meaning",
    "connection_score": "the entities have loose connections",
    "flat_score": "the ontology lacks a clear hierarchical structure",
}


def suggestions(scores: dict, threshold: float = 4.0) -> list:
    out = []
    for key, label in CORE_LABELS.items():
        value = scores.get(key)
        if value is not None and value < threshold:
            out.append(f"{label} Score is low ({value:.2f}/10) because {_CORE_REASONS[key]}.")
    return out


def summary_banner(scores: dict) -> str | None:
    present = {k: v for k, v in scores.items() if v is not None}
    if not present:
        return None
    weakest_key, weakest_value = min(present.items(), key=lambda kv: kv[1])
    weakest = CORE_LABELS.get(weakest_key, weakest_key)
    avg = sum(present.values()) / len(present)
    if avg >= 8.0:
        return (f"This ontology scores well across all dimensions (average {avg:.2f}/10). "
                f"The main area to improve is {weakest} ({weakest_value:.2f}/10).")
    if avg >= 5.0:
        return (f"This ontology has mixed quality (average {avg:.2f}/10). "
                f"The weakest dimension is {weakest} ({weakest_value:.2f}/10).")
    return (f"This ontology scores low overall (average {avg:.2f}/10). "
            f"Several dimensions need work, starting with {weakest} ({weakest_value:.2f}/10).")


# =============================================================================
# Ontology-level statistics (extra, no effect on scores)
# =============================================================================

def ontology_stats(index: OntologyIndex, entities: list) -> dict:
    type_counts = {str(t): len(s) for t, s in index._by_type.items()}
    top_types = dict(sorted(type_counts.items(), key=lambda kv: -kv[1])[:30])
    pred_counts = {str(p): len(pairs) for p, pairs in index.predicate_groups()}
    top_preds = dict(sorted(pred_counts.items(), key=lambda kv: -kv[1])[:40])
    lang = Counter()
    for prop in (*LABEL_PROPS, *DEFINE_DEFINITION_PROPS):
        for _, o in index.pairs(prop):
            if isinstance(o, Literal):
                lang[o.language or "(none)"] += 1
    deprecated = 0
    for e in entities:
        for o in index.objects(e, OWL.deprecated):
            if str(o).strip().lower() in _TRUE_STRINGS:
                deprecated += 1
                break
    imports = sorted({str(o) for _, o in index.pairs(OWL.imports)})
    return {
        "triple_count": index.triple_count,
        "subjects": len(index._spo),
        "distinct_predicates": len(pred_counts),
        "classes": len(index.classes),
        "named_classes": len(index.named_classes),
        "individuals": len(index.individuals),
        "entities": len(entities),
        "annotation_properties": len(index.annotation_properties),
        "object_properties_declared": len(index.subjects_of_type(OWL.ObjectProperty)),
        "datatype_properties_declared": len(index.subjects_of_type(OWL.DatatypeProperty)),
        "owl_classes_typed": len(index.subjects_of_type(OWL.Class)),
        "rdfs_classes_typed": len(index.subjects_of_type(RDFS.Class)),
        "skos_concepts": len(index.subjects_of_type(SKOS.Concept)),
        "owl_restrictions": len(index.subjects_of_type(OWL.Restriction)),
        "subclassof_triples": len(index.pairs(RDFS.subClassOf)),
        "equivalentclass_triples": len(index.pairs(OWL.equivalentClass)),
        "deprecated_entities": deprecated,
        "owl_imports_count": len(imports),
        "owl_imports": imports[:200],
        "label_and_definition_languages": dict(lang.most_common(20)),
        "top_rdf_types": top_types,
        "top_predicates": top_preds,
    }


# =============================================================================
# One full evaluation
# =============================================================================

class StageTimer:
    def __init__(self, on_stage: Callable[[str], None] | None = None) -> None:
        self.marks: dict = {}
        self.on_stage = on_stage

    def run(self, name: str, fn: Callable[[], Any]) -> Any:
        if self.on_stage:
            self.on_stage(name)
        t0 = time.perf_counter()
        try:
            return fn()
        finally:
            self.marks[name] = round(time.perf_counter() - t0, 3)


def evaluate_file(path: str, encoder: ClsEncoder, *, min_tokens: int = 12, depth_target: int = 5,
                  branch_target: int = 3, suggestion_threshold: float = 4.0,
                  want_entities: bool = True, entity_rows_max: int | None = None,
                  fallback_encoder: Callable[[], ClsEncoder] | None = None,
                  on_stage: Callable[[str], None] | None = None,
                  extra_definition_props: Iterable | None = None,
                  use_owlready2: bool = True) -> dict:
    """Parse, index and score one ontology file with the four core metrics.

    Returns {"summary": flat dict, "details": nested dict, "entities": DataFrame | None}.
    Raises OntologyParseError when the file cannot be parsed.
    """
    timer = StageTimer(on_stage)
    t_total = time.perf_counter()

    sniffed = timer.run("sniff", lambda: sniff_format(path))
    if sniffed in UNSUPPORTED_FORMATS:
        kind = "not_an_ontology" if sniffed in NOT_AN_ONTOLOGY_FORMATS else "unsupported_format"
        raise OntologyParseError(f"{kind}:{sniffed}", [{"format": sniffed, "ok": False, "error": "skipped by sniffing"}])
    graph, fmt, attempts = timer.run("parse", lambda: parse_graph(path, sniffed, use_owlready2=use_owlready2))
    oid = timer.run("identity", lambda: ontology_id(graph, path))
    index = timer.run("index", lambda: OntologyIndex.from_graph(graph))
    del graph
    gc.collect()

    entities = list(index.entities)
    stats = timer.run("stats", lambda: ontology_stats(index, entities))

    describe, d_det, d_ent = timer.run("describe", lambda: compute_describe(index, entities))
    define, df_det, df_ent = timer.run(
        "define", lambda: compute_define(index, entities, encoder, min_tokens=min_tokens,
                                         fallback_encoder=fallback_encoder))
    bp_props, bp_sources = bp_definition_props(index, extra_definition_props)
    extra_props = frozenset(p for p in bp_props if p not in DEFINE_DEFINITION_PROPS)
    if bp_props == tuple(DEFINE_DEFINITION_PROPS):
        define_bp, dbp_det, dbp_ent = define, {"same_as_strict": True, "defined": df_det.get("defined")}, None
        bp_source = "same_as_strict"
    else:
        define_bp, dbp_det, dbp_ent = timer.run(
            "define_bp", lambda: compute_define(index, entities, encoder, min_tokens=min_tokens,
                                                fallback_encoder=fallback_encoder, props=bp_props,
                                                literal_only=extra_props))
        bp_source = "; ".join(f"{k} [{v}]" for k, v in bp_sources.items())
    dbp_det["extra_properties"] = bp_sources
    connection, c_det, c_ent = timer.run("connection", lambda: compute_connection(index, entities))
    flat, f_det, f_ent = timer.run(
        "flat", lambda: compute_flat(index, entities, depth_target=depth_target,
                                     branch_target=branch_target))

    scores = {"describe_score": describe, "define_score": define,
              "connection_score": connection, "flat_score": flat}
    core_average = sum(scores.values()) / 4.0
    core_average_bp = (describe + define_bp + connection + flat) / 4.0
    weakest = min(scores.items(), key=lambda kv: kv[1])[0]
    tips = suggestions(scores, suggestion_threshold)

    entities_df = None
    if want_entities and entities:
        def build_entities():
            import pandas as pd
            n = len(entities)
            limit = n if entity_rows_max is None else min(n, entity_rows_max)
            sel = slice(0, limit)
            cls = index.classes
            cols: dict = {
                "iri": [str(e) for e in entities[sel]],
                "kind": ["class" if e in cls else "individual" for e in entities[sel]],
                "label": [index.first_label(e)[:300] for e in entities[sel]],
                "label_source": [_label_source(index, e) for e in entities[sel]],
            }
            for src in (d_ent, df_ent, c_ent, f_ent):
                if src:
                    for k, v in src.items():
                        cols[k] = np.asarray(v)[sel]
            if dbp_ent:
                cols["has_definition_bp"] = np.asarray(dbp_ent["has_definition"])[sel]
                cols["define_bp_value"] = np.asarray(dbp_ent["define_value"])[sel]
            return pd.DataFrame(cols)
        entities_df = timer.run("entities_table", build_entities)

    timer.marks["total"] = round(time.perf_counter() - t_total, 3)
    summary = {
        "ontology_iri": oid.ontology_iri,
        "version_iri": oid.version_iri,
        "sniffed_format": sniffed,
        "parse_format": fmt,
        "parse_attempts": len(attempts),
        **{k: v for k, v in stats.items() if not isinstance(v, (dict, list))},
        "describe_score": describe,
        "define_score": define,
        "connection_score": connection,
        "flat_score": flat,
        "core_average": core_average,
        "core_average_2dp": round(core_average, 2),
        "weakest_metric": CORE_LABELS[weakest],
        "suggestions": "; ".join(tips) if tips else "No suggestions",
        "summary_banner": summary_banner(scores),
        "describe_described": d_det.get("described"),
        "describe_share": d_det.get("described_share"),
        "define_defined": df_det.get("defined"),
        "define_share": df_det.get("defined_share"),
        "define_cosine_mean": df_det.get("cosine_mean_used_for_zscore"),
        "define_cosine_std": df_det.get("cosine_std_used_for_zscore"),
        "define_match_mean": (df_det.get("match_stats") or {}).get("mean"),
        "define_adequacy_mean": (df_det.get("adequacy_stats") or {}).get("mean"),
        "define_definitions_truncated": df_det.get("definitions_truncated_at_bert_max_length"),
        "define_device": df_det.get("device"),
        "define_gpu_oom_fallback": df_det.get("gpu_oom_fallback"),
        "define_bp_score": define_bp,
        "define_bp_defined": dbp_det.get("defined"),
        "define_bp_source": bp_source,
        "core_average_bp": core_average_bp,
        "core_average_bp_2dp": round(core_average_bp, 2),
        "empty_ontology": len(entities) == 0,
        "connection_coverage": c_det.get("coverage"),
        "connection_diversity": c_det.get("diversity"),
        "connection_richness": c_det.get("richness"),
        "connection_connected": c_det.get("connected_entities"),
        "connection_object_properties": c_det.get("object_properties_used"),
        "connection_links_triples": c_det.get("links_from_triples"),
        "connection_links_restrictions": c_det.get("links_from_restrictions"),
        "flat_max_depth": f_det.get("max_depth"),
        "flat_avg_branching": f_det.get("avg_branching"),
        "flat_depth_score": f_det.get("depth_score"),
        "flat_breadth_score": f_det.get("breadth_score"),
        "flat_parents": f_det.get("parents_with_children"),
        "flat_edges": f_det.get("taxonomy_edges"),
        "flat_cycle_back_edges": f_det.get("cycle_back_edges_cut"),
        **{f"time_{k}": v for k, v in timer.marks.items()},
        "define_seconds_encode_labels": df_det.get("seconds_encode_labels"),
        "define_seconds_encode_definitions": df_det.get("seconds_encode_definitions_and_cosine"),
        "entity_rows_written": 0 if entities_df is None else len(entities_df),
        "wiseowl_source_version": WISEOWL_SOURCE_VERSION,
        "core4_version": CORE4_VERSION,
    }
    details = {
        "scores": scores,
        "core_average": core_average,
        "define_bp_score": define_bp,
        "core_average_bp": core_average_bp,
        "ontology_id": list(oid),
        "parse": {"sniffed_format": sniffed, "format_used": fmt, "attempts": attempts},
        "stats": stats,
        "describe": d_det,
        "define": df_det,
        "define_bp": dbp_det,
        "connection": c_det,
        "flat": f_det,
        "timings_seconds": timer.marks,
        "suggestions": tips,
        "summary_banner": summary_banner(scores),
    }
    return {"summary": summary, "details": details, "entities": entities_df}


def to_jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        f = float(obj)
        return None if math.isnan(f) else f
    if isinstance(obj, float) and math.isnan(obj):
        return None
    if isinstance(obj, np.ndarray):
        return to_jsonable(obj.tolist())
    return obj


def dump_json(obj: Any, path: str) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(to_jsonable(obj), fh, indent=2, ensure_ascii=False, default=str)
    os.replace(tmp, path)
