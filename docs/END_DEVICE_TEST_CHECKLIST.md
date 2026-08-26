# V3.5 end-device test checklist

- [ ] Install the wheel in a fresh Python 3.11+ virtual environment; confirm `flourite --version` prints `0.6.0` and the compatibility command `frontier --version` reports the same version.
- [ ] Install OMP, authenticate with `codex login`, then run `flourite doctor`; confirm ChatGPT subscription auth plus every configured model and tool capability.
- [ ] Inspect one `context-manifest.json`; confirm system/developer messages are empty, ambient discovery is disabled, tools match the call contract, and the capability digest and context delta are present.
- [ ] Run `flourite demo`, then `flourite verify latest`; confirm the run is complete, sealed, and integrity checks pass.
- [ ] Run one small live task; confirm the Lead gets a thread ID, later Lead calls resume it, and status ends `continuous`.
- [ ] Interrupt a live run and resume it; also simulate one Lead-resume failure and confirm usage is counted and continuity becomes `reconstructed_verified` or clearly `degraded`.
- [ ] Test `summit.mode = "auto"` on an easy task and `summit.mode = "on"` on a hard mechanism task; confirm auto stays sparse while on exposes a bounded exact-task Summit action.
- [ ] Inspect the Task Source, Charter, obligations, Artifact Spine, semantic CI, and Completion Case; confirm no reframe changed the user’s real objective and every release blocker is covered.
- [ ] Run a software task on a disposable Git repository; confirm isolated work, configured checks, no automatic mutation, and refusal to apply after the source fingerprint changes.
- [ ] Interrupt and resume an active run; confirm the ledger reconstructs the exact workspace and `flourite verify` still passes.
- [ ] Run a two-judge fake arena and a small live arena; confirm A/B positions alternate and adaptive/legacy solver budgets match.
- [ ] Inspect diagnostic and audit exports for sensitive content before sharing either file.
