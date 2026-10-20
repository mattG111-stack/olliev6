# Desktop browser enrichment

This reuses Apex Sourcing's desktop browser method, not its vehicle inventory.
No Trade Me server/proxy requests are made by these endpoints. No new schedule
or paid service is enabled. The laptop, browser and Codex must remain available.

1. In Review & publish, load the browser enrichment queue for the staged batch.
   The backend requires the latest same-batch CoreLogic job's saved property IDs.
   It rejects active/live and archived batches and never widens an absent scope.
2. Verify New Zealand browser egress, then use the existing CUA browser to open
   observed source links or the visible Trade Me property search. Inspect only
   the queued properties, preserving the exact street, unit and suburb. Stop on
   an access challenge; do not rotate connections or evade source restrictions.
3. Read visible property details only. Save a short evidence excerpt with the
   exact address and suburb, and quote each supplied numeric fact with its label.
   Supported blanks: floor area, land area, CV, bedrooms, bathrooms and parking.
   Areas are square metres; convert only explicit source units with retained
   evidence. Current implementation accepts directly quoted square-metre values;
   do not submit hectare conversions as if directly quoted. Do not substitute a
   portal estimate, asking price or historical sale for CV. Do not infer missing
   floors for a section, or land ownership area for a cross-lease unit.
4. Open Browser collector handoff and submit an array of up to ten observations.
   Prefer one row at a time. The authenticated backend matches the exact subject,
   checks the selected scope, source URL, timestamp, labels, bounds and quotes.
   Fields already present are retained. Any disagreement with supplied facts,
   type or sale method saves evidence and holds the row for source review.
5. Blank, non-conflicting facts are saved with field provenance and repriced
   through the existing single-row engine in the same transaction. A pricing
   error rolls back that row's facts and checkpoint. Earlier successful rows
   remain saved. Retrying the same row does not repeat an application.
6. Verify the save receipt and refreshed queue. `observed` means a source page was
   checked; it does not mean all gaps were filled. Source review rows remain held
   through repricing and publication checks until their disagreements are resolved.
   Nothing in this flow publishes, creates properties, or marks listings sold.

Observation shape (illustrative, not source evidence):

```json
[{"property_id":123,"address":"2/24 Example Road","suburb":"Westgate",
"source_url":"https://www.trademe.co.nz/a/property/residential/sale/auckland/westgate/listing/123456",
"observed_at":"2026-10-20T01:00:00Z",
"evidence":"2/24 Example Road, Westgate\nFloor area 155m²",
"facts":{"floor_area_m2":{"value":155,"quote":"Floor area 155m²"}}}]
```

The observation must use the actual observation time from the last 24 hours.
Do not save an outage, unfinished page, login page or no-search-result as a
successful observation. An exact page with no extra facts can be recorded with
an empty facts object; its genuine missing facts remain unknown. Failed source
access stays unfinished in the queue.

Reviewed corrections: the admin queue now exposes retained before/source values and evidence. POST /api/admin/release/browser-enrichment/review accepts only a stored finding_id and its staged batch_id. It revalidates exact identity, evidence age and fact quotes, refuses changed input/hold snapshots, retains originals and per-field provenance, and reprices in the same transaction. An engine failure rolls back facts, approval and valuation. Repeated approval is idempotent. This action never publishes a batch. Public source omissions remain missing; evidence that explicitly qualifies a measurement (e.g. total floor area including balcony) retains that qualification in its source quote.
