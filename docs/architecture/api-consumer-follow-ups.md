# Bridge API consumer follow-ups

The retrieval and Bridge 1.1 changes in ContextForge do not rewrite downstream
capsule assembly. Consumers such as `dsh-contextforge` should track the following
separately when migrating from Bridge 1.0:

- omit empty `compressed_summary` values instead of serializing placeholders;
- remove alphabetical fallback candidates when discovery reports
  `low-relevance-candidates`;
- truncate capsules only at complete record or section boundaries, never inside a
  Markdown table or source excerpt;
- use Bridge 1.1 match evidence and registered expansion candidate IDs rather than
  rebuilding candidate lists client-side;
- keep raw prompts, provider diagnostics, workspace permissions, approval policy,
  and other Harness metadata out of user-visible answers and packaged context.

These are consumer/UI responsibilities. ContextForge Bridge responses and context
packages expose bounded source evidence and safe diagnostics only.
