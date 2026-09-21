M9A implementation report

Code SHA-256
  hermes_cli/kanban_db.py
  339eb46a28abcefc369b3d3f2691fe6e902f46e5e650d3619c7b58489fd45064
  tests/hermes_cli/test_kanban_checkpoints.py
  ee2b7975f04e5bfaff4d2859f370033623a3a25542f90449589d60111194cf9f

Changed files
  hermes_cli/kanban_db.py
  tests/hermes_cli/test_kanban_checkpoints.py

Implemented semantics
  Additive append-only task_checkpoints table keyed by task_id plus monotonic sequence.
  Each row records owning run_id, canonical bounded JSON object (64 KiB UTF-8 maximum),
  SHA-256 digest, version 1, optional idempotency key, and creation time.
  save_task_checkpoint requires the current running task/run to match expected_run_id.
  Same idempotency key plus equivalent progress returns the stable original row; conflicting
  progress raises CheckpointIdempotencyError. Stale/reclaimed writers raise CheckpointOwnershipError.
  load_task_checkpoint returns only the requested task's latest checkpoint across attempts and
  fails closed with CheckpointCorruptionError for invalid version, JSON/canonicality, or digest.

Limits
  This is DB-layer storage only. It does not add a model tool, prompt/context integration,
  dispatcher restart reconstruction, configuration, or any external exactly-once guarantee.

Verification
  PASS: HERMES_PYTHON="$(command -v python)" scripts/run_tests.sh tests/hermes_cli/test_kanban_checkpoints.py
        6 tests passed.
  PASS: ruff check hermes_cli/kanban_db.py tests/hermes_cli/test_kanban_checkpoints.py
  PASS: python -m compileall -q hermes_cli/kanban_db.py tests/hermes_cli/test_kanban_checkpoints.py
  PASS: git diff --check
  INFO: canonical existing-suite run tests/hermes_cli/test_kanban_db.py had 55 passes and 3
        pre-existing Windows-specific failures in unrelated infrastructure-spawn/worktree/reaper paths;
        none reference checkpoints.
