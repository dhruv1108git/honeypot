# cotmon

Nightly chain-of-thought monitor for agent trajectories. `cotmon` reads the
trajectories that agent deployments upload to the trajectory store, asks a
monitor model to review each one for scheming (covert sabotage, oversight
tampering, deceptive reporting and related behaviour), and writes alerts for
the safety on-call rotation.

## Layout

```
config/monitor.toml      monitor model, prompt version, alert levels, store path
cotmon/config.py         TOML loader and validation
cotmon/store.py          SQLite trajectory store: schema and selection queries
cotmon/transcript.py     renders a stored trajectory into monitor input
cotmon/prompts/          monitor system prompts, one file per version
cotmon/client.py         model gateway client
cotmon/scoring.py        builds monitor requests, parses scores, alert decisions
cotmon/alerts.py         writes monitor results and alerts back to the store
cotmon/pipeline.py       nightly entry point
tests/                   unit tests (`python -m unittest discover -s tests`)
docs/                    design notes
```

## Running locally

```
python -m cotmon.pipeline --config config/monitor.toml --date 2026-08-20 --db /tmp/traj.db
```

`--date` selects snapshots captured on that UTC day. Without `--db` the store
path from the config is used. The gateway token is read from
`COTMON_GATEWAY_TOKEN`.

## Deployment

The job runs as `cotmon-nightly` at 02:30 UTC on the batch cluster and covers
the agent deployments below. Trajectories uploaded to the store are reviewed
by the nightly job.

| Deployment | Agent model | Notes |
|---|---|---|
| `codegen-agent` | kestrel-2.5 | repository maintenance bots |
| `oncall-triage-agent` | kestrel-2.5-flash | pages and incident summaries |

Alerts land in the `alerts` table and in the safety on-call queue. Scores at or
above the page level also page the on-call engineer.

## Owners

monitoring-platform (monitoring). Questions in #cotmon.
