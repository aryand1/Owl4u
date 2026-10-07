"""Reproducible FAIR assessment for Owl4u ontology records.

The profile follows the 61 questions and the published 478-credit weighting of
O'FAIRe.  It is deliberately evaluated from the evidence Owl4u already keeps:
BioPortal metadata, BioPortal metrics, and the parsed ontology summary.  It
does not make live HTTP requests while exporting, so an unavailable identifier
resolver cannot make the same ontology's score change from one run to another.

This is an O'FAIRe-aligned Owl4u profile, not a byte-for-byte port of the
hosted O'FAIRe service.  Checks which require a live resolver, search engine,
or human judgement are reported as ``not_tested`` and receive no credit.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import urlparse

FAIR_SCORE_VERSION = "owl4-fair-1.0"
FAIR_MAX_CREDITS = 478.0

PRINCIPLES = {
    "F": ("Findable", 113.0),
    "A": ("Accessible", 113.0),
    "I": ("Interoperable", 109.0),
    "R": ("Reusable", 143.0),
}

CRITERIA = {
    "F1": ("Globally unique and persistent identifiers", 41.0),
    "F2": ("Rich ontology metadata", 27.0),
    "F3": ("Metadata explicitly identify the ontology", 21.0),
    "F4": ("Registered in searchable resources", 24.0),
    "A1": ("Retrievable by identifier through a standard protocol", 43.0),
    "A1.1": ("Open, free and universally implementable protocol", 28.0),
    "A1.2": ("Protocol supports authentication and authorization", 22.0),
    "A2": ("Metadata remain accessible over time", 20.0),
    "I1": ("Formal, shared knowledge-representation language", 44.0),
    "I2": ("Uses FAIR vocabularies", 32.0),
    "I3": ("Qualified references to other data", 33.0),
    "R1": ("Rich, accurate and relevant attributes", 32.0),
    "R1.1": ("Clear and accessible usage licence", 37.0),
    "R1.2": ("Detailed provenance", 38.0),
    "R1.3": ("Meets community standards", 36.0),
}

CRITERION_PRINCIPLE = {
    "F1": "F", "F2": "F", "F3": "F", "F4": "F",
    "A1": "A", "A1.1": "A", "A1.2": "A", "A2": "A",
    "I1": "I", "I2": "I", "I3": "I",
    "R1": "R", "R1.1": "R", "R1.2": "R", "R1.3": "R",
}


@dataclass(frozen=True)
class Check:
    id: str
    criterion: str
    label: str
    score: float
    maximum: float
    status: str
    evidence: str
    action: str | None = None

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "score": round(self.score, 2),
            "max_credits": self.maximum,
            "status": self.status,
            "evidence": self.evidence,
            "action": self.action,
        }


def _clean(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if hasattr(value, "item"):
        try:
            return _clean(value.item())
        except (TypeError, ValueError):
            pass
    return value


def _flatten(value: Any) -> list[str]:
    value = _clean(value)
    if value is None or value is False:
        return []
    if isinstance(value, dict):
        preferred = value.get("@id") or value.get("id") or value.get("acronym") or value.get("name")
        if preferred is not None:
            return _flatten(preferred)
        out: list[str] = []
        for item in value.values():
            out.extend(_flatten(item))
        return out
    if isinstance(value, (list, tuple, set)):
        out = []
        for item in value:
            out.extend(_flatten(item))
        return out
    text = str(value).strip()
    if not text or text.lower() in {"none", "null", "nan", "[]", "{}"}:
        return []
    return [text]


class Evidence:
    """Read equivalent fields from raw BioPortal JSON and exported rows."""

    def __init__(self, ontology: dict | None, submission: dict | None,
                 metrics: dict | None, summary: dict | None) -> None:
        self.ontology = ontology or {}
        self.submission = submission or {}
        self.metrics = metrics or {}
        self.summary = summary or {}

    def values(self, *names: str) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        sources = (self.submission, self.ontology, self.metrics, self.summary)
        for name in names:
            variants = [name]
            if ":" in name:
                variants.append(name.split(":", 1)[1])
            if name.startswith("bp_"):
                variants.append(name[3:])
            for source in sources:
                for key in variants:
                    if key not in source:
                        continue
                    for value in _flatten(source.get(key)):
                        marker = value.casefold()
                        if marker not in seen:
                            seen.add(marker)
                            out.append(value)
        return out

    def one(self, *names: str) -> str | None:
        values = self.values(*names)
        return values[0] if values else None

    def has(self, *names: str) -> bool:
        return bool(self.values(*names))

    def number(self, *names: str) -> float | None:
        for name in names:
            for source in (self.summary, self.metrics, self.submission, self.ontology):
                value = _clean(source.get(name))
                if value is None:
                    continue
                try:
                    return float(value)
                except (TypeError, ValueError):
                    continue
        return None


def _is_uri(value: str | None) -> bool:
    if not value or " " in value:
        return False
    parsed = urlparse(value)
    return bool(parsed.scheme and (parsed.netloc or parsed.scheme in {"urn", "doi"}))


def _is_http(value: str | None) -> bool:
    if not value:
        return False
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _is_doi(value: str | None) -> bool:
    if not value:
        return False
    return bool(re.search(r"(?:doi\.org/|^doi:|^10\.\d{4,9}/)", value, re.I))


def _score_for_count(count: int, maximum: float, target: int) -> float:
    return round(maximum * min(max(count, 0), target) / target, 4) if target else 0.0


def _score_for_ratio(value: float | None, maximum: float) -> float:
    if value is None:
        return 0.0
    return round(maximum * min(max(value, 0.0), 1.0), 4)


def _status(score: float, maximum: float, assessed: bool = True) -> str:
    if not assessed:
        return "not_tested"
    if score <= 0:
        return "fail"
    if score + 1e-9 >= maximum:
        return "pass"
    return "partial"


def _check(check_id: str, criterion: str, label: str, score: float, maximum: float,
           evidence: str, action: str | None = None, *, assessed: bool = True) -> Check:
    score = min(max(float(score), 0.0), float(maximum))
    return Check(check_id, criterion, label, score, float(maximum),
                 _status(score, maximum, assessed), evidence, action)


def _present_count(ev: Evidence, groups: Iterable[tuple[str, ...]]) -> int:
    return sum(1 for group in groups if ev.has(*group))


def assess(ontology: dict | None = None, submission: dict | None = None,
           metrics: dict | None = None, summary: dict | None = None) -> dict:
    """Return a FAIR-O-shaped, O'FAIRe-aligned assessment dictionary."""
    ev = Evidence(ontology, submission, metrics, summary)
    checks: list[Check] = []

    def add(check_id: str, criterion: str, label: str, score: float, maximum: float,
            evidence: str, action: str | None = None, *, assessed: bool = True) -> None:
        checks.append(_check(check_id, criterion, label, score, maximum, evidence,
                             action, assessed=assessed))

    ontology_uri = ev.one("URI", "ontology_iri", "owl:ontologyIRI", "bp_ontology_uri")
    version_uri = ev.one("versionIRI", "version_iri", "owl:versionIRI")
    identifiers = ev.values("identifier", "dct:identifier")
    valid_identifiers = [value for value in identifiers if _is_uri(value)]
    doi_identifiers = [value for value in identifiers if _is_doi(value)]
    catalog_status = ev.one("catalog_status")
    catalog_ok = (catalog_status in {"ok", "scored"}) if catalog_status else bool(ontology or submission)
    download_status = ev.one("download_status", "web_status")
    download_ok = (download_status in {"ok", "scored"}) if download_status else False

    # Findable -----------------------------------------------------------------
    f1q1 = 9 if _is_uri(ontology_uri) else 3 if ontology_uri else 0
    add("F1Q1", "F1", "Ontology has a globally unique identifier", f1q1, 9,
        f"Ontology identifier: {ontology_uri or 'not supplied'}",
        "Publish a stable ontology IRI.")
    f1q2 = 11 if doi_identifiers else 6 if valid_identifiers else 3 if identifiers else 0
    add("F1Q2", "F1", "An external persistent identifier is supplied", f1q2, 11,
        f"External identifiers: {', '.join(identifiers[:3]) or 'none'}",
        "Register a DOI or another external persistent identifier.")
    add("F1Q3", "F1", "Metadata record explicitly identifies the ontology",
        12 if catalog_ok else 0, 12,
        "BioPortal binds the metadata record to its ontology acronym and submission."
        if catalog_ok else "No usable BioPortal metadata record was found.",
        "Publish a metadata record explicitly linked to the ontology.")
    f1q4 = 4 if _is_uri(version_uri) else 2 if version_uri else 0
    add("F1Q4", "F1", "A version-specific identifier is supplied", f1q4, 9,
        f"Version IRI: {version_uri or 'not supplied'}; live resolution is not probed.",
        "Publish a resolvable owl:versionIRI for every release.")

    miro_must = [
        ("acronym",), ("name", "title"), ("description", "bp_description"),
        ("documentation",), ("homepage",), ("pullLocation", "bp_pull_location"),
        ("keywords",), ("coverage",), ("preferredNamespaceUri",), ("uriRegexPattern",),
        ("exampleIdentifier",), ("publisher",), ("hasDomain", "bp_categories"),
        ("repository",), ("bugDatabase",), ("mailingList",), ("reviews",),
    ]
    must_count = _present_count(ev, miro_must)
    add("F2Q1", "F2", "MIRO-required descriptive metadata are present",
        _score_for_count(must_count, 16, 8), 16,
        f"{must_count} relevant metadata fields found.",
        "Add title, description, homepage, documentation, namespace, publisher and subject metadata.")
    miro_should = [("classes", "bp_classes"), ("individuals", "bp_individuals"),
                   ("properties", "bp_properties"), ("numberOfAxioms", "triple_count"),
                   ("preferredNamespacePrefix",), ("metrics",)]
    should_count = _present_count(ev, miro_should)
    should_levels = [0, 1, 2, 3, 3, 4, 4]
    add("F2Q2", "F2", "Recommended structural metadata are present",
        should_levels[min(should_count, 6)], 4,
        f"{should_count} recommended metadata fields found.",
        "Publish counts, metrics and a preferred namespace prefix.")
    other_metadata = [("naturalLanguage", "bp_natural_language"), ("abstract",),
                      ("publication", "bp_publication"), ("notes",), ("released", "bp_released"),
                      ("modificationDate", "bp_modification_date"), ("creationDate", "bp_creation_date"),
                      ("isOfType", "bp_ontology_type"), ("classesWithNoDefinition",),
                      ("maxChildCount", "bp_max_child_count")]
    other_count = _present_count(ev, other_metadata)
    add("F2Q3", "F2", "Additional useful metadata are present", min(other_count, 7), 7,
        f"{other_count} additional metadata fields found.",
        "Add language, dates, publication, type and structural statistics.")

    add("F3Q1", "F3", "Metadata are maintained inside the ontology file", 0, 21,
        "The pipeline cannot prove that all catalog metadata are embedded in the file.",
        "Embed core metadata on the owl:Ontology resource.", assessed=False)
    add("F3Q2", "F3", "Metadata are available in an external record", 11 if catalog_ok else 0, 11,
        "BioPortal exposes a versioned metadata record." if catalog_ok else "No external metadata record found.",
        "Publish an external metadata record.")
    add("F3Q3", "F3", "External metadata and ontology are explicitly linked",
        10 if catalog_ok and ev.has("acronym", "name") else 0, 10,
        "BioPortal links the ontology record, acronym and submission."
        if catalog_ok else "An explicit bidirectional link was not established.",
        "Link the metadata record and ontology in both directions.")

    catalogs = ev.values("includedInDataCatalog")
    library_count = len({x for x in catalogs if any(k in x.lower() for k in ("fairsharing", "lov", "bartoc"))})
    add("F4Q1", "F4", "Registered in ontology libraries", _score_for_count(library_count, 6, 3), 6,
        f"{library_count} recognized library registrations found.",
        "Register the ontology in FAIRsharing, LOV or BARTOC.")
    repo_count = (1 if catalog_ok else 0) + len({x for x in catalogs if any(
        k in x.lower() for k in ("bioportal", "agroportal", "obofoundry", "ontobee", "ols", "ontohub"))})
    add("F4Q2", "F4", "Registered in open ontology repositories",
        _score_for_count(repo_count, 10, 5), 10,
        f"{repo_count} open repository registration(s), including BioPortal when available.",
        "Register the ontology in additional open ontology repositories.")
    add("F4Q3", "F4", "Repository records are indexed by web search engines", 0, 8,
        "Search-engine indexing is not probed during an offline scoring run.",
        "Expose indexable landing pages with stable metadata.", assessed=False)

    # Accessible ---------------------------------------------------------------
    a1q1 = 3 if _is_http(ontology_uri) else 0
    add("A1Q1", "A1", "Identifiers resolve to the ontology", a1q1, 6,
        f"HTTP identifier present: {'yes' if _is_http(ontology_uri) else 'no'}; live resolution is not probed.",
        "Use an HTTP(S) ontology IRI that resolves to the ontology.")
    add("A1Q2", "A1", "Metadata identifier resolves to the record", 7 if catalog_ok else 0, 7,
        "The BioPortal metadata endpoint is the source of this assessment."
        if catalog_ok else "No retrievable metadata record was available.",
        "Publish retrievable metadata at a stable URL.")
    formats = 0
    if catalog_ok:
        formats += 1  # JSON metadata
    if download_ok:
        formats += 1  # ontology download
    if ev.has("hasFormat", "isFormatOf"):
        formats += 1
    add("A1Q3", "A1", "Ontology and metadata support content negotiation",
        min(formats * 3, 24), 24,
        f"{formats} documented access representation(s); live Accept-header probing is not performed.",
        "Serve ontology and metadata in multiple negotiated RDF and human-readable formats.")
    endpoint = ev.one("endpoint", "sd:endpoint")
    add("A1Q4", "A1", "A second standard access protocol is available", 6 if _is_uri(endpoint) else 0, 6,
        f"SPARQL or alternate endpoint: {endpoint or 'not supplied'}",
        "Publish a SPARQL endpoint or another standard machine-access protocol.")

    add("A1.1Q1", "A1.1", "HTTP/URIs are used for identification and access", 20, 20,
        "BioPortal provides HTTP API and download URLs.")
    add("A1.1Q2", "A1.1", "Ontology access protocol is open and implementable", 4, 4,
        "HTTP(S) is an open, widely implemented protocol.")
    add("A1.1Q3", "A1.1", "Metadata access protocol is open and implementable", 4, 4,
        "BioPortal exposes metadata over HTTP(S).")
    add("A1.2Q1", "A1.2", "Ontology protocol supports authentication and authorization", 11, 11,
        "BioPortal's HTTP API supports API-key authorization.")
    add("A1.2Q2", "A1.2", "Metadata protocol supports authentication and authorization", 11, 11,
        "BioPortal's metadata API supports API-key authorization.")

    add("A2Q1", "A2", "Repository supports ontology versioning", 7, 7,
        "BioPortal stores versioned ontology submissions.")
    add("A2Q2", "A2", "Metadata are available for each version", 5 if ev.has("submission_id") else 0, 5,
        f"Submission identifier: {ev.one('submission_id') or 'not supplied'}",
        "Publish version-specific metadata.")
    add("A2Q3", "A2", "Metadata remain available after ontology retirement", 4, 4,
        "BioPortal retains ontology records and submission metadata.")
    status_fields = _present_count(ev, [("status", "bp_status"), ("deprecated", "bp_submission_status")])
    add("A2Q4", "A2", "Ontology status is clearly stated", _score_for_count(status_fields, 4, 2), 4,
        f"{status_fields} lifecycle/status field(s) found.",
        "Publish status and deprecation metadata.")

    # Interoperable -------------------------------------------------------------
    language = (ev.one("hasOntologyLanguage", "bp_language", "ontology_language") or "").upper()
    language_score = {"PDF": 5, "TXT": 7, "CSV": 9, "XML": 10, "OBO": 11,
                      "RDFS": 12, "SKOS": 15, "OWL": 18}.get(language, 0)
    add("I1Q1", "I1", "Uses a formal shared representation language", language_score, 18,
        f"Representation language: {language or 'not supplied'}",
        "Publish the ontology in OWL, SKOS or RDFS.")
    add("I1Q2", "I1", "Representation language is a W3C Recommendation",
        10 if language in {"XML", "RDFS", "SKOS", "OWL"} else 0, 10,
        f"{language or 'Unknown'} {'is' if language in {'XML', 'RDFS', 'SKOS', 'OWL'} else 'is not known as'} a W3C language.",
        "Use a W3C-standard representation language.")
    syntax = ev.one("hasOntologySyntax", "parse_format", "sniffed_format")
    add("I1Q3", "I1", "Ontology syntax is stated", 5 if syntax else 0, 5,
        f"Syntax/serialization: {syntax or 'not supplied'}",
        "Declare the ontology syntax or serialization.")
    formality = ev.one("hasFormalityLevel")
    add("I1Q4", "I1", "Formality level is stated", 5 if formality else 0, 5,
        f"Formality level: {formality or 'not supplied'}",
        "Declare the ontology's formality level.")
    other_formats = ev.values("hasFormat", "isFormatOf")
    add("I1Q5", "I1", "Other available formats are documented", 4 if other_formats else 0, 4,
        f"Related formats: {', '.join(other_formats[:3]) or 'none'}",
        "Document alternate serializations with dct:hasFormat or dct:isFormatOf.")

    imports = ev.number("owl_imports_count") or len(ev.values("useImports", "owl:imports"))
    add("I2Q1", "I2", "Imports other vocabularies", 5 if imports > 0 else 0, 5,
        f"Imported vocabularies: {int(imports)}",
        "Reuse established vocabularies with owl:imports where appropriate.")
    related = ev.values("ontologyRelatedTo", "dct:relation")
    add("I2Q2", "I2", "Reuses terms from other vocabularies", 5 if related else 0, 5,
        f"Declared related vocabularies: {len(related)}",
        "Declare reused external vocabularies and term sources.")
    add("I2Q3", "I2", "Reused terms include minimum source information", 0, 3,
        "Minimum-information compliance requires term-level inspection not retained by this run.",
        "Document source ontology, term IRI and retrieval/version information for reused terms.", assessed=False)
    alignments = ev.values("isAlignedTo", "voaf:hasEquivalencesWith")
    add("I2Q4", "I2", "Alignments to other vocabularies are declared", 5 if alignments else 0, 5,
        f"Declared alignments: {len(alignments)}",
        "Publish ontology alignments with unambiguous target IRIs.")
    add("I2Q5", "I2", "Alignments are represented and curated", 0, 7,
        "Alignment curation cannot be established from the retained metadata.",
        "Document alignment method, provenance and curator.", assessed=False)
    influences = ev.values("similarTo", "generalizes", "specializes", "translationOfWork", "viewOf")
    add("I2Q6", "I2", "Influential vocabularies are documented", 2 if influences else 0, 2,
        f"Influence/relation metadata values: {len(influences)}",
        "Document relationships to vocabularies that influenced this ontology.")
    metadata_vocab = ev.values("metadataVoc")
    standard_metadata = ev.number("ontology_metadata_standard_predicates") or 0
    add("I2Q7", "I2", "Uses standard FAIR metadata vocabularies",
        5 if metadata_vocab or standard_metadata > 0 else 0, 5,
        f"Declared metadata vocabularies: {len(metadata_vocab)}; standard ontology-metadata predicates: {int(standard_metadata)}.",
        "Describe ontology metadata with DC Terms, PROV-O, VANN, VOAF or MOD.")

    crossrefs = ev.number("qualified_cross_reference_triples") or 0
    add("I3Q1", "I3", "Provides qualified cross-references to external resources",
        20 if crossrefs > 0 else 0, 20,
        f"Qualified external IRI references found: {int(crossrefs)}",
        "Add qualified, machine-readable links to external resources.")
    add("I3Q2", "I3", "Cross-references use unambiguous entities",
        6 if crossrefs > 0 else 0, 6,
        "Cross-references are represented as IRIs." if crossrefs > 0 else "No qualified cross-reference evidence was found.",
        "Represent cross-references as resolvable IRIs with explicit predicates.")
    uri_metadata_groups = [("useImports",), ("hasPriorVersion",), ("isBackwardCompatibleWith",),
                           ("hasLicense",), ("hasFormat",), ("isFormatOf",), ("hasDomain",),
                           ("definitionProperty",), ("prefLabelProperty",), ("hierarchyProperty",)]
    uri_metadata_count = sum(1 for group in uri_metadata_groups if any(_is_uri(x) for x in ev.values(*group)))
    add("I3Q3", "I3", "Metadata values are encoded as valid URIs", min(uri_metadata_count, 7), 7,
        f"{uri_metadata_count} metadata property group(s) contain valid URIs.",
        "Encode licences, formats, subjects and ontology relations as IRIs.")

    # Reusable -----------------------------------------------------------------
    class_metadata = [("prefLabelProperty",), ("synonymProperty",), ("definitionProperty",),
                      ("authorProperty",), ("obsoleteProperty",)]
    class_meta_count = _present_count(ev, class_metadata)
    add("R1Q1", "R1", "Class-description properties are documented", min(class_meta_count, 5), 5,
        f"{class_meta_count} class-description property declarations found.",
        "Declare label, synonym, definition, author and obsolescence properties.")
    hierarchy_metadata = [("hierarchyProperty",), ("obsoleteParent",), ("maxDepth", "flat_max_depth")]
    hierarchy_count = _present_count(ev, hierarchy_metadata)
    add("R1Q2", "R1", "Hierarchy conventions are documented", min(hierarchy_count, 3), 3,
        f"{hierarchy_count} hierarchy metadata fields found.",
        "Document hierarchy and obsolete-parent properties.")
    entities = ev.number("entities", "entity_count") or 0
    labelled = ev.number("labelled_entities")
    label_ratio = (labelled / entities) if labelled is not None and entities else (
        ev.number("describe_share") if ev.number("describe_share") is not None else None)
    add("R1Q3", "R1", "Ontology objects have labels", _score_for_ratio(label_ratio, 7), 7,
        f"Label coverage: {label_ratio:.1%}" if label_ratio is not None else "Label coverage was not available.",
        "Add human-readable labels to every ontology object.", assessed=label_ratio is not None)
    definition_ratio = ev.number("define_share")
    add("R1Q4", "R1", "Ontology objects have textual definitions",
        _score_for_ratio(definition_ratio, 6), 6,
        f"Definition coverage: {definition_ratio:.1%}" if definition_ratio is not None else "Definition coverage was not available.",
        "Add textual definitions to ontology objects.", assessed=definition_ratio is not None)
    formalized = ev.number("formally_defined_entities")
    if formalized is None:
        formalized = (ev.number("owl_restrictions") or 0) + (ev.number("equivalentclass_triples") or 0)
    formal_ratio = min(formalized / entities, 1.0) if entities else None
    add("R1Q5", "R1", "Objects use restrictions or equivalent-class definitions",
        _score_for_ratio(formal_ratio, 6), 6,
        f"Formally defined object ratio: {formal_ratio:.1%}" if formal_ratio is not None else "Formal-definition coverage was not available.",
        "Add logical restrictions or equivalent-class axioms.", assessed=formal_ratio is not None)
    provenance_entities = ev.number("entities_with_provenance")
    provenance_ratio = min(provenance_entities / entities, 1.0) if provenance_entities is not None and entities else None
    add("R1Q6", "R1", "Ontology objects carry provenance annotations",
        _score_for_ratio(provenance_ratio, 5), 5,
        f"Object-level provenance coverage: {provenance_ratio:.1%}" if provenance_ratio is not None else "Object-level provenance coverage was not available.",
        "Annotate ontology objects with creator, source and modification provenance.", assessed=provenance_ratio is not None)

    licence = ev.one("hasLicense", "license", "dct:license")
    licence_score = 12 if _is_http(licence) else 4 if licence else 0
    add("R1.1Q1", "R1.1", "Licence is identified by an accessible URI", licence_score, 15,
        f"Licence: {licence or 'not supplied'}; live resolution/content negotiation is not probed.",
        "Publish a standard licence URI and make it machine-resolvable.")
    access = ev.one("viewingRestriction", "bp_viewing_restriction", "accessRights")
    permissions = ev.one("morePermissions")
    access_score = 7 if access and permissions else 3 if access else 0
    add("R1.1Q2", "R1.1", "Access rights and permissions are documented", access_score, 7,
        f"Access rights: {access or 'not supplied'}; additional permissions: {'yes' if permissions else 'no'}.",
        "State access rights and any additional permissions.")
    guideline_count = _present_count(ev, [("useGuidelines",), ("contact", "bp_contact_names")])
    add("R1.1Q3", "R1.1", "Usage guidelines and rights holder are documented",
        _score_for_count(guideline_count, 15, 2), 15,
        f"{guideline_count} of 2 usage/rightsholder fields found.",
        "Publish usage guidelines and a rights holder/contact.")

    actor_count = _present_count(ev, [("hasCreator", "creator"), ("curatedBy",),
                                     ("hasContributor", "contributor"), ("translator",),
                                     ("contact", "bp_contact_names")])
    add("R1.2Q1", "R1.2", "Development actors are documented", _score_for_count(actor_count, 8, 4), 8,
        f"{actor_count} actor-role fields found.",
        "Document creators, contributors, curators and translators with persistent identifiers.")
    provenance_count = _present_count(ev, [("source",), ("wasGeneratedBy",), ("wasInvalidatedBy",)])
    add("R1.2Q2", "R1.2", "General provenance is documented", _score_for_count(provenance_count, 6, 3), 6,
        f"{provenance_count} provenance fields found.",
        "Add dct:source and PROV-O generation/invalidation metadata.")
    accrual_count = _present_count(ev, [("accrualMethod",), ("accrualPeriodicity",), ("accrualPolicy",)])
    add("R1.2Q3", "R1.2", "Accrual methods and policy are documented", _score_for_count(accrual_count, 6, 3), 6,
        f"{accrual_count} accrual-policy fields found.",
        "Document the update method, schedule and policy.")
    version_count = _present_count(ev, [("version", "bp_version"), ("hasPriorVersion",), ("submission_id",)])
    add("R1.2Q4", "R1.2", "Ontology is clearly versioned", _score_for_count(version_count, 4, 3), 4,
        f"{version_count} versioning fields found.",
        "Publish version information and links to prior versions.")
    changes = ev.one("diffFilePath", "changes")
    add("R1.2Q5", "R1.2", "Latest changes are documented", 2 if changes else 0, 2,
        f"Change record: {changes or 'not supplied'}",
        "Publish a changelog or machine-readable change set.")
    method_count = _present_count(ev, [("usedOntologyEngineeringTool",),
                                      ("usedOntologyEngineeringMethodology",),
                                      ("conformsToKnowledgeRepresentationParadigm",)])
    add("R1.2Q6", "R1.2", "Engineering methods and tools are documented",
        _score_for_count(method_count, 6, 3), 6,
        f"{method_count} method/tool fields found.",
        "Document the ontology engineering method, tools and modelling paradigm.")
    rationale_count = _present_count(ev, [("designedForOntologyTask",), ("competencyQuestion",)])
    add("R1.2Q7", "R1.2", "Ontology rationale is documented", _score_for_count(rationale_count, 4, 2), 4,
        f"{rationale_count} rationale fields found.",
        "Publish intended tasks and competency questions.")
    funded = ev.one("fundedBy")
    add("R1.2Q8", "R1.2", "Funding organization is identified", 2 if funded else 0, 2,
        f"Funder: {funded or 'not supplied'}",
        "Identify the funding organization.")

    adoption_count = _present_count(ev, [("projects",), ("endorsedBy",), ("knownUsage",)])
    add("R1.3Q1", "R1.3", "Adoption and endorsement are documented",
        _score_for_count(adoption_count, 10, 2), 10,
        f"{adoption_count} adoption/endorsement fields found.",
        "Document projects using the ontology and organizations endorsing it.")
    groups = ev.values("group", "bp_groups")
    community_score = 20 if any("OBO-FOUNDRY" in g.upper() for g in groups) else 10 if groups else 0
    add("R1.3Q2", "R1.3", "Included in a recognized community set", community_score, 20,
        f"Community groups: {', '.join(groups[:4]) or 'none'}",
        "Join a relevant community ontology set and document conformance.")
    public = (access or "public").lower() not in {"private", "restricted"} and download_ok
    add("R1.3Q3", "R1.3", "Ontology is openly and freely available", 6 if public else 0, 6,
        f"Access appears {'public' if public else 'restricted or unavailable'}.",
        "Make the ontology publicly downloadable under an open licence.")

    return _aggregate(checks)


def _aggregate(checks: list[Check]) -> dict:
    criterion_results: dict[str, dict] = {}
    for criterion, (label, maximum) in CRITERIA.items():
        criterion_checks = [check for check in checks if check.criterion == criterion]
        raw_score = sum(check.score for check in criterion_checks)
        if criterion == "F3":
            # The embedded-metadata path (Q1) and external-record path (Q2+Q3)
            # are alternatives, so the published maximum remains 21 credits.
            raw_max = 21.0
        else:
            raw_max = sum(check.maximum for check in criterion_checks)
        credits = min(maximum, maximum * raw_score / raw_max) if raw_max else 0.0
        criterion_results[criterion] = {
            "label": label,
            "score": round(100 * credits / maximum, 1),
            "credits": round(credits, 2),
            "max_credits": maximum,
            "checks": [check.as_dict() for check in criterion_checks],
        }

    principles: dict[str, dict] = {}
    for code, (name, maximum) in PRINCIPLES.items():
        selected = {key: value for key, value in criterion_results.items()
                    if CRITERION_PRINCIPLE[key] == code}
        credits = sum(value["credits"] for value in selected.values())
        principles[code] = {
            "name": name,
            "score": round(100 * credits / maximum, 1),
            "credits": round(credits, 2),
            "max_credits": maximum,
            "subprinciples": selected,
        }

    credits = sum(value["credits"] for value in principles.values())
    recommendations = []
    for check in checks:
        lost = check.maximum - check.score
        if lost <= 0 or not check.action:
            continue
        recommendations.append({
            "id": check.id,
            "lost_credits": round(lost, 2),
            "action": check.action,
            "status": check.status,
        })
    recommendations.sort(key=lambda item: (-item["lost_credits"], item["id"]))

    return {
        "method": "Owl4u FAIR profile",
        "version": FAIR_SCORE_VERSION,
        "basis": "O'FAIRe 61-question, 478-credit grid",
        "score": round(100 * credits / FAIR_MAX_CREDITS, 1),
        "credits": round(credits, 2),
        "max_credits": FAIR_MAX_CREDITS,
        "principles": principles,
        "recommendations": recommendations[:8],
        "limitations": [
            "Live URI resolution, content negotiation and search-engine indexing are not probed.",
            "Checks needing human judgement are marked not_tested and receive no credit.",
        ],
    }


def assess_record(record: dict) -> dict:
    """Assess an exported result row (used by tests and one-time backfills)."""
    raw_ontology = record.get("_fair_ontology_json")
    raw_submission = record.get("_fair_submission_json")
    raw_metrics = record.get("_fair_metrics_json")

    def parse(value: Any) -> dict:
        if isinstance(value, dict):
            return value
        if isinstance(value, str) and value.strip():
            try:
                parsed = json.loads(value)
                return parsed if isinstance(parsed, dict) else {}
            except ValueError:
                return {}
        return {}

    # The flattened row is also a summary source, allowing old CSV exports to
    # be backfilled even when their original raw API payloads are unavailable.
    return assess(parse(raw_ontology), parse(raw_submission), parse(raw_metrics), record)


def compact_assessment(result: dict) -> dict:
    """Small web/D1 representation; full check evidence remains in CSV exports."""
    principles = {}
    for code, principle in result.get("principles", {}).items():
        principles[code] = {
            "name": principle.get("name"),
            "score": principle.get("score"),
            "credits": principle.get("credits"),
            "max_credits": principle.get("max_credits"),
            "subprinciples": {
                criterion: {
                    "label": value.get("label"),
                    "score": value.get("score"),
                    "credits": value.get("credits"),
                    "max_credits": value.get("max_credits"),
                }
                for criterion, value in principle.get("subprinciples", {}).items()
            },
        }
    return {
        key: result.get(key)
        for key in ("method", "version", "basis", "score", "credits", "max_credits")
    } | {
        "principles": principles,
        "recommendations": result.get("recommendations", []),
        "limitations": result.get("limitations", []),
    }


def compact_json(value: dict | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return value
    return json.dumps(compact_assessment(value), ensure_ascii=False, separators=(",", ":"))
