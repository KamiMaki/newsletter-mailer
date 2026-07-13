# newsletter-mailer

Mailer for the AI newsletter. The cloud generator pushes `newsletters/{date}-{slug}.html` + metadata; GitHub Actions sends via Gmail and archives to Notion using repo Secrets. No secrets or recipient lists live in this repo.
