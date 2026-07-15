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
repository index. The scanner reads local files to classify and hash them. The
v0.3 context builder additionally includes explicitly selected UTF-8 source
content in Markdown or JSON output, so packages and stdout must be handled as
copies of the selected source data.

Repository paths in snapshots and packages are portable relative paths.
Absolute, traversal, Windows drive-relative, and UNC-style selectors are
rejected. Symbolic links and Windows directory junctions encountered below the
repository root are not followed. Selected-file reads revalidate each path
component, regular-file identity, size, and SHA-256 before content is accepted.
JSON inspection is bounded and does not access paths named by a package.

ContextForge assumes the selected repository root and output destination are
local paths the invoking user is authorized to read or write. A concurrently
modified repository can cause a scan or package build to fail; run against a
quiescent working tree when a reproducible snapshot is required. These
portable checks reduce filesystem races but are not a sandbox boundary against
another process with the same account continuously rewriting the tree.

Output parents must already exist. Package output is fully rendered before a
sibling temporary file is atomically published. Existing destinations are
refused unless the context or tree command is given `--force`.
