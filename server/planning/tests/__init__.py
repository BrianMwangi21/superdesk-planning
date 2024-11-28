from superdesk.tests import TestCase as _TestCase, update_config


class TestCase(_TestCase):
    test_context = None  # avoid using test_request_context

    def setUp(self):
        config = {"INSTALLED_APPS": ["planning"], "MODULES": ["planning.module"]}
        update_config(config)
        super().setUp()
