# REVIEW — fork po merge'u upstreamu (gałąź `fable-work`, 2026-11-16)

Zakres: zmiany forka względem upstreamu, potem ścieżki używane na obozie (zgłoszenia, ocena, rankingi,
uprawnienia, wiele grup, pliki paczek). Każdą hipotezę z lektury sprawdzałem testem lub sondą, zanim trafiła
tutaj — trzy pierwotne podejrzenia okazały się fałszywe i są wymienione na końcu, żeby nikt ich nie powtarzał.

**Druga tura (ten sam dzień): audyt autoryzacji wokół rejudge'a i testy własnościowe rankingów — §6 i §7.**
Naprawione dodatkowo 2.1 (`suspendjudge` między grupami) i walidacja typu rejudge'a; nowe 2.5 i 2.6.

**Wynik w skrócie (pierwsza tura):** 1 poważny błąd uprawnień naprawiony (z testami), 4 rzeczy opisane do decyzji,
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
- **Czy są bliźniacze dziury (sprawdzone 2026-11-16):** przejrzane wszystkie `get_urls` w
  `oioioi/*/admin.py`. To jedyny endpoint adminowy bez żadnej kontroli. `globalmessage` opakowuje
  `admin_site.admin_view`; `problems` `download_view` jest surowy, ale sam robi `has_change_permission`;
  `suspendjudge` (resume/suspend) ma `enforce_condition(contest_exists & is_contest_basicadmin)` — nie jest
  obejściem logowania, ma tylko problem braku sprawdzenia konkursu (§2.1). Aplikacje forka (`czeciecamp`,
  `llmdetect`, `problembase`) nie mają własnego `get_urls`; ich widoki idą przez `urls.py` z
  `enforce_condition`/`is_superuser`.
- **Co to umożliwia / klasa błędu:** brak autoryzacji na operacji zmieniającej stan.
  - **Dostępność (wysoki wpływ, główny):** `submissions` to lista dowolnych id, bez limitu liczby i bez
    filtrowania po konkursie (`Submission.objects.in_bulk(ids)`). Jedno żądanie kolejkuje tyle jobów
    oceniania, ile poda atakujący — w praktyce wszystkie zgłoszenia na instancji — i można to powtarzać.
    W trakcie rundy zapcha to kolejkę `evalmgr`/sioworkers i zablokuje sprawdzanie zgłoszeń uczestników.
    Anonimowo, trywialne do zeskryptowania.
  - **Integralność (nisko-średni):** rejudge przelicza wynik z aktualnych testów; `FULL` na niezmienionych
    testach daje ten sam wynik, `NEW`/`SELECTED` z pustym wyborem nic nie sprawdza i zachowuje stare wyniki,
    a crafted `tests` nie zeruje już ocenionych testów w raporcie (prześledzone w `programs/handlers.py::collect_tests`).
    Jedyne realne okno: gdyby admin zmienił/dodał testy i jeszcze nie zrobił rejudge'a, atakujący wymusiłby
    je wcześniej. Poza tym churn statusów („oczekuje") i powtarzane unieważnianie rankingów.
  - **Poufność: brak.** Endpoint tylko przekierowuje (`302`), nie zwraca danych zgłoszeń; komunikat „Queued N"
    trafia do sesji, a anonima redirect odsyła do logowania. Nie jest kanałem do czytania cudzego kodu ani
    wyników; cross-contest dotyczy wyzwalania rejudge'a, nie podglądania.
  - **CSRF to nie bariera bezpieczeństwa:** blokuje tylko żądanie bez tokenu; każdy wylogowany bierze token
    z formularza dowolnej strony i przechodzi. Potwierdzone ręcznie na produkcji (serwer ma `CSRF_USE_SESSIONS`,
    token jest w sesji, nie w ciasteczku).

## 2. Opisane, nienaprawione (od najpoważniejszych)

### 2.1 [NAPRAWIONE w drugiej turze] Admin jednej grupy mógł wstrzymać/wznowić ocenianie zadania innej grupy (`suspendjudge`)

- **Gdzie:** `oioioi/suspendjudge/admin.py` — cztery widoki (`suspend_all`, `suspend_all_but_init`,
  `resume_and_rejudge`, `resume_and_clear`) brały `problem_instance_id` z URL-a i wymagały tylko bycia adminem
  konkursu *z adresu*; nie sprawdzały, że zadanie do niego należy.
- **Scenariusz:** opiekun grupy A wysyłał `POST /c/A/admin/contests/probleminstance/<id zadania z B>/suspend_all/`
  — ocenianie zadania w grupie B stawało; `resume_and_rejudge` odpalało rejudge jego wstrzymanych zgłoszeń,
  `resume_and_clear` kasowało jego kolejkę.
- **Poprawka:** commit `suspendjudge: suspend and resume only problems of the current contest` — każdy widok
  zaczyna od `get_object_or_404(ProblemInstance, pk=…, contest=request.contest)` (obce zadanie → 404).
  Osobny commit, żeby dało się go zgłosić upstreamowi razem z 1.1 albo wycofać.
- **Testy:** `oioioi/czeciecamp/test_rejudge_permissions.py` — bez poprawki pada 8 przypadków.

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

### 2.5 Zamrożenie ustawione przed końcem rundy nie zamraża „Dzienne”

- **Gdzie:** `oioioi/czeciecamp/controllers.py`, `serialize_ranking` → `best_submissions`: moment zamrożenia
  obcina tylko Stałe (`stale_end`), Dzienne obcina wyłącznie koniec zliczania.
- **Kiedy widać:** tylko gdy w panelu ustawisz własny czas zamrożenia *wcześniejszy* niż koniec trwającej
  rundy. Przy domyślnym zamrożeniu (koniec ostatniej rundy) problemu nie ma, bo po nim nic nie liczy się do
  Dziennych.
- **Skutek:** zgłoszenie wysłane po zamrożeniu, ale jeszcze w trakcie rundy, zmienia uczestnikom punkty w
  rankingu Dzienne i w „Dzienne + Stałe” (dymek komórki pokazuje nowe Dzienne obok starego Stałego), i nie
  jest policzone w znaczniku „Po zamrożeniu: N zgłoszeń” rankingu samych Dziennych. Dodatkowo komórka
  rankingu łączonego ma wtedy punkty bez linku do zgłoszenia.
- **Test:** `test_properties.py::TestCampRankingVisibilityProperties::test_freeze_hides_daily_points`,
  oznaczony `xfail(strict=True)` — po naprawie zacznie „niespodziewanie przechodzić” i trzeba będzie zdjąć
  znacznik. Znane wcześniej (zapisane przy projekcie przebudowy obozu), dotąd bez testu.
- **Dlaczego nie naprawiłem:** to zmiana tego, co uczestnicy widzą w rankingu w trakcie obozu — Twoja
  decyzja. Poprawka jest mała: w `best_submissions` liczyć Dzienne tylko dla zgłoszeń `<= stale_end`
  (wtedy raport rejudge'a, który podaje `counting_end` w obu miejscach, zostaje bez zmian) i liczyć
  znacznik „po zamrożeniu” także dla rankingów bez Stałych.

### 2.6 Drobiazgi z audytu uprawnień (druga tura), wszystkie w kodzie upstreamu

- **Rejudge wzorcówek działa na GET** (`problems/views.py::rejudge_model_solutions_view`, link w
  szablonie). Uprawnienie jest sprawdzane (`contest_admin` konkursu zadania), ale operacja zmieniająca stan
  pod GET-em omija ochronę CSRF: admin, który otworzy podsunięty adres, przeliczy wzorcówki. Skutek mały
  (kilka zadań w kolejce na kliknięcie), więc tylko odnotowuję; poprawka to `@require_POST` + formularz.
- **`rejudge_all` z błędną datą → 500.** `date_from`/`date_to` idą prosto do filtra; `?date_from=cokolwiek`
  kończy się wyjątkiem zamiast komunikatu. Dostępne tylko dla admina konkursu, nic nie psuje.
- **Niezalogowany zna liczbę uczestników grupy:** `/c/<id>/contest_info/` zwraca `{"users_count": N}` bez
  logowania (trzeba znać identyfikator konkursu). Strona wyboru konkursu pod `/c/<id>/` pokazuje też jego
  nazwę. Bez nazwisk i wyników.
- **Obserwator może uruchomić ocenianie „user outs”** (`programs/views.py::generate_user_output_view` →
  `can_generate_user_out` przepuszcza adminów i obserwatorów). To zamierzone w upstreamie; wymieniam, bo to
  jedyna ścieżka, którą ktoś bez praw admina wstawia pracę do kolejki sprawdzaczek poza zwykłym zgłoszeniem.
- **Admin konkursu, do którego zadanie dodano jako pierwsze, administruje samym zadaniem** (testy,
  wzorcówki, paczka) także wtedy, gdy używa go inna grupa (`problems/utils.py::can_admin_problem`). Zamierzone;
  dotyczy treści zadania, nie zgłoszeń ani wyników drugiej grupy.

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

## 6. Audyt autoryzacji wokół rejudge'a (druga tura, 2026-11-16)

**Metoda.** (1) Lektura każdego miejsca, które woła `controller.judge(...)` albo zmienia to, od czego rejudge
zależy. (2) Sonda dynamiczna: jednorazowy skrypt przeszedł po wszystkich nazwanych wzorcach URL instancji
testowej (1279 adresów, w tym `/c/<grupa A>/` i `/c/<grupa B>/` z identyfikatorami obiektów grupy B) jako
niezalogowany, uczestnik grupy A i admin grupy A, GET i POST, z podmienionym `judge`; ręcznie przejrzałem
wszystko, co nie skończyło się odmową. (3) Stała macierz testów dla ścieżek rejudge'a (niżej).

**Ograniczenia sondy:** POST-y były puste, więc widok, który sprawdza uprawnienia dopiero po walidacji
formularza, mógł się prześlizgnąć — dlatego ścieżki rejudge'a mają osobne testy z prawdziwymi danymi. 54
wzorce wymagające nietypowych parametrów (m.in. `teachers`, `balloons`, API z `contest_id` w ścieżce) nie
zostały odwiedzone. Instancja testowa ma włączone więcej aplikacji niż produkcja.

**Wynik sondy:** nikt nieuprawniony nie uruchomił oceniania żadnym adresem. Jedyna dziura między grupami to
`suspendjudge` (2.1, naprawione). Reszta nieodmów to strony publiczne z założenia, przekierowania do
własnego konkursu i błędy 500 przy brakujących parametrach (odmowa, tylko brzydka).

**Wszystkie wejścia do rejudge'a i kto może ich użyć:**

| Wejście | Kod | Wymaga | Zgłoszenia/zadanie ograniczone do konkursu |
|---|---|---|---|
| Rejudge jednego zgłoszenia | `contests/views.py::rejudge_submission_view` | POST, admin (basic) konkursu zgłoszenia | tak (`get_submission_or_error`) |
| Rejudge zadania / kilku zadań | `contests/views.py::rejudge_all_submissions_for_problem_view` | admin (basic); ocenianie tylko na POST z poprawnym formularzem | tak |
| Rejudge wybranych zgłoszeń, krok 1 (akcja admina) | `contests/admin.py::rejudge_action` | admin (basic); obserwatorowi akcja jest usuwana | tak (lista admina) |
| Rejudge wybranych zgłoszeń, krok 2 | `contests/admin.py::rejudge_view` | POST, admin (basic) — poprawione w 1.1 | tak (od 1.1); typ rejudge'a walidowany od tej tury |
| Rejudge wzorcówek | `problems/views.py::rejudge_model_solutions_view` | admin (pełny) konkursu zadania; **GET** (2.6) | tak (uprawnienie na konkursie zadania) |
| Wznów i rejudge'uj | `suspendjudge/admin.py` | POST, admin (basic) | tak (od tej tury, 2.1) |
| Kolejka oceniania konkursu, akcja „rejudge” | `evalmgr/admin.py::ContestJobsQueueAdmin` | admin (pełny) | tak (queryset konkursu) |
| Kolejka oceniania całej instancji | `evalmgr/admin.py::SystemJobsQueueAdmin` | superuser | — |
| Zmiana rodzaju SUSPECTED → inny (rejudge jako skutek) | `contests/views.py::change_submission_kind_view` | POST, admin (basic) | tak |
| „Wymaga rejudge'a” / „nie wymaga” | `contests/views.py::change_needs_rejudge_val` | admin (basic), zmiana tylko na POST | tak |
| Raport rejudge'a obozu | `czeciecamp/views.py::rejudge_report_view` | admin (basic) obozu | tak |
| Generowanie „user outs” | `programs/views.py::generate_user_output_view` | POST; admin, obserwator albo właściciel zgłoszenia, jeśli zadanie na to pozwala | tak |

**Poprawione w tej turze:**

- 2.1 — `suspendjudge` (opis wyżej).
- `rejudge_view` przyjmował dowolny `rejudge_type` i nazwy testów. Przy nieznanym typie nazwy nie były
  porównywane z testami zadania i zadanie oceniania wywracało się w `collect_tests` (`KeyError`). Tylko admin
  własnego konkursu, więc to odporność, nie uprawnienia; teraz nieznany typ → 400. Commit
  `contests: reject an unknown rejudge type, test who may rejudge`.

**Testy (`oioioi/czeciecamp/test_rejudge_permissions.py`, 10 testów, 154 podprzypadki):** 14 żądań (wszystkie
wiersze tabeli poza kolejką instancji i „user outs”, plus wstrzymywanie oceniania) × niezalogowany, uczestnik
tej grupy, obserwator tej grupy, uczestnik innej grupy, admin i basic-admin innej grupy (także przez adresy
własnej grupy z cudzymi identyfikatorami). Sprawdzane: zero wywołań `judge`, brak zmiany stanu, kod odmowy.
Potem to samo żądanie jako admin grupy musi zadziałać — żeby odmowa nie była „odmową”, bo żądanie i tak było
błędne. Osobno: basic-admin nie rusza wzorcówek ani kolejki; lista zgłoszeń admina pomija cudze identyfikatory.

## 7. Testy własnościowe matematyki rankingów (druga tura)

Plik `oioioi/czeciecamp/test_properties.py` (35 testów + 1 `xfail`, ok. 7,7 tys. podprzypadków, ~40 s na 6
procesach). Zamiast wybranych przykładów każdy test formułuje regułę i sprawdza ją na losowych obozach:
1–4 rundy (część „All-time”, część bez końca, część nieopublikowana), 1–2 zadania na rundę, 2–6 uczestników,
do 30 zgłoszeń ustawionych *na* granicach i minutę obok (start i koniec rundy, koniec zliczania, zamrożenie),
ukryci i zdyskwalifikowani, rankingi o losowych wagach (także ujemnych i zerowych), obu trybach Dziennych i
trzech trybach medali.

Bez nowej zależności: generator na `random.Random(seed)`. Przypadki są powtarzalne; porażka podaje ziarno i
pełny opis obozu, a `CAMP_PROPERTY_SEED=<ziarno> CAMP_PROPERTY_CASES=1` odtwarza ten jeden. Kosztem jest brak
automatycznego zmniejszania kontrprzykładu, które dałoby `hypothesis` — do rozważenia, jeśli zechcesz dodać
zależność deweloperską.

| Grupa | Własności |
|---|---|
| Zgodność z wzorcem | `best_submissions` i cały `serialize_ranking` (każda liczba każdego wiersza i komórki, miejsca, medale, różnica, procent, licznik „po zamrożeniu”) równe niezależnej, celowo naiwnej implementacji reguł — dla uczestnika, admina i obserwatora, dla każdego rankingu obozu |
| Spójność wiersza | suma = waga×Dzienne + waga×Stałe = suma komórek; wiersze posortowane; miejsce = 1 + liczba lepszych; równe punkty → równe miejsce i medal; więcej punktów → nie gorszy medal; brak medalu za 0; CSV pokazuje te same liczby |
| Algebra | liniowość wag (a×Dz + b×St = a×[Dzienne] + b×[Stałe]); przeskalowanie wag nie zmienia miejsc ani medali; ranking kilku rund = suma rankingów pojedynczych rund; Dzienne obozu = suma rankingów rund; Stałe ≥ Dzienne; komórka Stałych = wynik z listy zadań |
| Monotoniczność | dodatkowe zgłoszenie / podniesienie wyników nigdy nie odbiera punktów (tryb „najlepsze”), pomaga tylko wysyłającemu, nikogo innego nie przesuwa w górę; tryb „ostatnie” liczy ostatnie, także gorsze |
| Niewidoczne nie wpływa na widoczne | nic po końcu zliczania i nic sprzed startu rundy; zgłoszenia osób spoza obozu, IGNORED i nieocenione; rundy nieopublikowane; ukryty/zdyskwalifikowany = jakby go nie było; uczestnik widzi ranking admina bez ukrytych; zamrożenie ukrywa punkty Stałych (zostaje tylko licznik) i nic nie zmienia adminowi; po odmrożeniu uczestnik widzi liczby admina |
| Granice | oba końce rundy należą do rundy; runda „All-time” nie ma Dziennych |
| Miejsca, medale, tekst | `_assign_places`; progi IOI (1/12, 1/6, 1/4 zaokrąglane w górę, monotoniczne, połowa przy wielokrotności 12); polska odmiana „zgłoszenie/zgłoszenia/zgłoszeń” |
| Punktacja testu | `threshold_linear_test_scorer`: wynik w [0, max], pełny do połowy limitu, wolniej nigdy nie lepiej, ≥1 za poprawną odpowiedź w limicie, 0 i TLE po limicie, 0 za błędną; `min_group_scorer`, `sum_group_scorer`, `sum_score_aggregator` |

**Czy te testy coś wykrywają:** sprawdziłem to mutacyjnie — 19 pojedynczych zepsuć reguł w
`czeciecamp/controllers.py` i `models.py` (m.in. `<=` ↔ `<` na każdej granicy, pominięcie końca zliczania,
miejsca dla ukrytych, medal za 0 punktów, zgubiona waga, zamrożenie ignorujące „odmrożone”, pokazanie rund
nieopublikowanych, podłoga zamiast sufitu w progach IOI). Wszystkie 19 wykryte.

**Co testy znalazły:** jedną rzecz — 2.5 (zamrożenie przed końcem rundy nie zamraża Dziennych). Poza tym
matematyka rankingów zgadza się z regułami we wszystkich wylosowanych obozach.

**Czego nie obejmują:** `RoundTimeExtension` (2.2 — generator nie tworzy przedłużeń, więc test tego nie
pokaże), renderowanie HTML rankingu, unieważnianie cache'u rankingów, `llmdetect`, rankingi innych typów
konkursów (poza wspólnym `_assign_places`).
