# Operations

```bash
flourite init flourite.toml
flourite doctor --config flourite.toml
flourite run --config flourite.toml "Task"
flourite status latest
flourite inspect latest
flourite events latest
flourite verify latest
flourite resume latest
flourite export latest --mode diagnostic --output diagnostic.zip
flourite live latest
```

Use `resume` for interrupted active runs. A satisfied, exhausted, blocked, stopped, or failed run is an honest terminal record rather than a second hidden execution path. Verify before important reuse or export. Review `END_DEVICE_TEST_CHECKLIST.md` before important live use.
