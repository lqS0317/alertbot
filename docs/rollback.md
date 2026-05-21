# AlertBot Rollback

Use this when cards stop updating, Alertmanager silence creation misbehaves, or the
meta-channel reports a systemic failure.

## Helm Rollback

```bash
helm history alertbot -n alertbot
helm rollback alertbot <REVISION> -n alertbot
kubectl rollout status deploy/alertbot-alertbot -n alertbot
```

After rollback:

```bash
curl -fsS https://alertbot.hashkeychain.net/healthz
curl -fsS https://alertbot.hashkeychain.net/metrics | head
```

## Database Safety

Rollback does not delete database rows. Before any schema rollback:

1. Snapshot the database.
2. Export recent `alerts`, `silences`, and `audit_log` rows.
3. Confirm whether the old app version can read the current schema.

If a migration must be reverted, use Alembic only after confirming data compatibility:

```bash
alembic downgrade -1
```

## Manual Silence Cleanup

AlertBot v1 does not cancel silences from Lark. If a bad deployment created incorrect
silences:

1. Identify them in Alertmanager by `createdBy`.
2. Expire them from the Alertmanager UI or API.
3. Preserve corresponding `silences` and `audit_log` rows for audit.

## Communication

Post rollback start/end messages to the operations meta-channel with:

- incident link
- rollback revision
- trace ids involved
- whether manual Alertmanager silence cleanup was needed
