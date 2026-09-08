# ContextForge 0.5.1 Bridge API consumer follow-ups

The retrieval, Bridge 1.1, and opt-in Bridge 2 changes shipped together in
ContextForge 0.5.1. They do not rewrite downstream capsule assembly. Consumers
such as `dsh-contextforge` should track the following when migrating from Bridge
1.0:

- omit empty `compressed_summary` values instead of serializing placeholders;
- remove alphabetical fallback candidates when discovery reports
  `low-relevance-candidates`;
- truncate capsules only at complete record or section boundaries, never inside a
  Markdown table or source excerpt;
- use Bridge 1.1 match evidence and registered expansion candidate IDs rather than
  rebuilding candidate lists client-side;
- negotiate Bridge 2 only when ContextForge should own index build/update,
  writer-lock, staging, cancellation, and atomic publication lifecycle;
- consume correlated progress continuously and treat its sequence as monotonic
  rather than contiguous because cumulative burst snapshots may be coalesced;
- after `REQUEST_TIMEOUT`, allow tracked background cleanup to release the index
  lock before starting another mutation;
- keep raw prompts, provider diagnostics, workspace permissions, approval policy,
  and other Harness metadata out of user-visible answers and packaged context.

These are consumer/UI responsibilities. ContextForge Bridge responses and context
packages expose bounded source evidence and safe diagnostics only.
