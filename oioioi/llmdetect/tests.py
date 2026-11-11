import importlib.util
import io
import json
import os
import random
import re
import tempfile
import time
from datetime import UTC, datetime, timedelta
from io import StringIO
from types import SimpleNamespace
from unittest import TestCase as SimpleTestCase
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.core.files.base import ContentFile, File
from django.core.management import call_command
from django.http import HttpResponse
from django.test import RequestFactory
from django.urls import resolve, reverse
from django.utils import translation

from oioioi.base.tests import TestCase
from oioioi.contests.models import Contest, ContestPermission, ProblemInstance, Round
from oioioi.contests.scores import IntegerScore
from oioioi.llmdetect import features as F
from oioioi.llmdetect.controllers import LLMDetectContestControllerMixin
from oioioi.llmdetect.explain import HABIT_LABELS, SIGNAL_LABELS, describe, habit_value
from oioioi.llmdetect.habits import extract_habits
from oioioi.llmdetect.middleware import ProblemViewMiddleware
from oioioi.llmdetect.models import (
    LLMDetectConfig,
    LLMReferenceSolution,
    LLMStatement,
    ProblemViewEvent,
    ReviewDecision,
    RiskAssessment,
    SubmissionFeatures,
    get_config,
    mark_dirty,
)
from oioioi.llmdetect.prompts import PROMPTS, PROMPTS_FORMAT, build_prompt, extract_code, parse_references
from oioioi.llmdetect.scoring import (
    FLAG_THRESHOLD,
    ContestAssessor,
    assess_contest,
    compute_features,
    history_submissions,
    needs_update,
    read_source,
    update_if_needed,
    user_rankings,
)
from oioioi.problems.models import Problem
from oioioi.programs.models import ProgramSubmission

T0 = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)

LLM_BFS = """#include <iostream>
#include <vector>
#include <queue>

int main() {
    std::ios::sync_with_stdio(false);
    std::cin.tie(nullptr);

    int numVertices, numEdges;
    std::cin >> numVertices >> numEdges;

    // Build the adjacency list for the undirected graph.
    std::vector<std::vector<int>> adjacency(numVertices + 1);
    for (int i = 0; i < numEdges; ++i) {
        int from, to;
        std::cin >> from >> to;
        adjacency[from].push_back(to);
        adjacency[to].push_back(from);
    }

    // Run BFS from vertex 1 to compute the shortest distances.
    std::vector<long long> distance(numVertices + 1, -1);
    std::queue<int> pending;
    distance[1] = 0;
    pending.push(1);
    while (!pending.empty()) {
        int current = pending.front();
        pending.pop();
        for (const int& neighbor : adjacency[current]) {
            if (distance[neighbor] == -1) {
                distance[neighbor] = distance[current] + 1;
                pending.push(neighbor);
            }
        }
    }

    // Print the distance to every vertex.
    for (int v = 1; v <= numVertices; ++v) {
        std::cout << distance[v] << (v == numVertices ? '\\n' : ' ');
    }
    return 0;
}
"""

HUMAN_BFS = """#include <bits/stdc++.h>
using namespace std;
#define pb push_back
const int N=2e5+7;
vector<int> g[N];
long long d[N];int n,m;
int main(){
    ios_base::sync_with_stdio(0);cin.tie(0);
    cin>>n>>m;
    for(int i=0;i<m;i++){int a,b;cin>>a>>b;g[a].pb(b);g[b].pb(a);}
    //cerr<<"ok"<<endl;
    queue<int> q;q.push(1);
    for(int i=1;i<=n;i++)d[i]=-1;
    d[1]=0;
    while(!q.empty()){int v=q.front();q.pop();
        for(int u:g[v])if(d[u]==-1){d[u]=d[v]+1;q.push(u);}
    }
    for(int i=1;i<=n;i++)cout<<d[i]<<' ';
}
"""

# A beginner's solution: clearly not LLM-like.
SCHOOL_CODE = """#include <iostream>
using namespace std;

int main()
{
    int n;
    cin >> n;

    int a[30000];

    for (int i = 0; i < n; i++)
    {
        cin >> a[i];
    }

    int ile = 0;

    for (int i = 1; i < n - 1; i++)
    {
        if (a[i] > a[i - 1] && a[i] > a[i + 1])
        {
            ile++;
        }
    }

    cout << ile;

    return 0;
}
"""

_STATEMENTS = [
    "for(int i=0;i<n;i++)a[i]{op}=b[i];",
    "if(x{cmp}y)swap(x,y);",
    "while(k{cmp}n){{k{op}=2;c++;}}",
    "s{op}=a[i]*b[j];",
    "for(int j=n-1;j>=0;j--)if(a[j]{cmp}m)m=a[j];",
    "ans=max(ans,dp[i]{op}w[i]);",
    "dp[i][j]=min(dp[i-1][j],dp[i][j-1]){op}1;",
    "cout<<ans<<'\\n';",
    "if(!vis[v]){{vis[v]=1;dfs(v);}}",
    "sort(a,a+n);",
    "r=(l+r)/2;",
    "cnt[a[i]]{op}=1;",
]


def human_code(seed):
    """A short program in a typical olympiad style, different for each seed."""
    rng = random.Random(seed)
    lines = ["#include <bits/stdc++.h>", "using namespace std;", "#define ll long long", "int n,m,k,a[100005],b[100005];", "int main(){", "    cin>>n;"]
    for _ in range(18):
        template = rng.choice(_STATEMENTS)
        lines.append("    " + template.format(op=rng.choice("+-*^|"), cmp=rng.choice(["<", ">", "<=", "==", "!="])))
    if rng.random() < 0.5:
        lines.append('    //cerr<<"dbg "<<ans<<endl;')
    lines.append("}")
    return "\n".join(lines) + "\n"


class _CreatedSubmission:
    def create_submission(self, request, problem_instance, form_data, **kwargs):
        return self.submission


class _PastingController(LLMDetectContestControllerMixin, _CreatedSubmission):
    pass


class TestFeatures(SimpleTestCase):
    def test_llm_style_separates_typical_code(self):
        llm_score, reasons = F.llm_style(F.extract_features(LLM_BFS, F.C_FAMILY))
        human_score, _reasons = F.llm_style(F.extract_features(HUMAN_BFS, F.C_FAMILY))
        self.assertGreater(llm_score, 0.8)
        self.assertLess(human_score, 0.1)
        self.assertIn("sentence_comment_ratio", [name for name, _c in reasons])

    def test_using_namespace_std_is_not_a_std_prefix(self):
        features = F.extract_features(SCHOOL_CODE, F.C_FAMILY)
        self.assertEqual(features["std_qualified_per_100"], 0)
        self.assertEqual(features["using_namespace_std"], 1)
        self.assertGreater(F.extract_features("int main() { std::cout << 1; std::cin.tie(0); }", F.C_FAMILY)["std_qualified_per_100"], 0)

    def test_llm_style_explained_both_ways(self):
        score, reasons = F.llm_style(F.extract_features(SCHOOL_CODE, F.C_FAMILY))
        self.assertLess(score, 0.1)
        detail = {
            "score": score,
            "reasons": [name for name, c in reasons if c > 0][:3],
            "against": [name for name, c in reasons if c < 0][:3],
        }
        text = describe("llm_style", detail)
        self.assertIn("LLM-likeness of the code: ", text)
        self.assertIn("Unlike LLM code: using namespace std, one- or two-letter identifiers", text)
        self.assertNotIn("std:: prefixes", text)

    def test_fingerprints_survive_renaming_and_comment_removal(self):
        disguised = LLM_BFS
        for old, new in [("numVertices", "n"), ("numEdges", "m"), ("adjacency", "g"), ("distance", "d"), ("pending", "q"), ("current", "v")]:
            disguised = disguised.replace(old, new)
        disguised = "\n".join(line for line in disguised.splitlines() if not line.strip().startswith("//"))
        original = F.fingerprints(LLM_BFS, F.C_FAMILY)
        self.assertGreater(F.containment(F.fingerprints(disguised, F.C_FAMILY), original), 0.9)
        self.assertLess(F.containment(F.fingerprints(HUMAN_BFS, F.C_FAMILY), original), 0.4)

    def test_lexer(self):
        tokens, comments = F.lex('#include <x>\nint a = 5; // one\nchar* s = "// not a comment"; /* two */\n', F.C_FAMILY)
        self.assertEqual([c.strip() for c in comments], ["one", "two"])
        self.assertIn(("str", "// not a comment"), tokens)
        self.assertEqual(tokens[0], ("pp", "#include <x>"))
        tokens, comments = F.lex('def f():\n    """Docstring."""\n    return "x"  # note\n', F.PYTHON)
        self.assertEqual([c.strip() for c in comments], ["Docstring.", "note"])

    def test_polish_translations(self):
        with translation.override("pl"):
            self.assertEqual(str(SIGNAL_LABELS["llm_style"]), "Styl typowy dla LLM")
            self.assertEqual(str(HABIT_LABELS["includes"]), "nagłówki")
            self.assertEqual(habit_value("none"), "brak")

    def test_language_and_style_vector(self):
        self.assertEqual(F.language_of("__pasted_code.py"), F.PYTHON)
        self.assertEqual(F.language_of("a.cpp"), F.C_FAMILY)
        vector = F.style_vector(F.extract_features(HUMAN_BFS, F.C_FAMILY))
        self.assertEqual(len(vector), len(F.STYLE_FEATURES))


class TestHabits(SimpleTestCase):
    def test_habits_of_typical_code(self):
        human = extract_habits(HUMAN_BFS, F.C_FAMILY)["values"]
        llm = extract_habits(LLM_BFS, F.C_FAMILY)["values"]
        expected = {
            "includes": ("bits/stdc++.h", "separate"),
            "namespace": ("using namespace std;", "std::"),
            "fast_io": ("ios_base::sync_with_stdio(0); cin.tie(0)", "std::ios::sync_with_stdio(false); std::cin.tie(nullptr)"),
            "storage": ("global arrays", "vectors"),
            "size_constant": ("N = 1eX+c", None),
            "keyword_space": ("if(", "if ("),
            "operator_space": ("tight", "spaced"),
            "naming": ("short names", "camelCase"),
            "main": ("int main()", "int main()"),
            "line_endings": ("LF", "LF"),
        }
        for habit, (human_value, llm_value) in expected.items():
            self.assertEqual((human[habit], llm[habit]), (human_value, llm_value), habit)
        self.assertEqual(extract_habits(HUMAN_BFS, F.C_FAMILY)["macros"], ["pb"])

    def test_aliases_tests_and_functions(self):
        code = "#include <bits/stdc++.h>\ntypedef long long ll;\nvoid solve(){ll x;}\nsigned main(){int t;cin>>t;while(t--)solve();}\n"
        values = extract_habits(code, F.C_FAMILY)["values"]
        self.assertEqual(values["ll_alias"], "typedef long long ll;")
        self.assertEqual(values["test_loop"], "solve() in a test loop")
        self.assertEqual(values["main"], "signed main()")
        self.assertEqual(values["function_names"], "short")
        template = extract_habits(code, F.C_FAMILY)["template"]
        self.assertEqual(template, ["#include<bits/stdc++.h>", "typedef long long ll;"])

    def test_line_endings_and_indentation(self):
        code = "int main() {\r\n\tint a;\r\n\tint b;\r\n\treturn 0;\r\n}\r\n"
        self.assertEqual(extract_habits(code, F.C_FAMILY)["values"]["line_endings"], "CRLF")
        self.assertEqual(extract_habits(code, F.C_FAMILY)["values"]["indent"], "tabs")
        # A browser sends a pasted form field with CRLF, whatever the author uses.
        self.assertNotIn("line_endings", extract_habits(code, F.C_FAMILY, pasted=True)["values"])

    def test_chat_leftovers_and_paste_characters(self):
        answer = "Here is the solution:\n```cpp\n// Time complexity: O(n log n)\nint main() {\n    int\u00a0n;\n}\n```\n"
        habits = extract_habits(answer, F.C_FAMILY)
        self.assertEqual(set(habits["chat"]), {"markdown fence", "complexity", "big O"})
        self.assertEqual(habits["paste_chars"], ["no-break space"])
        clean = extract_habits("\ufeff" + HUMAN_BFS, F.C_FAMILY)
        self.assertEqual((clean["chat"], clean["paste_chars"]), ([], []))
        # Prose in code is not a chat leftover, comments are searched.
        self.assertEqual(extract_habits("// here is the dp\nint main(){}", F.C_FAMILY)["chat"], ["here is"])


class LLMDetectTestCase(TestCase):
    fixtures = ["test_users"]

    def setUp(self):
        super().setUp()
        self.contest = Contest.objects.create(id="camp", name="Camp", controller_name="oioioi.programs.controllers.ProgrammingContestController")
        self.rounds = [
            Round.objects.create(contest=self.contest, name=f"Day {day}", start_date=T0 + timedelta(days=day), end_date=T0 + timedelta(days=day, hours=5))
            for day in range(4)
        ]
        self.pis = [self._problem(round, name) for round, name in zip(self.rounds, ["aaa", "bbb", "ccc", "ddd"], strict=False)]
        self.humans = [User.objects.create_user(f"human{i}", first_name="Human", last_name=str(i)) for i in range(7)]
        self.cheater = User.objects.get(username="test_user")
        self.admin = User.objects.get(username="test_admin")

    def _problem(self, round, short_name):
        controller = "oioioi.programs.controllers.ProgrammingProblemController"
        problem = Problem.objects.create(legacy_name=short_name, short_name=short_name, controller_name=controller)
        return ProblemInstance.objects.create(problem=problem, round=round, contest=self.contest, short_name=short_name)

    def _submit(self, user, pi, code, minutes, score=100, pasted=False):
        date = pi.round.start_date + timedelta(minutes=minutes)
        submission = ProgramSubmission(user=user, problem_instance=pi, date=date, score=IntegerScore(score), max_score=IntegerScore(100))
        submission.source_file.save("__pasted_code.cpp" if pasted else "sol.cpp", ContentFile(code.encode()), save=False)
        submission.save()
        if pasted:
            # As the submission form does.
            controller = _PastingController()
            controller.submission = submission
            controller.create_submission(None, pi, {"file": None, "code": code})
        return submission

    def _camp(self, cheater_code=LLM_BFS, pasted=False):
        """Everybody writes in their own style on the first three days; on
        the last one the cheater pastes an LLM solution, quickly.
        """
        for i, user in enumerate(self.humans + [self.cheater]):
            for day, pi in enumerate(self.pis[:3]):
                self._submit(user, pi, human_code(f"{i}-{day}"), minutes=90 + 7 * i)
        for i, user in enumerate(self.humans):
            self._submit(user, self.pis[3], human_code(f"{i}-3"), minutes=100 + 9 * i)
        return self._submit(self.cheater, self.pis[3], cheater_code, minutes=6, pasted=pasted)


class TestAssessment(LLMDetectTestCase):
    def test_pasted_llm_solution_is_flagged(self):
        LLMReferenceSolution.objects.create(problem_instance=self.pis[3], model_name="chat", source=LLM_BFS)
        disguised = LLM_BFS.replace("numVertices", "n").replace("adjacency", "g")
        submission = self._camp(disguised, pasted=True)
        assess_contest(self.contest)

        flagged = RiskAssessment.objects.get(submission=submission)
        self.assertGreaterEqual(flagged.risk, 0.9)
        for name in ("llm_reference", "llm_style", "style_shift", "fast_solve", "pasted"):
            self.assertIn(name, flagged.signals)
        others = RiskAssessment.objects.filter(contest=self.contest).exclude(submission=submission)
        self.assertEqual(others.count(), 4 * 7 + 3)
        self.assertLess(max(a.risk for a in others), FLAG_THRESHOLD)

        rows = user_rankings(self.contest)
        self.assertEqual(rows[0]["user_id"], self.cheater.id)
        self.assertEqual(rows[0]["flagged"], 1)

    def test_style_alone_without_references(self):
        submission = self._camp()
        assess_contest(self.contest)
        assessment = RiskAssessment.objects.get(submission=submission)
        self.assertNotIn("llm_reference", assessment.signals)
        self.assertIn("style_shift", assessment.signals)
        self.assertEqual(user_rankings(self.contest)[0]["user_id"], self.cheater.id)
        self.assertGreater(assessment.context["profile"]["confidence"], 0.3)

    def test_staff_submissions_are_left_out(self):
        self._submit(self.admin, self.pis[0], LLM_BFS, minutes=1)
        contest_admin = User.objects.get(username="test_user2")
        ContestPermission.objects.create(user=contest_admin, contest=self.contest, permission="contests.contest_basicadmin")
        self._submit(contest_admin, self.pis[0], LLM_BFS, minutes=1)
        self._submit(self.humans[0], self.pis[0], HUMAN_BFS, minutes=50)
        assess_contest(self.contest)
        self.assertEqual(list(RiskAssessment.objects.values_list("user_id", flat=True)), [self.humans[0].id])

    def test_needs_update(self):
        self.assertTrue(needs_update(self.contest))
        assess_contest(self.contest)
        self.assertFalse(needs_update(self.contest))
        self._submit(self.humans[0], self.pis[0], HUMAN_BFS, minutes=50)
        self.assertTrue(needs_update(self.contest))
        assess_contest(self.contest)
        LLMReferenceSolution.objects.create(problem_instance=self.pis[0], source=LLM_BFS)
        config = LLMDetectConfig.objects.get(contest=self.contest)
        config.dirty = True
        config.save()
        self.assertTrue(needs_update(self.contest))

    def test_trusted_rounds_weigh_more(self):
        self._camp()
        assess_contest(self.contest)
        before = RiskAssessment.objects.get(user=self.cheater, submission__problem_instance=self.pis[3]).context["profile"]["weight"]
        config = LLMDetectConfig.objects.get(contest=self.contest)
        config.trusted_rounds.add(self.rounds[0])
        assess_contest(self.contest)
        after = RiskAssessment.objects.get(user=self.cheater, submission__problem_instance=self.pis[3]).context["profile"]["weight"]
        self.assertGreater(after, before)

    def test_rewrite_and_peer_similarity(self):
        self._camp()
        self._submit(self.cheater, self.pis[2], HUMAN_BFS, minutes=200, score=10)
        second = self._submit(self.cheater, self.pis[2], LLM_BFS, minutes=204)
        copy = self._submit(self.humans[0], self.pis[2], LLM_BFS.replace("pending", "todo"), minutes=230)
        assess_contest(self.contest)
        signals = RiskAssessment.objects.get(submission=second).signals
        self.assertIn("rewrite", signals)
        self.assertIn("peer_similarity", RiskAssessment.objects.get(submission=copy).signals)

    def test_after_round_only_code_signals(self):
        self._camp()
        # After the round both write the solution discussed at the lecture.
        late = [self._submit(user, self.pis[2], HUMAN_BFS, minutes=8 * 60) for user in self.humans[:2]]
        assess_contest(self.contest)
        for submission in late:
            assessment = RiskAssessment.objects.get(submission=submission)
            self.assertFalse(assessment.context["during_round"])
            self.assertEqual(assessment.context["peer"]["similarity"], 1.0)
            self.assertNotIn("peer_similarity", assessment.signals)
            self.assertNotIn("burst", assessment.signals)

    def test_broken_habits_and_new_names(self):
        submission = self._camp()
        assess_contest(self.contest)
        assessment = RiskAssessment.objects.get(submission=submission)
        broken = {row["habit"] for row in assessment.signals["habit_break"]["detail"]["broken"]}
        self.assertTrue({"includes", "namespace", "ll_alias", "template", "naming"} <= broken)
        text = describe("habit_break", assessment.signals["habit_break"]["detail"])
        self.assertIn("Template: includes, std namespace, long long alias", text)
        self.assertIn("Details in the habits table below.", text)
        # One or two broken habits are described one by one.
        self.assertIn("includes: usually", describe("habit_break", {"broken": assessment.signals["habit_break"]["detail"]["broken"][:1]}))
        self.assertIn("numVertices", assessment.signals["vocabulary"]["detail"]["new"])
        others = RiskAssessment.objects.filter(contest=self.contest).exclude(submission=submission)
        self.assertFalse([a for a in others if "habit_break" in a.signals or "vocabulary" in a.signals])

    def test_habit_changed_by_everybody_on_a_problem_is_not_counted(self):
        for i, user in enumerate(self.humans):
            for day, pi in enumerate(self.pis[:3]):
                self._submit(user, pi, human_code(f"{i}-{day}"), minutes=90)
            # E.g. the statement suggests a template with tabs.
            self._submit(user, self.pis[3], human_code(f"{i}-3").replace("    ", "\t"), minutes=100)
        assess_contest(self.contest)
        for assessment in RiskAssessment.objects.filter(submission__problem_instance=self.pis[3]):
            self.assertNotIn("habit_break", assessment.signals)
            indent = next(row for row in assessment.context["habits"] if row["habit"] == "indent")
            self.assertTrue(indent["broken"] and indent["problem"])

    def test_chat_answer_pasted_as_a_whole(self):
        answer = "Here is the solution:\n```cpp\n" + LLM_BFS + "```\nExplanation: BFS.\n"
        submission = self._camp(answer.replace("int current", "int\u00a0current"))
        assess_contest(self.contest)
        signals = RiskAssessment.objects.get(submission=submission).signals
        self.assertEqual(signals["chat_leftovers"]["value"], 1.0)
        self.assertGreater(signals["paste_characters"]["value"], 0.8)


class TestHooks(LLMDetectTestCase):
    def test_features_computed_when_judged(self):
        submission = self._submit(self.humans[0], self.pis[0], HUMAN_BFS, minutes=10)
        self.contest.controller.submission_judged(submission)
        features = SubmissionFeatures.objects.get(submission=submission)
        self.assertFalse(features.pasted)
        self.assertEqual(features.language, F.C_FAMILY)

    def test_pasted_code_is_remembered(self):
        submission = self._submit(self.humans[0], self.pis[0], HUMAN_BFS, minutes=10, pasted=True)
        self.assertTrue(SubmissionFeatures.objects.get(submission=submission).pasted)
        # Judging later keeps it, and so does recomputing the features.
        self.contest.controller.submission_judged(submission)
        compute_features(submission)
        self.assertTrue(SubmissionFeatures.objects.get(submission=submission).pasted)

    def test_middleware_logs_first_statement_view(self):
        factory = RequestFactory()
        middleware = ProblemViewMiddleware(lambda request: HttpResponse())
        for minutes in (5, 30):
            request = factory.get("/")
            request.user = self.humans[0]
            request.contest = self.contest
            request.timestamp = T0 + timedelta(minutes=minutes)
            request.resolver_match = SimpleNamespace(url_name="problem_statement", kwargs={"problem_instance": "aaa"})
            middleware(request)
        event = ProblemViewEvent.objects.get()
        self.assertEqual(event.date, T0 + timedelta(minutes=5))
        self.assertEqual(event.problem_instance, self.pis[0])


class TestViews(LLMDetectTestCase):
    def _url(self, name, **kwargs):
        return reverse(name, kwargs={"contest_id": self.contest.id, **kwargs})

    def test_permissions(self):
        self._camp()
        # Risk scores are sensitive: basic admins do not see them either.
        ContestPermission.objects.create(user=self.humans[0], contest=self.contest, permission="contests.contest_basicadmin")
        self.assertTrue(self.client.login(username="human0"))
        self.assertEqual(self.client.get(self._url("llmdetect_panel")).status_code, 403)
        self.assertTrue(self.client.login(username="test_user2"))
        for url in (self._url("llmdetect_panel"), self._url("llmdetect_user", user_id=self.cheater.id), self._url("llmdetect_references")):
            self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(self._url("llmdetect_recompute")).status_code, 403)

    def test_panel_and_user_page(self):
        LLMReferenceSolution.objects.create(problem_instance=self.pis[3], model_name="chat", source=LLM_BFS)
        submission = self._camp()
        self.assertTrue(self.client.login(username="test_admin"))
        response = self.client.get(self._url("llmdetect_panel"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["rows"][0]["user"], self.cheater)
        response = self.client.get(self._url("llmdetect_user", user_id=self.cheater.id))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected"].submission_id, submission.id)
        self.assertContains(response, "numVertices")  # the submission and the reference
        self.assertIsNotNone(response.context["typical_source"])

    def test_submission_links(self):
        submission = self._camp()
        self.assertTrue(self.client.login(username="test_admin"))
        code_url = reverse("submission", kwargs={"contest_id": self.contest.id, "submission_id": submission.id})
        analysis_url = self._url("llmdetect_user", user_id=self.cheater.id) + f"?submission={submission.id}"
        # The queue leads to the analysis...
        panel = self.client.get(self._url("llmdetect_panel"))
        self.assertContains(panel, f'href="{analysis_url}"')
        # ...which is above the list of submissions, whose numbers open the code.
        page = self.client.get(analysis_url)
        content = page.content.decode()
        self.assertLess(content.index("Nearest LLM reference"), content.index('<h2 class="h4 mt-4">Submissions</h2>'))
        self.assertContains(page, f'<a href="{code_url}" title="open the submission">#{submission.id}</a>', html=True)
        self.assertEqual(self.client.get(code_url).status_code, 200)

    def test_disqualify_button(self):
        from oioioi.disqualification.models import Disqualification

        submission = self._camp()
        self.assertTrue(self.client.login(username="test_admin"))
        url = self._url("llmdetect_user", user_id=self.cheater.id) + f"?submission={submission.id}"
        page = self.client.get(url)
        self.assertContains(page, "Disqualify this submission")
        form_url = page.context["disqualify_url"]
        self.assertIn(f"submission={submission.id}", form_url)
        self.assertIn(f"user={self.cheater.id}", form_url)
        self.assertTrue(form_url.startswith(f"/c/{self.contest.id}/admin/"), form_url)
        form = self.client.get(form_url)
        self.assertEqual(form.status_code, 200)
        initial = form.context["adminform"].form.initial
        self.assertEqual(str(initial["submission"]), str(submission.id))
        self.assertIn(f"#{submission.id}", initial["content"])
        Disqualification.objects.create(contest=self.contest, user=self.cheater, submission=submission, title="LLM", content="Generated.")
        page = self.client.get(url)
        self.assertNotContains(page, "Disqualify this submission")
        self.assertTrue(page.context["selected_disqualified"])

    def test_confirmed_decision_becomes_reference(self):
        submission = self._camp()
        self.assertTrue(self.client.login(username="test_admin"))
        url = self._url("llmdetect_user", user_id=self.cheater.id)
        response = self.client.post(url, {"verdict": ReviewDecision.VERDICT_CONFIRMED, "submission": submission.id, "note": "Could not explain it."})
        self.assertEqual(response.status_code, 302)
        decision = ReviewDecision.objects.get()
        self.assertEqual(decision.reviewer, self.admin)
        self.assertIsNotNone(decision.risk)
        reference = LLMReferenceSolution.objects.get()
        self.assertEqual(reference.kind, LLMReferenceSolution.KIND_CONFIRMED)
        self.assertEqual(reference.problem_instance, self.pis[3])
        self.assertTrue(LLMDetectConfig.objects.get(contest=self.contest).dirty)

    def test_add_and_delete_references(self):
        self.assertTrue(self.client.login(username="test_admin"))
        url = self._url("llmdetect_references") + f"?problem={self.pis[1].id}"
        files = [ContentFile(LLM_BFS.encode(), name="a.cpp"), ContentFile(HUMAN_BFS.encode(), name="b.cpp")]
        with patch("oioioi.llmdetect.views.statement_text", return_value=("", None, [])):
            response = self.client.post(url, {"action": "answers", "model_name": "m", "prompt_variant": "student", "files": files})
            self.assertEqual(response.status_code, 302)
            self.assertEqual(LLMReferenceSolution.objects.filter(problem_instance=self.pis[1], model_name="m").count(), 2)
            self.assertEqual(set(LLMReferenceSolution.objects.values_list("filename", flat=True)), {"a.cpp", "b.cpp"})
            self.assertTrue(LLMReferenceSolution.objects.first().fingerprints)
            response = self.client.post(url, {"action": "answers"})
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "Paste an answer or choose files.")
        self.assertEqual(LLMReferenceSolution.objects.count(), 2)
        reference = LLMReferenceSolution.objects.first()
        self.client.post(self._url("llmdetect_delete_reference", reference_id=reference.id))
        self.assertEqual(LLMReferenceSolution.objects.count(), 1)

    def test_chat_answers_added_by_hand_keep_only_the_code(self):
        self.assertTrue(self.client.login(username="test_admin"))
        url = self._url("llmdetect_references") + f"?problem={self.pis[1].id}"
        answer = "Here is the solution:\n\n```cpp\n" + LLM_BFS + "```\n\nExplanation: we run BFS from vertex 1.\n"
        with patch("oioioi.llmdetect.views.statement_text", return_value=("", None, [])):
            self.client.post(url, {"action": "answers", "text": answer})
            # The same code from a file is already stored.
            self.client.post(url, {"action": "answers", "files": [ContentFile(answer.encode(), name="chat.txt")]})
            self.client.post(url, {"action": "answers", "text": HUMAN_BFS})
        sources = list(LLMReferenceSolution.objects.order_by("id").values_list("source", flat=True))
        self.assertEqual(sources, [LLM_BFS, HUMAN_BFS])

    def test_settings(self):
        self.assertTrue(self.client.login(username="test_admin"))
        self.client.post(self._url("llmdetect_settings"), {"trusted_rounds": [self.rounds[0].id]})
        self.assertEqual(list(LLMDetectConfig.objects.get(contest=self.contest).trusted_rounds.all()), [self.rounds[0]])


class TestCommands(LLMDetectTestCase):
    def test_import_update_purge(self):
        self._camp()
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "ddd"))
            os.makedirs(os.path.join(root, "nope"))
            with open(os.path.join(root, "ddd", "gpt__short__1.cpp"), "w") as file:
                file.write(LLM_BFS)
            with open(os.path.join(root, "ddd", "gemini__plain__1.txt"), "w") as file:
                file.write("Sure! ```cpp\n" + HUMAN_BFS + "``` This reads the graph.")
            call_command("llmdetect_import_references", "camp", root, stdout=StringIO(), stderr=StringIO())
        reference = LLMReferenceSolution.objects.get(model_name="gpt")
        self.assertEqual((reference.prompt_variant, reference.source), ("short", LLM_BFS))
        self.assertEqual(LLMReferenceSolution.objects.get(model_name="gemini").source, HUMAN_BFS)

        call_command("llmdetect_update", "camp", stdout=StringIO())
        self.assertTrue(RiskAssessment.objects.exists())
        ReviewDecision.objects.create(contest=self.contest, user=self.cheater, verdict=ReviewDecision.VERDICT_TALK, note="x")

        call_command("llmdetect_purge", "camp", stdout=StringIO())
        self.assertFalse(RiskAssessment.objects.exists())
        self.assertFalse(SubmissionFeatures.objects.exists())
        decision = ReviewDecision.objects.get()
        self.assertIsNone(decision.user)
        self.assertEqual(decision.note, "")


def _load_script():
    path = os.path.join(os.path.dirname(__file__), "tools", "llm_references.py")
    spec = importlib.util.spec_from_file_location("llm_references", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


script = _load_script()


class TestPrompts(SimpleTestCase):
    def test_extract_code(self):
        answer = "Here is the solution:\n```cpp\nint main() {}\n```\nAnd a test:\n```\n1 2\n```"
        self.assertEqual(extract_code(answer), "int main() {}\n")
        self.assertEqual(extract_code("int main() {}"), "int main() {}\n")
        self.assertEqual(script.extract_code(answer), extract_code(answer))

    def test_prompts_contain_the_statement(self):
        for variant in PROMPTS:
            prompt = build_prompt(variant, "Given n, print n+1.\n")
            self.assertTrue(prompt.endswith("\n\nGiven n, print n+1."))
            self.assertIn("C++", prompt)

    def test_statement_text_formats(self):
        from oioioi.llmdetect.statements import file_text

        self.assertEqual(file_text(b"<html><body><h1>Zadanie</h1><p>Dane jest n.</p></body></html>", ".html"), "Zadanie\nDane jest n.")
        self.assertEqual(file_text(b"Dane\r\n\r\n\r\n\r\njest n.", ".txt"), "Dane\n\njest n.")
        self.assertEqual(file_text(b"\x00", ".ps"), "")


class TestBulkFormat(SimpleTestCase):
    problems = {"abc": "pi-abc", "xyz": "pi-xyz"}

    def test_parse(self):
        text = (
            '### reference problem=abc model="ChatGPT free" prompt=polish\n'
            "int main(){}\n"
            "\n"
            "### Reference problem=xyz model=groq/openai/gpt-oss-120b prompt=student n=2 extra=ignored\n"
            "Here is the solution:\n```cpp\nint main() { return 0; }\n```\nExplanation: none.\n"
            "### reference model=duck.ai\n"
            "int f(){}\n"
        )
        references, errors = parse_references(text, self.problems, default_problem="pi-default")
        self.assertEqual(errors, [])
        self.assertEqual(
            [(r["problem_instance"], r["model"], r["prompt"], r["source"], r["line"]) for r in references],
            [
                ("pi-abc", "ChatGPT free", "polish", "int main(){}\n", 1),
                ("pi-xyz", "groq/openai/gpt-oss-120b", "student", "int main() { return 0; }\n", 4),
                ("pi-default", "duck.ai", "", "int f(){}\n", 10),
            ],
        )

    def test_errors(self):
        text = "stray text\n### reference problem=nope\nint main(){}\n### reference problem=abc\n\n### reference\nint g(){}\n"
        references, errors = parse_references(text, self.problems)
        self.assertEqual(len(errors), 4)
        self.assertIn("before the first", errors[0])
        self.assertIn("no problem nope", errors[1])
        self.assertIn("Line 4: no code", errors[2])
        self.assertIn("Line 6: no problem given", errors[3])
        self.assertEqual(parse_references("int main(){}", self.problems)[1], ['No "### reference" lines found.'])


class TestScript(SimpleTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.output = os.path.join(self.directory.name, "references.txt")
        self.now = [0.0]
        self.slept = []
        patches = [
            patch.object(script, "clock", lambda: self.now[0]),
            patch.object(script, "sleep", self._sleep),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.directory.cleanup()

    def _sleep(self, seconds):
        self.slept.append(round(seconds, 3))
        self.now[0] += seconds

    def _providers(self, **configs):
        return [script.Provider(name, config) for name, config in configs.items()]

    prompts = [{"problem": "abc", "prompt": "plain", "text": "P1"}, {"problem": "xyz", "prompt": "student", "text": "P2"}]

    def test_output_is_the_bulk_format_and_providers_alternate(self):
        asked = []

        def ask(config, model, prompt):
            asked.append((config["name"], model, prompt))
            return f"Sure:\n```cpp\n// {model} {prompt}\nint main(){{}}\n```"

        providers = self._providers(a={"name": "a", "models": ["m1"], "min_interval": 10}, b={"name": "b", "models": ["m2", "m3"], "min_interval": 10})
        with patch.object(script, "ask", ask):
            result = script.run(self.prompts, providers, count=2, output=self.output, log=lambda *a: None)
        self.assertEqual(result, {"done": 12, "failed": 0, "left": 0})
        # a and b take turns; neither waits for the other's interval.
        self.assertEqual([name for name, _m, _p in asked[:4]], ["a", "b", "a", "b"])
        with open(self.output, encoding="utf-8") as file:
            text = file.read()
        references, errors = parse_references(text, {"abc": "pi-abc", "xyz": "pi-xyz"})
        self.assertEqual(errors, [])
        self.assertEqual(len(references), 12)
        self.assertEqual(references[0]["source"], "// m1 P1\nint main(){}\n")
        self.assertEqual({r["model"] for r in references}, {"a/m1", "b/m2", "b/m3"})

    def test_resume_and_quota(self):
        calls = {"n": 0}

        def ask(config, model, prompt):
            calls["n"] += 1
            if config["name"] == "b":
                raise script.QuotaError("HTTP 429: daily limit", retry_after=3600)
            return "int main(){}"

        providers = self._providers(a={"name": "a", "models": ["m"]}, b={"name": "b", "models": ["m"]})
        with patch.object(script, "ask", ask):
            result = script.run(self.prompts, providers, count=1, output=self.output, log=lambda *a: None)
        # b is out of quota: left for the next run, a finished.
        self.assertEqual(result, {"done": 2, "failed": 0, "left": 2})
        self.assertEqual(providers[1].stopped.startswith("out of quota"), True)
        # The next run asks only for what is missing.
        providers = self._providers(a={"name": "a", "models": ["m"]}, b={"name": "b", "models": ["m"]})
        with patch.object(script, "ask", lambda config, model, prompt: "int g(){}"):
            result = script.run(self.prompts, providers, count=1, output=self.output, log=lambda *a: None)
        self.assertEqual(result, {"done": 2, "failed": 0, "left": 0})
        self.assertEqual(len(script.done_keys(self.output)), 4)

    def test_short_waits_and_daily_limit(self):
        answers = iter([script.QuotaError("HTTP 429", retry_after=20), "int a(){}", "int b(){}", "int c(){}"])

        def ask(config, model, prompt):
            answer = next(answers)
            if isinstance(answer, Exception):
                raise answer
            return answer

        providers = self._providers(a={"name": "a", "models": ["m"], "daily_limit": 2})
        with patch.object(script, "ask", ask):
            result = script.run(self.prompts, providers, count=2, output=self.output, log=lambda *a: None)
        self.assertIn(20, self.slept)
        # Two requests (one refused, one answered), then the daily limit.
        self.assertEqual(result, {"done": 1, "failed": 0, "left": 3})
        self.assertIn("daily_limit", providers[0].stopped)

    def test_unknown_model_is_skipped(self):
        def ask(config, model, prompt):
            if model == "gone":
                raise script.ModelError("HTTP 404: no such model")
            return "int main(){}"

        providers = self._providers(a={"name": "a", "models": ["gone", "ok"]})
        with patch.object(script, "ask", ask) as _ask:
            result = script.run(self.prompts, providers, count=2, output=self.output, log=lambda *a: None)
        self.assertEqual(result, {"done": 4, "failed": 4, "left": 0})

    def test_providers_without_a_key_are_skipped(self):
        config = os.path.join(self.directory.name, "providers.json")
        with open(config, "w") as file:
            json.dump(
                {"x": {"api_key_env": "LLMDETECT_TEST_UNSET", "models": ["m"]}, "local": {"base_url": "http://h/v1", "models": ["m"]}, "empty": {"models": []}},
                file,
            )
        logged = []
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LLMDETECT_TEST_UNSET", None)
            providers = script.load_providers(config, log=logged.append)
        self.assertEqual([p.name for p in providers], ["local"])
        self.assertIn("LLMDETECT_TEST_UNSET is not set", logged[0])

    def test_openai_request(self):
        response = MagicMock()
        response.read.return_value = json.dumps({"choices": [{"message": {"content": "int main(){}"}}]}).encode()
        response.__enter__.return_value = response
        with patch.object(script.urllib.request, "urlopen", return_value=response) as urlopen, patch.dict(os.environ, {"LLMDETECT_TEST_KEY": "k"}):
            answer = script.ask({"base_url": "https://example.com/v1/", "api_key_env": "LLMDETECT_TEST_KEY", "max_tokens": 100}, "m", "prompt")
        self.assertEqual(answer, "int main(){}")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://example.com/v1/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer k")
        self.assertEqual(json.loads(request.data), {"model": "m", "messages": [{"role": "user", "content": "prompt"}], "max_tokens": 100})

    def test_rate_limit_headers(self):
        self.assertEqual(script.seconds("30"), 30)
        self.assertEqual(script.seconds("2m59.5s"), 179.5)
        self.assertEqual(script.seconds("1h"), 3600)
        self.assertEqual(script.seconds(str(int((1_800_000_000 + 90) * 1000)), now=1_800_000_000), 90)
        self.assertIsNone(script.seconds("soon"))
        self.assertEqual(script.header(problem="abc", model="ChatGPT free", n=1), '### reference problem=abc model="ChatGPT free" n=1')


class TestReferencesPage(LLMDetectTestCase):
    def _url(self, name="llmdetect_references", **params):
        url = reverse(name, kwargs={"contest_id": self.contest.id})
        return url + ("?" + "&".join(f"{k}={v}" for k, v in params.items()) if params else "")

    def setUp(self):
        super().setUp()
        self.assertTrue(self.client.login(username="test_admin"))

    def _read(self, text="Dane jest $n \\le 10^9$.", source="package", warnings=()):
        return patch("oioioi.llmdetect.views.statement_text", return_value=(text, source, list(warnings)))

    def test_statement_read_once_then_checked_and_edited(self):
        with self._read(warnings=["Check it."]) as read:
            response = self.client.get(self._url(problem=self.pis[0].id))
            self.client.get(self._url(problem=self.pis[0].id))
        self.assertEqual(read.call_count, 1)  # stored after the first reading
        self.assertEqual(response.context["statement_form"].initial["text"], "Dane jest $n \\le 10^9$.")
        self.assertContains(response, "Read from the LaTeX source in the problem package")
        self.assertContains(response, "Check it.")
        self.assertContains(response, "not checked yet")
        for _label, instruction in PROMPTS.values():
            self.assertContains(response, f'data-instruction="{instruction}"')
        self.client.post(self._url(problem=self.pis[0].id), {"action": "statement", "text": "Poprawione."})
        statement = LLMStatement.objects.get(problem_instance=self.pis[0])
        self.assertEqual((statement.text, statement.source, statement.checked), ("Poprawione.", "edited", True))
        # Reading again replaces the edited text.
        with self._read(text="Nowa treść.", source="file"):
            self.client.post(self._url(problem=self.pis[0].id), {"action": "reread"})
        statement.refresh_from_db()
        self.assertEqual((statement.text, statement.source, statement.checked), ("Nowa treść.", "file", False))

    def test_unreadable_statement(self):
        with self._read(text="", source=None, warnings=["No statement found for this problem."]):
            response = self.client.get(self._url(problem=self.pis[1].id))
        self.assertContains(response, "No statement found for this problem.")
        self.assertContains(response, "it could not be read")

    def test_download_prompts_of_one_problem_saves_the_statement(self):
        with self._read():
            response = self.client.post(
                self._url(problem=self.pis[0].id), {"action": "statement", "text": "Edited.", "prompts": ["plain", "student"], "download": "1"}
            )
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertIn("prompts-camp-aaa.json", response["Content-Disposition"])
        data = json.loads(response.content)
        self.assertEqual(data["format"], PROMPTS_FORMAT)
        self.assertEqual(data["prompts"], [{"problem": "aaa", "prompt": v, "text": build_prompt(v, "Edited.")} for v in ("plain", "student")])
        self.assertTrue(LLMStatement.objects.get(problem_instance=self.pis[0]).checked)

    def test_download_prompts_of_all_problems_and_the_script(self):
        LLMStatement.objects.create(problem_instance=self.pis[0], text="Checked aaa.", checked=True)
        with patch(
            "oioioi.llmdetect.views.statement_text", side_effect=lambda pi: ("", None, []) if pi.short_name == "bbb" else (f"Read {pi.short_name}", "file", [])
        ):
            data = json.loads(self.client.get(self._url("llmdetect_all_prompts")).content)
        self.assertEqual({p["problem"] for p in data["prompts"]}, {"aaa", "ccc", "ddd"})
        self.assertEqual(len(data["prompts"]), 3 * len(PROMPTS))
        self.assertIn("Checked aaa.", next(p["text"] for p in data["prompts"] if p["problem"] == "aaa"))
        response = self.client.get(self._url("llmdetect_script"))
        self.assertIn("llm_references.py", response["Content-Disposition"])
        self.assertIn(b"def main(argv=None):", response.content)

    def test_add_a_pasted_chat_answer(self):
        with self._read():
            answer = "Oto rozwiązanie:\n```cpp\nint main(){}\n```\nWyjaśnienie: proste."
            self.client.post(self._url(problem=self.pis[1].id), {"action": "answers", "text": answer, "model_name": "ChatGPT", "prompt_variant": "polish"})
        reference = LLMReferenceSolution.objects.get()
        self.assertEqual(
            (reference.problem_instance, reference.model_name, reference.prompt_variant, reference.source), (self.pis[1], "ChatGPT", "polish", "int main(){}\n")
        )

    def test_add_many(self):
        text = "### reference problem=aaa model=x prompt=plain\nint a(){}\n### reference problem=bbb model=y\n```cpp\nint b(){}\n```\n"
        with self._read():
            response = self.client.post(self._url(problem=self.pis[2].id), {"action": "answers", "text": text})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            sorted(LLMReferenceSolution.objects.values_list("problem_instance__short_name", "model_name", "source")),
            [("aaa", "x", "int a(){}\n"), ("bbb", "y", "int b(){}\n")],
        )
        self.assertTrue(LLMDetectConfig.objects.get(contest=self.contest).dirty)
        # The same again, as a file, with one more: duplicates are skipped;
        # without problem= it is for the problem of the page.
        upload = ContentFile((text + "### reference\nint c(){}\n").encode(), name="references.txt")
        with self._read():
            self.client.post(self._url(problem=self.pis[2].id), {"action": "answers", "files": upload}, follow=True)
        self.assertEqual(LLMReferenceSolution.objects.count(), 3)
        self.assertEqual(LLMReferenceSolution.objects.get(source="int c(){}\n").problem_instance, self.pis[2])

    def test_add_many_with_errors_adds_nothing(self):
        text = "### reference problem=aaa\nint a(){}\n### reference problem=nope\nint b(){}\n"
        with self._read():
            response = self.client.post(self._url(problem=self.pis[0].id), {"action": "answers", "text": text})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "no problem nope")
        self.assertEqual(LLMReferenceSolution.objects.count(), 0)


def habits_of(source):
    return extract_habits(source, F.C_FAMILY)["values"]


class TestHostileCode(SimpleTestCase):
    """Participants control the code that is analysed on every submission
    (in the web request for pasted code, in the evaluation worker after
    judging): no input may make the analysis slow or its stored result big.
    """

    N = 100 * 1024  # the default submission size limit

    def _analyse(self, source, filename="a.cpp"):
        analysis = F.analyze_source(source, filename)
        return analysis, extract_habits(source, analysis["language"])

    def test_letters_and_digits_of_other_alphabets(self):
        # Text pasted above the code, outside a comment: "ę" is a letter and
        # "²" a digit for Python, but not for the ASCII patterns of the lexer.
        source = "11:34\nżółć gęślą jaźń ☺\ufe0f🙉 x² ① ٣\n#include <iostream>\nusing namespace std;\nint main(){int n;cin>>n;}\n"
        for filename in ("a.cpp", "a.py", "a.pas"):
            analysis, habits = self._analyse(source, filename)
            self.assertGreater(analysis["code_lines"], 0)
        tokens, _comments = F.lex(source, F.C_FAMILY)
        self.assertIn(("id", "żółć"), tokens)
        self.assertIn(("op", "①"), tokens)
        self.assertEqual(habits_of(source)["namespace"], "using namespace std;")

    def test_random_text_never_breaks_the_analysis(self):
        alphabet = list("ax_09 \t\n;{}()<>=+-*/#\"'\\") + list("ąęŁÓ☺🙉²①٣\u00a0\u200b\u0301ß日") + ["//", "/*", "*/", "#include", "1e9", "0x1F"]
        rng = random.Random(3)
        for _ in range(500):
            source = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 80)))
            for filename in ("a.cpp", "a.py", "a.pas"):
                self._analyse(source, filename)

    def test_no_quadratic_patterns(self):
        n = self.N
        cases = {
            "big O in a comment": "// " + "O(n" * (n // 3),
            "calls in a comment": "// " + "f(x)" * (n // 4),
            "open parentheses in a comment": "// " + "a(" * (n // 2),
            "block comment": "/*" + "f(x)" * (n // 4) + "*/",
            "main( repeated": "int main(" * (n // 9),
            "const without &": "const a " * (n // 8),
            "for( without ;": "for(" * (n // 4),
            "Python comment": "# " + "O(n" * (n // 3),
        }
        for name, source in cases.items():
            start = time.monotonic()
            self._analyse(source, "a.py" if name.startswith("Python") else "a.cpp")
            # Each took 2-11 s before the patterns were made linear; ~0.1 s now.
            self.assertLess(time.monotonic() - start, 2.0, name)

    def test_stored_analysis_is_bounded(self):
        n = self.N
        for source in ("a\n" * (n // 2), " ".join(f"v{i}" for i in range(n // 7)), "".join(f"int g{i};\n" for i in range(n // 10))):
            analysis, habits = self._analyse(source)
            size = sum(len(json.dumps(value)) for value in (analysis["fingerprints"], analysis["line_hashes"], analysis["features"], habits))
            self.assertLess(size, 100 * 1024)
        self.assertLessEqual(len(F.fingerprints(" ".join(f"x{i} + {i};" for i in range(20000)), F.C_FAMILY)), F.MAX_FINGERPRINTS)

    def test_unclosed_code_fences(self):
        start = time.monotonic()
        # The fences pair up into empty blocks, as with the previous regex.
        self.assertEqual(extract_code("```cpp\n" * 300_000), "\n")
        self.assertEqual(extract_code("Here:\n```cpp\nint a;\n```\nand\n```\nint bb;\n```"), "int bb;\n")
        self.assertLess(time.monotonic() - start, 2.0)

    def test_bulk_upload_limit(self):
        from oioioi.llmdetect import prompts

        with patch.object(prompts, "MAX_BULK_REFERENCES", 3):
            references, errors = parse_references("### reference problem=abc\nint a;\n" * 4, {"abc": "pi"})
        self.assertEqual(references, [])
        self.assertIn("More than 3 references", errors[0])


class TestPanelExplainsEmptyQueue(LLMDetectTestCase):
    def setUp(self):
        super().setUp()
        self.assertTrue(self.client.login(username="test_admin"))
        self.url = reverse("llmdetect_panel", kwargs={"contest_id": self.contest.id})

    def test_summary(self):
        self._submit(self.humans[0], self.pis[0], HUMAN_BFS, minutes=10)
        self._submit(self.admin, self.pis[0], HUMAN_BFS, minutes=10)
        response = self.client.get(self.url)
        self.assertEqual(response.context["summary"], {"total": 2, "counted": 1, "staff": 1, "analysed": 1, "history": 0})
        self.assertContains(response, "Analysed 1 of 1 submissions by participants.")
        self.assertContains(response, "1 submissions by staff")

    def test_only_staff_submissions(self):
        self._submit(self.admin, self.pis[0], HUMAN_BFS, minutes=10)
        self.assertContains(self.client.get(self.url), "All submissions of this contest are by staff")

    def test_unreadable_sources_are_reported_and_retried(self):
        submission = self._submit(self.humans[0], self.pis[0], HUMAN_BFS, minutes=10)
        with patch("oioioi.llmdetect.scoring.read_source", side_effect=FileNotFoundError("no such file")):
            response = self.client.get(self.url)
        self.assertContains(response, "1 submission could not be analysed")
        self.assertContains(response, f"#{submission.id}: FileNotFoundError: no such file")
        self.assertEqual(response.context["rows"], [])
        # Once the file can be read, Recompute brings it in.
        self.client.post(reverse("llmdetect_recompute", kwargs={"contest_id": self.contest.id}))
        response = self.client.get(self.url)
        self.assertNotContains(response, "could not be analysed")
        self.assertEqual(len(response.context["rows"]), 1)


class _StreamWithoutClose:
    """Like filetracker's ``_FileLikeFromResponse``: it can only be read."""

    def __init__(self, data):
        self.data = data

    def read(self, size=None):
        data, self.data = self.data, b""
        return data


class TestConcurrentAssessments(LLMDetectTestCase):
    """Two requests (two staff members on the panel, the panel and
    llmdetect_update) assessing one contest at once.
    """

    def setUp(self):
        super().setUp()
        self._camp()
        self.count = ProgramSubmission.objects.count()

    def test_save_over_existing_results_does_not_break_the_unique_key(self):
        from django.db.models.query import QuerySet

        assess_contest(self.contest)
        # As when another request's results were not visible to the DELETE.
        with patch.object(QuerySet, "delete", return_value=(0, {})):
            items = assess_contest(self.contest)
        self.assertEqual(len(items), self.count)
        self.assertEqual(RiskAssessment.objects.count(), self.count)

    def test_second_request_uses_the_results_of_the_first(self):
        update_if_needed(self.contest)
        with patch.object(ContestAssessor, "_compute") as compute:
            update_if_needed(self.contest)
            # Out of date when checked without the lock, up to date once locked.
            self.assertIsNone(ContestAssessor(self.contest).run(force=False))
        compute.assert_not_called()

    def _during_computation(self, assessor, action):
        compute = assessor._compute

        def compute_then_act():
            items = compute()
            action()
            return items

        assessor._compute = compute_then_act

    def test_older_run_does_not_overwrite_a_newer_one(self):
        first = ContestAssessor(self.contest)
        newer = []

        def another_run_with_a_new_submission():
            newer.append(self._submit(self.humans[0], self.pis[3], HUMAN_BFS, minutes=200))
            assess_contest(self.contest)

        self._during_computation(first, another_run_with_a_new_submission)
        self.assertIsNone(first.run())
        self.assertTrue(RiskAssessment.objects.filter(submission=newer[0]).exists())
        self.assertEqual(RiskAssessment.objects.count(), self.count + 1)
        self.assertEqual(get_config(self.contest).last_submission_id, newer[0].id)

    def test_change_during_the_computation_is_not_lost(self):
        assessor = ContestAssessor(self.contest)
        # E.g. a reference added while the contest is being assessed.
        self._during_computation(assessor, lambda: mark_dirty(self.contest))
        self.assertIsNotNone(assessor.run())
        self.assertTrue(get_config(self.contest).dirty)
        self.assertTrue(needs_update(self.contest))

    def test_failed_computation_leaves_the_contest_out_of_date(self):
        mark_dirty(self.contest)
        with patch.object(ContestAssessor, "_compute", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                assess_contest(self.contest)
        self.assertTrue(get_config(self.contest).dirty)

    def test_assessing_views_run_outside_the_request_transaction(self):
        for name, kwargs in (("llmdetect_panel", {}), ("llmdetect_recompute", {}), ("llmdetect_user", {"user_id": 1})):
            view = resolve(reverse(name, kwargs={"contest_id": self.contest.id, **kwargs})).func
            self.assertIn("default", getattr(view, "_non_atomic_requests", set()), name)


class TestSharedCodeAndDisqualifying(LLMDetectTestCase):
    """Submissions of others sharing code, on the participant's page and on
    the page of a submission, and disqualifying from there.
    """

    def setUp(self):
        super().setUp()
        for i, user in enumerate(self.humans):
            for day, pi in enumerate(self.pis[:2]):
                self._submit(user, pi, human_code(f"{i}-{day}"), minutes=90 + 7 * i)
        # Three participants send the same solution, one of them changed a bit.
        self.original = self._submit(self.humans[0], self.pis[2], HUMAN_BFS, minutes=30)
        self.copy = self._submit(self.humans[1], self.pis[2], HUMAN_BFS.replace("d[1]=0;", "d[1]=0;//start"), minutes=40)
        self.third = self._submit(self.humans[2], self.pis[2], HUMAN_BFS, minutes=50)
        for i, user in enumerate(self.humans[3:]):
            self._submit(user, self.pis[2], human_code(f"own-{i}"), minutes=60)
        assess_contest(self.contest)
        self.assertTrue(self.client.login(username="test_admin"))

    def _user_page(self, user, submission, **params):
        url = reverse("llmdetect_user", kwargs={"contest_id": self.contest.id, "user_id": user.id}) + f"?submission={submission.id}"
        return self.client.get(url + "".join(f"&{k}={v}" for k, v in params.items()))

    def test_list_of_shared_code_and_comparison(self):
        peers = RiskAssessment.objects.get(submission=self.copy).context["peers"]
        self.assertEqual({peer["submission"] for peer in peers}, {self.original.id, self.third.id})
        self.assertTrue(all(peer["similarity"] > 0.9 for peer in peers))
        response = self._user_page(self.humans[1], self.copy)
        self.assertEqual([peer["submission"] for peer in response.context["peers"]][:2], [p["submission"] for p in peers][:2])
        self.assertContains(response, f"compare={self.original.id}")
        response = self._user_page(self.humans[1], self.copy, compare=self.original.id)
        compare = response.context["compare"]
        self.assertEqual(compare["submission"], self.original)
        marked = dict(compare["lines"])
        self.assertTrue(marked["    queue<int> q;q.push(1);"])
        self.assertFalse(marked["    d[1]=0;//start"])  # only in this one
        self.assertFalse(marked["}"])  # too short to mean anything
        self.assertContains(response, 'class="llm-common"')

    def test_comparison_only_with_others_of_this_contest(self):
        other = Contest.objects.create(id="other", name="Other", controller_name="oioioi.programs.controllers.ProgrammingContestController")
        round = Round.objects.create(contest=other, name="R", start_date=T0)
        foreign = self._submit(
            self.humans[0], ProblemInstance.objects.create(problem=self.pis[0].problem, round=round, contest=other, short_name="x"), HUMAN_BFS, 5
        )
        for wanted in (foreign.id, self.copy.id, "abc"):
            self.assertIsNone(self._user_page(self.humans[1], self.copy, compare=wanted).context["compare"])

    def test_submission_page_shows_the_staff_the_detection(self):
        url = reverse("submission", kwargs={"contest_id": self.contest.id, "submission_id": self.copy.id})
        response = self.client.get(url)
        self.assertContains(response, 'id="llmdetect"')
        self.assertContains(response, "Shared code with other participants")
        self.assertContains(response, f"#{self.original.id}")
        self.assertContains(response, "Disqualify this submission")
        # Participants never see it, not even on their own submission.
        self.assertTrue(self.client.login(username=self.humans[1].username))
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'id="llmdetect"')

    def test_submission_page_never_starts_an_assessment(self):
        mark_dirty(self.contest)
        url = reverse("submission", kwargs={"contest_id": self.contest.id, "submission_id": self.copy.id})
        with patch.object(ContestAssessor, "_compute") as compute:
            self.assertEqual(self.client.get(url).status_code, 200)
        compute.assert_not_called()

    def test_disqualify_from_the_submission_page(self):
        from oioioi.disqualification.models import Disqualification

        url = reverse("submission", kwargs={"contest_id": self.contest.id, "submission_id": self.copy.id})
        page = self.client.get(url).content.decode()
        form_url = re.search(r'href="([^"]*disqualification/disqualification/add/[^"]*)"', page).group(1).replace("&amp;", "&")
        self.assertIn(f"submission={self.copy.id}", form_url)
        self.assertIn("came_from=", form_url)
        form = self.client.get(form_url)
        self.assertEqual(form.status_code, 200)
        initial = form.context["adminform"].form.initial
        response = self.client.post(
            form_url,
            {"user": self.humans[1].id, "submission": self.copy.id, "title": initial["title"], "content": initial["content"], "guilty": "on"},
        )
        # Back on the submission page, now marked.
        self.assertRedirects(response, url, fetch_redirect_response=False)
        self.assertTrue(Disqualification.objects.filter(submission=self.copy, guilty=True).exists())
        self.assertNotContains(self.client.get(url), "Disqualify this submission")


class TestNewAnalysisVersion(LLMDetectTestCase):
    def test_older_analysis_is_recomputed(self):
        submission = self._submit(self.humans[0], self.pis[0], HUMAN_BFS, minutes=10)
        assess_contest(self.contest)
        self.assertFalse(needs_update(self.contest))
        SubmissionFeatures.objects.filter(submission=submission).update(version=F.FEATURES_VERSION - 1)
        self.assertTrue(needs_update(self.contest))
        assess_contest(self.contest)
        self.assertEqual(SubmissionFeatures.objects.get(submission=submission).version, F.FEATURES_VERSION)
        self.assertFalse(needs_update(self.contest))


TEST_FILES = os.path.join(os.path.dirname(__file__), "test_files")

SINOL_TEX = r"""\documentclass[zad,zawodnik,utf8,pl]{sinol}
\title{Sumy}
\id{tst}
\konkurs{Tajny obóz 2026}
\date{2026-11-09}
\RAM{256}
\begin{document}
\begin{tasktext}
\noindent {}%
Dane są liczby $a_1, \ldots, a_n$ w~kolejności -- policz ich sumę.
% A comment of the authors.

\section{\sinolSNameInput}
W~pierwszym wierszu $n$ ($1 \le n \le 10^6$), w~drugim \textbf{\emph{liczby}} $a_i$ ($|a_i| \le 10^9$).

\section{\sinolSNameOutput}
Wypisz \texttt{TAK} albo \texttt{NIE}.

\section{Przykład}
\makecompactexample{0}

\section{Podzadania}
\begin{tabular}{|l|l|}
\hline
\textbf{\sinolSNameSubtask} & \textbf{\sinolSNamePoints} \\ \hline
1 & $n \le 1000$ \\ \hline
\end{tabular}
\end{tasktext}
\end{document}
"""


class TestStatements(SimpleTestCase):
    def test_pdf_keeps_exponents_indices_and_polish_letters(self):
        from oioioi.llmdetect.pdftext import pdf_text

        # Bitmap fonts without a character map (T1), and the polski package (OT4).
        for name in ("small_t1.pdf", "small_ot4.pdf"):
            with open(os.path.join(TEST_FILES, name), "rb") as file:
                text = pdf_text(file.read())
            self.assertIn("(1 ≤ n ≤ 10^6, |a_{i−1}| ≤ 2 · 10^9)", text, name)
            self.assertIn("∑^n_{i=1} a^2_i", text, name)
            self.assertIn("Żółć", text, name)
            self.assertIn("gęślą jaźń", text, name)
            # The example input and output side by side, as in the PDF.
            self.assertRegex(text, r"Dla danych wejściowych: +poprawnym wynikiem jest:")
            self.assertRegex(text, r"\n3 +14\n1 2 3")

    def test_latex_source(self):
        from oioioi.llmdetect.statements import latex_statement

        text, warnings = latex_statement(SINOL_TEX, lambda name: ("2\n1 2\n", "3\n") if name == "0" else None)
        self.assertEqual(warnings, [])
        self.assertTrue(text.startswith("# Sumy\nDostępna pamięć: 256 MB.\n\nDane są liczby $a_1, \\ldots, a_n$ w kolejności – policz ich sumę."))
        self.assertIn("## Wejście\n\nW pierwszym wierszu $n$ ($1 \\le n \\le 10^6$), w drugim liczby $a_i$ ($|a_i| \\le 10^9$).", text)
        self.assertIn("Wypisz TAK albo NIE.", text)
        self.assertIn("Dla danych wejściowych:\n```\n2\n1 2\n```\npoprawnym wynikiem jest:\n```\n3\n```", text)
        self.assertIn("| Podzadanie | Liczba punktów |\n|---|---|\n| 1 | $n \\le 1000$ |", text)
        # What must not leave the camp, and the authors' comments.
        for secret in ("Tajny", "2026-11-09", "comment"):
            self.assertNotIn(secret, text)

    def test_latex_source_without_example_or_with_pictures(self):
        from oioioi.llmdetect.statements import latex_statement

        tex = SINOL_TEX.replace("Wypisz", "\\includegraphics[width=5cm]{rys.png} Wypisz")
        text, warnings = latex_statement(tex)
        self.assertIn("[example 0]", text)
        self.assertIn("[picture]", text)
        self.assertEqual(len(warnings), 2)


class TestStatementSources(LLMDetectTestCase):
    def _package(self, tex=SINOL_TEX, example=True):
        import zipfile

        from oioioi.problems.models import ProblemPackage

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("tst/doc/tstzad.tex", tex)
            if example:
                archive.writestr("tst/in/tst0.in", "2\n1 2\n")
                archive.writestr("tst/out/tst0.out", "3\n")
            archive.writestr("tst/in/tst1a.in", "x" * 1000)
        package = ProblemPackage(problem=self.pis[0].problem, status="OK")
        package.package_file.save("tst.zip", ContentFile(buffer.getvalue()), save=False)
        package.save()
        return package

    def test_package_source_first(self):
        from oioioi.llmdetect.statements import SOURCE_PACKAGE, statement_text

        self._package()
        text, source, warnings = statement_text(self.pis[0])
        self.assertEqual(source, SOURCE_PACKAGE)
        self.assertIn("poprawnym wynikiem jest:\n```\n3\n```", text)
        self.assertEqual(warnings, [])

    def test_statement_file_through_a_stream_without_close(self):
        from oioioi.llmdetect.statements import SOURCE_FILE, statement_text
        from oioioi.problems.models import ProblemStatement

        with open(os.path.join(TEST_FILES, "small_ot4.pdf"), "rb") as file:
            data = file.read()
        statement = ProblemStatement(problem=self.pis[1].problem)
        statement.content.save("bbbzad.pdf", ContentFile(data), save=False)
        statement.save()
        # As FiletrackerStorage._open returns it.
        with patch("django.db.models.fields.files.FieldFile.read", side_effect=lambda *args: _StreamWithoutClose(data).read()):
            with patch("django.db.models.fields.files.FieldFile.close", side_effect=AttributeError("no close")):
                text, source, warnings = statement_text(self.pis[1])
        self.assertEqual(source, SOURCE_FILE)
        self.assertIn("10^6", text)
        self.assertIn("Read from the PDF: check the formulas, the examples and the tables.", warnings)

    def test_errors_are_reported(self):
        from oioioi.llmdetect.statements import statement_text

        self.assertEqual(statement_text(self.pis[2]), ("", None, ["No statement found for this problem."]))
        self._package()
        with patch("oioioi.llmdetect.statements.latex_statement", side_effect=ValueError("broken")):
            text, source, warnings = statement_text(self.pis[0])
        self.assertEqual((text, source), ("", None))
        self.assertEqual(warnings, ["The statement could not be read: ValueError: broken"])


def reformatted(code):
    """The code after an editor formatted it: spaces after keywords, around
    operators and after commas, two-space indentation.
    """
    import re as _re

    lines = []
    for line in code.split("\n"):
        if not line.startswith("#"):  # formatters leave preprocessor lines alone
            line = _re.sub(r"\b(for|if|while)\(", r"\1 (", line)
            line = _re.sub(r"(?<=[\w\]\)])(<=|>=|==|!=|\+=|-=|\*=|\^=|\|=|=|<|>|\+|\*|/|\^|\|)(?=[\w\(\[-])", r" \1 ", line)
            line = _re.sub(r",(?=\S)", ", ", line)
            line = line.replace("    ", "  ")
        lines.append(line)
    return "\n".join(lines)


SCHOOL_TEMPLATE = """#include <bits/stdc++.h>
using namespace std;
inline int czytaj(){
    int x=0,znak=1;char c=getchar();
    while(c<'0'||c>'9'){if(c=='-')znak=-1;c=getchar();}
    while(c>='0'&&c<='9'){x=x*10+(c-'0');c=getchar();}
    return x*znak;
}
inline void pisz(long long x){
    if(x<0){putchar('-');x=-x;}
    if(x>9)pisz(x/10);
    putchar('0'+x%10);
}
"""


class TestFairness(LLMDetectTestCase):
    """Honest participants must not look like cheaters."""

    def _risk(self, submission):
        return RiskAssessment.objects.get(submission=submission)

    def test_editor_formatting_counts_once_and_weakly(self):
        for i, user in enumerate(self.humans):
            for day, pi in enumerate(self.pis):
                code = human_code(f"{i}-{day}")
                if user == self.humans[0] and day == 3:
                    code = reformatted(code)
                last = self._submit(user, pi, code, minutes=90 + 7 * i)
                if user == self.humans[0] and day == 3:
                    formatted = last
        assess_contest(self.contest)
        assessment = self._risk(formatted)
        broken = assessment.signals["habit_break"]["detail"]["broken"]
        self.assertGreaterEqual(len({row["habit"] for row in broken}), 3)
        self.assertEqual({row["group"] for row in broken}, {"formatting"})
        # Several habits, one change: at most one weak group.
        self.assertLessEqual(assessment.signals["habit_break"]["value"], 0.8 * 0.6 + 1e-6)
        self.assertLess(assessment.risk, FLAG_THRESHOLD)
        self.assertIn("Formatting changes together", describe("habit_break", assessment.signals["habit_break"]["detail"]))

    def _speed_camp(self, fast, slow_then_fast):
        """Everybody solves every problem; ``fast`` is always the first,
        ``slow_then_fast`` is the last except on the last problem.
        """
        submissions = {}
        for i, user in enumerate(self.humans):
            for day, pi in enumerate(self.pis):
                minutes = 60 + 15 * i
                if user == fast:
                    minutes = 10
                elif user == slow_then_fast:
                    minutes = 14 if day == 3 else 250
                submissions[user, day] = self._submit(user, pi, human_code(f"{i}-{day}"), minutes=minutes)
        assess_contest(self.contest)
        return submissions

    def test_speed_compared_with_own_pace(self):
        fast, slow = self.humans[0], self.humans[6]
        submissions = self._speed_camp(fast, slow)
        for day in range(4):
            self.assertNotIn("fast_solve", self._risk(submissions[fast, day]).signals)
        sudden = self._risk(submissions[slow, 3]).signals["fast_solve"]
        self.assertGreater(sudden["value"], 0.5)
        self.assertIn("usually faster than 0%", describe("fast_solve", sudden["detail"]))

    def test_time_from_the_round_start_with_paper_statements(self):
        # Statements on paper: the first opening in SIO2 is late.
        late = self.humans[3]
        submissions = {}
        for i, user in enumerate(self.humans):
            for day, pi in enumerate(self.pis):
                minutes = 200 if user == late and day == 3 else 60 + 10 * i
                submissions[user, day] = self._submit(user, pi, human_code(f"{i}-{day}"), minutes=minutes)
                if user == late and day == 3:
                    ProblemViewEvent.objects.create(user=user, problem_instance=pi, date=pi.round.start_date + timedelta(minutes=195))
        assess_contest(self.contest)
        self.assertNotIn("fast_solve", self._risk(submissions[late, 3]).signals)
        # When statements are read in SIO2, the opening counts.
        config = LLMDetectConfig.objects.get(contest=self.contest)
        config.time_from_statement = True
        config.save()
        assess_contest(self.contest)
        self.assertIn("fast_solve", self._risk(submissions[late, 3]).signals)

    def test_strong_participant_solving_a_hard_problem_at_once(self):
        strong, weak = self.humans[0], self.humans[1]
        results = {}
        for i, user in enumerate(self.humans):
            for day, pi in enumerate(self.pis):
                if day == 3 and user not in (strong, weak):
                    score = 20  # a hard problem
                elif day < 3:
                    score = 100 if user == strong else 30 + 5 * i
                else:
                    score = 100
                results[user, day] = self._submit(user, pi, human_code(f"{i}-{day}"), minutes=90 + 7 * i, score=score)
        assess_contest(self.contest)
        self.assertNotIn("first_try", self._risk(results[strong, 3]).signals)
        self.assertIn("first_try", self._risk(results[weak, 3]).signals)

    def test_shared_school_template_is_not_similarity(self):
        friends = self.humans[:2]
        for i, user in enumerate(self.humans + [self.cheater]):
            for day, pi in enumerate(self.pis):
                code = human_code(f"{i}-{day}")
                if user in friends:
                    code = SCHOOL_TEMPLATE + code.split("\n", 2)[2]
                self._submit(user, pi, code, minutes=90 + 7 * i)
        assess_contest(self.contest)
        for assessment in RiskAssessment.objects.filter(user__in=friends):
            self.assertNotIn("peer_similarity", assessment.signals)
            self.assertLess(assessment.context["peer"]["similarity"], 0.5)

    def test_confirmed_case_does_not_flag_the_original_author(self):
        original = self._submit(self.humans[0], self.pis[3], HUMAN_BFS, minutes=30)
        copied = self._submit(self.cheater, self.pis[3], HUMAN_BFS, minutes=60)
        later = self._submit(self.humans[1], self.pis[3], HUMAN_BFS, minutes=90)
        for i, user in enumerate(self.humans[2:]):
            self._submit(user, self.pis[3], human_code(f"other-{i}"), minutes=100)
        LLMReferenceSolution.objects.create(problem_instance=self.pis[3], kind=LLMReferenceSolution.KIND_CONFIRMED, source=HUMAN_BFS, submission=copied)
        assess_contest(self.contest)
        self.assertNotIn("llm_reference", self._risk(original).signals)
        self.assertNotIn("llm_reference", self._risk(copied).signals)  # not with itself
        self.assertIn("llm_reference", self._risk(later).signals)

    def test_after_the_round_style_signals_count_half(self):
        submission = self._camp()
        late = self._submit(self.cheater, self.pis[3], LLM_BFS.replace("pending", "waiting"), minutes=8 * 60)
        assess_contest(self.contest)
        during = self._risk(submission).signals["habit_break"]
        after = self._risk(late).signals["habit_break"]
        self.assertTrue(after["detail"]["after_round"])
        self.assertNotIn("after_round", during["detail"])
        self.assertIn("Counted half", describe("habit_break", after["detail"]))


class TestFiletrackerSources(LLMDetectTestCase):
    def test_source_without_close_is_read(self):
        submission = self._submit(self.humans[0], self.pis[0], HUMAN_BFS, minutes=10)
        submission = ProgramSubmission.objects.get(id=submission.id)
        # As FiletrackerStorage._open returns it.
        submission.source_file.file = File(_StreamWithoutClose(HUMAN_BFS.encode()), submission.source_file.name)
        self.assertEqual(read_source(submission), HUMAN_BFS)


class TestTextAboveTheCode(LLMDetectTestCase):
    def test_submission_with_polish_text_and_emoji_is_assessed(self):
        source = '11:34\nżółć gęślą jaźń ☺\ufe0f🙉\n#include <iostream>\nusing namespace std;\nint main(){string s=" ";cin>>s;}\n'
        submission = self._submit(self.humans[0], self.pis[0], source, minutes=10)
        self._submit(self.humans[1], self.pis[0], HUMAN_BFS, minutes=20)
        assess_contest(self.contest)
        self.assertEqual(get_config(self.contest).read_errors, 0)
        self.assertTrue(RiskAssessment.objects.filter(submission=submission).exists())


class TestLineEndingsOfOldSubmissions(LLMDetectTestCase):
    """Pasted code always arrives with CRLF. Submissions analysed long after
    they were sent (an older contest) may have been pasted or uploaded, so
    their line endings must not count as a habit.
    """

    CRLF = HUMAN_BFS.replace("\n", "\r\n")

    def test_unknown_paste_has_no_line_endings_habit(self):
        self.assertNotIn("line_endings", extract_habits(self.CRLF, F.C_FAMILY, paste_known=False)["values"])
        self.assertEqual(extract_habits(self.CRLF, F.C_FAMILY)["values"]["line_endings"], "CRLF")

    def test_backfilled_and_judged_submissions(self):
        old = self._submit(self.humans[0], self.pis[0], self.CRLF, minutes=10)
        assess_contest(self.contest)  # analyses it after the fact
        features = SubmissionFeatures.objects.get(submission=old)
        self.assertFalse(features.paste_known)
        self.assertNotIn("line_endings", features.habits["values"])
        new = self._submit(self.humans[1], self.pis[0], self.CRLF, minutes=10)
        self.contest.controller.submission_judged(new)  # analysed when judged: an uploaded file
        features = SubmissionFeatures.objects.get(submission=new)
        self.assertTrue(features.paste_known)
        self.assertEqual(features.habits["values"]["line_endings"], "CRLF")
        # Recomputing keeps what is known.
        compute_features(old)
        self.assertFalse(SubmissionFeatures.objects.get(submission=old).paste_known)

    def test_no_false_habit_break_in_an_older_contest(self):
        user = self.humans[0]
        for day, pi in enumerate(self.pis[:3]):
            self._submit(user, pi, human_code(f"0-{day}"), minutes=60)  # uploaded, LF
        self._submit(user, self.pis[3], human_code("0-3").replace("\n", "\r\n"), minutes=60)  # pasted: CRLF
        for i, other in enumerate(self.humans[1:]):
            for day, pi in enumerate(self.pis):
                self._submit(other, pi, human_code(f"{i + 1}-{day}"), minutes=60)
        assess_contest(self.contest)
        assessment = RiskAssessment.objects.get(user=user, submission__problem_instance=self.pis[3])
        self.assertNotIn("line_endings", [row["habit"] for row in assessment.context.get("habits", [])])
        self.assertNotIn("habit_break", assessment.signals)

    def test_migration_marks_backfilled_features(self):
        import importlib

        from django.apps import apps

        migration = importlib.import_module("oioioi.llmdetect.migrations.0004_paste_known")
        sent_and_judged = self._submit(self.humans[0], self.pis[0], HUMAN_BFS, minutes=10)
        backfilled = self._submit(self.humans[1], self.pis[0], HUMAN_BFS, minutes=10)
        pasted = self._submit(self.humans[2], self.pis[0], HUMAN_BFS, minutes=10, pasted=True)
        for submission in (sent_and_judged, backfilled, pasted):
            compute_features(submission)
        SubmissionFeatures.objects.update(paste_known=True)
        SubmissionFeatures.objects.filter(submission=sent_and_judged).update(computed_at=sent_and_judged.date + timedelta(minutes=5))
        SubmissionFeatures.objects.filter(submission__in=[backfilled, pasted]).update(computed_at=backfilled.date + timedelta(days=30))
        migration.mark_backfilled(apps, None)
        known = dict(SubmissionFeatures.objects.values_list("submission_id", "paste_known"))
        self.assertEqual(known, {sent_and_judged.id: True, backfilled.id: False, pasted.id: True})


class TestHistoryContests(LLMDetectTestCase):
    """Submissions of the same participants in earlier contests (camps)."""

    def setUp(self):
        super().setUp()
        self.old = Contest.objects.create(id="old", name="Old camp", controller_name="oioioi.programs.controllers.ProgrammingContestController")
        start = T0 - timedelta(days=90)
        self.old_rounds = [
            Round.objects.create(contest=self.old, name=f"Day {day}", start_date=start + timedelta(days=day), end_date=start + timedelta(days=day, hours=5))
            for day in range(3)
        ]
        self.old_pis = [self._old_problem(round, name) for round, name in zip(self.old_rounds, ["xxx", "yyy", "zzz"], strict=True)]
        self.config = get_config(self.contest)

    def _old_problem(self, round, short_name, problem=None):
        if problem is None:
            controller = "oioioi.programs.controllers.ProgrammingProblemController"
            problem = Problem.objects.create(legacy_name=short_name, short_name=short_name, controller_name=controller)
        return ProblemInstance.objects.create(problem=problem, round=round, contest=self.old, short_name=short_name)

    def _first_day_with_cheater(self):
        """Everybody wrote their own code at the earlier camp; this camp has
        only had its first day, on which the cheater pastes an LLM solution.
        """
        for i, user in enumerate(self.humans + [self.cheater]):
            for day, pi in enumerate(self.old_pis):
                self._submit(user, pi, human_code(f"{i}-old-{day}"), minutes=90 + 7 * i)
        for i, user in enumerate(self.humans):
            self._submit(user, self.pis[0], human_code(f"{i}-0"), minutes=100 + 9 * i)
        return self._submit(self.cheater, self.pis[0], LLM_BFS, minutes=95)

    def test_habits_known_from_an_earlier_contest(self):
        submission = self._first_day_with_cheater()
        assess_contest(self.contest)
        # On the first day nobody has a history in this contest.
        alone = RiskAssessment.objects.get(submission=submission)
        self.assertNotIn("habit_break", alone.signals)
        self.config.history_contests.add(self.old)
        assess_contest(self.contest)
        assessment = RiskAssessment.objects.get(submission=submission)
        broken = {row["habit"] for row in assessment.signals["habit_break"]["detail"]["broken"]}
        self.assertTrue({"includes", "namespace", "ll_alias"} <= broken)
        self.assertGreater(assessment.risk, alone.risk)
        profile = assessment.context["profile"]
        self.assertAlmostEqual(profile["history"], 3 * self.config.history_weight, places=3)
        self.assertEqual(profile["weight"], profile["history"])
        self.assertTrue(self.client.login(username="test_admin"))
        page = self.client.get(reverse("llmdetect_user", kwargs={"contest_id": self.contest.id, "user_id": self.cheater.id}))
        self.assertContains(page, "1.5 from earlier contests")
        # Earlier submissions are only part of profiles: not analysed again.
        self.assertFalse(RiskAssessment.objects.filter(submission__problem_instance__contest=self.old).exists())
        others = RiskAssessment.objects.filter(contest=self.contest).exclude(submission=submission)
        self.assertFalse([a for a in others if "habit_break" in a.signals])

    def test_weight(self):
        self._first_day_with_cheater()
        self.config.history_contests.add(self.old)
        self.config.history_weight = 0.2
        self.config.save()
        assess_contest(self.contest)
        assessment = RiskAssessment.objects.filter(contest=self.contest).first()
        self.assertAlmostEqual(assessment.context["profile"]["history"], 0.6, places=3)

    def test_same_problem_in_an_earlier_contest_is_left_out(self):
        again = self._old_problem(self.old_rounds[0], "aaa", problem=self.pis[0].problem)
        submission = self._submit(self.humans[0], self.pis[0], HUMAN_BFS, minutes=10)
        self._submit(self.humans[0], again, HUMAN_BFS, minutes=10)
        self.config.history_contests.add(self.old)
        assess_contest(self.contest)
        self.assertEqual(RiskAssessment.objects.get(submission=submission).context["profile"]["history"], 0)

    def test_only_participants_of_this_contest(self):
        stranger = User.objects.create_user("stranger")
        self._submit(stranger, self.old_pis[0], HUMAN_BFS, minutes=10)
        self._submit(self.humans[0], self.old_pis[0], HUMAN_BFS, minutes=10)
        self._submit(self.humans[0], self.pis[0], HUMAN_BFS, minutes=10)
        self.config.history_contests.add(self.old)
        self.assertEqual([s.user for s in history_submissions(self.contest, [self.humans[0].id])], [self.humans[0]])
        # The summary counts those of the participants of this contest only.
        self.assertTrue(self.client.login(username="test_admin"))
        response = self.client.get(reverse("llmdetect_panel", kwargs={"contest_id": self.contest.id}))
        self.assertEqual(response.context["summary"]["history"], 1)
        self.assertContains(response, "Profiles also use 1 submissions of these participants from earlier contests.")

    def test_settings_list_only_administered_contests(self):
        secret = Contest.objects.create(id="secret", name="Secret", controller_name="oioioi.programs.controllers.ProgrammingContestController")
        admin = self.humans[6]
        for contest in (self.contest, self.old):
            ContestPermission.objects.create(user=admin, contest=contest, permission="contests.contest_admin")
        # Chosen earlier by an admin of both.
        self.config.history_contests.add(secret)
        self.assertTrue(self.client.login(username=admin.username))
        url = reverse("llmdetect_settings", kwargs={"contest_id": self.contest.id})
        form = self.client.get(reverse("llmdetect_panel", kwargs={"contest_id": self.contest.id})).context["settings_form"]
        self.assertEqual(list(form.fields["history_contests"].queryset), [self.old])
        self.assertEqual(form.hidden_history, [secret])
        # Contests the admin cannot see cannot be chosen...
        self.client.post(url, {"history_contests": [secret.id], "history_weight": "0.3"})
        self.config.refresh_from_db()
        self.assertEqual(self.config.history_weight, 0.5)
        # ...nor removed.
        self.client.post(url, {"history_contests": [self.old.id], "history_weight": "0.3"})
        self.config.refresh_from_db()
        self.assertEqual(set(self.config.history_contests.all()), {self.old, secret})
        self.assertEqual(self.config.history_weight, 0.3)
        self.assertTrue(self.config.dirty)
