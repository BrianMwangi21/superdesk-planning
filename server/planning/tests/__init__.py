from superdesk.tests import TestCase as BaseTestCase


class TestCase(BaseTestCase):
    test_context = None  # avoid using test_request_context
    app_config = {
        "INSTALLED_APPS": ["planning"],
        "MODULES": ["planning.module"],
    }
