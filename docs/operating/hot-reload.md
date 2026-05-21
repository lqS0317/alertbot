# Config Hot Reload (SC-009)

SC-009 requires a configuration change to take effect on the next inbound alert without
a service restart.

## What Reloads

- `oncall.static_service_map`
- `severity_colors`
- `silence_buttons`
- `timezone`
- other YAML fields validated by `AlertBotConfig`

Secrets are not stored in YAML; the YAML stores environment variable names only.

## Staging E2E Check

1. Deploy staging with `deploy/helm/alertbot/values-staging.yaml`.
2. Trigger a synthetic `incident.created` for `payment-api`; confirm the current mapped
   user is mentioned.
3. Change `config.oncall.static_service_map.payment-api` in the ConfigMap value.
4. Wait for the file projection / reload interval.
5. Trigger another synthetic `incident.created`.
6. Confirm the new mapped user is mentioned without restarting the Pod.

Expected result: no Pod restart, no config validation error in the meta-channel.

## Failure Path

If invalid YAML is projected:

- the previous valid config snapshot remains active
- the error is reported to the meta-channel
- business flow continues using the old snapshot
