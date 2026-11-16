# REVIEW — stan częściowy (przerwane limitem użycia, 2026-11-16)

**To nie jest pełne review.** Przeczytane w całości: `czeciecamp/{controllers,models,views,forms,rejudge_report,admin,urls}.py`,
`base/auth.py`, `base/utils/user.py`, zmiany forka w `problems/forms.py`, `problems/problem_site.py`,
`base/utils/archive.py`, `contests/forms.py`, `contests/api.py`, `participants/`, `programs/` (języki).
**Nieprzejrzane:** `llmdetect`, `problembase`, szablony i JS, `programs/handlers.py::collect_tests`
(częściowe rejudge), `deploy.sh`, ścieżka importu paczek, testy. Żaden bug nie został naprawiony —
poniższe punkty to obserwacje z lektury, bez testów odtwarzających.

## Do sprawdzenia (od najpoważniejszych)

1. **Przedłużenia czasu rundy nie liczą się do „Dzienne”** — `oioioi/czeciecamp/controllers.py:70`
   (`is_daily_submission`), użyte w `:105`, `:189`, `:286`.
   Funkcja porównuje datę z `round.end_date`, a nie z czasami z `get_round_times` (które uwzględniają
   `RoundTimeExtension` dla użytkownika). Scenariusz: uczestnik dostaje +30 min, wysyła w 20. minucie
   przedłużenia — zgłoszenie jest przyjęte jako „w rundzie” przez `can_submit`, ale ranking Dzienny go nie
   policzy, a limit „w trakcie rundy” go nie obejmie. Propozycja: w `best_submissions` i `_binding_limit`
   liczyć koniec rundy per użytkownik (słownik `user_id → extra_time` pobrany raz). Wątpliwe, bo nie wiem,
   czy na obozach używacie przedłużeń.
2. **Usunięcie rundy w panelu nie usuwa zadań** — `oioioi/czeciecamp/views.py:357` (`delete_round_view`).
   `ProblemInstance.round` ma `SET_NULL`: zadania zostają bez rundy (ukryte dla uczestników), a ich
   zgłoszenia znikają ze wszystkich rankingów; ranking rundy jest kasowany kaskadowo. Jeśli szablon panelu
   nie ostrzega o tym przy potwierdzeniu (nie sprawdziłem `panel.html`), warto dodać liczbę zadań i zgłoszeń
   do komunikatu.
3. **Kreator obozu przesuwa dni o stałe 24 h** — `oioioi/czeciecamp/views.py:469`
   (`start + timedelta(days=day)`). Obóz obejmujący zmianę czasu (ostatnia niedziela marca/października)
   dostanie rundy przesunięte o godzinę. W tym samym pliku jest już `_next_day` (`:92`), który robi to
   poprawnie — wystarczy go użyć w pętli. Drobne, łatwe do naprawy z testem.
4. **`EmailAuthBackend` dziedziczy po `ModelBackend`** — `oioioi/base/auth.py:8`. Jako drugi backend
   w `AUTHENTICATION_BACKENDS` powtarza zapytania o uprawnienia (`has_perm` pyta każdy backend). Tylko
   koszt, nie błąd. Propozycja: nadpisać `get_user_permissions`/`get_group_permissions`/`has_perm` tak, by
   zwracały puste zbiory, albo dziedziczyć po `BaseBackend` i skopiować `user_can_authenticate`/`get_user`.
5. **Zdublowany filtr w API** — `oioioi/contests/api.py:118`: po merge'u `can_see_problem` jest wywoływane
   dwa razy na zadanie (opis w `MERGE_REPORT.md` §2.5). Kosmetyka.

## Sprawdzone, bez zastrzeżeń

- Edycja plików paczki (`PackageChangesForm`, `_prepare_changed_package`): ścieżki `replace`/`delete`
  muszą istnieć w archiwum, `add` odrzuca `..`, ścieżki bezwzględne i znaki sterujące; archiwa tar
  z dowiązaniami są odrzucane przy rozpakowaniu, zip nie tworzy dowiązań — zapisu poza katalog
  tymczasowy nie znalazłem.
- Widoki panelu obozu: wszystkie mają `contest_exists & is_contest_basicadmin & is_czecie_camp`, obiekty
  pobierane z `contest=request.contest`, akcje zmieniające stan są `@require_POST` i unieważniają rankingi.
- Logowanie e-mailem: adres pasujący do więcej niż jednego konta nie loguje; konto nieaktywne odrzucone.
