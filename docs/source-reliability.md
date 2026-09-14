# Source reliability

## Journal discovery and enrichment

Crossref's August 24, 2026 API upgrade rejects cursor pagination combined with
publication-date sorting. The previous combination returned HTTP 400 even when
the journal's RSS feed succeeded. See the
[Crossref change notice](https://community.crossref.org/t/changes-to-cursors-filtering-and-sorting-in-the-rest-api/16246).

Single-page abstract enrichment now uses publication sorting without a cursor.
Standalone Crossref sources use cursors without publication sorting, retain the
date filter on every page, and sort the results locally.

A valid official feed with no recent entries counts as a successful update.
Optional enrichment failures are recorded separately and displayed as a warning.
They never rescue a failed official feed. Unrecognized or incomplete XML feeds
fail without advancing the successful update time. Standalone Crossref pagination
also fails if a page fails. A subsequent clean update clears earlier diagnostics.

## arXiv requests

Discovery and abstract enrichment share one process-wide request gate, with only
one API request active and at least three seconds after its completion before
the next request. This follows the
[arXiv API usage guidance](https://info.arxiv.org/help/api/tou.html).

HTTP 429 responses impose a shared cooldown. Missing or invalid Retry-After
values use 30 minutes; valid longer values are preserved. Explicit maintenance
cooldowns on retryable server errors are also preserved. Long cooldowns return
control to the scheduler instead of holding a worker asleep. The next scheduled
attempt is persisted in SQLite. Other arXiv failures retry after approximately
30 minutes; successful updates return to the announcement schedule.

Responses are validated before caching. Invalid XML, API error entries, malformed
paper metadata, and inconsistent OpenSearch page metadata cannot silently become
successful empty updates. Invalid legacy cache entries are fetched again.

## Verification boundary

On September 14, 2026, local regression tests and live journal updates verified
the Crossref correction and the distinction between source failures and optional
enrichment warnings. Live arXiv API probes still encountered read timeouts on
both the normal network path and the existing application-level direct path.
The changes improve pacing, failure handling, and automatic recovery; they do
not establish that upstream arXiv availability or the local connection has recovered.
