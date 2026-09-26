# Telemetry roadmap questions

Confirmed by the approved implementation plan: ClickHouse; dev/staging/prod;
PHP 8.2–8.4 and compatible Laravel 10–12/Symfony 6.4–7.x; FPM and queue workers;
one discovery agent/proxy per host; authenticated CI archives; opt-in snapshots.

Remaining review questions (do not block Phase 0 contracts):

- Dirty development source: recommend approximate nearest-clean-commit joins
  marked dirty; never overwrite a clean snapshot or include dirty evidence in
  production reports. CI snapshot uploads are clean, full-commit only.
- Routes sharing method/template across domains and tables without a concrete
  connection: roadmap IDs cannot distinguish them. Reject ambiguous facts;
  no new suffixes without contract review.
- Two closures on one source line: reject as ambiguous under the roadmap ID.
- Trait aliases/conflicts and inherited methods need explicit effective-class
  attribution in Phase 2; declaration IDs stay distinct from using-class IDs.
- Production sampling/overhead settings remain gated by measurements. Initial
  validation records all spans; aggregate metrics precede retention sampling.
- No activity rolls into a new commit as fresh observation. Historical
  continuity must be separately labeled. Aggregation cadence is one minute.
