# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

OIOIOI is the Django web interface of SIO2, a platform for algorithmic contests (szkopul.edu.pl, Polish Olympiad in Informatics). Python 3.13, Django 5.2, PostgreSQL, Celery/RabbitMQ, Filetracker for file storage, sioworkers for judging.

This repository is the **Czecie fork** (`kubat1234/oioioi`): one production instance for olympiad training camps, ~150 participants, several groups at once, each group being its own contest.

Read next, in this order:

- `docs/ARCHITECTURE.md` — apps, the submission → judging → ranking flow, the camp rules and where they live, what the fork changes in upstream code, pitfalls.
- `AGENTS.md` — upstream's generic guide for coding agents (code style, test conventions). **Where it differs from this file, this file wins** (branch names, PR workflow, CI).
- `internal/` (git-ignored, only in the maintainer's checkout) — local reports of past merges and reviews, e.g. `internal/MERGE_REPORT.md`, `internal/REVIEW.md`. The upstream merge of 2026-11 is summed up in PR #23.

## Git workflow (fork kubat1234/oioioi)

- The main branch is `main` (renamed from `czecie-camp`). The remote of the fork may be called `fork` or `origin` in a clone — check `git remote -v`.
- `main` is protected: never commit or push to it directly. Every change goes on a branch (`camp-…`, `fix-…`), then a PR to `main`, merged with "Squash and merge".
- CI (`.github/workflows/czecie-ci.yml`) runs ruff, a missing-migrations check, the test suite and a Docker image build in parallel on every PR; on `main` the image (Dockerfile target `base`, layer cache in `ghcr.io/kubat1234/oioioi:buildcache`) is pushed as `ghcr.io/kubat1234/oioioi:main` and `:sha-<short>`. Keep dependency installs in the Dockerfile before `COPY . ./` so code-only changes hit the cache. Run ruff and the relevant tests locally before opening a PR.
- Production (`docker-compose.yml`) runs the released image `ghcr.io/kubat1234/oioioi:${SIO_TAG}`; it is updated only with `./deploy.sh <version>` (refuses during a running round, backs up the DB, runs migrations in the one-shot `migrate` service before `web` starts). Do not build images on the server.
- Releases: Actions → Release → Run workflow (`.github/workflows/release.yml`) tags the already built image of the current `main` commit as `vYYYY.MM.DD[.N]` and `latest` and creates a GitHub Release with generated notes. Never create `v*` tags by hand.
- `sio2project/oioioi` `master` is upstream (`origin/master` in the maintainer's clone). Do NOT merge upstream into `main` without the maintainer's explicit OK. PRs meant for upstream branch from upstream `master`, not from `main`.
- Merging upstream: on a branch (`merge-upstream`), as a real merge; fixes after the merge as separate commits; write up every file both sides changed in a report in `internal/` and the important parts in the PR description (as in PR #23). Read the upstream diff of `oioioi/default_settings.py` and `Dockerfile` line by line — they change production behaviour without touching fork code. The PR of such a branch must be merged with **"Create a merge commit", not squashed**: a squash drops upstream from the history of `main` and the next upstream merge conflicts on everything again.
- Keep changes to upstream files minimal and local (a hook on a controller instead of an edit in a view, a new template instead of a rewritten one): every changed upstream line is a future merge conflict. No drive-by refactors, reformatting or dependency bumps.

## Development environment (Docker)

Everything runs in containers defined in `docker-compose-dev.yml` (`web`, `worker`, `db`, `broker`, `rustfs`, `s3dedup`). The repo is mounted at `/sio2/oioioi` in `web`; the Django deployment dir (`manage.py`, `settings.py`) is `/sio2/deployment`. Use `easy_toolbox.py` (run `./easy_toolbox.py -h`):

```bash
./easy_toolbox.py build          # build image (needed after dependency changes in pyproject.toml)
./easy_toolbox.py up             # start containers, wait for migrations
./easy_toolbox.py run            # dev server on localhost:8000 (not started by `up`)
./easy_toolbox.py stop | down    # down also removes containers
./easy_toolbox.py bash           # shell in web
./easy_toolbox.py exec <cmd>     # run a command in web
./easy_toolbox.py add-superuser  # admin/admin
```

Running Django management commands directly:

```bash
docker compose -f docker-compose-dev.yml exec -T web bash -c "cd /sio2/deployment && python manage.py <command>"
```

Settings: `/sio2/deployment/settings.py` is generated from `oioioi/deployment/settings.py.template` and imports `oioioi/default_settings.py`. Optional apps are enabled by uncommenting them in `INSTALLED_APPS` there. In this checkout the file is kept on the host as `deployment/settings.py` (gitignored) and bind-mounted into `web` by `docker-compose-dev.yml`, so edit it on the host (in-container `sed -i` fails on a single-file mount).

## Tests and lint

pytest with `DJANGO_SETTINGS_MODULE=oioioi.test_settings` (see `pytest.ini`; SQLite, runs with `--nomigrations`, deprecation warnings are errors). CI runs `./test.sh -n auto --migrations`. Usually run inside the `web` container:

```bash
./easy_toolbox.py test                                                    # whole suite, parallel
docker compose -f docker-compose-dev.yml exec -T web ../oioioi/test.sh oioioi/<app>
docker compose -f docker-compose-dev.yml exec -T web ../oioioi/test.sh oioioi/<app>/tests.py::TestClass::test_method
./easy_toolbox.py test-slow                                               # includes @pytest.mark.slow (--runslow)
```

Without Docker the suite also runs natively: `uv sync` (needs `libpq-dev libdb-dev libmemcached-dev`), then `./test.sh -n 8 oioioi/<app>`. Tests that need `node_modules`, a browser or missing LaTeX packages are skipped or fail there (`mailsubmit` needs `texlive-pstricks`), so compare with a run of `main` before blaming a change.

Before a PR, the same checks as CI:

```bash
ruff check && ruff format --check
python -m django makemigrations --check --dry-run     # with test settings and an in-memory DB, see czecie-ci.yml
./test.sh -n auto --migrations
```

Test-only apps must be listed in `oioioi/test_settings.py` `INSTALLED_APPS`. `MOCK_RANKINGSD = True` there makes rankings serialize synchronously. Tests mostly use JSON fixtures from `<app>/fixtures/` and `oioioi.base.tests.TestCase` / `fake_time`.

Lint/format (CI runs both; config in `pyproject.toml`, line length 160, migrations and `*settings.py` excluded):

```bash
ruff check --fix oioioi/<app> && ruff format oioioi/<app>
```

After model changes: `python manage.py makemigrations <app>` (in `/sio2/deployment`) and commit the migration. Frontend assets are built with webpack (`package.json`: `build`, `watch`, `lint`); `./easy_toolbox.py eslint` / `npm`. E2E: Cypress via `./test_cypress.sh` (see `GUIDE.md`).

## Architecture in one screen

Details and the full flow are in `docs/ARCHITECTURE.md`.

- `oioioi/` holds ~75 Django apps. A core set is always installed (`default_settings.py`); the rest is enabled in the deployment's `settings.py`. `oioioi/urls.py` autodiscovers every installed app's `views`, `controllers` and `urls` (`contest_patterns` live under `/c/<contest_id>/` with `request.contest` set).
- **Controllers are the extension mechanism.** `Contest.controller_name` → a `ContestController` subclass (a contest type); `Problem.controller_name` → a `ProblemController`. Apps add behaviour to all controllers of a class with `SomeController.mix_in(Mixin)`; admin classes take mixins too.
- **Permissions** are composable request conditions (`base/permissions.py`): `enforce_condition(contest_exists & is_contest_basicadmin)`.
- **Judging**: `submit_view` → `controller.create_submission` → `judge` → an `evalmgr` Celery job runs a *recipe* of handlers (`programs/controllers.py`, `programs/handlers.py`) → sioworkers → `SubmissionReport`/`TestReport` → `update_user_results` → `submission_judged` invalidates rankings.
- **Rankings** are computed by the `rankingsd` daemon and cached per `(contest, full key)` in `rankings.models.Ranking`. Whatever a ranking depends on must invalidate it (`Ranking.invalidate_contest`, `invalidate_pi`).
- **Problems**: upload → `unpackmgr` Celery job → `sinolpack/package.py` → tests, model solutions, statements. A `ProblemInstance` attaches a problem to a contest round with its own copy of the tests.

## The fork

Own apps (enabled in production, all listed in `test_settings.py`):

- `oioioi/czeciecamp` — the camp contest type (`CzecieCampContestController`): one contest = one camp of one group, rounds are days, admins add participants, submitting stays open after a round. Rankings are `CampRanking` rows: `daily_weight × Dzienne + stale_weight × Stałe` over chosen rounds, recomputed from submissions in `CzecieCampRankingController.serialize_ranking` (not from `UserResultForProblem`); freeze, counting end, hidden and disqualified participants, two submission limits. Admin UI: the "Czecie panel" (`/c/<id>/camp/`) and the wizard (`/czeciecamp/create/`). "Dzienne" and "Stałe" are never translated; an "All-time" round is `Round.is_trial`.
- `oioioi/llmdetect` — flags submissions that look pasted from an LLM; own `README.rst`, `DESIGN.md`, `SETUP.md`.
- `oioioi/problembase` — catalogue of problems on the instance (families of re-uploaded packages, descriptions, who has seen what). Requires `oioioi.participants`.

Changes in upstream apps (full table in `docs/ARCHITECTURE.md` §7): login by username or e-mail with unique e-mails (`base/auth.py`, `base/utils/user.py`), partial rejudges (`programs/handlers.py::collect_tests`, `contests/forms.py`), per-contest allowed languages (`programs`), hidden problems/rounds in the API (`contests/api.py`), package files tree (`problems/problem_site.py`), controller hooks for the problems list and rejudges (`contests/controllers.py`), production `docker-compose.yml` + `deploy.sh` + fork CI.

## Pitfalls

The long list is `docs/ARCHITECTURE.md` §8. The ones that bite most often:

- Production `settings.py` lives on the server and imports `default_settings.py`: changing a default changes production at the next deploy. Sandboxes (compilers) are files in Filetracker, not part of the image.
- Tests use SQLite, production PostgreSQL (NUL bytes, regex lookups, locking differ).
- A view that changes anything rankings depend on must call `Ranking.invalidate_contest(contest)`. `serialize_ranking` runs in a daemon, without a real request.
- Use `request.timestamp`, not `timezone.now()`, in views and controllers.
- Look objects up with `contest=request.contest`; groups are separate contests on one instance.
- A mixin on a controller or admin class applies to every contest type — guard camp-only behaviour with `isinstance(contest.controller, CzecieCampContestController)`.
- Bootstrap 5 only (`data-bs-*`, `text-bg-*`); webpack bundles are referenced with `{% bundle_static %}`.

## Translations

UI strings use `gettext_lazy as _`; catalogs live in `oioioi/_locale/locale/<lang>/LC_MESSAGES/` and are managed through Transifex (see `CONTRIBUTING.rst`), not edited by hand except for correcting English msgids. Fork strings: the fork's own apps carry their own `locale/pl/`; Polish strings for fork changes inside upstream apps go to `oioioi/_locale/locale-overrides/pl/LC_MESSAGES/` (`.po` and compiled `.mo` are both committed).
