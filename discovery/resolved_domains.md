# Resolved careers domains

Seeded M14 Part B1 from findings scattered across `companies/corporate.yaml`
comments (M11–M13). One row per employer M10 originally rejected as "no
sitemap found under the domain I checked" (guessed hostnames), plus every
employer resolved since. Recorded whether or not the domain yields postings,
so no future session repeats this work. "Added" means a `companies/*.yaml`
entry exists; "Rejected"/"Unresolved" employers are not in config.

| Employer | Resolved domain | Vendor | Status | Notes |
|---|---|---|---|---|
| Eramet | jobs.eramet.com | SuccessFactors RMK | Added | sitemap mode |
| Nexans | career.nexans.com | SuccessFactors RMK | Rejected | fully client-rendered, unresolved even with a real browser (M13) |
| Worldline | jobs.worldline.com | SuccessFactors RMK | Added | sitemap mode, lenient itemprop |
| Capgemini | jobs.capgemini.com | SuccessFactors RMK | Rejected | feed stuck at 1 item by every method tried (M13/M14); listing page reports real counts but no rows materialize in the DOM (M13 rendered.py attempt). M15 Part A re-check: its `/services/t/l` JS-driven listing path is disallowed by a plain `Disallow: /services/` rule with no overriding `Allow:` anywhere -- correctly disallowed under BOTH the old buggy parser and the M14-corrected one, not a false negative. Verdict unchanged, still excluded |
| Alstom | jobsearch.alstom.com | SuccessFactors RMK | Added | RSS mode |
| Sephora | jobs.sephora.com | SuccessFactors RMK | Added | RSS mode |
| Atos | jobs.atos.net | SuccessFactors RMK | Added | sitemap mode |
| Bel | jobs.groupe-bel.com | SuccessFactors RMK | Added | sitemap mode |
| Lactalis | jobs.lactalisexperience.fr | SuccessFactors RMK | Added | sitemap mode |
| Arkema | jobs.arkema.com | SuccessFactors RMK | Added | sitemap mode |
| Solvay | careers.solvay.com | SuccessFactors RMK | Added | sitemap mode |
| Servier | jobs.servier.com | SuccessFactors RMK | Added | sitemap mode |
| Kering | careers.kering.com (API `domain=kering.com`) | Eightfold AI | Added | M14 Part A |
| Danone | careers.danone.com | iCIMS behind Adobe Experience Manager | Rejected | known-rejected vendor (M9b); confirmed via `frapply-danone.icims.com` application link, not SuccessFactors despite an earlier guess |
| Geodis | workatgeodis.com is real but the WRONG entity (North America subsidiary, zero French content) | — | Rejected | real French board is `injob.geodis.com`, a proprietary ASP.NET portal, no sitemap, no JSON-LD |
| Manitou | careers.manitou-group.com | sitemap_jsonld | Added | |
| Scaleway | jobs.lever.co/scaleway | Lever | Added | |
| MAIF | recrutement.maif.fr | Talentsoft (custom vanity domain) | Added | proved custom-domain Talentsoft tenants are real, not just `*.talent-soft.com` |
| Sodebo | sodebo-career.talent-soft.com | Talentsoft | Added | |
| TotalEnergies | jobs.totalenergies.com | Avature (client-rendered) | Added (M15 Part B) | M13 wrongly concluded its own robots.txt disallowed everything; that was the M14-fixed parser bug. Re-checked live: `Allow: /$`, `Allow: /careers`, `Allow: /*/careers` all correctly win now. No JSON search endpoint exists (confirmed live: the search page's query param and a real Enter-key submission both return the same unfiltered "20 most recent" list). Added via `rendered.py`'s new sitemap mode (M15 Part B): discovers candidate URLs via the existing sitemap pipeline, renders each one, extracts title (`h2`) + body (`main`, a stable labeled Country/Area/Workplace location/.../Activities/Candidate Profile template). Also found and fixed live: the sitemap lists every posting once per locale mirror (6x inflation), fixed via a new trailing-numeric-ID dedup in sitemap_discovery.py. Live, location-narrowed: 20 unique real French candidates, 63.0s total |
| Saint-Gobain | joinus.saint-gobain.com | — | Rejected | Cloudflare challenge page (403, "Just a moment..."), excluded per the no-Cloudflare-evasion rule |
| Legrand | careers.legrand.com (real electrical-equipment company) | — | Unresolved | no sitemap found; `carrieres.groupe-legrand.fr` is a **different, same-named company** (marine/auto-parts, M11) — do not reuse that domain |
| Dassault Systemes | 3ds.com | — | Unresolved | only sitemap found is corporate-wide, not careers-scoped; not RMK/Eightfold (M14 sweep) |
| La Poste | laposterecrute.fr | — | Unresolved | Drupal, no declared sitemap; not RMK/Eightfold (M14 sweep, though this fetch failed outright this session — TLS/connectivity issue, worth a retry) |
| Hermes | talents.hermes.com | — | Unresolved | not RMK/Eightfold (M14 sweep) |
| Societe Generale | careers.societegenerale.com | — | Unresolved | not RMK/Eightfold (M14 sweep) |
| Credit Agricole SA | groupecreditagricole.jobs | — | Unresolved | distinct from Credit Agricole CIB (already added, Talentsoft) |
| Fnac Darty | recrutement.fnacdarty.com | Talentsoft (URL shape matches) | Unresolved | TLS certificate still expired on their end as of M14 Part B4 (re-checked, same `SEC_E_CERT_EXPIRED`) -- not our bug, retry in a future session |
| Elior | eliorgroup.com | — | Rejected | not RMK/Eightfold; `elior-coll.profils.org` seen in search results, not yet investigated as its own vendor |
| Groupe SEB | groupeseb-careers.com | — | Rejected | 404s on robots.txt/sitemap; not RMK/Eightfold |
| Covea | recrutement.covea.com | Cornerstone OnDemand (`covea.csod.com`) | Rejected | different vendor than RMK/Eightfold, not investigated further |
| Decathlon | recrutement.decathlon.fr / joinus.decathlon.fr | — | Unresolved | not RMK/Eightfold |
| L'Oreal | career.loreal.com | — | Unresolved | not RMK/Eightfold |
| Michelin | recrutement.michelin.fr | — | Unresolved | not RMK/Eightfold. Note: **Michelin is already `Added` via Workday** under `companies/corporate.yaml` (a different Michelin careers surface) — this domain is a second, unconfirmed candidate, not yet reconciled with the existing Workday entry |
| Amadeus | jobs.amadeus.com / amadeus.com | — | Unresolved | not RMK/Eightfold |
| Safran | safran-group.com | — | Unresolved | sitemap 403s; not RMK/Eightfold |
| Schneider Electric | careers.se.com | Jibe | Added (M16 Part C) | not RMK/Eightfold, but careers.se.com is itself a real Jibe tenant -- see the M16 Part C section below |
| Enedis | enedis.fr | — | Unresolved | not RMK/Eightfold |
| BPCE | recrutement.bpce.fr | — | Unresolved | not RMK/Eightfold |
| Natixis | recrutement.natixis.com | — | Unresolved | not RMK/Eightfold |
| Groupama | groupama-gan-recrute.com | — | Unresolved | not RMK/Eightfold |
| Bouygues Construction | careers.bouygues-construction.com | SuccessFactors RMK | Added (M14) | one entity within Bouygues Group; sitemap mode, real French Alternance/Stage/Apprenti content |
| Bouygues Telecom | bouyguestelecom-recrute.talent-soft.com | Talentsoft (URL shape, unverified) | Unresolved | domain no longer resolves (DNS failure, M14) -- was a stale web-search result, not re-found |
| Bouygues (group / Immobilier / Equans) | not resolved | — | Unresolved | no single group-wide domain; only Construction resolved so far |
| Vinci | emplois.vinci.com, jobs.vinci.com both checked | — | Unresolved | neither is RMK/Eightfold (M14); emplois.vinci.com failed to connect this session (worth a retry); subsidiaries (VINCI Autoroutes, VINCI Energies) have their own separate domains, not investigated |
| Suez | not resolved | — | Unresolved | no single clean careers domain found as of M13 |
| Intermarche | not resolved | — | Unresolved | no single clean careers domain found as of M13 |
| Accenture France | accenture.wd103.myworkdayjobs.com/AccentureCareers | Workday | Added (M16 Part C) | see the M16 Part C section below |
| Air France | airfrance-recrute.talent-soft.com | Talentsoft | Added (M14) | search_terms narrowed to 11 real "Stage" postings |
| Engie | jobs.engie.com | SuccessFactors RMK | Added (M14) | sitemap mode, hit the 150-page cap (164 real candidates), real French "ALTERNANCE" content via its GRDF subsidiary |
| Sodexo France | sodexo-recrute.talent-soft.com | Talentsoft | Added (M14 Part B1) | found in M12's search sweep, never actually added until now |
| OVHcloud | careers.ovhcloud.com (links to career5.successfactors.eu) | SuccessFactors Career Site Builder | Rejected | a different SAP SuccessFactors product than RMK, on a shared multi-tenant domain whose own robots.txt disallows the job search path entirely (`Disallow: /`, only `/login` allowed) -- see the M16 Part C section below |
| Doctolib | job-boards.greenhouse.io/doctolib (via connect.doctolib.com/fr/carrieres) | Greenhouse | Added (M16 Part C) | 129 real postings -- see the M16 Part C section below |
| Blablacar | jobs.lever.co/blablacar | Lever | Added (M16 Part A) | 12 real postings, all France/remote-plausible -- found via the systematic Greenhouse/Lever/Ashby/SmartRecruiters sweep, not a domain-resolution attempt |
| Mirakl | job-boards.greenhouse.io/mirakl | Greenhouse | Added (M14) | 14 real postings, confirmed live. A second board, "miraklfr", exists but is empty (0 jobs) -- do not use it |
| Contentsquare | jobs.lever.co/contentsquare | Lever | Added (M14) | 31 real postings, confirmed live |
| Auchan | www.auchan-recrute.fr | — | Unresolved | platform not identified as of M11 |

Vendors already confirmed NOT to match, so a future session doesn't re-probe
(checked against `discovery/probe_vendor.py`'s RMK + Eightfold signals, M14):
3ds.com, amadeus.com, career.loreal.com, careers.axa.com, careers.legrand.com,
careers.se.com, careers.societegenerale.com, eliorgroup.com, enedis.fr,
geodis.com, groupama-gan-recrute.com, groupeseb-careers.com,
injob.geodis.com, joinus.decathlon.fr, joinus.saint-gobain.com,
laposterecrute.fr, recrutement.bpce.fr, recrutement.covea.com,
recrutement.decathlon.fr, recrutement.michelin.fr, recrutement.natixis.com,
safran-group.com, talents.hermes.com, workatgeodis.com, kering.com (the
corporate site itself, as opposed to careers.kering.com).

## M16 Part A: self-serve ATS sweep (Greenhouse / Lever / Ashby / SmartRecruiters)

`discovery/probe_ats.py` swept 44 French data/AI scale-ups against all four
vendors' own multi-tenant board APIs by guessing the tenant slug (lowercase,
no-spaces, hyphenated, alphanumeric-only), since these four are self-serve
products a startup picks without a domain-level vendor signal to look for.
Already-configured companies (Aircall, Dust, Owkin) were skipped. Every hit
below with a nonzero posting count was live-verified through the real
adapter before being added to `companies/hot.yaml`; a company not listed
here returned no hit on any of the four under any slug variant tried.

| Company | Slug tried | Vendor hit | Postings | Status |
|---|---|---|---|---|
| Mistral AI | mistral (extra guess) | Lever | 0 | Not added -- real tenant, currently zero postings |
| Hugging Face | huggingface, hf | none | — | No hit on any of the four |
| Poolside | poolside | Ashby | 15 | Added |
| Alan | alan | Ashby | 113 | Added |
| Qonto | qonto | Lever AND Ashby | 43 / 43 | Added via Lever only -- see the companies/hot.yaml comment for why |
| Swile | swile | Lever | 24 | Added |
| Ledger | ledger | Lever AND Ashby | 1 / 8 | Added via Ashby only -- see the companies/hot.yaml comment for why |
| Back Market | backmarket | Ashby | 18 | Added |
| ManoMano | manomano | none | — | No hit on any of the four |
| Veepee | veepee | Lever | 73 | Added |
| BlaBlaCar | blablacar | Lever | 12 | Added |
| Deezer | deezer | none | — | No hit on any of the four |
| Payfit | payfit, thegreatpayfit | none | — | No hit on any of the four |
| Spendesk | spendesk | Ashby | 0 | Not added -- real tenant, currently zero postings |
| Pennylane | pennylane | Ashby | 163 | Added |
| Shift Technology | shifttechnology | Greenhouse | 23 | Added |
| Sorare | sorare | Ashby | 4 | Added |
| Younited | younited | Lever | 5 | Added |
| Lydia | lydia | none | — | No hit on any of the four |
| Kili Technology | kilitechnology, kili | none | — | No hit on any of the four |
| Hiflow | hiflow | none | — | No hit on any of the four |
| Descartes Underwriting | descartesunderwriting | none | — | No hit on any of the four |
| Pigment | pigment | Lever | 113 | Added |
| Ledger Investing | ledgerinvesting | none | — | No hit on any of the four |
| Malt | malt | Lever | 32 | Added |
| Alma | alma | none | — | No hit on any of the four |
| Agicap | agicap | Lever | 30 | Added |
| Libeo | libeo | none | — | No hit on any of the four |
| Silvr | silvr | Greenhouse | 1 | Added |
| Ramify | ramify | none | — | No hit on any of the four |
| Finary | finary | Ashby | 10 | Added |
| Indy | indy | none | — | No hit on any of the four |
| Luko | luko | none | — | No hit on any of the four (acquired/wound down; expected) |
| Meilisearch | meili (extra guess) | Lever | 1 | Added -- a standing open-application listing, not a specific role |
| Clever Cloud | clevercloud | none | — | No hit on any of the four |
| Platform.sh | platformsh | Greenhouse | 7 | Added |
| Nabla | nabla | Ashby | 15 | Added |
| Cardiologs | cardiologs | Lever | 1 | Added |
| Therapixel | therapixel | none | — | No hit on any of the four |
| Gleamer | gleamer | none | — | No hit on any of the four |
| Raidium | raidium | Lever | 4 | Added |
| Bioptimus | bioptimus | none | — | No hit on any of the four |
| Adaptive ML | adaptiveml | none | — | No hit on any of the four |
| Linagora | linagora | none | — | No hit on any of the four |

22 added. A "no hit" here only rules out these four self-serve vendors under
an obvious slug guess -- it says nothing about SuccessFactors, Workday,
Talentsoft, Eightfold, or a company's own sitemap, which is a different,
not-yet-attempted investigation for any of these names.

## M16 Part B: French research institutions and universities

This was in M10's original scope, never done until now. Method for each: find
the real recruitment site (never a directory/aggregator), check robots.txt
with the corrected parser, run the full adapter cascade (SuccessFactors both
modes, Eightfold, Talentsoft incl. vanity domains, Greenhouse/Lever/Ashby/
SmartRecruiters, Workday, sitemap_jsonld). Talentsoft turned out to be
surprisingly common -- 6 confirmed, live-verified real tenants, added to
`companies/institutions.yaml`.

| Institution | Recruitment site found | Vendor | Status | Notes |
|---|---|---|---|---|
| INRIA | jobs.inria.fr / recrutement.inria.fr | Custom (no known vendor) | Investigated, not added | See the dedicated M16 Part B2 writeup below -- flagged for approval, not built |
| CNRS | emploi.cnrs.fr, carrieres.cnrs.fr | Custom (no known vendor) | Unresolved | .aspx-shaped but no Talentsoft signature found; no JSON-LD on the listing page checked |
| CEA | cea-recrute.talent-soft.com (also reachable as emploi.cea.fr) | Talentsoft | Added | 779 total, 55 search_terms-matched incl. a real generative-AI apprenticeship |
| INRAE | jobs.inrae.fr | Custom (no known vendor) | Unresolved | No vendor marker, no JSON-LD found |
| ONERA | rejoindre.onera.fr | Custom (no known vendor) | Unresolved | No vendor marker, no JSON-LD found |
| IFPEN | ifpen-employee.talent-soft.com (also reachable as emploi.ifpen.fr) | Talentsoft | Added | 13 total, 1 search_terms-matched |
| IRT SystemX | not resolved | — | Unresolved | Every search result was an aggregator (Indeed/LinkedIn/JobTeaser/Glassdoor) -- no dedicated official recruitment domain found at all |
| Institut Pasteur | institutpasteur-recrute.talent-soft.com (also reachable as emploi.pasteur.fr) | Talentsoft | Added | 18 total, 1 search_terms-matched |
| IGN | ign.fr/nous-rejoindre/offres-emploi | Custom (no known vendor) | Unresolved | No vendor marker, no JSON-LD found |
| CEREMA | cerema.fr/fr/recrutement | Custom (no known vendor) | Unresolved | No vendor marker, no JSON-LD found |
| IRD | emploi-recrutement.ird.fr | Talentsoft | Added | 31 total, 1 search_terms-matched |
| BRGM | brgm-recrute.talent-soft.com | Talentsoft | Added | 21 total, 0 currently match search_terms (a genuine zero, confirmed via `?Keywords=` directly, not a narrowing bug). Found and fixed a real talentsoft.py parsing bug here -- see jobbot/sources/talentsoft.py's module comment on the "top offer" nested-icon-div case |
| IFREMER | ifremer.jobs.net | Unconfirmed ("jobs.net") | Unresolved | This host times out from this environment on every attempt -- not investigated further |
| CNES | cnes.fr/en/job-openings | Custom (no known vendor) | Unresolved | No vendor marker, no JSON-LD found; no individual job links found in the listing page's static HTML either (possibly JS-rendered -- not pursued, rule against adding rendered sources unless nothing else works) |
| ANSSI | ssi.gouv.fr/recrutement, talents.ssi.gouv.fr | Unconfirmed | Unresolved | talents.ssi.gouv.fr returned HTTP 503 when checked |
| Institut Curie | institutcurie-cand.talent-soft.com (also reachable as curie.fr/offres-emploi) | Talentsoft | Rejected | The Talentsoft tenant itself is a stub: its single "posting" is a placeholder ("Find our offers on our website -- link in the ad"), not real content. Real listings apparently live elsewhere; not chased further this session |
| INSERM | inserm.softy.pro | Softy (no adapter) | Unresolved | Real vendor identified (Softy, a French recruitment SaaS product), not one of ours; not built per the "no new adapters unless Part C proves one is needed" rule |
| Telecom Paris | telecom-paris.fr links to institutminestelecom.recruitee.com | Recruitee (no adapter) | Unresolved | The whole Institut Mines-Telecom group (Telecom Paris, IMT Atlantique, and likely others) centralizes through this one Recruitee tenant |
| CentraleSupelec | jobs.centralesupelec.fr | Custom (no known vendor) | Unresolved | No vendor marker, no JSON-LD found |
| Ecole Polytechnique | recrutement.polytechnique.edu | Custom (no known vendor) | Unresolved | No vendor marker, no JSON-LD found |
| ENS Paris-Saclay | ens-paris-saclay.fr/lecole/recrutement | Custom (no known vendor) | Unresolved | No vendor marker, no JSON-LD found |
| Sorbonne Universite | jobs.sorbonne-universite.fr | Talentsoft | Added | 61 total, 0 currently match search_terms (a genuine zero, confirmed via `?Keywords=` directly) |
| Universite Paris-Saclay | universite-paris-saclay.candidater.fr | "candidater.fr" (no adapter) | Unresolved | A distinct third-party product, not one of ours |
| Nantes Universite | univ-nantes.nous-recrutons.fr | "nous-recrutons.fr" (no adapter) | Unresolved | A distinct third-party product, not one of ours; the page does carry generic ld+json (not JobPosting-typed) |
| Centrale Nantes | jobs.ec-nantes.fr | Recruitee (no adapter) | Unresolved | Confirmed via CSP fingerprint match + explicit "recruitee" mention in the page body |
| IMT Atlantique | imt-atlantique.fr/fr/tags/recrutement | Likely Recruitee (same group as Telecom Paris) | Unresolved | Not independently confirmed to link to the same institutminestelecom.recruitee.com tenant this session |

6 added (CEA, IFPEN, Institut Pasteur, IRD, BRGM, Sorbonne Universite), all
via Talentsoft. Recruitee now has THREE separate sightings across this
session (Institut Mines-Telecom group, Centrale Nantes, and Part C's own
sweep may add more) -- worth a dedicated future investigation into whether
it's common enough among French institutions/employers to justify a real
adapter, but not built here per the "no new adapters unless proven needed"
rule.

### M16 Part B2: INRIA, investigated and flagged for approval

jobs.inria.fr / recrutement.inria.fr (both resolve to the same content):
robots.txt allows everything (`Disallow:` with no value). No declared
sitemap (`/sitemap.xml` 404s). No RSS/Atom feed link found. No JSON-LD
anywhere on either the listing or a real job detail page checked. No JSON
API found (no `/api/`, `fetch(`, `XMLHttpRequest`, `.json`, or `axios`
reference anywhere in the listing page's own source; a real og:title meta
tag is present on job detail pages, but nothing more structured).

What it IS: a plain, server-rendered HTML listing (no JavaScript execution
needed -- confirmed by fetching with plain httpx and finding real job
titles and links already in the response body), with real content on
every page checked: 99 unique job links found on the first listing page
alone (`/public/classic/en/offres`), a substantial volume, consistent with
INRIA's real, well-known focus on internships, PhDs, and postdocs in
computer science/AI/data -- exactly this project's domain.

This is the exact shape M16 Part B2 anticipated: a real, high-volume,
on-domain source that doesn't fit any existing adapter (no JSON-LD, no
sitemap, no RSS, no JSON API -- genuinely needs its own small HTML parser,
the same kind of investment talentsoft.py itself represented for a
different vendor). Per the explicit instruction, this is flagged for
approval rather than built unasked. Pagination scheme for the full board
wasn't determined (no `?page=` parameter found in the page 1 HTML) --
would need to be worked out as part of building the adapter, not before.

### M16 Part B3: Place de l'Emploi Public, flagged for a policy decision

Two candidate portal domains found:

- `choisirleservicepublic.gouv.fr`: reachable, robots.txt allows crawling
  (only `/wp-admin/` and a PDF-uploads path disallowed) and declares a
  sitemap index. That sitemap index is stale (every entry dated
  2023-04-14) and lists only static content/PDF/image sitemaps generated
  by a third-party SEO crawler tool (Screaming Frog), not a live per-
  posting job feed -- this looks like the institution's own informational/
  employer-branding site, not the actual job search engine.
- `place-emploi-public.gouv.fr`, the name most commonly cited as the real
  government-wide job portal (launched 2019, unifies State/territorial/
  hospital civil service postings, reportedly 100,000+ active postings):
  does not resolve from this environment at all (confirmed via both this
  session's own httpx client and an independent WebFetch call, both
  failing DNS resolution) -- not investigated further.

Per the explicit instruction, this is a policy question for the user, not
a technical one this session resolves alone: a government-wide publication
portal aggregating postings ACROSS many public employers is a different
category from a commercial job-board aggregator (Indeed/LinkedIn/WTTJ),
but it IS still an aggregation point rather than any single institution's
own first-party system, which is what CLAUDE.md rule 2/3 are built around.
Flagged, not added, pending that decision.

## M16 Part C: the unresolved corporates, done via the real navigation method

Method used for every company below, exactly as specified: load the
corporate homepage, find and follow the careers link, find and follow the
link to actual job listings, record the destination host, identify the
vendor from that destination (CSP header, robots.txt boilerplate, or URL
shape) in one request. Michelin skipped (already added via Workday).
BlaBlaCar already resolved in Part A (Lever) -- not re-investigated here.

| Company | Destination found | Vendor | Status | Notes |
|---|---|---|---|---|
| Safran | safran-group.com (403 on every path tried) | — | Rejected | confirmed still a 403 wall to our honest User-Agent, exactly as prior sessions found -- not retried beyond this one check, per the explicit rule |
| Schneider Electric | careers.se.com | Jibe | Added | see companies/corporate.yaml's own comment for the full finding |
| Dassault Systemes | 3ds.com/careers/jobs | Unidentified (likely client-rendered) | Unresolved | no CSP/body vendor marker found, no JSON-LD, no external ATS script host visible in the page source; not pursued into a full Playwright investigation this session |
| Societe Generale | careers.societegenerale.com/en/search | Oracle Taleo (socgen.taleo.net) | Unresolved | real, direct link found ("Create my profile" -> socgen.taleo.net/careersection/..."); no adapter exists for Taleo and only one tenant confirmed so far -- not built, per the "several Oracle tenants" threshold this session also applies to Taleo |
| Credit Agricole SA | groupecreditagricole.jobs -> casa-recrute.talent-soft.com | Talentsoft | Rejected | confirmed real Talentsoft tenant, but its own robots.txt is a bare `Disallow: /` with no overriding Allow at all -- a genuine wall, not a parser-bug false negative (there is nothing for a longest-match fix to override here). Distinct from Credit Agricole CIB's own Talentsoft tenant (already added), whose robots.txt carries no rules |
| Accenture France | accenture.com/fr-fr/careers/jobsearch -> accenture.wd103.myworkdayjobs.com/AccentureCareers | Workday | Added | see companies/corporate.yaml's own comment for the full finding, incl. a real workday.py bug found and fixed |
| L'Oreal | 3ds... career.loreal.com / careers.loreal.com | Avature | Investigated, not added | confirmed via CSP body markers (avature, avacdn) and robots.txt's own "Disallow: / then selective Allow: subpaths" shape (the exact pattern TotalEnergies and this session's own rendered.py sitemap mode already handle). A `/FR/sitemap_index.xml` exists, but "FR" turned out to be an internal Avature portal/division code, NOT a France filter -- the first candidate URL checked was a real posting for "Internship Program Colombia". Finding France-relevant candidates here needs more investigation than TotalEnergies did (a different portal segment, not yet identified) -- deferred rather than rushed, flagged for a future session |
| Decathlon | joinus.decathlon.fr/fr/annonces | Unidentified | Unresolved | no vendor marker found on the actual listings page; not pursued further this session |
| Doctolib | connect.doctolib.com/fr/carrieres -> job-boards.greenhouse.io/doctolib | Greenhouse | Added | see companies/corporate.yaml's own comment for the full finding |
| OVHcloud | careers.ovhcloud.com -> career5.successfactors.eu | SuccessFactors Career Site Builder | Rejected | a DIFFERENT SAP SuccessFactors product from RMK (a shared multi-tenant domain, `career_company=ovh` query param, not a per-tenant subdomain the way RMK works) -- confirmed NOT an RMK match via probe_vendor.py. Its own robots.txt is `Disallow: /` with only `Allow: /login` -- the job search page itself is walled off, a genuine robots.txt block, not a parser-bug false negative |
| Vinci | emplois.vinci.com (fails to connect, same as M14) / vinci-groupe.profils.org (real, reachable) | "Profils.org" (no adapter) | Unresolved | emplois.vinci.com's connectivity problem persists across two sessions now, apparently a real, ongoing issue on Vinci's end; the working alternate (profils.org) is a vendor not in our adapter roster, no JSON-LD found either |
| Bouygues (group) | talents.bouygues.com and groupebouygues-cand.talent-soft.com both found in search results | Talentsoft (unconfirmed) | Unresolved | BOTH candidate domains fail DNS resolution from this environment -- stale web-search results, same pattern as M14's own Bouygues Telecom finding. Bouygues Construction (already added) remains the only resolved entity in this group |
| Suez | not resolved | — | Unresolved | every search result was an aggregator; no dedicated official domain found, same as M13 |
| La Poste | laposterecrute.fr | Unconfirmed | Unresolved | TLS certificate hostname mismatch (`certificate is not valid for 'laposterecrute.fr'`) -- a real, ongoing misconfiguration on their end, not investigated further, same category of problem as Fnac Darty's expired certificate |
| Legrand | careers.legrand.com/en/sites/CX_1001 | Oracle Cloud HCM | Investigated, not added | confirmed via CSP/body markers (oraclecloud, oraclecloud.com) and the `/sites/CX_*` URL shape. This IS the real electrical-equipment Legrand (confirmed by domain -- careers.legrand.com, not the wrong-entity carrieres.groupe-legrand.fr from M11). No adapter exists for Oracle Cloud HCM and only one other tenant found this session (Hermes) -- below the "several tenants" threshold, not built |
| Amadeus | not resolved | — | Unresolved | every search result was an aggregator; no dedicated official domain found |
| Intermarche | carrieres-mousquetaires.com (Les Mousquetaires group) / careers.smartrecruiters.com/GroupementLesMousquetaires | SmartRecruiters (confirmed, empty) | Unresolved | the SmartRecruiters tenant is real (confirmed via the API directly) but returns `totalFound: 0` -- either inactive or superseded by their own primary domain, which shows no recognized vendor marker of its own |
| Hermes | talents.hermes.com/en/sites/CX | Oracle Cloud HCM | Confirmed, not added | matches Legrand's exact URL shape and CSP markers -- the second Oracle Cloud HCM tenant found this session, still below the "several tenants" threshold per the explicit instruction not to build for one tenant |

3 added this part (Schneider Electric via Jibe, Accenture France via Workday,
Doctolib via Greenhouse) -- none needed a new adapter; all three fit an
adapter this project already had. Two vendor families now have multiple
sightings without an adapter: Oracle Cloud HCM (Legrand, Hermes -- 2) and
Oracle Taleo (Societe Generale, and Schneider Electric's OWN secondary
profile-login link -- arguably 1.5). Neither meets "several" yet; worth
revisiting if a future session's sweep turns up a third.

## M17 Part A: subsidiary tenants of groups already resolved

French groups routinely run a separate ATS tenant per subsidiary; since a
group is treated as one company entry, subsidiary-only boards were never
checked. Swept via discovery/probe_ats.py (self-serve ATS slug guesses) for
every subsidiary, plus discovery/probe_vendor.py and quick manual checks
where a plausible domain could be found or guessed.

| Group | Subsidiary | Vendor found | Added? | Notes |
|---|---|---|---|---|
| Safran | Aircraft Engines, Electronics & Defense, Landing Systems, Nacelles, Seats, Cabin, Helicopter Engines | none | No | no hit on any of the four self-serve ATS under any obvious slug; not pursued further via manual navigation this session given the volume of the rest of this milestone |
| Bouygues | Immobilier, Telecom | none | No | no hit on the self-serve sweep |
| Bouygues | Colas | none (self-hosted Drupal) | No | colas.fr/fr/nous-rejoindre is a Drupal site with generic (non-JobPosting) ld+json and the stock Drupal robots.txt boilerplate -- real listings likely live elsewhere, not found this session |
| Bouygues | Equans | none | No | no hit on the self-serve sweep |
| Bouygues | TF1 | none | No | no hit on the self-serve sweep; groupe-tf1.fr/fr/carrieres 404s, correct URL not found |
| Vinci | Energies | none | No | vinci-energies.com/en/careers/ 404s; no hit on the self-serve sweep |
| Vinci | Autoroutes, Airports, Eurovia, Cegelec | none | No | no hit on the self-serve sweep |
| Vinci | Construction | SmartRecruiters (confirmed) | No | real tenant (slug `vinciconstruction`) but only 1 posting, in Boston, MA -- no French relevance, possibly a different regional entity under the same brand name |
| Vinci | Axians | SmartRecruiters (confirmed) | No | real tenant, 3 postings, all Spain/Germany/Netherlands -- no French content in the current board |
| Thales | Alenia Space, DIS, Services | none | No | no hit on the self-serve sweep |
| Renault | Ampere, Alpine, Dacia France, Renault Trucks | none | No | no hit on the self-serve sweep |
| Stellantis | Forvia | none | No | no hit on the self-serve sweep (Forvia is investigated again, more thoroughly, in Part B) |
| Stellantis | Opel France | none | No | no hit on the self-serve sweep |
| Stellantis | Free2Move | SmartRecruiters | **Added** | 9 real postings, all Paris/IDF |
| Engie | Solutions, GRTgaz, Storengy, Green | none | No | no hit on the self-serve sweep; likely reachable via the already-added Engie SuccessFactors RMK tenant instead, which M14 already confirmed surfaces GRDF-subsidiary content -- not independently re-checked |
| Engie | GRDF | — | No | grdf.fr returns 403 to our honest User-Agent -- a wall, not retried |
| Capgemini | Engineering, Invent, Sogeti, Frog | none | No | no hit on the self-serve sweep |
| Atos | Eviden | none | No | eviden.com/careers/ shows no recognized vendor marker |
| Atos | Bull | none | No | no hit on the self-serve sweep |
| Orange | Business | none | No | orange-business.com shows no recognized vendor marker |
| Orange | Cyberdefense | none | No | guessed URL 404s, not resolved |
| Orange | Sofrecom | none | No | no hit on the self-serve sweep |
| Credit Agricole | CA Technologies & Services | none | No | no hit on the self-serve sweep |
| Credit Agricole | Amundi | Talentsoft | **Added** | 23 real search_terms-matched candidates, all Paris |
| Credit Agricole | Indosuez | none | No | no hit on the self-serve sweep |
| Societe Generale | Ayvens, Sogeprom | none | No | no hit on the self-serve sweep |
| Societe Generale | Boursorama | none (self-hosted) | No | groupe.boursorama.fr's own careers page failed to connect this session; no recognized vendor found in the search results either |
| BPCE | Natixis | none | No | natixis.groupebpce.com shows no recognized vendor marker |
| BPCE | Banque Populaire, Caisse d'Epargne, Oney | none | No | no hit on the self-serve sweep; Oney's guessed domain failed to connect |

2 added (Free2Move, Amundi). Given the scale of this milestone's four
parts, subsidiaries with no self-serve ATS hit and no quickly-findable
domain were recorded as "no hit" rather than chased through a full manual
homepage-to-careers-to-listings navigation for each -- that deeper method
was reserved for Parts B/C/D's own named companies, which is what the
milestone's method section describes it for.

## M17 Part B: automotive, aerospace, transport, logistics

Full cascade (probe_ats.py, then probe_vendor.py, then manual navigation)
run against every company named.

| Company | Vendor found | Added? | Notes |
|---|---|---|---|
| Trigo | SmartRecruiters | **Added** | 158 total, 2 real French candidates |
| Segula Technologies | SmartRecruiters | **Added** | 1 real posting, Paris |
| Alten | SmartRecruiters | **Added** | 1123 total, 32 search_terms-matched, 23 France-relevant |
| Loft Orbital | Lever (confirmed real) | No | 57 postings, all US (San Francisco/Golden CO) -- zero French relevance |
| Valeo | Workday | **Added** | 310 real search_terms-matched candidates, all apprenticeships |
| Dassault Aviation | Talentsoft | **Added** | 3 real search_terms-matched candidates |
| Aeroports de Paris | Talentsoft | **Added** | 7 real search_terms-matched candidates |
| CMA CGM | SuccessFactors RMK | **Added** | confirmed via probe_vendor.py; 19 real search_terms-matched candidates |
| SNCF | sitemap_jsonld (real JobPosting JSON-LD) | **Added** | a dedicated sitemap-jobs.xml declared in robots.txt; 40 real search_terms-matched candidates |
| Geodis | Talentsoft | **Added** | M11 rejected injob.geodis.com as "proprietary ASP.NET, no sitemap, no JSON-LD" -- true but irrelevant, it's Talentsoft (needs neither); a genuine correction of a prior session's miss. 33 real French candidates |
| RATP | Workday | **Added** | found via a direct link on ratpgroup.com; 18 real French apprenticeships |
| Keolis | SuccessFactors RMK | **Added** | confirmed via probe_vendor.py; 17 real French candidates |
| Getlink | sitemap_jsonld (real JobPosting JSON-LD) | Investigated, not added | real content confirmed (9 real French candidates via a manual override), but job URLs are shaped "/o/{slug}", which matches no DEFAULT_JOB_PATH_MARKERS and no numeric segment -- CompanySource has no config-level `job_path_markers` override field yet, only the adapter constructor does. Onboarding this needs a small config-schema change, which is engineering, not "companies only" -- not done this session, flagged for a future one |
| ArianeGroup | Workday | **Added** | 13 real French candidates incl. Kourou and Toulouse apprenticeships |
| Forvia (Faurecia) | Eightfold (confirmed: `window._EF_GROUP_ID = "faurecia.com"`) | Rejected | the real API call returns HTTP 403 -- a wall to our honest User-Agent, not retried |
| MBDA | "Gestmax" (no adapter) | No | a vendor not in our roster |
| KNDS / Nexter | "Profils.org" (no adapter) | No | same unsupported vendor seen for Vinci and Elior |
| Gefco | Unconfirmed | No | gefco-careers.net fails to resolve from this environment |
| STEF | WordPress (no real job board found) | No | stef.jobs is a WordPress content site with no job-posting URLs found; a WP feed exists but carries no structured postings |
| ID Logistics | Talentsoft (URL shape confirmed) | No | career.id-logistics.com has an expired TLS certificate -- a real, ongoing misconfiguration on their end, same category as Fnac Darty and La Poste |
| Air France KLM | Talentsoft (airfrance-recrute.talent-soft.com) | Already added | same tenant as the existing "Air France" entry (M14) -- no separate action needed |
| Plastic Omnium, Hutchinson, Continental France, Bosch France, ZF France, Mahle France, Novares, Akka, Expleo, Altran | none | No | no hit on the self-serve sweep; not pursued via manual navigation given the scale of this milestone |
| Latecoere, Liebherr Aerospace, Daher, Figeac Aero, Lisi Aerospace, Arquus, Ariane Group (Exotrail/Loft Orbital/Unseenlabs/Preligens covered above or below) | none | No | no hit on the self-serve sweep; not pursued further |
| SNCF Connect, Transdev, Corsair, Transavia France, Bollore Logistics, DB Schenker France, XPO France | none | No | no hit on the self-serve sweep; not pursued via manual navigation given the scale of this milestone |

13 added. Two real, confirmed vendor walls found this part: Forvia's
Eightfold API (403) and ID Logistics' expired certificate. One genuine
correction of a prior session's mistake (Geodis, M11).

## M17 Part C: energy, state-owned companies, industry

Same cascade. These are companies, including state-owned ones, per the
user's own explicit scope decision for this milestone.

| Company | Vendor found | Added? | Notes |
|---|---|---|---|
| Socotec | SmartRecruiters | **Added** | 733 total, 25 real French candidates |
| Air Liquide | Workday | **Added** | 198 real French Alternance candidates |
| Framatome | Talentsoft | **Added** | 29 real French candidates |
| EDF | — | No | edf.fr returns 403 to our honest User-Agent -- a wall, not retried |
| Orano | Avature (confirmed: avature/avacdn markers) | Investigated, not added | rendered sources are excluded per the explicit rule this milestone; a JSON API was not investigated given the time this took for TotalEnergies/L'Oreal in prior milestones |
| Enedis | Talentsoft (confirmed: `enedisJobApi` config value naming enedis-recrute.talent-soft.com) | Investigated, not added | the tenant reports 126 real offers in its own page text, but uses a JS-driven template variant this project's Talentsoft parser doesn't recognize at all (no `ts-offer-*` markup anywhere in the server-rendered HTML) -- a genuine parsing gap, not a wall; needs further investigation in a future session |
| Bureau Veritas | SuccessFactors RMK (confirmed via probe_vendor.py) | Investigated, not added | real tenant, but the 15 real search_terms-matched postings found were all Italy/Malaysia -- zero French relevance in this sample |
| RTE | Unconfirmed | No | recrutement.rte-france.com timed out on every attempt |
| Bpifrance | Unconfirmed | No | talents.bpifrance.fr returns 403 to our honest User-Agent -- a wall, not retried |
| Caisse des Depots | Unconfirmed (self-hosted, generic ld+json) | No | caissedesdepots-recrute.fr shows no recognized vendor marker |
| Groupe SEB | "SelectMinds" (no adapter) | No | groupeseb.referrals.selectminds.com is real (ld+json present) but not JobPosting-typed on the landing page; not pursued further given time. groupeseb-careers.com (M14's own prior lead) remains 404 |
| Nexans | Avature (confirmed, M13) | No | unchanged from M13's own finding: fully client-rendered, unresolved even with a real browser |
| Vallourec, GRTgaz, Storengy, Technip Energies, Saipem France, Suez, Paprec, Derichebourg, La Francaise des Jeux, Banque de France, Docaposte, SNCF Reseau, Imprimerie Nationale, Sonepar, Fives, Haulotte, SGS France, Dekra France, Apave | none | No | no hit on the self-serve sweep; not pursued via manual navigation given the scale of this milestone's remaining part |

3 added. Two real, non-wall parsing gaps found this part worth a future
session's attention: Enedis' JS-driven Talentsoft template variant, and
Orano's Avature tenant (not investigated for a JSON API this session).

## M17 Part D: extending M16 Part A's tech/fintech sweep

Same discovery/probe_ats.py sweep (obvious slug variants), a longer
company list, per the explicit instruction. Every hit live-verified
through the real adapter before adding.

| Company | Slug tried | Vendor hit | Postings | Status |
|---|---|---|---|---|
| Ankorstore | ankorstore | Ashby | 2 | Added -- both Paris |
| Vestiaire Collective | vestiairecollective | Lever | 11 | Added -- 6 France-plausible |
| Leboncoin, Datadog France, Talend, Sinequa, Systran, Golem.ai, LightOn, Giskard, Hugging Face, Clevy, Zeliq, Sage France, Lucca, PayFit, Combo, Javelo, OpenClassrooms, Ironhack France, Wild Code School, Jellysmack, Deezer, Blade, Homa Games, Quantic Dream, Amplitude Studios, Asobo, Arkane Lyon, Criteo, Teads, Adot, Zenly, Alma, Lydia, Memo Bank, Green-Got, Yousign, Leocare, Luko, Descartes Underwriting, Zelros, Cardif | (various) | none | — | No hit on any of the four self-serve ATS under any obvious slug |
| Cegid | cegid | SmartRecruiters (confirmed real) | 1 | Not added -- the one posting is in Kingston, Ontario, Canada, zero French relevance |
| 360Learning | 360learning | Lever | 30 | Added -- 12 Paris-specific |
| Believe | believe | SmartRecruiters | 17 | Added -- 5 France-relevant incl. a real Paris "Engineering Manager" |
| Voodoo | voodoo | Ashby | 102 | Added -- 71 Paris-specific, a real substantial French mobile-gaming board |
| Gameloft | gameloft | SmartRecruiters | 43 | Added -- 4 Paris-specific (most of the global board is Ukraine/Spain studios) |
| Don't Nod | dontnod | SmartRecruiters | 3 | Added -- thin (1 real Paris posting) but real, a known French video-game studio |
| Equativ | equativ | Lever | 23 | Added -- 4 France-relevant incl. a real Paris Back-End Engineer role |
| Ogury | ogury | Lever AND SmartRecruiters | 21 / 1 | Added via Lever -- 5 France-relevant incl. two real Paris apprenticeships; SmartRecruiters' single posting makes Lever clearly the primary board |
| Sunday | sunday | Ashby (confirmed real) | 24 | Not added -- 23 of 24 postings are Redwood City, CA -- almost certainly a same-named but different (US) company, not a French one |
| Epsor | epsor | SmartRecruiters | 2 | Added -- both Paris |
| Wakam | wakam | Greenhouse (confirmed real) | 0 | Not added -- a real tenant, currently zero postings |

10 added. Two real tenants found and deliberately not added: Cegid (real,
zero French relevance) and Sunday (real, but almost certainly a
different, US-based company sharing the name).
