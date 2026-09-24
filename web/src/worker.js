// Owl4u search API (Cloudflare Worker).
//
// GET /api/search?q=<keyword>
//   1. Look up the keyword in the D1 cache (BioPortal matches only; scores are
//      always read fresh from ontology_scores).
//   2. On a miss, ask BioPortal /search with a time limit and group the matching
//      classes by ontology into a relevance score.
//   3. If BioPortal is slow or down, use a stale cache entry, or else the local
//      full-text index of ontology names and descriptions.
//   4. Join with the precomputed WiseOwl scores and return ranks.
// Everything else is served from ./public (the web page).

const PROVIDER = "bioportal";
const CACHE_HOURS = 24;
const METRICS = ["average_bp", "describe", "define_bp", "connection", "flat"];

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    try {
      if (url.pathname === "/api/search") return await search(url, env, ctx);
      if (url.pathname === "/api/health") return json({ ok: true, time: new Date().toISOString() });
    } catch (err) {
      return json({ error: "internal error", detail: String(err && err.message || err) }, 500);
    }
    return env.ASSETS.fetch(request);
  },
};

function json(body, status = 200, extraHeaders = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", ...extraHeaders },
  });
}

export function normalize(text) {
  return String(text || "").toLowerCase().normalize("NFKC").replace(/\s+/g, " ").trim();
}

async function search(url, env, ctx) {
  const t0 = Date.now();
  const q = normalize(url.searchParams.get("q"));
  if (!q) return json({ error: "Add a keyword, for example ?q=diabetes" }, 400);
  if (q.length > 200) return json({ error: "Keyword is too long (200 characters at most)" }, 400);

  const timing = {};
  let source = "cache";
  let note = null;
  let matches = null;
  let totalHits = null;

  const t1 = Date.now();
  const cached = await env.DB.prepare(
    "SELECT results_json, fetched_at FROM keyword_cache WHERE keyword = ? AND provider = ?"
  ).bind(q, PROVIDER).first();
  timing.cache_ms = Date.now() - t1;
  const fresh = cached && (Date.now() - Date.parse(cached.fetched_at)) / 36e5 < CACHE_HOURS;
  if (fresh) {
    const c = JSON.parse(cached.results_json);
    matches = c.matches;
    totalHits = c.total_hits;
  }

  if (!matches) {
    const t2 = Date.now();
    try {
      const r = await bioportalSearch(q, env);
      matches = r.matches;
      totalHits = r.totalHits;
      source = "bioportal";
      ctx.waitUntil(
        env.DB.prepare(
          "INSERT OR REPLACE INTO keyword_cache (keyword, provider, results_json, fetched_at) VALUES (?, ?, ?, ?)"
        ).bind(q, PROVIDER, JSON.stringify({ matches, total_hits: totalHits }), new Date().toISOString()).run()
      );
    } catch (err) {
      if (cached) {
        const c = JSON.parse(cached.results_json);
        matches = c.matches;
        totalHits = c.total_hits;
        source = "stale_cache";
        note = "BioPortal did not answer in time, so these matches are from an earlier search of the same keyword.";
      } else {
        matches = await fallbackSearch(q, env);
        source = "fallback";
        note = "BioPortal did not answer in time, so these are approximate matches on ontology names and descriptions.";
      }
      timing.bioportal_error = String(err && err.message || err).slice(0, 200);
    }
    timing.bioportal_ms = Date.now() - t2;
  }

  const t3 = Date.now();
  const rows = await loadScores(matches.map((m) => m.acronym), env);
  timing.scores_ms = Date.now() - t3;

  const results = matches.map((m) => {
    const r = rows.get(m.acronym);
    return r ? toResult(m, r) : pendingResult(m);
  });
  addRanks(results);

  return json(
    { keyword: q, source, note, total_hits: totalHits, count: results.length, took_ms: Date.now() - t0, timing, results },
    200,
    { "Cache-Control": "public, max-age=60" }
  );
}

async function bioportalSearch(q, env) {
  const base = (env.BIOPORTAL_API || "https://data.bioontology.org").replace(/\/$/, "");
  const timeoutMs = Number(env.BIOPORTAL_TIMEOUT_MS || 1300);
  const params = new URLSearchParams({
    q,
    pagesize: String(env.BIOPORTAL_PAGESIZE || 100),
    include: "prefLabel,synonym",
    display_context: "false",
    display_links: "true", // the ontology of each class is only in its links
  });
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort("timeout"), timeoutMs);
  let resp;
  try {
    resp = await fetch(`${base}/search?${params}`, {
      headers: { Authorization: `apikey token=${env.BIOPORTAL_API_KEY}`, Accept: "application/json" },
      signal: controller.signal,
    });
    if (!resp.ok) throw new Error(`BioPortal HTTP ${resp.status}`);
    const data = await resp.json();
    return { matches: groupByOntology(q, data.collection || []), totalHits: data.totalCount ?? null };
  } finally {
    clearTimeout(timer);
  }
}

// Relevance of an ontology = sum over its matching classes of 1 / (rank + 1),
// plus 1 if one of its classes has the keyword as its exact label or synonym,
// scaled so the best ontology for this keyword is 1.0.
export function groupByOntology(q, collection) {
  const byAcronym = new Map();
  collection.forEach((item, rank) => {
    const ontologyLink = item.links && item.links.ontology;
    if (!ontologyLink) return;
    const acronym = ontologyLink.replace(/\/$/, "").split("/").pop();
    let e = byAcronym.get(acronym);
    if (!e) {
      e = { acronym, hits: 0, rr: 0, exact: false, best_rank: rank + 1, example: firstString(item.prefLabel) };
      byAcronym.set(acronym, e);
    }
    e.hits += 1;
    e.rr += 1 / (rank + 1);
    const labels = [].concat(item.prefLabel || [], item.synonym || []).map(normalize);
    if (labels.includes(q)) e.exact = true;
  });
  const list = [...byAcronym.values()];
  const raw = (e) => e.rr + (e.exact ? 1 : 0);
  const max = Math.max(1e-9, ...list.map(raw));
  return list
    .map((e) => ({ acronym: e.acronym, relevance: Math.round((1000 * raw(e)) / max) / 1000, hits: e.hits,
                   exact: e.exact, best_rank: e.best_rank, example: e.example }))
    .sort((a, b) => b.relevance - a.relevance || a.best_rank - b.best_rank);
}

function firstString(v) {
  return Array.isArray(v) ? (v[0] ?? null) : (v ?? null);
}

async function fallbackSearch(q, env) {
  const terms = q.split(" ").map((t) => t.replace(/[^\p{L}\p{N}_-]/gu, "")).filter(Boolean);
  if (!terms.length) return [];
  const quoted = terms.map((t) => `"${t}"`);
  const run = async (match) => (await env.DB.prepare(
    "SELECT acronym, bm25(ontology_search) AS score FROM ontology_search " +
    "WHERE ontology_search MATCH ? AND provider = ? ORDER BY score LIMIT 40"
  ).bind(match, PROVIDER).all()).results;
  try {
    // all words first; only if that finds little, accept any word
    let results = await run(quoted.join(" AND "));
    if (results.length < 5 && terms.length > 1) {
      const seen = new Set(results.map((r) => r.acronym));
      results = results.concat((await run(quoted.join(" OR "))).filter((r) => !seen.has(r.acronym)));
    }
    if (!results.length) return [];
    // bm25 scores are negative and lower is better: the best match gets 1.0,
    // the others get their share of it (never 0 for a real match)
    const best = Math.min(...results.map((r) => r.score));
    return results.map((r, i) => ({
      acronym: r.acronym,
      relevance: best < 0 ? Math.max(0.01, Math.round((1000 * r.score) / best) / 1000) : 1,
      hits: null, exact: false, best_rank: i + 1, example: null,
    })).sort((a, b) => b.relevance - a.relevance);
  } catch (err) {
    return []; // full-text table not loaded
  }
}

async function loadScores(acronyms, env) {
  const out = new Map();
  const unique = [...new Set(acronyms)];
  const cols = "acronym, name, description, categories, ontology_language, submission_id, version, released, " +
    "bioportal_url, status, status_detail, describe_score, define_score, define_bp_score, define_bp_source, " +
    "connection_score, flat_score, core_average, core_average_bp, entity_count, triple_count, evaluated_at";
  for (let i = 0; i < unique.length; i += 90) { // D1 allows up to 100 bound values per query
    const chunk = unique.slice(i, i + 90);
    const sql = `SELECT ${cols} FROM ontology_scores WHERE provider = ? AND acronym IN (${chunk.map(() => "?").join(",")})`;
    const { results } = await env.DB.prepare(sql).bind(PROVIDER, ...chunk).all();
    for (const r of results) out.set(r.acronym, r);
  }
  return out;
}

function toResult(m, r) {
  const scored = r.status === "scored";
  return {
    acronym: r.acronym, name: r.name || r.acronym,
    relevance: m.relevance, hits: m.hits, exact: m.exact, example: m.example,
    status: r.status, status_detail: r.status_detail,
    scores: scored ? {
      average_bp: r.core_average_bp, average: r.core_average, describe: r.describe_score,
      define_bp: r.define_bp_score, define: r.define_score, connection: r.connection_score, flat: r.flat_score,
    } : null,
    define_bp_source: r.define_bp_source,
    description: r.description ? r.description.slice(0, 400) : null,
    categories: r.categories ? r.categories.split(";").filter(Boolean) : [],
    language: r.ontology_language, version: r.version, released: r.released,
    entities: r.entity_count, triples: r.triple_count, evaluated_at: r.evaluated_at,
    bioportal_url: r.bioportal_url || `https://bioportal.bioontology.org/ontologies/${encodeURIComponent(r.acronym)}`,
  };
}

function pendingResult(m) {
  return {
    acronym: m.acronym, name: m.acronym, relevance: m.relevance, hits: m.hits, exact: m.exact, example: m.example,
    status: "pending", status_detail: "Not scored yet (new since the last scoring run).", scores: null,
    categories: [], bioportal_url: `https://bioportal.bioontology.org/ontologies/${encodeURIComponent(m.acronym)}`,
  };
}

// Competition ranking ("1, 2, 2, 4") among scored results, for each metric.
export function addRanks(results) {
  const scored = results.filter((r) => r.scores);
  for (const metric of METRICS) {
    const values = scored.map((r) => r.scores[metric]).filter((v) => v !== null && v !== undefined);
    for (const r of scored) {
      const v = r.scores[metric];
      r.ranks = r.ranks || {};
      r.ranks[metric] = v === null || v === undefined ? null : 1 + values.filter((x) => x > v).length;
    }
  }
  for (const r of results) r.scored_count = scored.length;
}
