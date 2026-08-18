/**
 * Collect hh.ru search results from an authenticated browser session.
 *
 * hh blocks api.hh.ru from this machine and its robots.txt disallows crawling
 * the search pages, so collection runs inside the user's own logged-in tab at a
 * deliberate low pace. Every page already ships the data as JSON in
 * `<template id="HH-Lux-InitialState">` — the same shape the API would return —
 * so nothing is scraped out of rendered HTML.
 *
 * Paste into the browser console on https://hh.ru, then:
 *   await collectHH(PHRASES)         // -> array of normalised vacancies
 *
 * The result is kept in localStorage under `qa_export` so a closed pane or a
 * navigation does not lose a half-finished run.
 */

const PHRASES = [
  '"Senior QA Engineer"',
  '"QA Engineer"',
  '"Senior Manual QA"',
  '"Test Engineer"',
  '"Manual QA"',
  '"Tester manual"',
];

const SEARCH_PARAMS = {
  work_format: "REMOTE",
  // hh.ru's web search reads `search_period`; `period` is the API's name and is
  // silently ignored here, which once let month-old vacancies through.
  search_period: "7",
  order_by: "publication_time",
  items_on_page: "100",
};

function normalise(vacancy, phrase) {
  const pay = vacancy.compensation || {};
  const company = vacancy.company || {};
  return {
    vacancy_id: String(vacancy.vacancyId),
    title: vacancy.name,
    company: company.visibleName || company.name || null,
    // Carried so the queue can build a direct "respond" link: hh's response
    // form needs the employer id, and without it the UI can only open the
    // vacancy page and make the user click through.
    employer_id: company.id ?? null,
    area: (vacancy.area || {}).name || null,
    published_at: (vacancy.publicationTime || {}).$ || null,
    // Employers bump old vacancies, which refreshes publicationTime; keep the
    // creation time too so real age stays visible.
    created_at: vacancy.creationTime || null,
    url: (vacancy.links || {}).desktop || null,
    salary_from: pay.from ?? null,
    salary_to: pay.to ?? null,
    salary_currency: pay.currencyCode || null,
    no_salary: Boolean(pay.noCompensation),
    work_formats: (vacancy.workFormats || []).flatMap((w) => w.workFormatsElement || []),
    experience: vacancy.workExperience || null,
    has_test: Boolean(vacancy.userTestPresent),
    source_phrase: phrase,
  };
}

function readState(doc) {
  const template = doc.querySelector("template#HH-Lux-InitialState");
  if (!template) return null;
  const raw = template.content ? template.content.textContent : template.textContent;
  return JSON.parse(raw);
}

async function collectHH(phrases = PHRASES, { pauseMs = 700, maxPages = 10 } = {}) {
  const collected = [];
  for (const phrase of phrases) {
    for (let page = 0; page < maxPages; page += 1) {
      const params = new URLSearchParams({ ...SEARCH_PARAMS, text: phrase, page: String(page) });
      const response = await fetch(`https://hh.ru/search/vacancy?${params}`, {
        credentials: "include",
      });
      if (!response.ok) {
        console.warn(`${phrase} page ${page}: HTTP ${response.status} — stopping`);
        return finish(collected);
      }
      const state = readState(new DOMParser().parseFromString(await response.text(), "text/html"));
      const result = state && state.vacancySearchResult;
      if (!result) {
        console.warn(`${phrase} page ${page}: no InitialState — stopping`);
        return finish(collected);
      }
      collected.push(...result.vacancies.map((v) => normalise(v, phrase)));
      console.log(`${phrase} page ${page}: +${result.vacancies.length} of ${result.totalResults}`);
      if ((page + 1) * 100 >= result.totalResults) break;
      await new Promise((resolve) => setTimeout(resolve, pauseMs));
    }
    await new Promise((resolve) => setTimeout(resolve, pauseMs));
  }
  return finish(collected);
}

function finish(rows) {
  // One vacancy can match several phrases; keep every phrase that found it.
  const byId = new Map();
  for (const row of rows) {
    const existing = byId.get(row.vacancy_id);
    if (!existing) {
      byId.set(row.vacancy_id, { ...row, source_phrases: [row.source_phrase] });
    } else if (!existing.source_phrases.includes(row.source_phrase)) {
      existing.source_phrases.push(row.source_phrase);
    }
  }
  const unique = [...byId.values()].map(({ source_phrase, ...rest }) => rest);
  localStorage.setItem("qa_export", JSON.stringify(unique));
  console.log(`collected ${rows.length}, unique ${unique.length}`);
  return unique;
}
