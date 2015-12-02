from django.core.urlresolvers import resolve, reverse
from django.test import RequestFactory, TestCase
from django.test.utils import override_settings

import mock

from rest_framework.test import APITestCase

from accounts import verify, views
from amo.tests import create_switch
from api.tests.utils import APIAuthTestCase
from users.models import UserProfile

FXA_CONFIG = {'some': 'stuff', 'that is': 'needed'}


class TestFxALoginWaffle(APITestCase):

    def setUp(self):
        self.login_url = reverse('accounts.login')
        self.register_url = reverse('accounts.register')

    def test_login_404_when_waffle_is_off(self):
        create_switch('fxa-auth', active=False)
        response = self.client.post(self.login_url)
        assert response.status_code == 404

    def test_login_422_when_waffle_is_on(self):
        create_switch('fxa-auth', active=True)
        response = self.client.post(self.login_url)
        assert response.status_code == 422

    def test_register_404_when_waffle_is_off(self):
        create_switch('fxa-auth', active=False)
        response = self.client.post(self.register_url)
        assert response.status_code == 404

    def test_register_422_when_waffle_is_on(self):
        create_switch('fxa-auth', active=True)
        response = self.client.post(self.register_url)
        assert response.status_code == 422


class TestLoginUser(TestCase):

    def setUp(self):
        self.request = RequestFactory().get('/login')
        self.user = UserProfile.objects.create(email='real@yeahoo.com')
        self.identity = {'email': 'real@yeahoo.com', 'uid': '9001'}
        patcher = mock.patch('accounts.views.login')
        self.login = patcher.start()
        self.addCleanup(patcher.stop)

    def test_user_gets_logged_in(self):
        views.login_user(self.request, self.user, self.identity)
        self.login.assert_called_with(self.request, self.user)

    def test_identify_success_sets_fxa_data(self):
        assert self.user.fxa_id is None
        views.login_user(self.request, self.user, self.identity)
        user = self.user.reload()
        assert user.fxa_id == '9001'
        assert not user.has_usable_password()

    def test_identify_success_account_exists_migrated_different_email(self):
        self.user.update(email='different@yeahoo.com')
        views.login_user(self.request, self.user, self.identity)
        user = self.user.reload()
        assert user.fxa_id == '9001'
        assert user.email == 'real@yeahoo.com'


class TestRegisterUser(TestCase):

    def setUp(self):
        self.request = RequestFactory().get('/register')
        self.identity = {'email': 'me@yeahoo.com', 'uid': '9005'}
        patcher = mock.patch('accounts.views.login')
        self.login = patcher.start()
        self.addCleanup(patcher.stop)

    def test_user_is_created(self):
        user_qs = UserProfile.objects.filter(email='me@yeahoo.com')
        assert not user_qs.exists()
        views.register_user(self.request, self.identity)
        assert user_qs.exists()
        user = user_qs.get()
        assert user.username == 'me@yeahoo.com'
        assert user.fxa_id == '9005'
        assert not user.has_usable_password()
        self.login.assert_called_with(self.request, user)

    def test_username_taken_creates_user(self):
        UserProfile.objects.create(
            email='you@yeahoo.com', username='me@yeahoo.com')
        user_qs = UserProfile.objects.filter(email='me@yeahoo.com')
        assert not user_qs.exists()
        views.register_user(self.request, self.identity)
        assert user_qs.exists()
        user = user_qs.get()
        assert user.username.startswith('me@yeahoo.com')
        assert user.username != 'me@yeahoo.com'
        assert user.fxa_id == '9005'
        assert not user.has_usable_password()


@override_settings(FXA_CONFIG=FXA_CONFIG)
class BaseAuthenticationView(APITestCase):

    def setUp(self):
        self.url = reverse(self.view_name)
        create_switch('fxa-auth', active=True)
        self.fxa_identify = self.patch('accounts.views.verify.fxa_identify')

    def patch(self, thing):
        patcher = mock.patch(thing)
        self.addCleanup(patcher.stop)
        return patcher.start()


class TestLoginView(BaseAuthenticationView):
    view_name = 'accounts.login'

    def setUp(self):
        super(TestLoginView, self).setUp()
        self.login_user = self.patch('accounts.views.login_user')

    def test_no_code_provided(self):
        response = self.client.post(self.url)
        assert response.status_code == 422
        assert response.data['error'] == 'No code provided.'
        assert not self.login_user.called

    def test_identify_no_profile(self):
        self.fxa_identify.side_effect = verify.IdentificationError
        response = self.client.post(self.url, {'code': 'codes!!'})
        assert response.status_code == 401
        assert response.data['error'] == 'Profile not found.'
        self.fxa_identify.assert_called_with('codes!!', config=FXA_CONFIG)
        assert not self.login_user.called

    def test_identify_success_no_account(self):
        self.fxa_identify.return_value = {'email': 'me@yeahoo.com', 'uid': '5'}
        response = self.client.post(self.url, {'code': 'codes!!'})
        assert response.status_code == 422
        assert response.data['error'] == 'User does not exist.'
        self.fxa_identify.assert_called_with('codes!!', config=FXA_CONFIG)
        assert not self.login_user.called

    def test_identify_success_account_exists(self):
        user = UserProfile.objects.create(email='real@yeahoo.com')
        identity = {'email': 'real@yeahoo.com', 'uid': '9001'}
        self.fxa_identify.return_value = identity
        response = self.client.post(self.url, {'code': 'code'})
        assert response.status_code == 200
        assert response.data['email'] == 'real@yeahoo.com'
        self.login_user.assert_called_with(mock.ANY, user, identity)

    def test_identify_success_account_exists_migrated_multiple(self):
        UserProfile.objects.create(email='real@yeahoo.com', username='foo')
        UserProfile.objects.create(
            email='different@yeahoo.com', fxa_id='9005', username='bar')
        self.fxa_identify.return_value = {'email': 'real@yeahoo.com',
                                          'uid': '9005'}
        with self.assertRaises(UserProfile.MultipleObjectsReturned):
            self.client.post(self.url, {'code': 'code'})
        assert not self.login_user.called


class TestRegisterView(BaseAuthenticationView):
    view_name = 'accounts.register'

    def setUp(self):
        super(TestRegisterView, self).setUp()
        self.register_user = self.patch('accounts.views.register_user')

    def test_no_code_provided(self):
        response = self.client.post(self.url)
        assert response.status_code == 422
        assert response.data['error'] == 'No code provided.'
        assert not self.register_user.called

    def test_identify_no_profile(self):
        self.fxa_identify.side_effect = verify.IdentificationError
        response = self.client.post(self.url, {'code': 'codes!!'})
        assert response.status_code == 401
        assert response.data['error'] == 'Profile not found.'
        self.fxa_identify.assert_called_with('codes!!', config=FXA_CONFIG)
        assert not self.register_user.called

    def test_identify_success_no_account(self):
        identity = {u'email': u'me@yeahoo.com', u'uid': u'e0b6f'}
        self.register_user.return_value = UserProfile(email=identity['email'])
        self.fxa_identify.return_value = identity
        response = self.client.post(self.url, {'code': 'codes!!'})
        assert response.status_code == 200
        assert response.data['email'] == 'me@yeahoo.com'
        self.fxa_identify.assert_called_with('codes!!', config=FXA_CONFIG)
        self.register_user.assert_called_with(mock.ANY, identity)


class TestAuthorizeView(BaseAuthenticationView):
    view_name = 'accounts.authorize'

    def setUp(self):
        super(TestAuthorizeView, self).setUp()
        self.login_user = self.patch('accounts.views.login_user')
        self.register_user = self.patch('accounts.views.register_user')

    def test_no_code_provided(self):
        response = self.client.get(self.url)
        assert response.status_code == 422
        assert response.data['error'] == 'No code provided.'
        assert not self.login_user.called
        assert not self.register_user.called

    def test_identify_no_profile(self):
        self.fxa_identify.side_effect = verify.IdentificationError
        response = self.client.get(self.url, {'code': 'codes!!'})
        assert response.status_code == 401
        assert response.data['error'] == 'Profile not found.'
        self.fxa_identify.assert_called_with('codes!!', config=FXA_CONFIG)
        assert not self.login_user.called
        assert not self.register_user.called

    def test_identify_success_no_account(self):
        user_qs = UserProfile.objects.filter(email='me@yeahoo.com')
        assert not user_qs.exists()
        identity = {u'email': u'me@yeahoo.com', u'uid': u'e0b6f'}
        self.fxa_identify.return_value = identity
        response = self.client.get(self.url, {'code': 'codes!!'})
        assert response.status_code == 302
        self.fxa_identify.assert_called_with('codes!!', config=FXA_CONFIG)
        assert not self.login_user.called
        self.register_user.assert_called_with(mock.ANY, identity)

    def test_identify_success_exists_logs_user_in(self):
        user = UserProfile.objects.create(email='real@yeahoo.com')
        identity = {'email': 'real@yeahoo.com', 'uid': '9001'}
        self.fxa_identify.return_value = identity
        response = self.client.get(self.url, {'code': 'code'})
        assert response.status_code == 302
        self.login_user.assert_called_with(mock.ANY, user, identity)
        assert not self.register_user.called


class TestProfileView(APIAuthTestCase):

    def setUp(self):
        self.create_api_user()
        self.url = reverse('accounts.profile')
        self.cls = resolve(self.url).func.cls
        super(TestProfileView, self).setUp()

    def test_good(self):
        res = self.get(self.url)
        assert res.status_code == 200
        assert res.data['email'] == 'a@m.o'

    def test_auth_required(self):
        self.auth_required(self.cls)

    def test_verbs_allowed(self):
        self.verbs_allowed(self.cls, ['get'])
