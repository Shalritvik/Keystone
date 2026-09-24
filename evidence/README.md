# evidence/

One directory per run, named by run id.

    <run-id>/
      run.jsonl        append-only structured record of every step
      result.json      the final typed result (atomic write; absent = crashed)
      screenshots/     captured on failure only

Local runs are gitignored. The demo runs referenced from README.md are
committed explicitly.
