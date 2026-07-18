# Logging configuration

Logging uses the project configuration precedence: CLI, environment,
`.contextforge/config.local.toml`, `.contextforge/config.toml`, then built-in
defaults. The supported environment variables are
`CONTEXTFORGE_LOG_LEVEL`, `CONTEXTFORGE_LOG_FORMAT`,
`CONTEXTFORGE_LOG_FILE`, and comma-separated
`CONTEXTFORGE_LOG_COMPONENTS`.

```toml
[logging]
level = "info"
format = "pretty"
file_enabled = true
file = ".contextforge/logs/contextforge.log"
rotation_bytes = 10000000
retained_files = 5

[logging.components]
provider = "debug"
budget = "trace"
synthesis = "debug"
```

The built-in defaults are `warning`, `auto`, file logging disabled, a
10,000,000-byte rotation threshold, and five retained files. File logs are
always JSON Lines even when the console is pretty. Put machine-specific paths
and preferences in `config.local.toml`; never place credential values in either
file. `credential_env` is a variable name, not a credential.

Model `context_window` precedence is separately recorded with all candidates.
Use `contextforge diagnostics config .` to see the effective integer and source
without revealing secret configuration values.
