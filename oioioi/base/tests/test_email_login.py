from django.contrib.auth import authenticate
from django.contrib.auth.models import User
from django.urls import reverse

from oioioi.base.tests import TestCase


class TestEmailLogin(TestCase):
    fixtures = ["test_users"]

    def setUp(self):
        self.user = User.objects.get(username="test_user")
        self.user.set_password("secret")
        self.user.save()

    def test_username_or_email(self):
        self.assertEqual(authenticate(username="test_user", password="secret"), self.user)
        self.assertEqual(authenticate(username="Test_User@Example.com ", password="secret"), self.user)
        self.assertIsNone(authenticate(username="test_user@example.com", password="wrong"))

    def test_gmail_dots_and_plus(self):
        self.user.email = "Jakub.Trela05@gmail.com"
        self.user.save()
        for login in ("jakubtrela05@gmail.com", "j.a.k.u.b.trela05+sio@googlemail.com", "JAKUB.TRELA05@GMAIL.COM"):
            self.assertEqual(authenticate(username=login, password="secret"), self.user, login)
        # Elsewhere dots matter.
        self.user.email = "jan.kowalski@wp.pl"
        self.user.save()
        self.assertIsNone(authenticate(username="jankowalski@wp.pl", password="secret"))
        self.assertEqual(authenticate(username="Jan.Kowalski@WP.pl", password="secret"), self.user)

    def test_email_of_several_accounts_is_not_accepted(self):
        other = User.objects.get(username="test_user2")
        other.email = self.user.email
        other.save()
        self.assertIsNone(authenticate(username="test_user@example.com", password="secret"))

    def test_login_page(self):
        url = reverse("login")
        self.assertContains(self.client.get(url), "Username or email")
        response = self.client.post(
            url,
            {"auth-username": "test_user@example.com", "auth-password": "secret", "login_view-current_step": "auth"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.user.id)
