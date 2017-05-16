# -*- coding: utf-8 -*-
import unittest
from copy import deepcopy
from uuid import uuid4
from pyramid.testing import DummyRequest
from pyramid import testing
from openprocurement.api.auth import (
    AuthenticationPolicy,
    check_accreditation,
    authenticated_role
)
from pyramid.authorization import ACLAuthorizationPolicy
from pyramid.events import NewRequest, ContextFound
from jsonpointer import resolve_pointer
from webtest import TestApp
from openprocurement.historical.core.constants import (
    VERSION, HASH, PREVIOUS_HASH
)
from openprocurement.historical.core.utils import (
    Root,
    add_responce_headers,
    parse_hash,
    extract_doc,
    HasRequestMethod,
)

from openprocurement.api.utils import (
    add_logging_context,
    set_logging_context,
)
from openprocurement.historical.core.tests.utils import mock_doc, Db


db = Db()


class HistoricalUtilsTestCase(unittest.TestCase):

    def _make_req(self):
        req = DummyRequest()
        req.registry.db = db
        req.matchdict['doc_id'] = mock_doc.id
        req.validated = {}
        return req

    def test_root(self):
        req = self._make_req()
        root = Root(req)
        self.assertEqual(req.registry.db, root.db)
        self.assertEqual(req, root.request)

    def test_parse_hash(self):
        _hash = ''
        self.assertEqual('', parse_hash(_hash))
        _hash = '2-909f500147c5c6d6ed16357fcee10f8b'
        self.assertEqual('909f500147c5c6d6ed16357fcee10f8b', parse_hash(_hash))
        _hash = '909f500147c5c6d6ed16357fcee10f8b'
        self.assertEqual('', parse_hash(_hash))

    def test_responce_headers(self):
        request = DummyRequest()
        add_responce_headers(request, version='22',
                             rhash='test-hash', phash='prev-hash')
        self.assertIn(VERSION, request.response.headers)
        self.assertEqual('22', request.response.headers[VERSION])
        self.assertIn(HASH, request.response.headers)
        self.assertEqual('test-hash', request.response.headers[HASH])

        self.assertIn(PREVIOUS_HASH, request.response.headers)
        self.assertEqual('prev-hash', request.response.headers[PREVIOUS_HASH])

        request = DummyRequest()
        add_responce_headers(request, version=42)
        self.assertIn(VERSION, request.response.headers)
        self.assertEqual('42', request.response.headers[VERSION])

        request = DummyRequest()
        request.validated = {}
        request.validated['historical_header_version'] = '42'
        add_responce_headers(request, version=42)
        self.assertIn(VERSION, request.response.headers)
        self.assertEqual('42', request.response.headers[VERSION])

        request = DummyRequest()
        request.validated = {}
        add_responce_headers(request)
        self.assertIn(VERSION, request.response.headers)
        self.assertEqual('', request.response.headers[VERSION])

    def test_has_request_method_predicate(self):
        config = testing.setUp()
        pred = HasRequestMethod('test', config)
        self.assertEqual('HasRequestMethod = test', pred.text())
        request = DummyRequest()
        self.assertFalse(pred(None, request))
        setattr(request, 'test', lambda x: True)
        self.assertTrue(pred(None, request))

    def test_find_date_modified(self):
        request = self._make_req()
        request.headers[VERSION] = '11'
        doc = extract_doc(request, 'mock')
        self.assertIn(VERSION, request.response.headers)
        self.assertEqual(request.response.headers[VERSION], '11')
        self.assertEqual(doc['dateModified'],
                         mock_doc['revisions'][11]['date'])

        request = self._make_req()
        request.headers[VERSION] = '2'
        doc = extract_doc(request, 'mock')
        self.assertIn(VERSION, request.response.headers)
        self.assertEqual(request.response.headers[VERSION], '2')
        self.assertEqual(doc['dateModified'],
                         mock_doc['revisions'][1]['date'])


class HistoricalResourceTestCase(unittest.TestCase):

    def setUp(self):
        from pyramid.renderers import JSONP

        self.config = testing.setUp()
        self.config.add_renderer('jsonp', JSONP(param_name='callback'))
        self.config.include("cornice")
        self.config.registry.server_id = uuid4().hex
        self.config.add_request_method(check_accreditation)
        self.authz_policy = ACLAuthorizationPolicy()
        self.config.set_authorization_policy(self.authz_policy)
        self.config.add_subscriber(add_logging_context, NewRequest)
        self.config.add_subscriber(set_logging_context, ContextFound)
        self.config.add_request_method(authenticated_role, reify=True)
        self.config.include(
            'openprocurement.historical.core.includeme.includeme'
        )

        self.config.registry.db = db

        self.authn_policy = AuthenticationPolicy(
            'openprocurement/historical/core/tests/auth.ini', __name__)
        self.config.set_authentication_policy(self.authn_policy)
        self.config.scan("openprocurement.historical.core.tests.utils")
        self.app = TestApp(self.config.make_wsgi_app())
        self.app.authorization = ('Basic', ('broker', ''))

    def tearDown(self):
        testing.tearDown()

    def test_not_found(self):
        resp = self.app.get('/mock/{}/historical'.format('invalid'),
                            status=404)
        self.assertEqual(resp.status, '404 Not Found')
        self.assertEqual(resp.json['status'], 'error')
        self.assertEqual(resp.json['errors'], [{
            u'description': u'Not Found',
            u'location': u'url',
            u'name': u'mock_id'}
        ])

    def test_base_view_called(self):
        resp = self.app.get('/mock/{}/historical'.format(mock_doc.id))
        self.assertEqual(resp.status, '200 OK')
        self.assertIn("Base", resp.json)
        self.assertEqual(resp.json["Base"], "OK!")

    def test_get_no_headers(self):
        resp = self.app.get('/mock/{}/historical'.format(mock_doc.id))
        self.assertEqual(resp.status, '200 OK')

    def test_forbidden(self):
        self.app.authorization = ('Basic', ('', ''))
        resp = self.app.get('/mock/{}/historical'.format(mock_doc.id),
                            status=403)
        self.assertEqual(resp.status, '403 Forbidden')

        # no accreditation
        self.app.authorization = ('Basic', ('broker1', ''))
        resp = self.app.get('/mock/{}/historical'.format(mock_doc.id),
                            status=403)
        self.assertEqual(resp.status, '403 Forbidden')

        # admin access
        self.app.authorization = ('Basic', ('administrator', ''))
        resp = self.app.get('/mock/{}/historical'.format(mock_doc.id))
        self.assertEqual(resp.status, '200 OK')

    def test_get_header_invalid(self):

        for header in ['0', '-1', 'asdsf', '10000000']:
            resp = self.app.get('/mock/{}/historical'.format(mock_doc.id),
                                headers={
                                    'X-Revision-N': header
                                }, status=404)
            self.assertEqual(resp.status, '404 Not Found')
            self.assertEqual(resp.json['status'], 'error')
            self.assertEqual(resp.json['errors'], [{
                u'description': u'Not Found',
                u'location': u'header',
                u'name': u'version'}
            ])

    def test_route_not_found(self):
        self.app.app.routes_mapper.routelist = [
            r for r in self.app.app.routes_mapper.routelist
            if r.name != 'MockBase'
        ]

        response = self.app.get('/mock/{}/historical'.format(mock_doc.id),
                                status=404)
        self.assertEqual(response.status, '404 Not Found')
        self.assertEqual(response.json['errors'], [{
            u'description': u'Not Found',
            u'location': u'url',
            u'name': u'mock_id'
        }])

    def test_responce_header_present(self):
        resp = self.app.get('/mock/{}/historical'.format(mock_doc.id))
        self.assertEqual(resp.status, '200 OK')
        self.assertIn(VERSION, resp.headers)
        self.assertEqual(resp.headers[VERSION],
                         str(len(mock_doc['revisions'])))

    def test_apply_patch(self):
        doc = deepcopy(mock_doc)
        revisions = doc.pop('revisions')
        for version, rev in enumerate(revisions[1:], 1):
            response = self.app.get('/mock/{}/historical'.format(mock_doc.id),
                                    headers={
                                        'X-Revision-N': str(version)
                                    })
            self.assertEqual(response.status, '200 OK')
            self.assertEqual(response.headers.get(HASH),
                             parse_hash(rev.get('rev')))
            self.assertEqual(response.headers.get(VERSION), str(version))
            data = response.json
            data.pop('Base')
            for ch in rev['changes']:
                val = ch['value'] if ch['op'] != 'remove' else 'missing'
                self.assertEqual(resolve_pointer(data, ch['path'], 'missing'),
                                 val)

    def test_invalid_patch(self):
        response = self.app.get('/mock/broken/historical', headers={
            'X-Revision-N': '1'
        }, status=501)
        self.assertEqual(response.status, '501 Not Implemented')
        self.assertEqual(response.json['errors'], [{
            u'description': u'Not Implemented',
            u'location': u'tender',
            u'name': u'revision'
        }])

    def test_hash_not_found(self):
        response = self.app.get('/mock/{}/historical'.format(mock_doc.id),
                                headers={
                                    'X-Revision-N': '1'
                                })
        self.assertEqual(response.status, '200 OK')

        response = self.app.get('/mock/{}/historical'.format(mock_doc.id),
                                headers={
                                    'X-Revision-N': '1',
                                    'X-Revision-Hash': '11111'
                                }, status=404)
        self.assertEqual(response.status, '404 Not Found')
        self.assertEqual(response.json['errors'], [{
            u'description': u'Not Found',
            u'location': u'header',
            u'name': u'hash'
        }])


def suite():
    suite = unittest.TestSuite()
    suite.addTest(unittest.makeSuite(HistoricalUtilsTestCase))
    suite.addTest(unittest.makeSuite(HistoricalResourceTestCase))
    return suite


if __name__ == '__main__':
    unittest.main(defaultTest='suite')
