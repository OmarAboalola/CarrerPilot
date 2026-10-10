# Local data and capability policy

This is a prototype, not a public service policy/compliance certification.

- Uploaded PDF bytes are processed in memory and are not stored. On confirmation,
  the reviewed source text, unmapped facts and literal evidence persist with the
  profile so the user can inspect extraction and matching. Treat that local profile
  data as private and exclude both data directories from source uploads.
- Local CV extraction reads explicitly listed skills and recognized CV sections without
  an external model. It retains source evidence and copies stated facts for review;
  it does not infer career goals or skill proficiency. Profile data is saved only on
  confirmation. The existing model-consent and supported-evidence checks still apply
  when a configured external model performs extraction.
- Confirmed profiles and prior versions persist locally. The example is sourced
  from the supplied CV plus GitHub FastAPI evidence; career goals remain suggestions.
- Fixture runs never load Gmail credentials or call the model. Synthetic data is labeled.
- Live external model processing is separately disclosed and checked.
- Gmail credentials are encrypted in SQLite. The encryption key and session key are
  separate local files protected by OS user permissions. They must not be shared.
- Live Gmail raw thread bodies are transient; only relevant bounded analysis snippets
  are stored in scan results. Scan email/action summaries older than seven days are
  stripped on the next startup. Draft records retain content for review/reconciliation
  until local-data deletion, including older drafts.
- Disconnect removes local tokens and cached scan mail before waiting for Google revocation.
  The UI distinguishes confirmed revocation from unconfirmed revocation and provides the
  Google account access-management link. The notice survives refresh until dismissed.
- Reconnection/switching invalidates cached Gmail scan results and existing proposals.
  Cached-mail questions/replies always require the scan's current Gmail identity,
  whether analysis uses an external model or the local baseline.
- Account-bound draft history is retained for recovery but its content is redacted when
  a different Gmail account is connected or Gmail is disconnected. Reconnect the original
  account to review/reconcile an uncertain write. Local job drafts prepared without Gmail
  remain available. A local-only draft must be prepared again after connecting before
  it can be saved to Gmail.
- Unsaved draft fields also use tab-scoped browser session storage for refresh recovery.
  This recovery copy contains editable fields and an account binding, never credentials
  or tokens. Save or Discard removes it; disconnect/account changes clear Gmail-bound
  recovery data. Recovery is filtered against the current Gmail account before display.
  Local-only and synthetic edits remain independent of a real Gmail connection.
- If a saved operation becomes locked while local edits exist, the UI shows the saved
  operation's exact content. The different unsaved text remains separately available
  for copying; it is never substituted into execution or reconciliation.
- The local mail baseline requires explicit request evidence and excludes automated/bulk
  mail from reply recommendations. It is a conservative heuristic, not verified semantic AI.
- Access logs are disabled by launch instructions so OAuth authorization codes do not
  appear in request logs. Application activity stores event names and resource IDs.
- Current backend identity is one explicit local owner; public identity/isolation is
  not implemented. Loopback binding and CSRF/host/origin controls limit exposure.
- Outbound email sending, archive/delete/mark-read and automated applications are disabled.
- Contact discovery is not performed. Contacts must be appropriate public/user-confirmed
  recruiting contacts and reviewed again before a Gmail draft.
- Real Gmail draft creation may time out after writing. Such actions remain unknown;
  a bounded read-only reconciliation is supported. Never auto-retry a mutation.

Public pilot gates: confirmed beneficiary/processing purpose, verified authentication,
all resource ownership checks under two real users, operational backups/key management,
retention/deletion policy, provider terms and applicable Google OAuth verification.

