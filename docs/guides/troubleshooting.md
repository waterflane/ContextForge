# Troubleshooting

## Model request exceeds configured context window

This message can be a local ContextForge preflight rejection. It does not mean
LM Studio rejected the request. Capture debug diagnostics and inspect sources:

```bash
contextforge context suggest . --task "..." --log-level debug
contextforge diagnostics config .
contextforge diagnostics last . --format json
```

Four values are distinct:

1. the LM Studio loaded-model context configured outside ContextForge;
2. any provider-reported or model-metadata context (OpenAI-compatible APIs do
   not standardize this);
3. ContextForge's configured `context_window`, resolved by precedence; and
4. the effective request budget: context window minus output, protocol, schema,
   and safety reserves.

For example, LM Studio/model metadata may report 98,304 while
`.contextforge/config.toml` explicitly sets 16,384. ContextForge uses 16,384,
logs `effective_context_window_source=config.toml`, and if a 22,140-token
estimate does not fit emits `budget.rejected` with
`request_dispatched=false`. Increase ContextForge `context_window` to the
verified loaded-model capacity, reduce candidates, enable hierarchical
synthesis, or select another provider. Do not merely increase LM Studio while
leaving the lower ContextForge override in place.

The budget record decomposes system, user, source, selected index, schema,
requested output, protocol overhead, and safety margin so the total can be
reproduced numerically without exposing the prompt.
