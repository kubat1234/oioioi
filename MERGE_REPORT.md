# MERGE_REPORT — upstream `master` → fork (`merge-upstream`)

Data: 2026-11-16. Gałąź `merge-upstream` wychodzi z `fork/main` (`5a36d8d4`), **nie** z lokalnego `main`
(lokalny `main` = `1f5d88ca` był o 1 commit za `fork/main`; nie ruszałem go).

| | |
|---|---|
| Baza (merge-base) | `da42499c` (2026-10-05, „Override s3dedup healthcheck…”, #816) |
| Moja strona | `fork/main` `5a36d8d4` — 89 commitów (bez merge'ów), 187 plików |
| Upstream | `origin/master` `b935cbfc` — 90 commitów, 190 plików |
| Commit merge'a | `60e1d4c7` |
| Poprawki po merge'u | `ab358d18`, `a426def6`, `af27fae9` (osobne commity, wszystkie tylko w testach) |
| Pliki zmienione przez obie strony | 31, z czego 11 z konfliktem tekstowym |

Nic nie zostało wypchnięte. Lokalny katalog `oioioi/` (gałąź `ci-faster-build` z niezacommitowaną zmianą
w `docker-compose-dev.yml`) jest nietknięty — praca odbywa się w worktree `/home/kubat/sio2/oioioi-wt`.

## 1. DO DECYZJI

Rzeczy, których nie rozstrzygałem sam. W każdej zostawiłem wariant zachowawczy (Twoja wersja albo
niezmieniony upstream) i opisałem alternatywę.

### D1. Domyślne kompilatory zmieniają się na GCC 14 (największe ryzyko wdrożeniowe)

- **Upstream** (`0b3bd1af`, #927) zmienił w `oioioi/default_settings.py`
  `DEFAULT_COMPILERS`: C `gcc4_8_2_c99` → `gcc14_2_0_c17`, C++ `g++4_8_2_cpp11` → `g++14_2_cpp23_amd64`
  (i dodał je do `AVAILABLE_COMPILERS`).
- **Fork** tego miejsca nie zmieniał, więc merge przeszedł automatycznie i nie ma czego „zachowywać”.
  Twoje `deployment/settings.py` (lokalne dev) **nie nadpisuje** `DEFAULT_COMPILERS`
  (linia jest zakomentowana), więc zakładam, że produkcyjne też nie.
- **Skutek po wdrożeniu:** każdy konkurs bez własnego `ContestCompiler`/`ProblemCompiler` zacznie kompilować
  C++ jako `g++ 14.2 -std=c++23` zamiast `g++ 4.8.2 -std=c++11`. Wymaga to sandboksa `compiler-gcc.14_2_0`
  w filetrackerze produkcji (jest w oficjalnym Manifeście, ale Twój obraz nie ma wbudowanych sandboksów —
  trzeba go wgrać ręcznie, patrz komentarz na górze `docker-compose.yml`). Bez sandboksa wszystkie zgłoszenia
  C/C++ skończą się błędem systemowym. Zmienia się też zachowanie rozwiązań (inne UB, inne czasy), więc
  rejudge starych obozów da inne wyniki niż wtedy.
- **Zostawiłem:** wersję upstreamu (nie cofam po cichu cudzej zmiany).
- **Do wyboru:**
  1. zaakceptować GCC 14 → przed deployem wgrać sandbox i przetestować jedno zgłoszenie;
  2. przypiąć stare w produkcyjnym `settings.py`:
     `DEFAULT_COMPILERS = {'C': 'gcc4_8_2_c99', 'C++': 'g++4_8_2_cpp11', 'Pascal': 'fpc2_6_2', 'Java': 'java1_8', 'Python': 'python', 'Output-only': 'output-only'}`;
  3. przypiąć stare w `default_settings.py` forka (trwała różnica względem upstreamu).
- Jeśli produkcja ma już własne `DEFAULT_COMPILERS` w `settings.py`, punkt jest nieaktualny — sprawdź to
  przed deployem: `grep -n COMPILERS $SIO_DATA_DIR/settings.py`.

### D2. NUL w komentarzach checkera: usuwać (fork) czy zamieniać na U+FFFD (upstream)

- **Fork** (`24d50c29`): `oioioi/programs/handlers.py::make_report` i `oioioi/testrun/handlers.py::make_report`
  robią `.replace("\x00", "")`.
- **Upstream** (`b4190e3c`, #933): nowa funkcja `sanitize_worker_text` zamienia NUL **i samotne surogaty**
  na U+FFFD, także w wyjściu kompilatora.
- Git połączył to bez konfliktu: najpierw Twoje usunięcie, potem `sanitize_worker_text`. Efekt: w komentarzu
  checkera NUL znika (Twoje zachowanie), surogaty i wyjście kompilatora dostają U+FFFD (upstream).
  To jedyne miejsce, gdzie intencje się naprawdę wykluczają, więc **zostało Twoje**, a test upstreamu
  dostosowałem (`ab358d18`).
- **Rekomendacja:** usunąć oba `.replace("\x00", "")` z forka i przyjąć zachowanie upstreamu. Wtedy znikają dwie
  różnice względem upstreamu, a w Twoich dwóch testach NUL (`programs/tests.py` ok. linii 2805,
  `testrun/tests.py` ok. linii 336) wystarczy zmienić oczekiwane `<0x5>` na `<0x\ufffd5>`. Nie zrobiłem tego,
  bo zmienia widoczny wynik.

### D3. `CLAUDE.md`: plik forka czy symlink do `AGENTS.md`

- **Upstream** (`7ebfd52a`, #855) dodał `AGENTS.md` i `CLAUDE.md` jako **symlink** do niego.
- **Fork** (`7d66120d` i późniejsze) ma własny, zwykły plik `CLAUDE.md` z zasadami forka.
- Konflikt typu pliku. **Zostawiłem Twój `CLAUDE.md` jako zwykły plik**, `AGENTS.md` wszedł bez zmian.
  W etapie 2 `CLAUDE.md` odsyła do `AGENTS.md` po zasady ogólne upstreamu, żeby nie dublować treści.
- Uwaga: `AGENTS.md` zawiera reguły upstreamu (m.in. o PR-ach do `master`), które miejscami różnią się od
  Twojego workflow (`main`, squash merge). `CLAUDE.md` ma pierwszeństwo — jest to tam zapisane.

### D4. Nowe workflowy i Dependabot z upstreamu

Upstream dodał `.github/workflows/smoke.yml` (testy przeglądarkowe + kompilacja SCSS, uruchamiane na każdym
`pull_request`), `copilot-setup-steps.yml`, `.github/dependabot.yml`, `pull_request_template.md` i zmienił
`CODEOWNERS`. Komentarz w `czecie-ci.yml` mówi, że upstreamowe `pytest.yml`/`ruff.yml`/`build.yml` są wyłączone
w ustawieniach Actions forka — **nowych plików to wyłączenie nie obejmuje**:

- `smoke.yml` zacznie się uruchamiać na PR-ach w forku (równolegle z Twoim CI);
- `dependabot.yml` może zacząć otwierać PR-y z podbiciami zależności, jeśli Dependabot jest włączony w repo;
- `CODEOWNERS` wskazuje ludzi z upstreamu, a szablon PR opisuje zasady upstreamu.

Niczego nie usuwałem (zakaz zmian „przy okazji”). Do decyzji: wyłączyć w ustawieniach Actions / usunąć
z forka / zostawić. `smoke.yml` jest akurat wartościowy — sprawdza m.in., czy kompiluje się SCSS `czeciecamp`.

### D5. Lokalny `main` jest za `fork/main`

Polecenie mówiło „gałąź merge-upstream od main”. Lokalny `main` (`1f5d88ca`) nie zawierał `5a36d8d4`
(„Faster image builds”, PR #22), który jest już na GitHubie. Wyszedłem z `fork/main`, bo merge na starszym
commicie musiałby zostać powtórzony. Lokalnego `main` nie przesuwałem.

## 2. Konflikty tekstowe (11 plików)

### 2.1 `.dockerignore`

- **Ja:** dodane `/.env` (`6b26de3c`, produkcyjny compose czyta sekrety z `.env`).
- **Upstream:** dodane `/oioioi_cypress/node_modules/`, blok „AI agents” (`/internal/`, `**/CLAUDE.local.md`,
  `/.claude/settings.local.json`), na końcu `/AGENTS.md`, `/CLAUDE.md`, `/.claude/`.
- **Rozwiązanie:** suma obu (obie strony dopisywały wiersze w tym samym miejscu).
- **Ryzyko:** brak. `CLAUDE.md`/`AGENTS.md` nie trafiają do obrazu — zgodne z intencją obu stron.

### 2.2 `.gitignore`

- **Ja:** `/.env`. **Upstream:** `/runserver.log`, `/schools/`, blok „AI agents”.
- **Rozwiązanie:** suma. Efekt uboczny na plus: Twój nieśledzony katalog `schools/` przestanie się pokazywać
  w `git status`.
- **Ryzyko:** brak.

### 2.3 `CLAUDE.md` (konflikt typu: plik vs symlink)

Patrz **D3**. Zostaje plik forka; `AGENTS.md` z upstreamu dodany bez zmian.

### 2.4 `oioioi/base/forms.py`

- **Ja** (`8a7190f6`, `e6e2c14e`): import `AuthenticationForm` i `users_with_email`; `validate_unique_email`;
  `clean_email` w `RegistrationFormWithNames`, `UserForm`, `OioioiUserChangeForm`;
  `UsernameOrEmailAuthenticationForm`.
- **Upstream** (`a4a161fb`, #765): preferowany język programowania — import
  `get_preferable_programming_languages`, pole `programming_language` w `PreferencesFactory`,
  obsługa w `handle_new_preference_fields`.
- **Konflikt:** tylko blok importów (obie strony zmieniły sąsiednie linie).
- **Rozwiązanie:** oba importy; reszta połączyła się automatycznie.
- **Ryzyko:** niskie. Zmiany dotyczą innych formularzy/pól. Styk pośredni: upstream przeniósł w
  `base/views.py::edit_profile_view` wywołanie `handle_new_preference_fields` **za** `form.is_valid()` —
  czyli gdy Twoje `clean_email` odrzuci e-mail, preferencje też się nie zapiszą (to poprawne zachowanie).

### 2.5 `oioioi/contests/api.py`

- **Ja** (`3332ecdf`): ukrywanie w API zadań i rund, których użytkownik nie widzi —
  `get_visible_problem_instance_or_404`, `visible_problem_instances(request)` w `GetContestProblems`,
  filtr `can_see_round` w `GetContestRounds`, helper użyty w `GetUserProblemSubmissionList` i `GetProblemIdView`.
- **Upstream** (`9f74bcf0`, #968): ta sama luka załatana niezależnie (`can_see_round`, `can_see_problem`
  w `GetContestProblems` i `GetProblemIdView`) **oraz** rzecz, której u Ciebie nie było:
  `CanEnterContest`/`UnsafeApiAllowed` przeniesione z `has_object_permission` (które DRF w `APIView` w ogóle
  nie wywoływał) do `has_permission`, z ustawieniem `request.contest` dla użytkownika z tokenu;
  `settings.MIDDLEWARE` zamiast importu z `default_settings`.
- **Rozwiązanie:**
  - `GetContestRounds`: kod identyczny po obu stronach — zostaje z Twoim komentarzem.
  - `GetProblemIdView`: Twój helper (robi to samo co 3 linie upstreamu, dodatkowo z komunikatem 404).
  - `GetContestProblems`: jest **i** Twoje `visible_problem_instances(request)`, **i** linia upstreamu
    `[pi for pi in problem_instances if controller.can_see_problem(...)]` (weszła automatycznie).
    Druga jest redundantna; zostawiłem ją celowo, żeby przyszłe merge'e tego fragmentu się nie kłóciły.
    Koszt: drugie wywołanie `can_see_problem` na zadanie.
  - `GetUserProblemSubmissionList`: Twój helper — upstream tego widoku **nie** zabezpieczył, więc tu fork
    jest szczelniejszy.
  - `CanEnterContest`/`UnsafeApiAllowed`: wersja upstreamu (u Ciebie bez zmian).
- **Ryzyko:** średnie–niskie. Zmienia się realne egzekwowanie uprawnień w API: dotąd `CanEnterContest` nie
  działało wcale, teraz użytkownik spoza konkursu dostanie 403 z endpointów `/api/c/<id>/…`. Dla obozu to
  pożądane (uczestnik jednej grupy nie czyta przez API zadań innej), ale jeśli masz skrypty używające tokenu
  konta, które nie jest uczestnikiem/adminem konkursu, przestaną działać.

### 2.6 `oioioi/contests/controllers.py`

- **Ja** (`bd53d3e1`, `7324575e`, `6b8fafeb`, `6801cb0f`, `8255daec`): poprawka literówki `text=bg-success`,
  domyślna klasa `text-bg-secondary`, `get_score_badge_class`, `get_submission_badge_class`, użycie jej
  w `submission_template_context`, nowe hooki kontrolera `problems_list_result_badge_class`,
  `show_last_submission_status`, `problem_rejudge_started`.
- **Upstream** (`af22c746`, #875): te same dwie poprawki klas Bootstrapa (`TESTRUN_OK`, `text-bg-secondary`).
- **Rozwiązanie:** Twoja wersja (nadzbiór). Linie upstreamu są w niej zawarte co do znaku.
- **Ryzyko:** brak.

### 2.7 `oioioi/contests/templates/contests/confirm_rejudge.html`

- **Ja** (`9d413366`): jeden formularz zamiast dwóch zagnieżdżonych, błędy formularza, `rejudge_options.html`
  (wybór testów / typu rejudge'a), usunięte ukryte pola drugiego formularza.
- **Upstream** (`af22c746`): `class="mb-3 form-group"` → `class="mb-3"` w dwóch miejscach (sprzątanie po
  Bootstrap 4).
- **Rozwiązanie:** Twoja struktura + sprzątanie upstreamu w obu `div`-ach z przyciskami. Drugi `<form>`
  (który upstream zostawił) nie wraca.
- **Ryzyko:** niskie; czysto wizualne. Testy `TestRejudgeView`/`TestRejudgeTypesView` przechodzą.

### 2.8 `oioioi/contests/tests/tests.py`

- **Ja:** testy kolorów plakietek, filtrów, rejudge'a z opcjami, API ukrywającego zadania/rundy.
- **Upstream:** `needs_linux`, test `STATUS_CLASSES`, testy poprawek bezpieczeństwa (#968), editoriali,
  `execution_mode`.
- **Konflikt:** jedna linia importu z `oioioi.contests.controllers`.
- **Rozwiązanie:** suma nazw (`STATUS_CLASSES, get_badge_class, get_score_badge_class, get_submission_badge_class`).
- **Ryzyko:** brak. Upstream usunął w `TestAPIGetProblemId` kilka linii (hunk `@@ -4282`) — weszło automatycznie.

### 2.9 `oioioi/problems/views.py`

- **Ja** (`1546d687`, `7339fe56`): `filter_problems_by_name` (wyszukiwanie po nazwie także przy ukrytych
  tagach); w `download_problem_package_file_view` otwieranie pliku jako `"rb"` (pliki binarne w paczce).
- **Upstream** (`9f74bcf0`): `_get_submission_of_current_contest` (raporty zgłoszeń tylko z bieżącego konkursu),
  `get_last_submissions` ograniczone do bieżącego konkursu, w `download_problem_package_file_view` blokada
  path traversal (`realpath`) i `raise Http404`, gdy pliku nie ma.
- **Konflikt:** dwie sąsiednie linie w `download_problem_package_file_view`.
- **Rozwiązanie:** `open(filepath, "rb")` (Ty) + sprawdzenie `realpath` i końcowe `Http404` (upstream).
- **Ryzyko:** niskie. Twój nowy widok plików paczki (`package-files.js`) buduje URL z `download_url` i ścieżki
  z archiwum — ścieżki z archiwum są zawsze wewnątrz katalogu, więc blokada upstreamu ich nie dotyka.

### 2.10 `oioioi/programs/controllers.py`

- **Ja** (`907e75bc`, `2990a55f`, `3f2ffe17`): gdy dozwolony jest jeden język — brak pustej opcji, język
  wstępnie wybrany i uzupełniany przy walidacji; ocenianie zgłoszeń w językach, których konkurs już nie
  dopuszcza (`get_compiler_for_submission`).
- **Upstream** (`a4a161fb` #765, `21400f13` #916): preferowany język użytkownika jako `initial`;
  przy wysłanym pliku język **zawsze** z rozszerzenia; refaktor raportu (`get_signal_from_comment`,
  `report_test_detail_flags`, `report_dom_id`, `has_test_details`).
- **Konflikt:** początek `_add_langs_to_form` (obie strony przepisały pętlę po językach).
- **Rozwiązanie:** jedna zmienna `allowed_languages` (słownik, jak w upstreamie) + `allowed_langs` (lista, jak
  u Ciebie). Kolejność ustalania `initial`: najpierw Twoje (jedyny język), potem upstream nadpisuje preferowanym,
  **jeśli jest dozwolony**. Przy jednym dozwolonym języku preferowany albo jest nim, albo nie jest dozwolony,
  więc Twoje zachowanie zawsze wygrywa; przy wielu — działa preferencja upstreamu.
- **Styk semantyczny (bez konfliktu tekstowego):** `validate_submission_form` — upstream zmienił
  `if not lang and is_file_chosen` na `if is_file_chosen`, bo wstępnie wybrany język nie może decydować
  o pliku. To dobrze współgra z Twoją preselekcją: plik `.py` przy wstępnie wybranym C++ zostanie rozpoznany
  po rozszerzeniu (i odrzucony, jeśli Python nie jest dozwolony).
- **Ryzyko:** średnie–niskie — to ścieżka wysyłania zgłoszeń. Pokryte testami obu stron
  (`test_single_allowed_language_is_preselected`, `test_language_required_when_several_allowed`,
  `test_disallowed_preferred_language_not_selected` i testy preferencji upstreamu).

### 2.11 `oioioi/programs/tests.py`

- **Ja:** testy języków konkursu, preselekcji, częściowych rejudge'y, NUL.
- **Upstream:** testy preferowanego języka, `sanitize_worker_text`, raportu, `needs_linux`; usunięty import
  `memoized_property`.
- **Konflikty:** blok importów (suma: `ContestLanguagesConfigForm`, `grade_tests`, `CompilationReport`,
  `ContestLanguagesConfig`) i koniec klasy `TestAllowedLanguages` (obie strony dopisały testy w tym samym
  miejscu — zostały wszystkie: 5 Twoich, potem 1 upstreamu).
- **Poprawki po merge'u (osobne commity):**
  - `a426def6` — `test_disallowed_preferred_language_not_selected` (upstream) dopuszczał tylko C i oczekiwał
    braku wybranego języka; u Ciebie jedyny język jest zawsze wybrany. Test dopuszcza teraz C i C++ i nadal
    sprawdza to, o co mu chodzi.
  - `ab358d18` — `TestMakeReportSanitizesWorkerText.test_checker_comment` oczekuje `"badoutput�"`
    zamiast `"bad�output�"` (patrz **D2**).
- **Ryzyko:** brak (testy).

## 3. Pliki zmienione przez obie strony, zmergowane automatycznie (20 plików)

Każdy przejrzałem ręcznie po obu stronach.

| Plik | Ja | Upstream | Wynik / ryzyko |
|---|---|---|---|
| `Dockerfile` | Warstwy zależności przed `COPY . ./` (`uv sync --no-install-project`, osobne `COPY` plików pnpm), `5a36d8d4` | Node 24 z obrazu `node:24-trixie-slim` zamiast `nodejs`/`npm` z Debiana, `pnpm@12` zamiast `pnpm@9` | Zmiany w różnych częściach pliku. **Ryzyko średnie:** pnpm 12 z Twoim wcześniejszym `COPY package.json pnpm-lock.yaml pnpm-workspace.yaml` + `oioioi_cypress/package.json` — `pnpm-workspace.yaml` ma teraz `allowBuilds`/`minimumReleaseAge`, innych plików konfiguracyjnych pnpm w repo nie ma, więc powinno działać, ale **obrazu nie zbudowałem** (patrz §6). Zbuduje go CI na PR. |
| `README.rst` | Nagłówek forka na górze | Wzmianka o `needs_linux` i macOS, usunięta lista mailingowa | Różne miejsca. Brak ryzyka. Link „full diff vs upstream” w Twoim nagłówku nadal wskazuje gałąź `czecie-camp` (przemianowaną na `main`) — nie ruszałem. |
| `oioioi/base/admin.py` | `ModelAdmin.response_add` wraca do `came_from` | `OioioiAutocompleteJsonView` (pełne imię w podpowiedziach użytkowników), `AdminSite.autocomplete_view` | Niezależne. Brak ryzyka. |
| `oioioi/base/templates/ingredients/menu.html` | `data-menu-key` (zapamiętywanie rozwinięcia każdej sekcji z osobna, `6acbeefb`) | `data-parent` → `data-bs-parent` | Sąsiednie linie, oba atrybuty są. **Możliwa zmiana zachowania, nie sprawdzona w przeglądarce:** stary atrybut `data-parent` był w Bootstrap 5 ignorowany, więc sekcje otwierały się niezależnie — na tym opiera się Twoje `menu-setup.js` (pamięta stan każdej sekcji). Z działającym `data-bs-parent` Bootstrap przy otwarciu jednej sekcji zwija pozostałe (akordeon). Stan nadal się zapamiętuje (zdarzenie `hidden.bs.collapse` też jest zapisywane), ale kilku sekcji naraz nie da się już trzymać otwartych klikaniem. Jeśli wolisz dotychczasowe zachowanie, wystarczy usunąć `data-bs-parent` z tego szablonu — zostawiłem wersję upstreamu, bo to jego poprawka. |
| `oioioi/base/templates/ingredients/navbar-user.html` | Placeholder „Username or email” | `btn-block` → `w-100` | Niezależne. Brak ryzyka. |
| `oioioi/base/views.py` | `LoginView` z `UsernameOrEmailAuthenticationForm` | `handle_new_preference_fields` dopiero po `form.is_valid()` | Niezależne funkcje. Brak ryzyka. |
| `oioioi/contests/admin.py` | Rejudge wybranych zgłoszeń bez wymogu aktywnych raportów; usunięty import `SubmissionReport` | Pole `execution_mode` w `ProblemInstanceAdmin` (tylko superuser może zmieniać) | Niezależne. Brak ryzyka. |
| `oioioi/contests/views.py` | Lista zadań: `show_last_submission_status`, `extra_badge_class`; rejudge zadania z `RejudgeOptionsForm` i `problem_rejudge_started` | Editoriale (`_can_access_editorial`), `contest=request.contest` w `get_object_or_404` dla rejudge'a / `needs_rejudge` / resetu limitów | W `rejudge_all_submissions_for_problem_view` zmiany w sąsiednich liniach — obie obecne: instancja zadania musi należeć do bieżącego konkursu (upstream), potem Twój formularz. **Ryzyko niskie**, pokryte `TestRejudgeView`. |
| `oioioi/default_settings.py` | `TASK_ARCHIVE_LINK_VISIBLE`, `EmailAuthBackend` | `dj_pagination` → `pagination`, `PAGINATION_DEFAULT_WINDOW`, nowe kompilatory i **`DEFAULT_COMPILERS`**, Celery bez result backendu, kolejki exclusive, `TASK_PUBLISH_CONFIRM_TIMEOUT` | Różne miejsca. **Ryzyko wysokie wdrożeniowo** — patrz **D1** i §5. |
| `oioioi/deployment/settings.py.template` | Wpisy `TASK_ARCHIVE_LINK_VISIBLE`, middleware llmdetect, aplikacje forka | `INTERNAL_IPS` dla debug-toolbar w Dockerze, paginacja, kompilatory, kolejki quorum; **zmiana trybu pliku 755 → 644** | Różne miejsca. `INSTALLATION_CONFIG_VERSION` zostaje 49 — istniejące `settings.py` nie wymagają migracji konfiguracji. |
| `oioioi/problems/problem_site.py` | Zarządzanie plikami paczki jako drzewo ze zmianami wsadowymi (`PackageChangesForm`) | `_html_statement_disallowed` przy podmianie treści | Różne funkcje. Brak ryzyka. |
| `oioioi/problems/tests/test_problem.py` | `test_search_name_without_tags` | Testy poprawek bezpieczeństwa, `needs_linux` | Niezależne. |
| `oioioi/problems/tests/tests.py` | `@override_settings(TASK_ARCHIVE_LINK_VISIBLE=True)` | `needs_linux` | Niezależne. |
| `oioioi/programs/handlers.py` | Częściowe rejudge (`SELECTED`, pomijanie `USER_OUTS`), `result_percentage` w `grade_tests`, usuwanie NUL | `sanitize_worker_text` dla komentarza checkera i wyjścia kompilatora | Patrz **D2**. Poza tym niezależne. |
| `oioioi/programs/models.py` | `ContestLanguagesConfig` | `execution_mode_options` przeniesione do `contests.models.problem_instance_execution_mode_options` | Niezależne. Sprawdziłem: nic w forku nie importuje starej nazwy. |
| `oioioi/sinolpack/tests.py` | Testy rejudge'a/plików paczki | `needs_linux`, test nazw plików dodatkowych (path traversal) | Niezależne. |
| `oioioi/szkopul/tests.py` | Testy ukrytego linku archiwum zadań | `TestMainPageLatestContests` | Niezależne. |
| `oioioi/testrun/handlers.py` | Usuwanie NUL | `sanitize_worker_text` | Patrz **D2**. |
| `oioioi/testrun/tests.py` | Test NUL | `needs_linux`, `test_signal_hints` | Niezależne. |
| `oioioi/timeline/static/timeline/timeline.scss` | `$gray-550` → `$gray-600` (`4fb1aeca`) | Ta sama poprawka (`9b40a521`) + `theme-color("danger")` → `$danger` | Identyczna zmiana po obu stronach. Brak ryzyka. |

## 4. Styki semantyczne poza plikami wspólnymi

Miejsca, gdzie upstream zmienił coś, na czym opiera się kod forka, choć pliki się nie pokrywają.

| Zmiana upstreamu | Co sprawdziłem w forku | Wynik |
|---|---|---|
| `dj-pagination` → `django-pagination-py3` (`cfa545f0`, #760); `RankingController._fake_request` ustawia `page` jako property | `czeciecamp/ranking.html` używa `{% load pagination_tags %}`, `autopaginate`, `paginate`; `CzecieCampRankingController._render_ranking_page` używa `_fake_request` | Nazwy tagów bez zmian; testy rankingów obozu przechodzą. Wygląd paginacji może się nieznacznie różnić (`PAGINATION_DEFAULT_WINDOW` 4 → 3, brak `MARGIN`). Jeśli produkcyjne `settings.py` ustawia `PAGINATION_DEFAULT_MARGIN` — jest już ignorowane. |
| Usunięte `memoized`, `memoized_property`, `reset_memoized`, `AccessDenied` (#940, #941) | grep po `czeciecamp`, `llmdetect`, `problembase` i plikach zmienianych przez fork | Brak użyć. |
| Sprzątanie po Bootstrap 4 (#874/#875): `data-toggle`, `btn-block`, `form-group`, `badge-*`… | grep po szablonach/JS/SCSS forka | Brak pozostałości BS4 w kodzie forka. |
| `ProblemInstance.execution_mode` (migracja `contests.0028`) | `ProblemInstanceForm` forka (runda opcjonalna w obozie), panel obozu | Pole ma `default`/`db_default` = `AUTO`; formularze forka go nie wymieniają jawnie. Brak wpływu. |
| `UserPreferences.programming_language` (migracja `base.0007`) | scalanie kont (skrypty poza repo), `validate_unique_email` | Brak wpływu na kod w repo. Skrypt `05_merge_accounts.py` (poza repo) nie przenosi tej preferencji — bez znaczenia, bo pole jest nowe. |
| Poprawki bezpieczeństwa #968: raport zgłoszenia tylko z bieżącego konkursu, `get_last_submissions` per konkurs, editoriale, podmiana treści HTML, nazwy plików dodatkowych w paczce | `llmdetect` (linki do zgłoszeń), `problembase`, widoki obozu | Fork nie wywołuje zmienionych widoków w sposób między-konkursowy. Zachowanie widoczne dla użytkownika: ramka „ostatnie zgłoszenia” pokazuje teraz tylko zgłoszenia z bieżącego konkursu — przy kilku grupach na jednej instancji to zmiana na plus. |
| Nowy wygląd raportu zgłoszenia (#916): `report.html`, `report-body.html`, `report-test-details.html` | `llmdetect` dokleja się do strony zgłoszenia, `czeciecamp` nie nadpisuje szablonów raportu | Brak nadpisań szablonów w forku. **Do obejrzenia w przeglądarce** (zmiana wizualna dla uczestników: komentarze testów rozwijane z wiersza). |
| `sioworkers` 1.5.9 → 1.5.11, `filetracker >= 2.3.3`, Celery 5.6, urllib3 2.x, DRF 3.18, mistune 3 | `docker-compose.yml` forka: worker z tego samego obrazu; opublikowane porty dla **zdalnych workerów** | Worker w compose zaktualizuje się razem z obrazem. **Zdalne workery na innych maszynach trzeba zaktualizować osobno** do tego samego obrazu/wersji sioworkers. |
| Celery: `CELERY_RESULT_BACKEND = None`, kolejki pidbox/event `exclusive`, potwierdzenia publikacji (#928) | `docker-compose.yml` forka: `rabbitmq:3.8` | Ustawienia wprowadzono pod RabbitMQ 4, ale są zgodne z 3.8 (kolejki exclusive i publisher confirms istnieją od dawna). **Nie testowałem na żywym brokerze.** Po deployu w RabbitMQ mogą zostać stare, nieużywane kolejki odpowiedzi `rpc` — nieszkodliwe. |
| `.github/actions/setup-env` przepisane (`uv sync --locked`, wspólne `setup-uv`, uv 0.13.1) | `czecie-ci.yml` używa `./.github/actions/setup-env` z `install-texlive` | Interfejs akcji zachowany (`install-texlive` nadal istnieje). `uv lock --check` przechodzi, więc `--locked` nie wysypie CI. |
| `ruff` w `pyproject.toml` 0.13.2, w `czecie-ci.yml` 0.15.1 | uruchomiłem obie wersje na zmergowanym drzewie | Obie: `All checks passed`, format bez zmian. |
| `easy_toolbox.py`, `docker-compose-dev.yml` | upstream nie zmienił `docker-compose*.yml`; `easy_toolbox.py` — tylko ustawienia Cypressa | Twój `docker-compose.yml` (produkcyjny) i `deploy.sh` nietknięte przez merge. |

## 5. Migracje Django

**Brak kolizji numeracji i brak rozgałęzień grafu.**

| Aplikacja | Fork (przed merge'em) | Upstream (nowe) | Po merge'u |
|---|---|---|---|
| `base` | — | `0007_userpreferences_programming_language` (zależy od `0006`) | liniowo |
| `contests` | — (fork nie dodał migracji w `contests`) | `0028_probleminstance_execution_mode` (zależy od `0027`) | liniowo |
| `programs` | `0024_contestlanguagesconfig` | — | bez zmian |
| `czeciecamp` | `0001`–`0016` | — | bez zmian |
| `llmdetect` | `0001`–`0006` | — | bez zmian |
| `problembase` | `0001`–`0003` | — | bez zmian |
| `authtoken` (DRF) | — | `0004_alter_tokenproxy_options` — przychodzi z podbiciem `djangorestframework` do 3.18, nie z repo | nowa |

Sprawdzone:

- `makemigrations --check --dry-run` (ustawienia testowe, wszystkie aplikacje opcjonalne włączone):
  **No changes detected** — na SQLite i na PostgreSQL.
- Ścieżka aktualizacji na **PostgreSQL 14.17** (ta sama wersja co w `docker-compose.yml`): pusta baza →
  `migrate` na `5a36d8d4` (stan produkcji) → `migrate` na `merge-upstream`. Zastosowały się dokładnie trzy
  migracje: `authtoken.0004`, `base.0007`, `contests.0028`. Wszystkie OK.
- Wszystkie trzy to `AddField` z wartością domyślną / zmiana opcji modelu — bez migracji danych, bez blokad
  dłuższych niż chwilowe (`contests_probleminstance` i `base_userpreferences` są małe). Odwracalne.
- **Nie sprawdzałem** na kopii prawdziwej bazy (`sio2_mig`) — to wymaga Twojego środowiska dev; `deploy.sh`
  i tak robi backup przed migracją.

## 6. Testy

Środowisko: lokalnie, `uv` + Python 3.13, SQLite, `./test.sh -n 8 --migrations` (jak w `czecie-ci.yml`).
Docker był niedostępny na początku pracy, więc testy szły poza kontenerem; brakuje tam `node_modules`
i `dist_webpack`, więc testy przeglądarkowe / SCSS są pomijane tak samo przed i po.

| | zaliczone | porażki | pominięte | xfail |
|---|---|---|---|---|
| **Przed** (`fork/main` `5a36d8d4`) | 1490 | 3 | 32 | 8 |
| **Po merge'u** (`a426def6`, pełny przebieg) | 1572 | 4 | 106 | 8 |
| **Po merge'u + `af27fae9`** | 1573 | 3 | 106 | 8 |

- 3 porażki **przed** (i te same po): `oioioi/mailsubmit/tests.py::TestMailSubmission::{test_accepting_mailsubmissions,
  test_mailsubmit_permissions, test_simple_mailsubmission}` — `pdflatex` nie znajduje `pst-barcode.sty`
  (lokalnie brak pakietu `texlive-pstricks`, nie mam sudo). Niezwiązane z kodem; w CI pakiet jest instalowany.
- Bezpośrednio po merge'u (przed poprawkami) padały dodatkowo dwa testy upstreamu opisane w §2.11
  (`a426def6`, `ab358d18`).

- Pełny przebieg po merge'u wykonałem na `a426def6`. Czwarta porażka to
  `oioioi/czeciecamp/tests.py::TestCampRoundSubmissionsLimit::test_admin_page_of_a_problem`: test wysyłał
  formularz admina zadania bez nowego, wymaganego pola upstreamu `execution_mode`, więc formularz był odrzucany
  i limit obozu się nie zapisywał. Przeglądarka wysyła to pole razem z formularzem, więc to wyłącznie sprawa
  testu — poprawione w `af27fae9`. **Po tej poprawce nie powtarzałem całego zestawu**, tylko
  `oioioi/czeciecamp` + `oioioi/contests/tests/tests.py` (338 zaliczonych, 0 porażek); wiersz
  „Po merge'u + `af27fae9`” to wynik pełnego przebiegu skorygowany o ten jeden test.
- Zaliczonych jest więcej o 83, bo doszły testy upstreamu. Pominiętych jest więcej (32 → 106), bo upstream
  dodał testy przeglądarkowe i SCSS, które lokalnie nie mają `node_modules`/Playwrighta
  („Bootstrap is missing”, „Playwright is not installed”). W Twoim CI też będą pomijane — uruchamia je
  dopiero `smoke.yml` (patrz **D4**).

Dodatkowo:

- `ruff check` + `ruff format --check` — czysto (0.13.2 z lockfile'a i 0.15.1 z `czecie-ci.yml`).
- `uv lock --check` — lockfile zgodny z `pyproject.toml`.
- `manage.py check` — bez błędów.
- Migracje na PostgreSQL 14 — patrz §5.

**Czego nie sprawdziłem:**

- **Build obrazu Docker** (`target: base`) — nie budowałem; ryzyko w `Dockerfile` opisane w §3. Zrobi to job
  `image` w CI po otwarciu PR.
- Testy przeglądarkowe (`smoke.yml`), kompilacja SCSS (`test_scss.py`), Cypress.
- Ocenianie na prawdziwym workerze z sandboksami (w tym nowy domyślny kompilator — **D1**).
- Działanie z RabbitMQ 3.8 na żywo.
- Ręczne obejrzenie UI (menu boczne, raport zgłoszenia, ranking obozu z nową paginacją).

## 7. Lista kontrolna przed wdrożeniem

1. Rozstrzygnąć **D1** (kompilatory) — wgrać sandbox `compiler-gcc.14_2_0` albo przypiąć stare.
2. `grep -n "COMPILERS\|PAGINATION\|CELERY\|dj_pagination" $SIO_DATA_DIR/settings.py` — jeśli produkcyjne
   ustawienia wymieniają `dj_pagination` w `INSTALLED_APPS`/`MIDDLEWARE`, zamienić na `pagination`
   (inaczej `ModuleNotFoundError` przy starcie).
3. PR `merge-upstream` → `main`: przejdzie pełne CI z buildem obrazu. Uwaga: zasada „Squash and merge”
   z `CLAUDE.md` **zniszczy historię merge'a** (upstream przestanie być przodkiem `main` i następny merge
   znów pokaże wszystkie 90 commitów jako konflikty). Ten jeden PR trzeba zmergować zwykłym „Create a merge
   commit”.
4. Po deployu: jedno zgłoszenie C++ i jedno Python na testowym konkursie, podgląd rankingu obozu, rejudge
   jednego zadania.
5. Zaktualizować zdalne workery do tego samego obrazu.
