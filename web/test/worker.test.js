import assert from "node:assert/strict";
import test from "node:test";

import { addRanks, groupByOntology, normalize } from "../src/worker.js";

test("normalize produces stable cache keys", () => {
  assert.equal(normalize("  Kidney\n Disease  "), "kidney disease");
});

test("groupByOntology aggregates BioPortal class matches", () => {
  const collection = [
    { prefLabel: "Kidney", synonym: [], links: { ontology: "https://example.test/ontologies/A" } },
    { prefLabel: "Renal organ", synonym: ["Kidney"], links: { ontology: "https://example.test/ontologies/A" } },
    { prefLabel: "Kidney disease", synonym: [], links: { ontology: "https://example.test/ontologies/B" } },
  ];
  const grouped = groupByOntology("kidney", collection);
  assert.equal(grouped[0].acronym, "A");
  assert.equal(grouped[0].hits, 2);
  assert.equal(grouped[0].exact, true);
});

test("addRanks ranks WiseOwl and FAIR independently", () => {
  const results = [
    { scores: { average_bp: 8, describe: 8, define_bp: 8, connection: 8, flat: 8 }, fair: { score: 45 } },
    { scores: { average_bp: 6, describe: 6, define_bp: 6, connection: 6, flat: 6 }, fair: { score: 75 } },
    { scores: null, fair: { score: 75 } },
  ];
  addRanks(results);
  assert.equal(results[0].ranks.average_bp, 1);
  assert.equal(results[1].ranks.average_bp, 2);
  assert.equal(results[0].fair.rank, 3);
  assert.equal(results[1].fair.rank, 1);
  assert.equal(results[2].fair.rank, 1);
  assert.equal(results[2].fair_count, 3);
});
