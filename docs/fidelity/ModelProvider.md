# ModelProvider fidelity

The active adapter is an in-process deterministic simulation. It returns a fixed-format summary
from the supplied synthetic reading and waits for the configured local latency. It proves the
application contract, asynchronous composition, deterministic tests, and local telemetry behavior.

It does not prove hosted-model availability, output quality, token accounting, safety behavior,
provider throttling, network failure behavior, or cost. No live endpoint or credential is used.
The fixed delay is not a performance measurement, capacity test, latency target, or availability
claim.
