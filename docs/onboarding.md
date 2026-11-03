# Onboarding the y1-community mirrors

The central packager lives at `ryan-specter/inni-packager`. It reads
`slidia_manifest.xml` and creates one thin mirror repository per unique
`type="img"` source repository:

```
slidia_manifest.xml
        │
        ▼
  tools/onboard.py  ──►  y1-community/<source>-mirror
        │                    ├── mirror.config.json
        │                    └── .github/workflows/mirror.yml
        │
        ▼
  ryan-specter/inni-packager
        └── .github/workflows/reusable-mirror.yml
                    │
                    ▼
        portable packages + releases + index.json + stats.txt
```

The mirror does not contain a copy of the Python tools. Its caller workflow
runs the reusable workflow from the central repository, so a packager fix or a
new Updater CE default reaches every mirror on the next run.

## 1. Prerequisites

- The public repository `ryan-specter/inni-packager` exists and contains this
  folder.
- Actions are enabled.
- The `y1-community` organisation allows repositories to be created by the
  token. The token must be an organisation owner or have repository
  administration permission.
- A `MIRROR_TOKEN` secret is available to the central repository. A
  fine-grained token needs:
  - **Administration: write** on `y1-community` (create repositories)
  - **Contents: read** on source repositories
  - **Contents: write** on the new mirror repositories
  - **Actions: write** on the mirrors (dispatch the first run)
  - **Workflows: write** if the token updates workflow files through the
    Contents API

  A classic token needs `repo` and `admin:org` (or the narrower equivalent).
  Without `MIRROR_TOKEN`, planning still works for public sources; applying
  cannot create repositories.

The secret is read as `GH_TOKEN`/`GITHUB_TOKEN` by the onboarding workflow. It
is never written to a config file or the report.

## 2. Plan first

Open **Actions → Onboard y1-community firmware mirrors → Run workflow** with
`apply` off. Or run locally:

```bash
GH_TOKEN=... python tools/onboard.py \
  --out onboarding-report.json
```

The plan:

- parses the live manifest;
- ignores comments, APKs and malformed repository names;
- groups multiple Y1/Y2 rows that point at the same repository;
- checks source releases and lists their `.zip` assets;
- checks whether each mirror already exists;
- writes `onboarding-report.json` without creating anything.

Review the report before applying. The expected source set is currently 16
unique repositories. A source that returns 404 is marked `source_error`; the
default apply skips it rather than creating a permanently broken mirror. Use
`--include-missing-sources` only when you intentionally want an empty future
mirror.

## 3. Apply

Run the workflow again with `apply: true`. For a controlled first batch, set
`only` to a comma-separated filter such as `y1-stock-rom,rondo`.

If a source returns 404, it is skipped by default. Set
`include_missing_sources: true` only when you deliberately want to reserve a
mirror name for a repository that does not exist yet.

The workflow creates missing repositories with `auto_init`, then writes:

- `mirror.config.json` — source, mirror, branch, model/type defaults, rules,
  README sync settings and updater repository.
- `.github/workflows/mirror.yml` — a caller workflow pinned to
  `ryan-specter/inni-packager/.github/workflows/reusable-mirror.yml@main`.

Existing `mirror.config.json` files are **kept** by default. This protects
per-product tuning. Use `update_config: true` only when you want to regenerate
them. The caller workflow is always brought up to date because it is managed by
the central repository.

The repository name is derived from the source repository name, lowercased,
with `-mirror` appended:

| Source | Mirror |
| --- | --- |
| `y1-community/y1-stock-rom` | `y1-community/y1-stock-rom-mirror` |
| `rockbox-y1/rockbox` | `y1-community/rockbox-mirror` |
| `schulzcode/Y2Player` | `y1-community/y2player-mirror` |
| `Leska1337/better-Y` | `y1-community/better-y-mirror` |

If two sources would collide after lowercasing, the owner is prefixed.

## 4. First run

`trigger` is off by default. Enabling it dispatches `mirror.yml` in every
selected mirror. That is convenient but can start many large backfills at once
(the Y2 Linux archive alone has 85 releases). On a first rollout, use `only` or
leave `trigger` off and run one mirror manually.

Each mirror's workflow:

1. checks out the mirror and `ryan-specter/inni-packager`;
2. runs the packager tests;
3. backfills releases oldest first;
4. copies the source README into the mirror;
5. publishes portable variants, copies originals and writes `stats.txt`;
6. commits `index.json` in the mirror;
7. uploads `mirror-report.json`.

## 5. Generated config policy

The generated config is deliberately conservative:

- default model is `Y1`, or `Y2` for a Y2-only product;
- default type is `Type A`, or `Stock` for a Y2-only product;
- first-match rules cover `360p`, `240p`, explicit `rom_y1_type_a/b`, generic
  `type_b`, and `rom_y2`;
- all five platform defaults are inherited, including optional arm64;
- `source_readme.path` is `README.md`, so the upstream README becomes the
  mirror landing page;
- `copy_original_assets` is true.

Some repositories have unusual assets. For example, the Y2 Linux repo publishes
`...-first-install.zip` rather than `rom.zip`, and one Y2 repo also carries Y1
ROMs. The packager only builds ZIPs that contain a `*scatter*.txt`; others are
skipped. If a product needs different `fw_package.ini` metadata, edit its
`mirror.config.json` after creation and leave `update_config` off on later
onboarding runs. Keep a copy of deliberate overrides outside the generated
report.

## 6. Updating an existing mirror

Re-running onboarding:

- detects existing repositories;
- leaves existing configs alone unless `update_config` is set;
- refreshes the managed caller workflow;
- can trigger the mirror again.

Changing the central packager code does not require onboarding. Every caller
uses `@main`; a mirror can pin `packager_ref` in its caller if it needs a
staged rollout.

## 7. Local dry run and report

```bash
# Full read-only plan
GH_TOKEN=... python tools/onboard.py \
  --out onboarding-report.json

# One product, apply, and start it
GH_TOKEN=... python tools/onboard.py \
  --only rondo --apply --trigger \
  --out onboarding-report.json

# Re-generate configs for every selected mirror
GH_TOKEN=... python tools/onboard.py \
  --only y1-stock-rom --apply --update-config
```

`onboarding-report.json` is gitignored. The Actions run also uploads it as an
artifact for 30 days.

## 8. Failure handling

| Symptom | Meaning | Fix |
| --- | --- | --- |
| `source unavailable: ... 404` | Source repo is gone or token cannot see it | Leave it skipped, or use `--include-missing-sources` for a future mirror |
| `403` creating repository | Token lacks organisation admin | Use an org-owner token with Administration write |
| `404` dispatching workflow | Workflow file was not indexed yet, or Actions disabled | Check the mirror's Actions tab; rerun with `trigger` later |
| Config kept unexpectedly | Existing config is protected by default | Use `--update-config` or edit the mirror directly |
| Mirror workflow fails checkout of central repo | Central repo is private or token lacks read | Make the central repo public or give `MIRROR_TOKEN` read access |
| `index.json` push rejected | Branch protection or README commit race | Allow the Actions bot to write, or relax the branch rule; the reusable workflow retries with a rebase |

## 9. What is not automated

- Creating the `ryan-specter/inni-packager` repository itself (do that once in
  GitHub, then push this folder).
- Granting organisation permissions or approving SSO for the token.
- Choosing product-specific `fw_package.ini` values that cannot be inferred
  from the manifest.
- Publishing the website's static pages; those live in `innioasis-updater`.

The onboarding report is the source of truth for what was planned and changed.
