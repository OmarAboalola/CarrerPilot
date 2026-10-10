# Architecture

CareerPilot is a local, single-owner application. FastAPI serves the JSON API and
the static dashboard. SQLite/SQLAlchemy persist reviewed profiles, bounded scans,
application tracking, draft state and activity. Alembic migrations run at startup.

`app/services/` holds source-backed CV extraction, normalization, matching, Gmail
thread analysis, question context and draft preparation. `app/integrations/` holds
the public job, configured model, Google OAuth/Gmail and encrypted-token clients.

The default launcher uses fixture mode and isolated `.demo-data/`. Live mode uses
your `.env` and `data/`; model and Gmail processing require separate approvals.
Queries/results are bounded and failures retain labeled partial/local results.

Reviewed drafts follow proposed, approved, executing and terminal/unknown states.
Edits invalidate approval. Unknown provider writes must be reconciled before retry.
No send, automatic application or inbox cleanup endpoint is exposed.

This build binds to loopback and uses one worker. Multi-user authentication, public
hosting, complete market/mail coverage and production operations are outside its
verified scope. Source evidence and scores remain reviewable; they do not prove
qualification, proficiency or hiring probability.
