# newsletter-mailer

Mailer for the AI newsletter. The cloud generator pushes `newsletters/{date}-{slug}.html` + metadata; GitHub Actions sends via Gmail and archives to Notion using repo Secrets. No secrets or recipient lists live in this repo.

**Delivery semantics: at-least-once, not exactly-once.** A send that partially succeeds (some recipients fail) writes no `sent/*.ok` marker, so a re-run of the job re-sends that newsletter to ALL of its recipients, including ones who already received it. This favors never silently dropping a newsletter over avoiding an occasional duplicate.
