# Real-broker integration tests

These tests run against an actual NATS server brought up via
[`testcontainers`](https://testcontainers-python.readthedocs.io/). They are
the canonical place to assert transport-contract behaviour that the
in-memory bus cannot prove (subject wildcards, real wire framing,
connection lifecycle, etc.).

## Running locally

```bash
pytest tests/integration -v
```

Requires Docker to be installed and the daemon reachable. Without Docker the
tests skip with `docker unavailable` — `pytest tests/` stays green for the
no-Docker path.

## Running in CI

CI sets `INTEGRATION_REQUIRED=1` so a missing daemon turns into a hard
failure instead of a silent skip:

```bash
INTEGRATION_REQUIRED=1 pytest tests/integration -v
```

## Upgrading the NATS image

The fixture pins `nats:2.10`. To bump:

1. Edit the image tag in `tests/integration/conftest.py` (search for
   `"nats:2.10"`).
2. Update the tag in this README.
3. Run `pytest tests/integration -v` locally to confirm the new image still
   exposes the `"Server is ready"` log line — the fixture's readiness probe
   depends on it.
4. Commit the bump in its own small PR so a bisect can land on it.
