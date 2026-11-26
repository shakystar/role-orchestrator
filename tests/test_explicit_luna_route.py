import unittest

from role_orchestrator.contracts import MODEL_NAMES, supported_route


class ExplicitLunaRouteTests(unittest.TestCase):
    def test_only_high_is_supported_for_canonical_luna(self):
        for effort, expected in (
            ('low', False), ('medium', False), ('high', True),
            ('', False), ('HIGH', False), ('unsupported', False),
            (None, False), ([], False), ({}, False),
        ):
            with self.subTest(effort=effort):
                self.assertIs(supported_route('gpt-6-luna', effort), expected)

    def test_existing_roster_aliases_and_canonical_routes(self):
        expected = {'Sol': 'gpt-6.2-sol', 'Astra': 'gpt-6.2-astra'}
        self.assertEqual(MODEL_NAMES, expected)
        for alias, canonical in expected.items():
            for effort in ('low', 'medium', 'high'):
                with self.subTest(alias=alias, effort=effort):
                    self.assertTrue(supported_route(canonical, effort))
                    self.assertFalse(supported_route(alias, effort))

    def test_disabled_and_invalid_models(self):
        for model in ('gpt-5.6-luna', 'Luna', 'unknown', '', None, [], {}):
            for effort in ('low', 'medium', 'high'):
                with self.subTest(model=model, effort=effort):
                    self.assertFalse(supported_route(model, effort))

    def test_invalid_efforts_on_existing_routes(self):
        for model in ('gpt-6.2-sol', 'gpt-6.2-astra'):
            for effort in ('', 'unsupported', None, [], {}):
                with self.subTest(model=model, effort=effort):
                    self.assertFalse(supported_route(model, effort))
