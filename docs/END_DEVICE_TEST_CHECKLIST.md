# End-device release check

- Install the wheel in a fresh Python 3.11+ environment.
- Run `flourite doctor`; confirm OMP, ChatGPT authentication, and the configured model work.
- Run one live generic task with a source file and materialize its artifact.
- Interrupt and resume one active run; verify its ledger afterward.
- Steer and pause one live run from a second terminal.
- Run one disposable software task, execute configured checks, and apply only after explicit approval.
- Inspect a diagnostic export before sharing it and keep audit exports private.

The deterministic suite covers state transitions. This check covers the real
provider, filesystem, process, and terminal boundaries that fakes cannot prove.
