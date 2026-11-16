# REVIEW — fork po merge'u upstreamu (gałąź `fable-work`, 2026-11-16)

Zakres: zmiany forka względem upstreamu, potem ścieżki używane na obozie (zgłoszenia, ocena, rankingi,
uprawnienia, wiele grup, pliki paczek). Każdą hipotezę z lektury sprawdzałem testem lub sondą, zanim trafiła
tutaj — trzy pierwotne podejrzenia okazały się fałszywe i są wymienione na końcu, żeby nikt ich nie powtarzał.

**Wynik w skrócie:** 1 poważny błąd uprawnień naprawiony (z testami), 4 rzeczy opisane do decyzji,
15 nowych testów integracyjnych dwóch obozów na jednej instancji — izolacja grup trzyma.

**Testy na `fable-work` (pełny przebieg, `./test.sh -n 8 --migrations`):** 1594 zaliczone, 3 porażki, 106
pominiętych, 8 xfail. Te 3 porażki to `oioioi/mailsubmit` — lokalnie brakuje pakietu LaTeX `pst-barcode`;
padają tak samo na `fork/main` sprzed merge'a. `ruff check`, `ruff format --check` i
`makemigrations --check` czyste.

**Czego nie przejrzałem:** wnętrza `llmdetect` (scoring, features, habits — ok. 3 tys. linii; sprawdziłem tylko
uprawnienia widoków i mixiny kontrolerów), wnętrza `problembase` (tylko uprawnienia widoków), szablonów i JS
forka, importu paczek od strony `sinolpack` (fork go nie zmienia), oceniania na prawdziwym workerze.

## 1. Naprawione

### 1.1 [KRYTYCZNE] Każdy, także niezalogowany, mógł zlecić rejudge dowolnych zgłoszeń

- **Gdzie:** `oioioi/contests/admin.py:778` (`SubmissionAdmin.get_urls`) i `:782` (`rejudge_view`).
- **Scenariusz:** `POST /c/<dowolny>/admin/contests/submission/rejudge/` z polami `submissions=<id>` i
  `rejudge_type=FULL`. Widok był zarejestrowany bez `admin_site.admin_view` i niczego nie sprawdzał, a
  zgłoszenia brał z `Submission.objects.in_bulk(ids)`. Sonda: użytkownik anonimowy → `302`, `judge()`
  wywołane. Skutki na obozie: uczestnik może w trakcie rundy zapchać kolejkę sprawdzaczek rejudge'ami
  (identyfikatory zgłoszeń są kolejnymi liczbami), a opiekun jednej grupy — rejudge'ować zgłoszenia innej.
  Rejudge po zmianie testów zmienia też wyniki.
- **Pochodzenie:** błąd upstreamu (jest w `sio2project/oioioi` `master`); fork go nie wprowadził, ale w
  `6b167792` usunął z tego widoku jedyny warunek (wymóg aktywnych raportów).
- **Poprawka:** commit `contests: only contest admins may rejudge chosen submissions` — `admin_view`,
  tylko POST, uprawnienie takie jak dla samej akcji (`has_rejudge_permission` lub superuser), zgłoszenia
  z `self.get_queryset(request)` (czyli bieżącego konkursu).
- **Testy:** `oioioi/contests/tests/tests.py::TestRejudgeSelectedSubmissionsPermissions` (5 testów:
  anonim, uczestnik, admin konkursu, admin innego konkursu, GET).
- **Do zrobienia przez Ciebie:** warto zgłosić to upstreamowi (prywatnie, jako błąd bezpieczeństwa) — nie
  zgłaszałem niczego na zewnątrz.

## 2. Opisane, nienaprawione (od najpoważniejszych)

### 2.1 Admin jednej grupy może wstrzymać/wznowić ocenianie zadania innej grupy (`suspendjudge`)

- **Gdzie:** `oioioi/suspendjudge/admin.py:26-45` — `_resume`, `_rejudge`, `_clear_queue`, `_suspend` biorą
  `problem_instance_id` z URL-a bez sprawdzenia, że zadanie należy do `request.contest`. Widoki mają
  `enforce_condition(contest_exists & is_contest_basicadmin)`, więc wymagają bycia adminem *jakiegoś* konkursu.
- **Scenariusz:** opiekun grupy A wysyła `POST /c/A/admin/contests/probleminstance/<id zadania z B>/suspend_all/`
  — ocenianie zadania w grupie B staje; `resume_and_rejudge` odpala rejudge wszystkich zgłoszeń tego zadania.
- **Dlaczego nie naprawiłem:** to kod upstreamu spoza Twoich zmian, a opiekunowie grup to u Was zaufana
  kadra — realne ryzyko jest małe. Poprawka jest jednolinijkowa w każdej z czterech metod:
  `get_object_or_404(ProblemInstance, id=..., contest=request.contest)`; ten sam wzorzec, co poprawki
  upstreamu z #968. Powiedz, jeśli mam ją dodać (z testem), albo zgłoś razem z 1.1.

### 2.2 Przedłużenie czasu rundy nie liczy się do „Dzienne” ani do limitu rundy

- **Gdzie:** `oioioi/czeciecamp/controllers.py:70` (`is_daily_submission`), użyte w `:105` (ranking),
  `:189` (złota ramka), `:286` (limit zgłoszeń w rundzie).
- **Potwierdzone sondą:** uczestnik z `RoundTimeExtension` +30 min wysyła w 20. minucie przedłużenia.
  `get_round_times(...).is_active` = prawda (runda dla niego trwa, więc m.in. nadal widzi tylko bieżącą rundę),
  zgłoszenie jest przyjęte, ale ranking Dzienny daje mu 0, a limit „w trakcie rundy” go nie obejmuje
  (`get_submissions_left` = brak limitu).
- **Dlaczego wątpliwe:** nie wiem, czy na obozach używacie przedłużeń. Jeśli nie — nic się nie dzieje.
  Jeśli tak — „Dzienne” jest dla takiej osoby po cichu zaniżone.
- **Propozycja:** w `best_submissions` liczyć koniec rundy per użytkownik (jedno zapytanie
  `RoundTimeExtension` na ranking → słownik `(user_id, round_id) → minuty`), a w `_binding_limit` użyć
  `self.get_round_times(request, round)` zamiast `round.end_date`. Albo, jeśli przedłużeń ma nie być:
  odnotować to w opisie typu konkursu.

### 2.3 `deploy.sh`: kontrola „trwa runda” przepuszcza, gdy nie da się zapytać bazy

- **Gdzie:** `deploy.sh:57-59` — `running=$(docker compose exec -T db psql … 2>/dev/null || true)`.
- **Scenariusz:** kontener `db` chwilowo nie odpowiada (albo zmieni się nazwa bazy/użytkownika) → zapytanie
  pada, `running` jest puste, skrypt uznaje, że żadna runda nie trwa, i restartuje serwis w trakcie rundy.
  Krok później backup też by padł i przerwał deploy, więc w praktyce okno jest wąskie — ale zabezpieczenie
  nie powinno milcząco zawodzić.
- **Propozycja:** rozróżnić błąd od pustego wyniku (`if ! running=$(…); then echo "Cannot check rounds"; exit 1; fi`,
  z `--force` jako obejściem). Kontrola nie uwzględnia też przedłużeń czasu (2.2) ani rund bez daty końca
  (to drugie jest w komentarzu skryptu jako celowe).

### 2.4 Zdublowany filtr widoczności w API

- **Gdzie:** `oioioi/contests/api.py:118`. Po merge'u `can_see_problem` jest wołane dwa razy na zadanie
  (Twoje `visible_problem_instances` + linia upstreamu). Kosmetyka; uzasadnienie zostawienia w
  `MERGE_REPORT.md` §2.5.

## 3. Sprawdzone, bez zastrzeżeń

- **Izolacja grup** (nowe testy, `oioioi/czeciecamp/test_integration.py::TestCampGroupsIsolation`):
  uczestnik grupy A dostaje 403 na liście zadań, rankingu, zgłoszeniach, formularzu i panelu grupy B; nie
  wyśle zgłoszenia do zadania B ani przez URL grupy B, ani podając jego id w formularzu grupy A; nie otworzy
  zgłoszenia ani kodu uczestnika B; lista konkursów i `api_contest_list` pokazują mu tylko własną grupę;
  API grupy B zwraca 403; ranking grupy B nie jest osiągalny przez URL grupy A (404).
- **Panel obozu:** każdy widok ma `contest_exists & is_contest_basicadmin & is_czecie_camp`; obiekty innej
  grupy adresowane przez panel własnej dają 404 i pozostają nietknięte (test
  `test_admin_of_one_group_cannot_manage_the_other`); akcje zmieniające stan są `@require_POST`
  i unieważniają rankingi.
- **Zgłaszanie:** przed startem rundy odrzucone, w trakcie i po końcu przyjęte, osoba spoza obozu 403;
  limit rundy działa tylko w trakcie rundy (`TestCampSubmitFlow`).
- **Rankingi po HTTP:** runda pojawia się uczestnikom dopiero po dacie publikacji; po zamrożeniu uczestnik
  widzi stare punkty, admin nowe; CSV tylko dla adminów (`TestCampRankingPages`).
- **Logowanie e-mailem:** adres w innej wielkości liter loguje na właściwe konto; adres pasujący do więcej niż
  jednego konta nie loguje; konto nieaktywne odrzucone.
- **Edycja plików paczki** (`PackageChangesForm`, `_prepare_changed_package`): `replace`/`delete` wymagają
  ścieżki istniejącej w archiwum, `add` odrzuca `..`, ścieżki bezwzględne, `\` i znaki sterujące; archiwa tar
  z dowiązaniami są odrzucane przy rozpakowaniu, zip nie tworzy dowiązań — zapisu poza katalog tymczasowy
  nie znalazłem. Po merge'u pobieranie pliku ma dodatkowo blokadę path traversal z upstreamu.
- **`llmdetect` / `problembase` — uprawnienia:** wszystkie widoki `llmdetect` mają
  `contest_exists & is_contest_admin` i filtrują po `request.contest`; wszystkie widoki `problembase` są
  tylko dla superusera.
- **Migracje:** patrz `MERGE_REPORT.md` §5.

## 4. Podejrzenia, które się nie potwierdziły

- **Kreator obozu a zmiana czasu** (`czeciecamp/views.py:469`, `start + timedelta(days=day)`): działa
  poprawnie — arytmetyka na dacie ze strefą `zoneinfo` zachowuje godzinę lokalną. Zostawiłem test
  regresyjny (`TestCreateCamp::test_days_keep_the_local_hour_across_a_dst_change`).
- **`EmailAuthBackend` dubluje zapytania o uprawnienia:** nie — dziedziczy po `ModelBackend` i dzieli z nim
  cache uprawnień na obiekcie użytkownika.
- **Usunięcie rundy w panelu kasuje zadania i zgłoszenia:** nie — `ProblemInstance.round` ma `SET_NULL`,
  a okno potwierdzenia w panelu mówi wprost, że zadania zostają w konkursie bez rundy.

## 5. Testy dodane w tym etapie

| Plik | Co sprawdza |
|---|---|
| `oioioi/czeciecamp/test_integration.py` (15 testów) | Dwa obozy na jednej instancji: zgłaszanie przez prawdziwy widok, limit rundy, strony wyników, izolacja uczestników i adminów, API, rankingi po HTTP z publikacją i zamrożeniem, CSV, logowanie e-mailem, dodatkowy czas. Ocenianie jest zamockowane. |
| `oioioi/contests/tests/tests.py::TestRejudgeSelectedSubmissionsPermissions` (5) | Uprawnienia widoku rejudge'a wybranych zgłoszeń (1.1). |
| `oioioi/czeciecamp/tests.py::TestCreateCamp::test_days_keep_the_local_hour_across_a_dst_change` | Kreator obozu przy zmianie czasu. |

Nadal bez testów integracyjnych: wgranie paczki do obozu i ocenienie zgłoszenia na prawdziwym workerze
(wymaga sandboksów — lokalnie niedostępne), częściowy rejudge całego zadania razem z raportem rejudge'a
przez HTTP, `llmdetect` na danych z dwóch grup.
