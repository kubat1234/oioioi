# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

OIOIOI is the Django web interface of SIO2, a platform for algorithmic contests (szkopul.edu.pl, Polish Olympiad in Informatics). Python 3.13, Django 5.2, PostgreSQL, Celery/RabbitMQ, Filetracker for file storage, sioworkers for judging.

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

pytest with `DJANGO_SETTINGS_MODULE=oioioi.test_settings` (see `pytest.ini`; runs with `--nomigrations`, deprecation warnings are errors). Tests must run inside the `web` container:

```bash
./easy_toolbox.py test                                                    # whole suite, parallel
docker compose -f docker-compose-dev.yml exec -T web ../oioioi/test.sh oioioi/<app>
docker compose -f docker-compose-dev.yml exec -T web ../oioioi/test.sh oioioi/<app>/tests.py::TestClass::test_method
./easy_toolbox.py test-slow                                               # includes @pytest.mark.slow (--runslow)
```

Test-only apps must be listed in `oioioi/test_settings.py` `INSTALLED_APPS`. `MOCK_RANKINGSD = True` there makes rankings serialize synchronously. Tests mostly use JSON fixtures from `<app>/fixtures/` and `oioioi.base.tests.TestCase` / `fake_time`.

Lint/format (CI runs both; config in `pyproject.toml`, line length 160, migrations and `*settings.py` excluded):

```bash
ruff check --fix oioioi/<app> && ruff format oioioi/<app>
```

After model changes: `python manage.py makemigrations <app>` (in `/sio2/deployment`) and commit the migration. Frontend assets are built with webpack (`package.json`: `build`, `watch`, `lint`); `./easy_toolbox.py eslint` / `npm`. E2E: Cypress via `./test_cypress.sh` (see `GUIDE.md`).

## Architecture

**Apps.** `oioioi/` contains ~70 Django apps, one per feature. A core set is always in `INSTALLED_APPS` (`default_settings.py`: `base`, `contests`, `problems`, `programs`, `sinolpack`, `rankings`, `evalmgr`, `sioworkers`, `filetracker`, `questions`, `quizzes`, `interactive`, ...). Others are optional and enabled per deployment. `check_django_app_dependencies` in a module asserts required apps.

**URLs are autodiscovered.** `oioioi/urls.py` imports every installed app's `views`, `controllers` and `urls` at startup (views register menu items via decorators; controllers register mixins at import). An app's `urls.py` defines `contest_patterns` (prefixed with `/c/<contest_id>/`, request gets `request.contest`), `urlpatterns` (both with and without prefix) and/or `noncontest_patterns` — see `oioioi/contests/urls.py`. URL names are referenced without namespace.

**Controllers are the extension mechanism.** `Contest.controller_name` is a dotted path to a `ContestController` subclass (`contests/controllers.py`); `Problem.controller_name` to a `ProblemController`. Contest types are just controller subclasses (`oi`, `acm`, `pa`, `mp`, ...); every subclass becomes selectable as a contest "type" (`RegisteredSubclassesBase`). Controllers decide: registration (`registration_controller()` → `RegistrationController`/`participants.ParticipantsController`), round times and visibility, `can_submit`, which reports are visible (`results_date` vs `public_results_date`, `separate_public_results()`), scoring (`update_user_result_for_problem`, delegated to the problem controller by default), and rankings (`ranking_controller()`). Apps add behaviour to *all* controllers of a class with `SomeController.mix_in(Mixin)` (`ObjectWithMixins`), e.g. `testrun`, `scoresreveal`, `printing`. Admin classes also take mixins (`ContestAdmin.mix_in`, `ProblemInstanceAdmin.mix_in`).

**Permissions** are composable request conditions (`base/permissions.py`: `make_request_condition`, `enforce_condition(contest_exists & is_contest_basicadmin)`), used on views and menu items (`contests/menu.py` registries).

**Problems.** `Problem` is the archive entity; `ProblemInstance` attaches it to a contest round with its own copy of `Test`s and limits (each problem also has a main instance without a contest). Uploads go through `problems/problem_sources.py` → a Celery `unpackmgr` job → a package backend (`sinolpack/package.py` `SinolPackage` for Sinol `.zip/.tgz` packages with `config.yml`, `in/`, `out/`, `prog/`, `doc/`, `attachments/`) → `post_upload_handlers`.

**Judging.** Submissions are evaluated by `evalmgr` running a *recipe* (list of handler dotted paths in an `environ` dict) built by `fill_evaluation_environ` on the contest and problem controllers (`programs/controllers.py`, `programs/handlers.py`). Compilation and test runs are dispatched to sioworkers; results become `SubmissionReport`/`TestReport`, scores are `ScoreValue` subclasses (`contests/scores.py`, stored as strings), then `update_user_results` updates `UserResultForProblem/Round/Contest`.

**Rankings** are computed asynchronously by the `rankingsd` daemon and cached per `(contest, full key)` in `rankings.models.Ranking`; controllers must invalidate them (`invalidate_pi`, `Ranking.invalidate_contest`). A *partial key* (in URLs) selects the ranking; the *full key* adds the viewer's permission level (`admin#c`, `regular#<round_id>`).

**Local app `czeciecamp`** (Czecie Camp Contest, for OI training camps): one contest = one camp, rounds are camp days, participants added by admins only, submissions allowed after a round ends. Rankings Dzienne+Stałe / Stałe / Dzienne / per round, computed from submissions in `CzecieCampRankingController.serialize_ranking`; freeze after the last non-trial round until unfrozen; per-camp settings in `CzecieCampConfig` (counting end, daily weight, round order). Admin UI is the "Camp panel" (`/c/<id>/camp/`) and the creation wizard (`/czeciecamp/create/`).

## Translations

UI strings use `gettext_lazy as _`; catalogs live in `oioioi/_locale/locale/<lang>/LC_MESSAGES/` and are managed through Transifex (see `CONTRIBUTING.rst`), not edited by hand except for correcting English msgids.
