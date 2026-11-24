import unittest

from role_orchestrator.contracts import strict_json


B = chr(0x5C)
LAYOUTS = (
    ('root', '"%s"', lambda result: result),
    ('array', '{"nested":["%s"]}', lambda result: result['nested'][0]),
    ('name', '{"nested":{"%s":"value"}}', lambda result: next(iter(result['nested']))),
)


class StrictJsonUnicodeScalarsTest(unittest.TestCase):
    def test_rejects_lone_surrogates(self):
        forms = (chr(0xD800), chr(0xDC00), B + 'uD800', B + 'uDC00')
        for prefix, suffix in (('', ''), ('a', 'b')):
            for form in forms:
                for location, template, _ in LAYOUTS:
                    with self.subTest(location=location, form=ascii(form), embedded=bool(prefix)):
                        with self.assertRaises(ValueError):
                            strict_json(template % (prefix + form + suffix))

    def test_preserves_valid_strings(self):
        cases = (
            ('한', '한'),
            (B + 'uD55C', '한'),
            ('😀', '😀'),
            (B + 'uD83D' + B + 'uDE00', '😀'),
            (B + B + 'uD800', B + 'uD800'),
            (B + B + 'uDC00', B + 'uDC00'),
        )
        for form, expected in cases:
            for location, template, extract in LAYOUTS:
                with self.subTest(location=location, form=ascii(form)):
                    self.assertEqual(extract(strict_json(template % form)), expected)
