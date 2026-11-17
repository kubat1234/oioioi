# Architecture of the Czecie fork of OIOIOI

A map for future work sessions (human or LLM). It describes the code as of the upstream merge of
2026-11-16 (`merge-upstream`, upstream `b935cbfc`). Short rules of the road are in `CLAUDE.md`; upstream's
generic agent guide is `AGENTS.md`. When this file and the code disagree, the code is right — fix the file.

## 1. What this is

OIOIOI is the Django web frontend of SIO2 (Polish Olympiad in Informatics, szkopul.edu.pl). This fork
(`kubat1234/oioioi`, branch `main`) runs one production instance for olympiad training camps: about
150 participants, several groups at once, **each group is its own contest** on the same instance.

Stack: Python 3.13, Django 5.2, PostgreSQL 14, Celery over RabbitMQ, Filetracker (file storage),
sioworkers + sio2jail (judging), webpack/pnpm for the frontend, uv for Python dependencies.

## 2. Repository layout

```
oioioi/                    ~75 Django apps, one per feature (see §3)
oioioi/default_settings.py defaults of every setting; production settings.py imports it
oioioi/test_settings.py    settings of the test suite (SQLite, every optional app enabled)
oioioi/deployment/         templates of the generated deployment dir (settings.py, supervisord.conf, …)
oioioi/_locale/            Transifex catalogs (locale/) and hand-made overrides (locale-overrides/)
Dockerfile                 image; target `base` is what production runs
docker-compose.yml         PRODUCTION setup of the camp server (fork-specific, replaced upstream's)
docker-compose-dev.yml     development setup (upstream's)
deploy.sh                  production update script (fork)
.github/workflows/czecie-ci.yml, release.yml   CI and releases of the fork
easy_toolbox.py            wrapper for the dev containers
test.sh, pytest.ini        test runner
rst/                       upstream's Sphinx docs
docs/                      fork docs (this file)
AGENTS.md                  upstream's guide for coding agents
CLAUDE.md                  fork's guide; overrides AGENTS.md where they differ
internal/                  local notes and reports, git-ignored
```

## 3. Apps

Always installed (`default_settings.INSTALLED_APPS`): `base`, `contests`, `problems`, `programs`,
`sinolpack`, `rankings`, `evalmgr`, `sioworkers`, `workers`, `filetracker`, `questions`, `quizzes`,
`interactive`, `status`, `su`, `clock`, `dashboard`, `analytics`, `celery`, `maintenancemode`, `_locale`.
Everything else is optional and switched on in the deployment's `settings.py`.

The ones that matter on a camp:

| App | Role |
|---|---|
| `base` | Users, login (2FA view, **e-mail login backend** — fork), preferences, menus, permissions machinery (`permissions.py`), admin site, shared templates and SCSS. |
| `contests` | `Contest`, `Round`, `ProblemInstance`, `Submission`, `SubmissionReport`, `UserResultFor*`; the `ContestController` base class; submit/list/rejudge views; REST API (`api.py`); current-contest middleware. |
| `problems` | `Problem`, `ProblemPackage`, statements, attachments, tags; upload pipeline (`problem_sources.py`, `unpackmgr`); problem site tabs (`problem_site.py`); problemset. |
| `sinolpack` | The package backend: reads a Sinol `.zip/.tgz` (`config.yml`, `in/`, `out/`, `prog/`, `doc/`) and creates tests, model solutions, statements. |
| `programs` | Programming problems: compilers and languages, `Test`, `TestReport`, the evaluation recipe (`controllers.py`) and its handlers (`handlers.py`), scorers (`utils.py`), report rendering. |
| `evalmgr` | Runs evaluation recipes as Celery jobs; `QueuedJob` state. |
| `sioworkers` / `workers` | Bridge to sioworkersd (sending jobs, receiving results) and the workers admin page. |
| `rankings` | `RankingController` base, the `Ranking`/`RankingPage` cache, the `rankingsd` daemon. |
| `participants` | `Participant` model and `ParticipantsController` (registration controller used by camps). |
| `disqualification` | Disqualifying a submission or a participant; camp rankings honour it. |
| `questions` | Questions and announcements, the unread-messages badge. |
| `contestexcl` | "Exclusive" contests (locks participants into a contest during a round). |
| **`czeciecamp`** | **Fork.** The camp contest type, its rankings and admin panel. §6. |
| **`llmdetect`** | **Fork.** Flags submissions that look pasted from an LLM. Has its own `README.rst`, `DESIGN.md`, `SETUP.md`. |
| **`problembase`** | **Fork.** Catalogue of all problems on the instance: groups re-uploaded packages into families, descriptions, who has seen what. |

### How apps plug in

- **URLs are autodiscovered.** `oioioi/urls.py` imports `views`, `controllers` and `urls` of every installed app.
  An app's `urls.py` may define `contest_patterns` (served under `/c/<contest_id>/`, `request.contest` is set),
  `urlpatterns` (both with and without the prefix) and `noncontest_patterns`. URL names have no namespace.
- **Controllers are the extension point.** `Contest.controller_name` is a dotted path to a `ContestController`
  subclass; every subclass is a selectable contest type (`RegisteredSubclassesBase`). `Problem.controller_name`
  likewise points to a `ProblemController`. A contest type is "just" a controller class overriding hooks.
- **Mixins.** `SomeController.mix_in(Mixin)` (`ObjectWithMixins`) adds behaviour to *every* controller of that
  class at import time (`testrun`, `scoresreveal`, `disqualification`, `llmdetect`, …). Admin classes take
  mixins the same way (`ProblemInstanceAdmin.mix_in`, used by `czeciecamp/admin.py`).
- **Permissions** are request conditions: `make_request_condition`, `enforce_condition(contest_exists & is_contest_basicadmin)`.
  The same conditions gate menu entries (`contest_admin_menu_registry`, `system_admin_menu_registry`).
- **Request context.** `CurrentContestMiddleware` sets `request.contest`; `request.timestamp` is "now"
  (admins can shift it with the clock tool, and tests use `fake_time`). Use `request.timestamp`, not
  `timezone.now()`, in anything a view decides.

## 4. The main flow: submission → judging → ranking

```
browser ── POST /c/<id>/submit/ ─► contests.views.submit_view
   │                                └─ SubmissionForm  ── controller.validate_submission_form
   │                                                     (language, size, limits; programs/controllers.py)
   │                                └─ controller.create_submission  → Submission + ProgramSubmission (source in Filetracker)
   │                                └─ controller.judge(submission)
   ▼
evalmgr  (Celery queue "evalmgr")
   environ = create_environ(); controller.fill_evaluation_environ(environ, submission)
   environ["recipe"] = ordered list of (name, "dotted.path.to.handler"):
       wait_for_submission_in_db
       compile → compile_end                                   ── job sent to sioworkersd, resumed on result
       collect_tests
       initial_run_tests → grade_tests → grade_groups → apply_subtask_dependencies → grade_submission → make_report (INITIAL)
       final_run_tests   → … same …                                               → make_report (NORMAL)
       [USER_OUTS / HIDDEN / FULL variants, depending on report kinds]
       delete_executable
       update_report_statuses → update_submission_score
       update_user_results  ─► controller.update_user_result_for_problem / _round / _contest
       call_submission_judged ─► controller.submission_judged ─► ranking_controller().invalidate_pi(pi)
   ▼
sioworkersd ──► worker (sio2jail / compilers from sandboxes stored in Filetracker)
   results come back through `receive_from_workers` and resume the evalmgr job
   ▼
rankingsd daemon
   picks invalidated `Ranking` rows, calls ranking_controller.serialize_ranking(key),
   renders pages (`_render_ranking_page`) and stores them in `RankingPage`
   ▼
browser ── GET /c/<id>/ranking/<partial_key>/ ─► rankings.views → cached page (or "being generated")
```

Things to know about each step:

- **Recipe.** Built in `ProgrammingProblemController.fill_evaluation_environ` / `generate_recipe`
  (`oioioi/programs/controllers.py`) and wrapped by the contest controller (`contests/controllers.py`).
  Other apps insert handlers with `add_before_placeholder` / `extend_after_placeholder` (`evalmgr/tasks.py`).
  Camps set `environ["test_scorer"]` to `threshold_linear_test_scorer`.
- **Reports.** A submission may have many `SubmissionReport`s; one per kind is `ACTIVE`. Kinds: `INITIAL`
  (example tests), `NORMAL` (all tests), `USER_OUTS`, `HIDDEN`, `FULL`, `FAILURE` (system error). Scores are
  `ScoreValue` subclasses serialized to strings (`contests/scores.py`).
- **Partial rejudge (fork).** `collect_tests` understands `extra_args["rejudge_type"]`: `FULL`, `NEW`
  (only tests the submission has no result for) and `SELECTED` (chosen tests; the others keep their results).
  UI: `contests/forms.py::RejudgeOptionsForm`, `contests/templates/contests/rejudge_options.html`.
- **Who sees a result.** `results_date` of the round controls personal results, `public_results_date`
  controls rankings when `separate_public_results()` is true (it is for camps). Controllers decide through
  `can_see_submission_score`, `results_visible`, `filter_my_visible_submissions`.
- **User results.** `UserResultForProblem` is what the problems list shows. The default takes the *latest*
  scored submission; a camp takes the *best* one (`CzecieCampContestController.update_user_result_for_problem`).
  Camp **rankings do not read `UserResultForProblem`** — they recompute from submissions (§6).
- **Ranking cache.** Key = partial key (which ranking; part of the URL) + permission level of the viewer
  (`admin`, `observer`, `regular`) + anything a controller appends. A ranking is recomputed only after it is
  invalidated: `Ranking.invalidate_contest(contest)` or `invalidate_pi`. Forgetting to invalidate after
  changing something a ranking depends on is the classic bug here. In tests `MOCK_RANKINGSD = True` makes
  the view serialize synchronously.

### Importing a problem package

```
upload form (problems/problem_sources.py: UploadedPackageSource)
   → ProblemPackage row + file in Filetracker
   → Celery queue "unpackmgr": problems/unpackmgr.py
   → backend chosen by `PROBLEM_PACKAGE_BACKENDS` → sinolpack/package.py: SinolPackage.unpack
        config.yml → limits, scores, extra files;  in/ out/ → Test rows;  prog/ → model solutions, checker;
        doc/ → statements;  attachments/
   → post_upload_handlers (e.g. attach to the round, judge model solutions)
```

Each `Problem` has a "main" `ProblemInstance` without a contest; adding the problem to a contest copies its
tests into a new `ProblemInstance` (so limits can differ per contest). Re-uploading a package sets
`needs_rejudge` on the affected instances (`problems/problem_sources.py`, `problems/handlers.py`); the
contest admin then sees a "rejudge needed" notice.

Fork additions here: the *Package files* tab of the problem site edits files inside a package as a tree and
re-uploads it (`problems/problem_site.py::problem_site_package_download_file`, `problems/forms.py::PackageChangesForm`,
`problems/static/problems/package-files.js`); in camps a problem may be uploaded without a round
(`allow_problems_without_round`).

## 5. Processes in production

One image (`ghcr.io/kubat1234/oioioi:<tag>`, Dockerfile target `base`), four services in `docker-compose.yml`:

| Service | What runs |
|---|---|
| `db` | PostgreSQL 14.17, data in `$SIO_DATA_DIR/postgres`. |
| `broker` | RabbitMQ 3.8. |
| `migrate` | One-shot: `manage.py migrate`. `web` starts only after it succeeds. |
| `web` | `oioioi_init.sh` → `manage.py supervisor`: uwsgi, `rankingsd`, `evalmgr`, `unpackmgr`, `sioworkersd`, `receive_from_workers`, Filetracker server, `mailnotifyd`, notifications server (see `oioioi/deployment/supervisord.conf.template`). |
| `worker` | `worker_init.sh`: an sioworkers worker with sio2jail (needs `cap_add: ALL`, unconfined AppArmor/seccomp). |

- `settings.py` is **not in the image**: it lives on the host in `$SIO_DATA_DIR/settings.py` and is
  bind-mounted. It starts with `from oioioi.default_settings import *`, so a change of a default in
  `default_settings.py` reaches production at the next deploy unless that file overrides it.
- Sandboxes (compilers, sio2jail) are **not in the image** either; they are files in Filetracker
  (`$SIO_DATA_DIR/filetracker`). A compiler named in `DEFAULT_COMPILERS` must have its sandbox uploaded.
- Ports 7888 (sioworkersd) and 9999 (Filetracker) are published on `SIO_WORKERS_BIND` for workers on other
  machines. Both are unauthenticated.
- Updating: `./deploy.sh <version>` only (refuses during a running round, backs up the DB, pulls, runs
  `migrate`, waits for health). Releases are made by the *Release* workflow, which tags the image CI already
  built for the current `main` commit as `vYYYY.MM.DD[.N]` and `latest`.

## 6. `czeciecamp` — the camp contest type

One contest = one camp of one group. Rounds are camp days. Participants are added by admins only.
Submitting stays open after a round ends.

**Files:** `controllers.py` (contest + ranking controllers, the scoring rules), `models.py`
(`CzecieCampConfig`, `CampRanking`, `CampProblemSettings`), `views.py` + `templates/czeciecamp/panel.html`
(the "Czecie panel" at `/c/<id>/camp/` and the creation wizard at `/czeciecamp/create/`), `forms.py`,
`rejudge_report.py` (what the latest rejudge of a problem changed), `admin.py` (mixin on the problem
instance admin), `tests.py`.

**Vocabulary** (the UI is Polish; these two names are never translated):

- **Dzienne** ("daily") — points from submissions sent *while the round was running* (`is_daily_submission`).
- **Stałe** ("all-time") — points from all submissions up to the counting end.
- An **"All-time" round** is stored as `Round.is_trial = True`: it has no Dzienne points at all.

**Rules, and where they live:**

| Rule | Code |
|---|---|
| A ranking = `daily_weight × Dzienne + stale_weight × Stałe` over a set of rounds; weights may be negative; several rankings per camp, one auto-created per round | `CampRanking`, `create_default_rankings`, `create_round_ranking` (post_save of `Round`) |
| Which submission counts: best (earliest of equals), or the last one for Dzienne if configured | `best_submissions` |
| Submissions before the round start, after `counting_end_date`, or (for participants) after the freeze are ignored | `best_submissions`, `serialize_ranking` |
| Freeze: participants' rankings stop at `freeze_date` (default: end of the last non-trial round) until an admin unfreezes; they see *that* someone submitted later, not the points | `is_frozen`, `get_freeze_date`, `_count_after_freeze` |
| A round appears in participants' rankings once `public_results_date` has passed; personal results follow `results_date` (or are immediate with `results_visible_during_round`) | `round_in_rankings`, `results_visible`, `_visible_rounds` |
| While any round is running, participants see only the running rounds | `can_see_round` |
| Hidden participants (`hidden_users`) and disqualified ones are left out for participants and shown greyed, without a place, to admins; a disqualified submission zeroes its whole problem | `serialize_ranking`, `_disqualifications` |
| Two submission limits: the problem's usual one for the whole camp, and a separate one for submissions sent during the round; compilation errors use up neither | `_binding_limit`, `_counted_submissions`, `get_round_submissions_limit` |
| Problems list shows the best result, no status badge, and a gold frame for full points scored during the round | `update_user_result_for_problem`, `show_last_submission_status`, `problems_list_result_badge_class` |
| Scoring of a test: threshold-linear in time | `fill_evaluation_environ` → `threshold_linear_test_scorer` |

**Ranking keys.** `CzecieCampRankingController.get_full_key` appends a *state tag* (ids of the rounds
published in rankings, plus `f` when frozen; `all` for admins/observers) and the viewer's language, because
the HTML is rendered once per key. So a scheduled publication or the freeze changes the key and yields a
fresh ranking without anybody invalidating it. Everything else that changes a ranking (settings, rounds,
participants, hidden users, disqualifications) calls `Ranking.invalidate_contest` — every POST handler in
`views.py` does; keep that up when adding one.

**Several groups.** Groups are separate contests, so isolation between them is the ordinary contest
isolation: `request.contest`, `can_enter_contest` (participants only), `visible_contests`. Anything that
looks an object up by id must also check it belongs to `request.contest`.

## 7. What the fork changes outside its own apps

Kept deliberately small, because every such line is a future merge conflict. Current list
(`git diff --stat origin/master...main -- . ':!oioioi/czeciecamp' ':!oioioi/llmdetect' ':!oioioi/problembase'`
gives the authoritative one):

| Area | Change | Files |
|---|---|---|
| Login | Log in with username **or e-mail** (case-insensitive; Gmail dots and `+tag` ignored); e-mails must be unique across accounts | `base/auth.py`, `base/utils/user.py`, `base/forms.py`, `base/views.py`, `default_settings.py` (`AUTHENTICATION_BACKENDS`) |
| Contest hooks | `problems_list_result_badge_class`, `show_last_submission_status`, `problem_rejudge_started`; score-coloured status badges | `contests/controllers.py`, `contests/views.py`, `contests/templates/contests/problems_list.html` |
| Rejudge | Rejudge chosen tests / only new tests, for one submission, many, or a whole problem; partial rejudges keep other results | `contests/forms.py`, `contests/views.py`, `contests/admin.py`, `programs/handlers.py`, `contests/static/common/rejudge.js` |
| Languages | Per-contest allowed languages (`ContestLanguagesConfig`, migration `programs.0024`); a single allowed language is preselected; submissions in no-longer-allowed languages are still judged | `programs/models.py`, `programs/forms.py`, `programs/admin.py`, `programs/controllers.py`, `programs/problem_instance_utils.py` |
| API | Problems and rounds the user may not see are hidden | `contests/api.py` |
| Package files | Tree view with batched changes | `problems/problem_site.py`, `problems/forms.py`, `base/utils/archive.py` |
| Problems without a round | Allowed when the controller says so | `contests/forms.py`, `problems/processors.py` |
| Rankings | Invalidated when a participant is added/removed | `participants/models.py`, `participants/admin.py` |
| UI | Side menu remembers open sections; unread-messages badge refreshes itself; webpack bundles get `?v=<mtime>` (`bundle_static`); `TASK_ARCHIVE_LINK_VISIBLE` (default off) | `base/static/js/menu-setup.js`, `questions/…/navbar_messages.js`, `base/templatetags/simple_tags.py`, `base/navbar_links.py` |
| Translations | Polish strings for fork changes in upstream apps | `oioioi/_locale/locale-overrides/pl/` (fork apps carry their own `locale/`) |
| Infra | Production compose, `deploy.sh`, `.env.example`, fork CI and Release workflows, Dockerfile layer order | repo root, `.github/workflows/` |

PR #23 (the upstream merge of 2026-11) lists where these met upstream changes and the open decisions.

## 8. Pitfalls

1. **Squash-merging a PR that merges upstream loses the merge.** `main` uses "Squash and merge", but an
   upstream merge must land as a real merge commit, or the next upstream merge conflicts on everything again.
2. **`default_settings.py` is production configuration.** Upstream changes to defaults (compilers,
   pagination app name, Celery options) take effect at the next deploy. Read the diff of that file on
   every upstream merge.
3. **Tests run on SQLite, production on PostgreSQL.** NUL bytes in text, regex lookups (`__iregex`),
   `select_for_update`, JSON field queries and case sensitivity behave differently. Anything touching those
   deserves a look at the SQL, or a run against Postgres.
4. **Warnings are errors in tests** (`pytest.ini`): a `DeprecationWarning` or naive datetime from oioioi code
   fails the test.
5. **`--nomigrations` is the default; CI adds `--migrations`.** A missing migration only shows up with the
   CI flags or `makemigrations --check`.
6. **Rankings are cached.** Change what a ranking depends on → invalidate it. And `serialize_ranking` runs
   in `rankingsd`, without a request: no `request.user`, no `request.timestamp`.
7. **`request.timestamp` vs `timezone.now()`.** Views and controllers use the former (it honours the admin
   clock and `fake_time`); `serialize_ranking` and Celery handlers have only the latter.
8. **Admin form tests.** POSTing a Django admin form in a test must include every editable field; upstream
   adding a model field (e.g. `ProblemInstance.execution_mode`) breaks such tests, not the page.
9. **Mixins apply at import time and globally.** A mixin on `ContestController` affects every contest type
   on the instance, not just camps. Guard with `isinstance(contest.controller, CzecieCampContestController)`
   (see `czeciecamp/admin.py::_is_camp`).
10. **Deleting a round** does not delete its problems: `ProblemInstance.round` becomes `NULL`
    (`on_delete=SET_NULL`). In a camp such problems are hidden from participants (only admins can submit),
    and their submissions drop out of every ranking; the round's own `CampRanking` is deleted (CASCADE).
11. **Objects by id.** `get_object_or_404(Model, id=…)` without `contest=request.contest` lets an admin of
    one group act on another group's object. Upstream fixed several of these in #968; keep the habit.
12. **Translations.** Fork strings in upstream apps go to `locale-overrides`, never to `_locale/locale`
    (Transifex overwrites it). Compile `.mo` files and commit them.
13. **Frontend.** Bootstrap 5 only (`data-bs-*`, `text-bg-*`, `w-100`, no `form-group`/`btn-block`).
    Upstream's `smoke.yml` compiles every SCSS file and opens key pages in a browser.
14. **Remote workers** must run the same sioworkers version as the server.
