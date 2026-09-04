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
| Schneider Electric | careers.se.com | — | Unresolved | not RMK/Eightfold |
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
| Accenture France | not resolved | — | Unresolved | jobs.accenture.com is the only lead so far, not yet checked (global site, likely not France-specific) |
| Air France | airfrance-recrute.talent-soft.com | Talentsoft | Added (M14) | search_terms narrowed to 11 real "Stage" postings |
| Engie | jobs.engie.com | SuccessFactors RMK | Added (M14) | sitemap mode, hit the 150-page cap (164 real candidates), real French "ALTERNANCE" content via its GRDF subsidiary |
| Sodexo France | sodexo-recrute.talent-soft.com | Talentsoft | Added (M14 Part B1) | found in M12's search sweep, never actually added until now |
| OVHcloud | careers.ovhcloud.com | — | Unresolved | ATS not identified, not yet checked |
| Doctolib | not resolved | — | Unresolved | not yet attempted |
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
