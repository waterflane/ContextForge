# Security Policy

## Supported versions

ContextForge is pre-1.0 software. Security fixes are expected to target the
latest released version.

## Reporting a vulnerability

Please do not open public issues for security vulnerabilities.

Report suspected vulnerabilities privately through the repository's security
advisory flow when available. If that is not available, contact the maintainers
using the private channel listed in the repository profile.

Include:

- affected version or commit;
- reproduction steps;
- impact assessment;
- any relevant logs or environment details without secrets.

## Scope

ContextForge does not execute repository content, make model calls, or build a
repository index. The unreleased v0.2 scanner reads local files only to classify
and hash them; it does not include file contents in output. Symbolic links and
Windows directory junctions are not followed. Security-sensitive behavior will
be documented as further features are introduced.
