import ast
from functools import wraps
from pathlib import Path
import time
import unittest

from flask import Flask, jsonify, request, session, redirect, flash, url_for


class ApiLoginErrorTests(unittest.TestCase):
    def setUp(self):
        self.app=Flask(__name__)
        self.app.secret_key='test-only'
        tree=ast.parse((Path(__file__).parents[1]/'routes/auth.py').read_text(encoding='utf-8'))
        nodes=[node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name in {'login_required','enforce_session_idle_timeout'}]
        for node in nodes:
            node.decorator_list=[]
        namespace=dict(wraps=wraps,jsonify=jsonify,request=request,session=session,redirect=redirect,
                       flash=flash,url_for=url_for,time=time,SESSION_IDLE_TIMEOUT_SECONDS=3600)
        exec(compile(ast.Module(body=nodes,type_ignores=[]),'auth.py','exec'),namespace)
        self.app.before_request(namespace['enforce_session_idle_timeout'])
        for path in ['/api/submit/1','/page']:
            self.app.add_url_rule(path,endpoint=path,view_func=namespace['login_required'](lambda:'ok'))
        self.app.add_url_rule('/login',endpoint='auth.login',view_func=lambda:'login')
        self.client=self.app.test_client()

    def test_expired_api_session_returns_specific_json(self):
        with self.client.session_transaction() as state:
            state.update(user_id=1,last_activity_ts=int(time.time())-4000)
        response=self.client.get('/api/submit/1')
        self.assertEqual(response.status_code,401)
        self.assertTrue(response.get_json()['login_required'])
        self.assertIn('登录已失效',response.get_json()['message'])

    def test_anonymous_api_returns_json_while_normal_page_still_redirects(self):
        self.assertEqual(self.client.get('/api/submit/1').status_code,401)
        self.assertEqual(self.client.get('/page').headers['Location'],'/login')


if __name__=='__main__':
    unittest.main()
