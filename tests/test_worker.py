import unittest
from datetime import date
import httpx
from lead_agent.worker import Portal, Store, address_key, eligible, flight


class WorkerTests(unittest.TestCase):
    def test_filters(self):
        base = dict(address='123 Main St', record_id='A', use='Commercial', kind='violation', status='Open')
        self.assertTrue(eligible(base, 90))
        self.assertFalse(eligible(base | {'use': 'Residential'}, 90))
        self.assertFalse(eligible(base | {'status': 'Closed'}, 90))
        backflow = base | dict(kind='backflow', deadline='2026-11-01')
        self.assertTrue(eligible(backflow, 90, date(2026, 10, 26)))
        self.assertFalse(eligible(backflow, 90, date(2026, 11, 2)))
        self.assertFalse(eligible(backflow | {'deadline':'bad'}, 90))

    def test_address_units_retained(self):
        self.assertEqual(address_key('123 MAIN ST.'), address_key('123 Main St'))
        self.assertNotEqual(address_key('123 Main St Unit 1'), address_key('123 Main St Unit 2'))

    def test_json_pagination_and_robots(self):
        def handler(request):
            if request.url.path == '/robots.txt':
                return httpx.Response(200, text='User-agent: *\nAllow: /')
            if request.url.path == '/one':
                return httpx.Response(200, json={'results':[{'id':'1'}], 'next':'/two'})
            return httpx.Response(200, json={'results':[{'id':'2'}]})
        cfg = dict(url='https://county.test/one', allowed_hosts=['county.test'], format='json', items_key='results', fields={'record_id':'id'}, request_delay_seconds=0)
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            self.assertEqual(list(Portal(cfg, client).rows()), [{'record_id':'1'}, {'record_id':'2'}])

    def test_access_denied_no_retry(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(403)
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(RuntimeError):
                Portal({'allowed_hosts':['county.test'], 'request_delay_seconds':0}, client).request('https://county.test/data')
        self.assertEqual(len(calls), 1)

    def test_markup_drift_fails(self):
        def handler(request):
            return httpx.Response(200, text='User-agent: *\nAllow: /' if request.url.path == '/robots.txt' else '<html>Login</html>')
        config = dict(url='https://county.test/data', allowed_hosts=['county.test'], format='html', container_selector='#results', row_selector='tr', request_delay_seconds=0)
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(ValueError):
                list(Portal(config, client).rows())

    def test_store_does_not_reset_existing_status(self):
        class Client:
            data = [{'id': 1}]
            def table(self, *_): return self
            def select(self, *_): return self
            def eq(self, *_): return self
            def limit(self, *_): return self
            def execute(self): return self
            def upsert(self, *_args, **_kwargs): raise AssertionError('Existing lead must not be updated')
        self.assertFalse(Store(Client()).insert_new({'address':'123 Main St'}, 'code_enforcement'))

    def test_source_failure_isolated(self):
        class FakeStore:
            def insert_new(self, *_): return True
        def handler(request):
            if request.url.path == '/robots.txt': return httpx.Response(200, text='User-agent: *\nAllow: /')
            if request.url.path == '/bad': return httpx.Response(403)
            return httpx.Response(200, json={'rows':[dict(address='123 Main St', record_id='a', use='Commercial', kind='violation', status='Open')]})
        base=dict(allowed_hosts=['county.test'], format='json', items_key='rows', request_delay_seconds=0, fields={k:k for k in ['address','record_id','use','kind','status']})
        config={'sources':[base | dict(name='code_enforcement', url='https://county.test/bad'), base | dict(name='building_permits', url='https://county.test/good')]}
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            counts=flight(config, FakeStore(), client)
        self.assertEqual(counts['sources_failed'], 1)
        self.assertEqual(counts['sources_ok'], 1)
        self.assertEqual(counts['inserted'], 1)

if __name__ == '__main__': unittest.main()
