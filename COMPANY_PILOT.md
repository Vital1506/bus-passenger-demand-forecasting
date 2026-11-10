# Later phase: company pilot (after approval)

This runbook is for a future, limited pilot after the company approves the project and hosting. It is not needed to review the current prototype. The current live website is a read-only synthetic demo; use generated sample data for demonstrations. Do not import company data as part of prototype review. Even after approval, a pilot is not automatically certified for dispatch or service decisions.

## What the pilot blueprint creates

Use `render.company.yaml` as a separate Blueprint path in Render. It defines a paid Docker web service, requires a generated bearer key for operator API access, and stores app data under a 10 GB persistent disk mounted at `/var/lib/bus-forecast`. The existing `render.yaml` remains the free public demo.

The current Render Blueprint documentation says persistent disks are only available to paid services, preserve files only under their mount path, attach to one service instance, prevent horizontal scaling, and can cause a short outage during redeploys. The pilot configuration is therefore a single-instance starting point, not a highly available architecture. Confirm current compute and disk prices in your Render account before creating it. Back up data outside the service and perform a restore exercise before importing operational data.

## Gate 1: Company ownership and access

Before creating the service, name an operations owner, data owner, technical owner, and incident contact. Confirm the company has approved Render and the intended data region for this data classification.

The app currently protects operator endpoints with one shared bearer key. It has no SSO, individual identities, roles, or per-user audit trail. Do not treat this as enterprise access control. For even a limited pilot, restrict access with company-approved network controls or an identity-aware proxy, issue the key through the company secret manager, restrict who can retrieve it, and rotate any key previously shared outside that secret manager. If this access pattern does not meet policy, do not import company data until SSO/RBAC is implemented and reviewed.

Keep the public demo separate. Never enter company credentials or company counts in the free demo.

## Gate 2: Data contract and reconciliation

The current API accepts daily upserts at `POST /api/data/ingest` and CSV uploads at `POST /api/data/upload`. API requests require the bearer key. Review the generated OpenAPI schema at `/docs` from the approved network before integrating.

Agree and record all of the following with the data owner:

- Whether the target is boardings, alightings, or another count, and the service-day time zone/boundary.
- Stable route IDs, treatment of route variants, route launches/retirements, and canceled service.
- Source system, correction process, late-arriving records, expected daily delivery time, and who resolves failed batches.
- Whether a missing route/date row means missing data, no service, or zero demand. The default is to reject gaps; only select zero-fill when the source semantics support it.
- Data classification, retention period, deletion process, backup access, and acceptable storage region.
- A reconciled sample covering normal days, weekends, known disruptions, corrected counts, and route changes.

Compare record totals and route/date coverage to the source of truth. Do not use personally identifiable passenger records; this forecasting target only needs aggregate counts.

## Gate 3: Forecast acceptance

Use a time-ordered evaluation window that was not used to choose the model. Compare the model to the seasonal-naive baseline overall and for each route. Operations and analytics must set acceptable error and data freshness thresholds before viewing results; this repository does not prescribe business thresholds.

Review every route with poor coverage, large errors, or worse performance than baseline. Check forecast ranges against observed misses; they are approximate historical-error bands, not guaranteed prediction intervals. Confirm the seven-day daily totals are appropriate for the intended staffing or planning decision. They are not per-trip loads, vehicle-capacity recommendations, or an optimized timetable.

Record the dataset hash/cutoff, model version, backtest period, metrics, exclusions, approvers, and any known limitations in the release record. A failed or inconclusive acceptance result means forecasts remain advisory and the pilot stays in shadow mode.

## Gate 4: Shadow operation

Run forecasts alongside existing planning for an owner-approved period without using them as the sole basis for service changes. At each cycle, record the forecast, data cutoff, model version, actual counts when available, and errors. Review missing/stale feeds, route-level error, and changes in source data. Agree on a fallback to the existing planning process if data, service, or model checks fail.

The app does not yet provide a scheduled vendor connector, automated drift alerts, formal forecast/actual tracking workflow, SSO/RBAC, multi-tenant isolation, or a high-availability datastore. Assign manual owners for feed checks and score reviews during the pilot; do not imply those controls are automated.

## Gate 5: Operations and recovery

Before company data arrives:

- Confirm the Render service is on the intended paid plan and that the disk is mounted at exactly `/var/lib/bus-forecast`.
- Verify HTTPS, approved access restrictions, secret storage, and a deliberate key rotation/revocation procedure.
- Export a backup outside the service. Document its encryption, access, retention, and deletion. Restore it into an isolated environment and verify the data, model, metrics, forecasts, and audit history.
- Set disk-space and service-health monitoring, on-call ownership, maintenance windows, and a manual rollback procedure.
- Verify a deploy/restart preserves the data directory and that a failed deployment does not silently switch operators to synthetic forecasts.

A persistent disk is not, by itself, a backup or disaster-recovery plan. This single-instance setup has deployment downtime and no automatic failover.

## Go/no-go record

The company owner should record a date and approver for each gate, the thresholds and results, the known limitations accepted, and the rollback decision. Until all required gates are signed off, use synthetic data or run a shadow pilot only; do not use this output as the sole basis for live scheduling decisions.
