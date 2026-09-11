# Contributing

Good beta reports include:

- Codex version and operating system;
- model and reasoning effort;
- a synthetic or fully public fixture;
- the `outcome`, arm order, verifier status, output-equivalence status, token
  counts, tool counts, and elapsed times from `comparison.json`;
- a second run with the opposite order when affordable.

Never upload secrets, private source, account data, or unredacted rollout logs.
Do not report only a percentage; a comparison is meaningful only when both
arms pass and the declared outputs are byte-identical.
