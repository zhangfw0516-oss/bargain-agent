import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from agent import ParserError, parse_user_instruction
from schemas import validate_task

URL = 'https://example.com/item'
REQUEST = f'Headphones {URL} below NZD 200 every hour'

def valid():
    return dict(schema_version='1', status='ready', product_name='headphones',
                url=URL, target_price='200', currency='NZD', comparison='lt',
                frequency_minutes=60, condition=None, missing_fields=[], clarification_question=None)

def fake(content, reason='stop'):
    client = Mock()
    client.chat.completions.create.return_value = SimpleNamespace(choices=[
        SimpleNamespace(finish_reason=reason, message=SimpleNamespace(content=content))])
    return client

class ParserTests(unittest.TestCase):
    def test_api_contract(self):
        client = fake(json.dumps(valid()))
        result = parse_user_instruction(REQUEST, client=client, model='test')
        self.assertEqual(result['target_price'], '200.00')
        kwargs = client.chat.completions.create.call_args.kwargs
        self.assertEqual(kwargs['response_format'], {'type': 'json_object'})
        self.assertEqual(kwargs['messages'][1]['content'], REQUEST)

    def test_missing_overrides_ready(self):
        data = valid(); data.update(currency=None, frequency_minutes=None)
        result = validate_task(data, REQUEST)
        self.assertEqual(result['status'], 'needs_clarification')
        self.assertEqual(result['missing_fields'], ['currency', 'frequency_minutes'])
        self.assertTrue(result['clarification_question'])

    def test_invalid_values(self):
        for field, values in {'target_price': [0, '0', '-1', 'NaN', '1.001'],
                              'frequency_minutes': [True, 0, 1, 5.5],
                              'currency': ['dollars'], 'comparison': ['gt']}.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    data = valid(); data[field] = value
                    with self.assertRaises(ValueError): validate_task(data, REQUEST)

    def test_unsafe_urls(self):
        for url, text in [(URL + '/invented', REQUEST),
                          ('http://127.0.0.1/a', 'http://127.0.0.1/a'),
                          ('http://localhost/a', 'http://localhost/a'),
                          ('https://u:p@example.com/a', 'https://u:p@example.com/a')]:
            with self.subTest(url=url):
                data = valid(); data['url'] = url
                with self.assertRaises(ValueError): validate_task(data, text)

    def test_invalid_output(self):
        for content, reason in [('', 'stop'), ('not json', 'stop'), ('[]', 'stop'),
                                ('{}', 'stop'), (json.dumps(valid()), 'length')]:
            with self.subTest(content=content):
                with self.assertRaises(ParserError):
                    parse_user_instruction(REQUEST, client=fake(content, reason), model='test')

    def test_error_sanitized(self):
        client = Mock(); client.chat.completions.create.side_effect = RuntimeError('secret-key')
        with self.assertRaises(ParserError) as error:
            parse_user_instruction(REQUEST, client=client, model='test')
        self.assertNotIn('secret-key', str(error.exception))

    def test_invalid_input_no_call(self):
        client = Mock()
        for text in ('', ' ', 'x' * 8001):
            with self.assertRaises(ParserError):
                parse_user_instruction(text, client=client, model='test')
        client.chat.completions.create.assert_not_called()

    def test_unsupported_and_operators(self):
        data = valid(); data.update(status='unsupported', clarification_question='Multiple products unsupported')
        self.assertEqual(validate_task(data, REQUEST)['status'], 'unsupported')
        for operator in ('lt', 'lte'):
            data = valid(); data['comparison'] = operator
            self.assertEqual(validate_task(data, REQUEST)['comparison'], operator)

if __name__ == '__main__':
    unittest.main()
