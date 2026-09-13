import os
import unittest
from unittest.mock import patch, Mock
import healthcheck
import server


class HostingTests(unittest.TestCase):
    def test_binding(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(server.bind_address(), '127.0.0.1')
        with patch.dict(os.environ, {'MTA_PROXY_BIND': '0.0.0.0'}):
            self.assertEqual(server.bind_address(), '0.0.0.0')

    def test_health_classification(self):
        for board, expected in [((503, {}), 2), ((200, {'stations': [{}], 'failed_feeds': ['gtfs']}), 2), ((200, {'stations': [{'partial': True}]}), 2), ((200, {'stations': [{}]}), 0), ((400, {}), 1), ((200, {}), 1)]:
            with self.subTest(board=board):
                get = Mock(side_effect=[(200, {'ok': True}), board])
                self.assertEqual(healthcheck.check('http://local', get)[0], expected)
        for failure in [OSError(), (500, {}), (200, {'ok': False})]:
            get = Mock(side_effect=[failure])
            self.assertEqual(healthcheck.check('http://local', get)[0], 1)
            self.assertEqual(get.call_count, 1)
        self.assertEqual(healthcheck.check('http://local', Mock(side_effect=[(200, {'ok': True}), TimeoutError()]))[0], 1)

    def test_logs_exclude_query(self):
        handler = object.__new__(server.Handler)
        handler.command = 'GET'
        handler.path = '/mta/board?lat=40.12345&lon=-73.98765'
        with self.assertLogs('mta-proxy') as logs:
            handler.log_request(200)
        self.assertIn('path=/mta/board status=200 duration_ms=', logs.output[0])
        self.assertNotIn('lat=', logs.output[0])

class HttpTests(unittest.TestCase):
    def test_failure_contract(self):
        import json
        import threading
        import urllib.request
        import urllib.error
        httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.object(server, 'board', side_effect=RuntimeError('private detail')):
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen('http://127.0.0.1:%s/mta/board?lat=0&lon=0' % httpd.server_port)
                self.assertEqual(caught.exception.code, 503)
                self.assertEqual(json.load(caught.exception), {'error': 'MTA feeds unavailable'})
                caught.exception.close()
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join()
