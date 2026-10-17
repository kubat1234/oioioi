import re

from django.core.exceptions import ValidationError

from oioioi.base.utils.validators import UnicodeValidator

USERNAME_REGEX = r"^[a-zA-Z0-9_]+$"

# Gmail ignores dots and everything after "+" in the local part.
GMAIL_DOMAINS = ("gmail.com", "googlemail.com")


def canonical_email(email):
    """The form of an e-mail address under which it reaches the same mailbox:
    lowercase, and for Gmail without dots and "+..." in the local part.
    """
    local, _, domain = email.strip().lower().rpartition("@")
    if domain in GMAIL_DOMAINS:
        local = local.split("+")[0].replace(".", "")
        domain = GMAIL_DOMAINS[0]
    return f"{local}@{domain}"


def users_with_email(email, queryset):
    """Users from ``queryset`` whose e-mail reaches the same mailbox as ``email``."""
    canonical = canonical_email(email)
    domain = canonical.rpartition("@")[2]
    domains = GMAIL_DOMAINS if domain in GMAIL_DOMAINS else (domain,)
    domains_regex = "|".join(re.escape(d) for d in domains)
    candidates = queryset.filter(email__iregex=rf"@({domains_regex})\s*$")
    return [user for user in candidates if canonical_email(user.email) == canonical]


""" All characters constituting users name and surname must
    belong to one of the categories specified in
    `UNICODE_CATEGORY_LIST`. For a guide of unicode categories
    see: https://unicodebook.readthedocs.io/unicode.html
"""
UNICODE_CATEGORY_LIST = [
    "Ll",
    "Lm",
    "Lo",
    "Lt",
    "Lu",
    "Nd",
    "Pf",
    "Pd",
    "Pi",
    "Pe",
    "Pc",
    "Sc",
]


def check_unicode_text(text, allow_spaces=False):
    try:
        UnicodeValidator(unicode_categories=UNICODE_CATEGORY_LIST, allow_spaces=allow_spaces)(text)
        return True
    except ValidationError:
        return False


def has_valid_username(user):
    return user is None or user.is_anonymous or re.match(USERNAME_REGEX, user.username) is not None


def has_valid_name(user):
    return (
        user is None or user.is_anonymous or (check_unicode_text(user.first_name, allow_spaces=True) and check_unicode_text(user.last_name, allow_spaces=True))
    )
