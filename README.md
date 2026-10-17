# HISI

**Health Information System Interoperability** — SHA claims technical validation for HMIS products that already completed DHA onboarding and first-round certification.

This Frappe app owns the HISI domain model. The user-facing interface is the separate `hisi` Next.js repository (frontend), which reads narrow, permission-aware views from the API in this app.

## Domain model (initial vertical slice)

| DocType | Purpose |
| --- | --- |
| HISI Vendor Organization | Vendor identity and member users (organization-scoped access boundary). |
| HISI Organization Member | Child table: member user and vendor role. |
| HISI HMIS Product | Vendor-owned product and the release under evaluation. |
| HISI DHA Certification Reference | DHA-issued certificate ID, status and verification evidence. Never mutates the DHA record itself. |
| HISI Checklist Version | Immutable, published release of the SHA criteria (CC-01 to CC-07). |
| HISI Checklist Criterion | Child table: source criterion ID, title, statement, evidence options. |
| HISI SHA Validation Case | One DHA-verified system bound to a release, checklist version, lifecycle state, owner and next action. |
| HISI Case Criterion Assessment | Child table: per-criterion reviewer assessment, rationale, assessor and time. |

The full proposed model (18 records) is defined in the HISI frontend repository under `docs/BRD-REQUIREMENTS.md` section 8; further slices add test runs, evidence, findings and surveillance.

## Access control

- `HISI Reviewer` and `HISI Vendor` roles are created on install.
- Vendor users read only documents belonging to their own organization (enforced in `has_permission` on each DocType, on top of role permissions).
- API methods are permission-aware server-side; client visibility is not an authorization control.

## Setup

```bash
# from the bench root
bench --site <site> install-app hisi

# optional: create synthetic demo data (idempotent, fictional records only)
bench --site <site> execute hisi.seed.run_demo_seed
```

## API

- `hisi.api.get_validation_case_view(case=None)` — narrow view model for one validation case: case state, vendor, product, DHA certificate track, checklist version and per-criterion assessments. Requires an authenticated session; returns only the fields a screen needs.

### Contributing

This app uses `pre-commit` for code formatting and linting (ruff, eslint, prettier, pyupgrade). Enable it with:

```bash
cd apps/hisi
pre-commit install
```

### License

mit
