from django.db import migrations

CONTROLLER_NAME = "oioioi.czeciecamp.controllers.CzecieCampContestController"


def _sum_scores(scores):
    scores = [score for score in scores if score is not None]
    return scores and sum(scores[1:], scores[0]) or None


def results_from_best_submissions(apps, schema_editor):
    """Problem results of camps now come from the best scored submission,
    not the latest one: recount the existing ones, and the sums for rounds
    and contests built from them.
    """
    UserResultForProblem = apps.get_model("contests", "UserResultForProblem")
    UserResultForRound = apps.get_model("contests", "UserResultForRound")
    UserResultForContest = apps.get_model("contests", "UserResultForContest")
    Submission = apps.get_model("contests", "Submission")
    SubmissionReport = apps.get_model("contests", "SubmissionReport")

    for result in UserResultForProblem.objects.filter(problem_instance__contest__controller_name=CONTROLLER_NAME):
        best = None
        submissions = Submission.objects.filter(problem_instance_id=result.problem_instance_id, user_id=result.user_id, kind="NORMAL", score__isnull=False)
        for submission in submissions.order_by("date", "id"):
            if best is None or submission.score > best.score:
                best = submission
        if best is None:
            result.score = None
            result.status = None
            result.submission_report = None
        else:
            result.score = best.score
            result.status = best.status
            result.submission_report = SubmissionReport.objects.filter(submission=best, status="ACTIVE", kind="NORMAL").first()
        result.save()

    for result in UserResultForRound.objects.filter(round__contest__controller_name=CONTROLLER_NAME):
        scores = UserResultForProblem.objects.filter(user_id=result.user_id, problem_instance__round_id=result.round_id).values_list("score", flat=True)
        result.score = _sum_scores(scores)
        result.save()

    for result in UserResultForContest.objects.filter(contest__controller_name=CONTROLLER_NAME):
        scores = UserResultForRound.objects.filter(user_id=result.user_id, round__contest_id=result.contest_id, round__is_trial=False).values_list(
            "score", flat=True
        )
        result.score = _sum_scores(scores)
        result.save()


class Migration(migrations.Migration):
    dependencies = [
        ("contests", "0027_probleminstance_can_access_editorial"),
        ("czeciecamp", "0015_round_submissions_limit"),
    ]

    operations = [
        migrations.RunPython(results_from_best_submissions, migrations.RunPython.noop),
    ]
