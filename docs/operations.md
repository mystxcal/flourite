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
flourite extend latest --additional-calls 12
flourite export latest --mode diagnostic --output diagnostic.zip
flourite arena --judges 4 "Task"
```

Use `resume` for interrupted active runs and `extend` for completed sealed runs. Verify before either. Review `END_DEVICE_TEST_CHECKLIST.md` before important live use.
