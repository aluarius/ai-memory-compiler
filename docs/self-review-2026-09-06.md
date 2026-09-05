# SQLite migration self-review: 2026-09-06

The review covers `9fc3424..e20ad12`: canonical storage, migration/export,
capture/recovery, compiler validation, MCP retrieval and operational checks.
The following corrections address reproduced failure cases, not ranking
preferences or style-only concerns.

## Corrected findings

All nine findings have Medium severity and regression coverage.

| Area | Failure | Correction |
| --- | --- | --- |
| Compiler project scope | A full replacement with omitted projects erased existing scope. | Omitted scope inherits the original value for both supported change formats. |
| Compiler provenance | A valid change could drop prior sources or omit the source whose checkpoint advances. | Preserve prior source identifiers and require the processed source on compiled changes before committing. |
| Capture privacy | Raw metadata and plaintext checkpoint identities bypassed context redaction. | Share a scalar provenance whitelist, redact metadata and hash the original scope; atomically upgrade legacy checkpoint keys without replay. |
| Import retries | Reimporting an already captured transcript could return success while its job remained failed. | Retry only unfinished jobs in the requested transcript/session/range; report cooldown, quarantine and active leases accurately. Completed imports ignore unrelated worker cooldown. |
| Import compilation | Canonical imports omitted the automatic compile trigger. | Run the existing trigger after successful scoped extraction. |
| Quarantine | Provider failures counted toward the malformed-response threshold. | Count malformed responses separately in the same transaction as the lease outcome. |
| MCP reads | Symlinks in an optional export could block canonical article reads. | Validate canonical identifiers without filesystem resolution; keep legacy filesystem containment checks. |
| Database availability | A directory or broken symlink at the database path enabled legacy fallback. | Treat occupied, unusable database paths as errors rather than absent installations. |
| Backup publication | Another process could create the destination after the existence check and have its backup overwritten. | Build a portable snapshot privately and publish it exclusively; never replace a competing destination or publish partial data. |

The malformed-response counter uses private application state, not untrusted
capture metadata. Opaque capture identities distinguish separate sessions even
when their redacted names are identical. Original job dedup identities remain
compatible; existing jobs and immutable source history are not rewritten.

## Verification and boundaries

Each correction has an observed failing regression followed by a passing test.
Independent focused reviews cover storage, MCP and the compiler/privacy fixes.
The final suite passes 405 tests on Python 3.12.2 (9.23 seconds) and Python
3.13.13 (6.42 seconds), up from 368 tests at rollout. Ruff's E4/E7/E9/F checks
over scripts and tests pass, as does `git diff --check`.

All failure reproductions use temporary stores, synthetic transcripts and
substituted model responses. The review does not invoke models or rewrite the
live corpus. Background capture from ordinary sessions remains active.
Read-only strict health passes; today's uncompiled journal is normal incremental
work, not an old backlog. One weak-connectivity suggestion remains.

The existing [retrieval limitations](sqlite-rollout-2026-09-05.md#final-retrieval-acceptance)
still apply. This review changes canonical reading boundaries, not ranking or
the frozen question fixture. Windows process behavior and semantic contradiction
checking are not established by these macOS/local tests.
