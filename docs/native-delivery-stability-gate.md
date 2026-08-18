# Fixed-path native delivery stability gate

The fixed-path JianYing mirror treats the saved draft directory as a live
output while JianYing may still be writing backups, timelines, or metadata.
Delivery therefore uses two continuous quiet windows:

1. Capture the complete tree receipt and require the receipt to remain
   unchanged for the configured quiet window before copying.
2. Copy to a unique temporary sibling and verify its complete receipt.
3. Observe the source for a second quiet window. If its receipt changed since
   the copied snapshot, delete only the temporary sibling and restart from a
   fresh pre-copy stable snapshot.
4. Promote the temporary sibling with an atomic rename only after the second
   window passes. Write the external receipt after promotion.

The default unattended policy is 6 seconds quiet, 1 second polling, a
120-second per-attempt timeout, and two retries. The CLI exposes overrides via
`--quiet-window-seconds`, `--poll-interval-seconds`,
`--stability-timeout-seconds`, and `--stability-retries`.

Timeout or retry exhaustion fails closed: temporary siblings are removed, no
final target is left, and no success receipt is written. Receipt evidence
records the policy, observation details, retry count, and both source-stable
booleans. Clock, sleep, and receipt capture are injectable in tests so the
contract is deterministic without real multi-second waits.
