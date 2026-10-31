import base64
from datetime import datetime
import io
import json
import sqlite3
import unittest
from unittest.mock import Mock

from flask import Response, flash, redirect, request, session, url_for
from werkzeug.utils import secure_filename

# Load the real optional dependency before the legacy generation test installs
# its tiny NumPy stub; otherwise later openpyxl tests see an incomplete module.
import numpy
from test_question_generation_service import question_generation_service
from routes.question import create_question_blueprint
from services.ai_compiled_runtime import generate_compiled
import test_ai_question_routes as route_fixture


class AIBackupTests(unittest.TestCase):
    def setUp(self):
        self.fixture = route_fixture.AIRouteTests(); self.fixture.setUp()
        db = sqlite3.connect(self.fixture.path)
        db.executescript('''ALTER TABLE exam_papers ADD COLUMN description TEXT;
            ALTER TABLE exam_papers ADD COLUMN is_enabled INT DEFAULT 1;
            ALTER TABLE exam_papers ADD COLUMN created_at TEXT;
            ALTER TABLE problem_templates ADD COLUMN status TEXT DEFAULT 'active';''')
        db.close()
        deps = dict(self.fixture.deps, request=request,session=session,flash=flash,redirect=redirect,
                    url_for=url_for,Response=Response,datetime=datetime,json=json,base64=base64,
                    secure_filename=secure_filename,question_generation_service=question_generation_service,
                    save_imported_image=Mock(return_value=None))
        self.fixture.app.register_blueprint(create_question_blueprint(deps))

    def tearDown(self): self.fixture.tearDown()

    def export(self):
        row = self.fixture.create(); self.assertEqual(self.fixture.import_draft(row).status_code,200)
        response = self.fixture.client.get('/admin/exam_papers/1/export')
        self.assertEqual(response.status_code,200)
        return json.loads(response.get_data(as_text=True))

    def restore(self, backup):
        return self.fixture.client.post('/admin/exam_papers/import',data={
            'paper_file':(io.BytesIO(json.dumps(backup).encode()),'backup.json')})

    def test_signed_compiled_backup_round_trips_without_losing_cases(self):
        backup = self.export()
        self.assertIn('generation_strategy',backup['templates'][0])
        response = self.restore(backup); self.assertEqual(response.status_code,302)
        db = sqlite3.connect(self.fixture.path); db.row_factory = sqlite3.Row
        rows = [dict(row) for row in db.execute('SELECT * FROM problem_templates ORDER BY id')]; db.close()
        self.assertEqual(len(rows),2)
        self.assertEqual(generate_compiled(rows[1],json.loads(rows[1]['generation_strategy']))['answer_count'],1)

    def test_edited_backup_is_rejected_and_import_transaction_rolls_back(self):
        backup = self.export(); backup['templates'][0]['solution_formula']='1'
        self.restore(backup)
        db = sqlite3.connect(self.fixture.path)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM problem_templates').fetchone()[0],1)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM exam_papers').fetchone()[0],1)
        db.close()

    def test_compiled_edit_redirects_to_revalidation_instead_of_mutating(self):
        row = self.fixture.create(); response = self.fixture.import_draft(row)
        template_id = response.json['template_id']
        reply = self.fixture.client.post('/admin/edit_problem/'+str(template_id),data={'solution_formula':'1'})
        self.assertEqual(reply.status_code,302)
        self.assertIn('/admin/ai-questions?',reply.headers['Location'])
        self.assertIn('template_id='+str(template_id),reply.headers['Location'])
