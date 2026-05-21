# AlertBot Runbook

## What To Watch

- Lark alert cards should transition in place: `firing → silenced → resolved`.
- The operations meta-channel receives visible failures with `trace_id`.
- Alertmanager silences created by AlertBot must have `createdBy` as a real email or
  `lark:<user_id>` fallback.

## Inspect Audit Records

Use the `audit_log` table to trace any event:

```sql
SELECT timestamp_utc, trace_id, event_source, operation, actor_lark_user_id,
       actor_email_redacted, result, result_summary
FROM audit_log
WHERE trace_id = '<trace-id>'
ORDER BY id;
```

Common operations:

- `webhook.fd.received`
- `webhook.lark.received`
- `alertmanager.silence.create`
- `card.action.silence.click`

## Investigate Silence Abuse

Use this section for any suspected silence abuse or accidental over-silencing.

1. Query `silences` for unusual durations or repeated operators.
2. Join back to `audit_log` using `lark_event_id` or `trace_id`.
3. Confirm `created_by` is not the bot identity.
4. If abuse is confirmed, revoke the Alertmanager service account and roll back with
   `docs/rollback.md`.

```sql
SELECT created_by, actor_lark_user_id, duration_choice, starts_at, ends_at
FROM silences
ORDER BY created_at DESC
LIMIT 50;
```

## Meta-channel Alerts

Every meta-channel alert should include:

- `trace_id`
- operation name
- redacted payload summary
- error type and result summary

Use the trace id to correlate application logs, audit rows, and Lark card state.

## Rotate Secrets

Secrets are injected through Vault or Sealed Secret. To rotate:

1. Generate a new secret in the source system (Lark, FlashDuty, Alertmanager, database).
2. Update Vault / SealedSecret value.
3. Roll the deployment.
4. Confirm `GET /healthz` and a synthetic webhook still work.
5. Verify no secret values appear in logs or `audit_log.payload_redacted`.

Never put secrets in `ConfigMap`, `values.yaml`, source code, or log output.
