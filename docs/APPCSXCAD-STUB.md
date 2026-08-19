# AppCSXCAD headless stub

The IHP EM installer builds **openEMS** with `-DBUILD_APPCSXCAD=NO` because Cloud Agent
and CI hosts have no display. Some PDK openEMS workflow scripts still invoke `AppCSXCAD` on
`PATH` for mesh preview or legacy entry points.

## Stub location

```
$IHP_EDA_ROOT/tools/bin/AppCSXCAD
```

(`IHP_TOOLS_PREFIX` defaults to `$IHP_EDA_ROOT/tools`.)

The stub is a small executable that exits successfully without opening a GUI. Python
bindings (`CSXCAD`, `openEMS`) and the `openEMS` FDTD binary are installed separately;
only the Qt viewer is omitted.

## When you need it

- Headless inductor characterization (`char/passive/ihp_ind_em.py`, PDK
  `openems_ihp_sg13g2/workflow`).
- `scripts/verify-ihp-em.sh` mesh preview (`run_line_noGDSII.py`) when a workflow step
  shells out to `AppCSXCAD`.

If EM verification reports a missing `AppCSXCAD` but openEMS Python imports pass, re-run
`./scripts/install-ihp-em.sh` or confirm the stub exists and is on `PATH` via
`source ~/.local/share/ihp-eda/em.env.sh`.

## Related

- [ENVIRONMENT.md](ENVIRONMENT.md) — optional EM tier install
- [scripts/AGENTS.md](../scripts/AGENTS.md) — `install-ihp-em.sh` / `verify-ihp-em.sh`
