# Privacy-first analytics

Design record for [i2mint/enlace#55](https://github.com/i2mint/enlace/issues/55). Module: `enlace/analytics.py`.

## In one paragraph

An app turns it on in its own `app.toml` with `[analytics] mode = "privacy"`. The enlace server that already serves the app counts that app's HTML page views as it serves them: no JavaScript, no beacon, no request to any other origin, nothing written to the visitor's device, and the page bytes are unchanged. It keeps only daily aggregates per app, with each dimension (path, referrer domain, primary language, device class) stored as its own count and never crossed with another. No IP address is read and no identifier is stored. The owner reads the counts with `enlace analytics` on the serving host.

## Why server-side, and not a self-hosted Plausible / Umami / GoatCounter

The issue asked for this to be weighed first. All three are good tools, and each would be a worse seam here:

- **They need a script in the page.** Plausible, Umami and GoatCounter all count through a JS snippet, so the page would have to change. Either the app edits its own HTML, which breaks "apps should not need to change", or enlace injects the snippet, which makes enlace a tracker-injector. A children's site promising that nothing runs besides its own code would then be running someone else's JS, even if self-hosted. Counting server-side avoids the question: there is nothing to block and nothing to explain.
- **They are another service to run.** Plausible needs PostgreSQL + ClickHouse. Umami needs PostgreSQL or MySQL. GoatCounter is the light one: a single Go binary with SQLite. Each is still another process, port, backup and upgrade on a host where disk is the binding constraint (see [i2mint/enlace#38](https://github.com/i2mint/enlace/issues/38)). enlace already sees every request, so counting costs one pure-ASGI middleware and a few small JSON files.
- **The seam stays open.** If a real analytics product is wanted later, the collector already produces `(app, path, referrer, language, device)` events at one place, `PageViewCounter.record_view`. Forwarding those to GoatCounter's HTTP API, or writing to a different store (`build_backend(..., analytics_store=...)` takes any `MutableMapping`), is an addition at an existing boundary, not a rewrite.

## What counts as a page view

A `GET` that the browser marks as a document navigation (`Sec-Fetch-Dest: document`, or `Accept: text/html` for older browsers), answered with a `200` HTML body or a `304` revalidation. Assets, `fetch()`/XHR calls, `HEAD`, prefetches/prerenders, redirects and errors are not page views.

For an SPA, a navigation to any unknown path is a view, because the SPA fallback serves `index.html`: the visitor did see a page. Client-side route changes after the first load are not seen by the server and are not counted. That is the price of having no script; a same-origin `navigator.sendBeacon` endpoint could add them later without changing the storage.

Attribution goes to the longest matching app mount (`/{name}/` or the app's route prefix). Any other path goes to the landing app, if the landing app opted in. Platform paths (`/_…`, `/auth/…`) are never attributed. A page of an app that did not opt in never falls through to an app that did.

Bots (by User-Agent, including an empty one and `HeadlessChrome`) are counted only as `bot_hits`, never in the dimensions.

## What is stored, and where

Key `{app}/{YYYY-MM-DD}/{writer}` → `{"pageviews", "bot_hits", "paths", "referrers", "languages", "devices"}`.

- **One writer per worker process.** Production runs several workers, so each writes only its own record for the day, overwriting its own cumulative totals. No worker ever read-modify-writes a shared record. Readers sum the writers.
- **Buffered.** A worker flushes at most every `flush_interval_seconds` (default 10), and always at server shutdown. A crash can lose up to that interval of counts.
- **Bounded.** Each dimension keeps at most `max_values_per_dimension` distinct values per day and writer; the rest go under `(other)`. This caps the damage from a flood of junk URLs against an SPA fallback. Paths are also truncated to 200 characters.
- **Retention.** Records older than `retention_days` (default 395, hard cap 25 months) are purged by each worker once a day.
- **Location.** The default is `$XDG_DATA_HOME/enlace/analytics`, i.e. `~/.local/share/enlace/analytics`: outside any app directory, as the "an app dir contains only code + build output" rule requires. Set it with `[analytics] store_path` in `platform.toml`.
- **Day boundary.** `[analytics] timezone` (default `UTC`) sets whose midnight starts a new day.

## Deliberately not counted: unique visitors

The Plausible/GoatCounter method is a hash of IP + User-Agent with a daily salt that is never stored. With several worker processes, the salt has to be shared for the count to mean anything. That is only possible by storing it somewhere for the day, or by deriving it from a long-lived secret. Deriving it would let anyone holding the secret recompute past salts, which defeats the rotation. The version without sharing (one in-memory salt per worker) over-counts by up to the number of workers. Neither was good enough to ship under a "privacy-first" name, so v1 counts page views only. If uniques are wanted, the decision is where the day's salt may live (a root-only file deleted at midnight is the obvious candidate). That needs an owner's call, not an implementation detail.

## Opting out

- `DNT: 1` and `Sec-GPC: 1` are honoured (`honor_opt_out_signals`, on by default).
- `GET /_analytics/opt-out` is a small no-script page, in English or French depending on the browser, meant to be linked from the privacy notice. Choosing "don't count my visits" sets one first-party cookie, `enlace_analytics_opt_out=1` (13 months, `HttpOnly`, `SameSite=Lax`). That cookie stores the visitor's refusal, and the CNIL exempts such cookies from consent. It is set only when the visitor asks. Choosing to be counted again deletes it.

## CNIL audience-measurement exemption: checklist

Researched 2026-09-25 against the texts in the references. This maps the design onto the conditions. It is not legal advice, and the operator of each site should confirm it.

**Is it in scope at all?** No CNIL text says outright whether purely server-side analytics, reading and storing nothing on the device, falls under Art. 82 of the Loi Informatique et Libertés (ePrivacy Art. 5(3)). The EDPB's Guidelines 2/2023 read "gaining access" broadly. They say that relying on protocol headers such as `Accept` or the User-Agent "can lead to the application of Article 5(3)" (§§42–43), and they treat an IP that originates from the terminal the same way (§55) [1]. The safe reading is to meet the CNIL exemption conditions in full, which makes the scope question moot. GDPR applies either way; the legal basis is legitimate interest [2 §52; 3].

| Condition (CNIL guidelines 2020-091 §51 [2], recommendation §5 [4], self-assessment tool [5]) | Status |
|---|---|
| Purpose limited to measuring the site's own audience, for the publisher alone | Met: first-party, no marketing features |
| Produces anonymous statistics only; no combination of criteria may isolate one user | Met: dimensions stored as separate marginal counts, never crossed; no identifiers |
| No tracking across sites or apps; no cross-site identifier | Met: no identifier at all |
| Data minimised; headers reduced | Met: device class and primary language subtag only; referrer reduced to its host |
| No campaign/CRM identifiers imported from URLs | Met: query string and fragment dropped from the stored path |
| IP used at most for city-level location, then truncated | Met: IP never read or stored |
| No session replay, no following one user's navigation | Met |
| Tracker lifetime ≤ 13 months | Met (not applicable): nothing placed on the device; the opt-out cookie lasts 13 months |
| Data retention ≤ 25 months | Met: `retention_days` validated ≤ 25 months, default 13 |
| No transfer to third parties; processor terms if a vendor is used | Met: no vendor. The hosting provider's own Art. 28 terms are the operator's |
| Users informed, and able to object | Met by enlace + operator: DNT/GPC honoured and `/_analytics/opt-out` exists; the operator must mention the processing in the privacy notice and link that page |
| No cross-referencing with other processing | Operator: do not join analytics with account/auth data or with access logs |

**What the site operator still has to do.** This is required even when the exemption applies [4 §5; 7]:

- Add a privacy-notice section: purpose (audience statistics), legal basis (legitimate interest), what is derived (path, referrer domain, language, device class; no IP stored), retention period, controller contact, the right to object and how to exercise it (link `/_analytics/opt-out`; DNT/GPC), and the right to complain to the CNIL.
- Add the processing to the Art. 30 record.
- Govern the other logs separately. The reverse proxy's and Uvicorn's access logs record raw IPs and full URLs. They are a separate processing that this feature neither creates nor controls.

**Sites for children.** The CNIL has no rule specific to audience measurement on children's sites. General rules apply [3; 8; 9]:

- The legitimate-interest balancing test weighs "in particular where the data subject is a child" (GDPR Art. 6(1)(f)) and should be written down.
- Information addressed to children must be clear and plain (Art. 12(1)). The CNIL recommends short sentences and icons.
- No profiling. This design does none.

**Platforms that also run `enlace_auth`.** Its CSRF middleware currently sets an `enlace_csrf` cookie on every safe request that arrives without one, including public pages. A site that promises "no cookie" has to be served without that plugin, or wait for the CSRF cookie to be scoped to where it is needed. Tracked in [i2mint/enlace_auth#31](https://github.com/i2mint/enlace_auth/issues/31).

**Pending law, not verified.** The Commission's Digital Omnibus proposal (2025/0360(COD)) would add a GDPR Art. 88a exempting aggregated first-party audience measurement from consent, and an Art. 88b making browser-level signals binding [10]. Whether it has been adopted was not confirmed as of 2026-09-25.

## REFERENCES

1. EDPB. [Guidelines 2/2023 on Technical Scope of Art. 5(3) of ePrivacy Directive, v2.0](https://www.edpb.europa.eu/system/files/documents/2024-10/edpb_guidelines_202302_technical_scope_art_53_eprivacydirective_v2_en_0.pdf). Adopted 7 Oct 2024; §§32–34, 42–43, 54–56.
2. CNIL. [Délibération n° 2020-091 du 17 septembre 2020 (lignes directrices cookies et autres traceurs)](https://www.cnil.fr/sites/default/files/atoms/files/lignes_directrices_de_la_cnil_sur_les_cookies_et_autres_traceurs.pdf). Art. 5, §§50–52.
3. [Regulation (EU) 2016/679 (GDPR)](https://eur-lex.europa.eu/eli/reg/2016/679/oj). Arts. 6(1)(f), 12(1), 13, 21, 30.
4. CNIL. [Recommandation « cookies et autres traceurs », version consolidée du 16 janvier 2026](https://www.cnil.fr/sites/default/files/2026-01/recommandation_cookies_consolidee.pdf). §5.
5. CNIL. [Outil d'auto-évaluation — mesure d'audience exemptée de consentement](https://www.cnil.fr/sites/default/files/2025-07/outil_d_auto-evaluation_mesure_d_audience.pdf). July 2025.
6. CNIL. [Cookies : solutions pour les outils de mesure d'audience](https://www.cnil.fr/fr/cookies-et-autres-traceurs/regles/cookies-solutions-pour-les-outils-de-mesure-daudience). 4 Jul 2025.
7. CNIL. [Questions-réponses sur les lignes directrices et la recommandation « cookies et autres traceurs »](https://www.cnil.fr/fr/cookies-et-autres-traceurs/regles/cookies/FAQ). Q10 (objection), Q18 (information).
8. CNIL. [Recommandation 6 : renforcer l'information et les droits des mineurs par le design](https://www.cnil.fr/fr/recommandation-6-renforcer-linformation-et-les-droits-des-mineurs-par-le-design). 9 Jun 2021.
9. CNIL. [Recommandation 8 : prévoir des garanties spécifiques pour protéger l'intérêt de l'enfant](https://www.cnil.fr/fr/recommandation-8-prevoir-des-garanties-specifiques-pour-proteger-linteret-de-lenfant). 9 Jun 2021.
10. Council of the EU. [Digital Omnibus, 2025/0360 (COD), working document of 19 Mar 2026](https://data.consilium.europa.eu/doc/document/WK-3736-2026-INIT/en/pdf). Secondary sources only; final text not verified.
