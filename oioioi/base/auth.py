from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.utils.translation import gettext_lazy as _

from oioioi.base.utils.user import users_with_email


class EmailAuthBackend(ModelBackend):
    """Lets users log in with their e-mail instead of the username, in any
    letter case, and for Gmail also with or without dots and "+..." (it is
    the same mailbox, see ``canonical_email``).

    Only an address that belongs to exactly one account is accepted;
    otherwise the login is left to the other backends (a username may
    contain "@" too).
    """

    description = _("Password authentication with e-mail")
    # Not offered separately when switching users (su), it is the same as
    # the password authentication.
    supports_authentication = False

    def authenticate(self, request, username=None, password=None, **kwargs):
        if not isinstance(username, str) or "@" not in username or password is None:
            return None
        users = users_with_email(username, get_user_model()._default_manager.all())
        if len(users) != 1:
            return None
        user = users[0]
        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None
