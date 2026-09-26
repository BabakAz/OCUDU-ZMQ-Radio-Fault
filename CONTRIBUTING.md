# Contributing

Issues and pull requests are welcome. This repository doubles as the
artifact of a paper, so a few rules keep its results reproducible.

## Paper files stay byte-identical

The 22 files listed in [`provenance/study_manifest.json`](provenance/study_manifest.json)
are the broker, recipe-input and contract files released byte-for-byte from
the paper's study. `make verify-provenance` and `tests/test_provenance.py` fail if one
of them changes. If a change is really needed (for example a security fix),
make it deliberately: update the manifest, explain the change in
[PROVENANCE.md](PROVENANCE.md), and note that the build identities recorded
in the paper no longer apply to the changed file.

## Recipes are fixed

Existing recipes must keep their pinned identities
(`tests/test_radio_fault_recipes.py`). To add a condition, add a new recipe
name (or a custom profile and schedule, see
[docs/FAULT_RECIPES.md](docs/FAULT_RECIPES.md#custom-schedules)) rather than
changing an existing one.

## Before sending a change

```bash
make test
make validate-local        # when broker, schedule, control or metrics code changes
make verify-provenance
```

Keep changes focused, describe what was and was not tested, and keep
documentation claims scoped to what the evidence shows: digital impairments
are not calibrated RF, and a broker-only check is not a RAN result.
By contributing you agree that your contribution is licensed under
GPL-3.0-only.
