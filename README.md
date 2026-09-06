# newsletter-mailer

Mailer for the AI newsletter. The cloud generator pushes `newsletters/{date}-{slug}.html` + metadata; GitHub Actions sends via Gmail and archives to Notion using repo Secrets. No secrets or recipient lists live in this repo.

**Delivery semantics: at-least-once, not exactly-once.** A send that partially succeeds (some recipients fail) writes no `sent/*.ok` marker, so a re-run of the job re-sends that newsletter to ALL of its recipients, including ones who already received it. This favors never silently dropping a newsletter over avoiding an occasional duplicate.

**Transient Google API errors are retried.** Reading the subscriber sheet and the Forms responses retries 5xx / 429 / network errors (`READ_RETRY_DELAYS` in `gsuite_https.py`, 5s → 15s → 45s). If the sheet still cannot be read and `FALLBACK_RECIPIENTS` is not set, the send exits non-zero and writes no `sent/*.ok` marker, so re-running the failed job re-delivers that newsletter. Only a sheet that *is* readable but has zero active subscribers falls back to sending to the sender alone.
