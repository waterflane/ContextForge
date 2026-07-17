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

ContextForge does not execute repository content or provide source-write,
shell, arbitrary-process, Git-mutation, worktree, or autonomous-agent tools.
The scanner and CodeMap extractor read local files to classify, hash, and parse
them without importing repository modules. Model-assisted analysis and
discovery send bounded verified source/facts only to the explicitly configured
provider. Packages, prompts, diffs, index interpretations, and stdout must be
handled as sensitive copies or interpretations of repository data.

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

The generated index uses immutable generations and an atomic active pointer.
Only `.contextforge/index` is removed by `index clean`; user-authored
`.contextforge/config.toml`, saved contexts, and runs are preserved. Index
paths, record references, and every filesystem component are validated without
following symlinks or junctions. Failed strict orchestration restores the prior
active pointer when one existed.

Provider configuration contains endpoint/model policy and, optionally, an
environment-variable name. It rejects inline credentials, sensitive query
parameters, unknown fields, and non-local endpoints when `local_only=true`.
When `local_only=false`, a remote endpoint is still rejected unless
`external_data_policy="allow_repository"`; `deny` and `allow_selected` do not
authorize remote transport in v0.4.0. Repository-wide authorization can send
any selectable snapshot file, including files with secret-like names, so users
must review ignore rules and provider retention before enabling it. ContextForge
does not claim complete secret detection.
Credential values are resolved only at request time, redacted from typed
errors, and forbidden from indexes, reviews, handoffs, prompts, and run data.

The MCP server is local stdio and pins one validated repository snapshot/index
generation per session. It delegates queries and reads to the same bounded
discovery executor as in-process callers. It advertises only read-only tools
and resources: no sampling, remote transport, subscriptions, source/index
mutation, shell/process execution, Git mutation, or agent orchestration.
Protocol output is isolated on stdout; diagnostics use stderr.
