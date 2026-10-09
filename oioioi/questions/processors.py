from django.urls import reverse
from django.utils.functional import lazy
from django.utils.translation import ngettext

from oioioi.base.utils import make_navbar_badge, request_cached
from oioioi.contests.utils import can_enter_contest, is_contest_basicadmin
from oioioi.questions.utils import unanswered_questions
from oioioi.questions.views import new_messages, visible_messages
from oioioi.status.registry import status_registry


def navbar_tip_processor(request):
    if not getattr(request, "contest", None):
        return {}
    if not request.user.is_authenticated:
        return {}
    if not can_enter_contest(request):
        return {}

    def generator():
        return make_navbar_badge(**navbar_messages_generator(request))

    return {"extra_navbar_right_messages": lazy(generator, str)()}


# How often contest admins' pages check for new questions, in ms.
ADMIN_MESSAGES_SYNC_TIME = 30000


@status_registry.register
def get_messages(request, response):
    messages = navbar_messages_generator(request)
    response["messages"] = messages
    # Rendered badge, so that common/navbar_messages.js can refresh it.
    response["messages_badge"] = make_navbar_badge(**messages) if messages else ""
    if getattr(request, "contest", None) is not None and is_contest_basicadmin(request):
        response["sync_time"] = min(ADMIN_MESSAGES_SYNC_TIME, response.get("sync_time", ADMIN_MESSAGES_SYNC_TIME))
    return response


@request_cached
def navbar_messages_generator(request):
    if request.contest is None:
        return {}

    is_admin = is_contest_basicadmin(request)
    vis_messages = visible_messages(request)
    if is_admin:
        messages = unanswered_questions(vis_messages)
    else:
        messages = new_messages(request, vis_messages)
    count = messages.count()
    if count:
        text = ngettext("%(count)d NEW MESSAGE", "%(count)d NEW MESSAGES", count) % {"count": count}

        if count == 1:
            m = messages.get()
            link = reverse(
                "message",
                kwargs={
                    "contest_id": request.contest.id,
                    "message_id": m.top_reference_id if vis_messages.filter(id=m.top_reference_id).exists() else m.id,
                },
            )
        else:
            link = reverse("contest_messages", kwargs={"contest_id": request.contest.id})
        return {"link": link, "text": text, "id": "contest_new_messages"}
    else:
        return {"link": None, "text": None, "id": "contest_new_messages"}
