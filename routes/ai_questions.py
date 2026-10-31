"""Admin-only authoring, immutable draft revisions and transactional import."""
import functools
import hmac
import json
import logging
import os
from pathlib import Path
import secrets
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

from flask import Blueprint, abort, jsonify, render_template, request, send_from_directory, session

from services import ai_question_agent as agent
from services.ai_compiled_runtime import signature
from services.ai_draft_store import DraftStore
from services.ai_template_compiler import CompileError, compile_isolated, fingerprint, render_case, require

logger = logging.getLogger(__name__)
executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='ai-authoring')
slots = threading.BoundedSemaphore(2)


def enabled():
    return os.getenv('AI_AUTHORING_ENABLED', 'false').lower() in ('1', 'true', 'yes', 'on')


def image_type(raw):
    if raw.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png', '.png'
    if raw.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg', '.jpg'
    if raw.startswith(b'RIFF') and raw[8:12] == b'WEBP':
        return 'image/webp', '.webp'
    raise CompileError('仅接受 PNG、JPEG 或 WebP 图片')


def create_ai_question_blueprint(deps):
    bp = Blueprint('ai_questions', __name__)
    store = DraftStore(deps['get_db_connection'])

    def admin(view):
        @functools.wraps(view)
        def wrapped(*args, **kwargs):
            if session.get('username') != 'admin':
                abort(403)
            if not enabled():
                abort(404)
            if request.method == 'POST':
                token = session.get('ai_csrf', '')
                if not token or not hmac.compare_digest(token, request.headers.get('X-AI-CSRF', '')):
                    abort(403)
            store.ensure()
            return view(*args, **kwargs)
        return deps['login_required'](db_check=True)(wrapped)

    def directory():
        default = Path(deps['app'].instance_path) / 'ai_question_sources' if 'app' in deps else Path.cwd() / 'instance' / 'ai_question_sources'
        path = Path(deps.get('ai_source_folder') or os.getenv('AI_SOURCE_FOLDER') or default)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def upload(field):
        image = request.files.get(field)
        if not image or not image.filename:
            return None
        raw = image.read(6 * 1024 * 1024 + 1)
        require(12 <= len(raw) <= 6 * 1024 * 1024, '每张图片须小于 6MB')
        mime, extension = image_type(raw)
        filename = uuid.uuid4().hex + extension
        (directory() / filename).write_bytes(raw)
        return {'filename': filename, 'mime': mime}

    def get_draft(draft_id):
        require(isinstance(draft_id, str) and len(draft_id) == 32, '草稿编号错误')
        rows = store.list(draft_id=draft_id)
        if not rows:
            abort(404)
        return rows[0]

    def serialize(row):
        data = row['data']
        artifact = data.get('artifact')
        result = {key: row[key] for key in ('id', 'paper_id', 'status', 'message', 'template_id', 'created_at')}
        result.update(source=data.get('source'), parent_id=data.get('parent_id'), plan=data.get('plan'),
                      artifact_hash=data.get('artifact_hash'), report=artifact.get('report') if artifact else None,
                      diagram=bool(data.get('diagram')), source_image=bool(data.get('image')), previews=[])
        if artifact:
            template = artifact['template']
            result['template'] = template
            result['analysis'] = artifact['analysis']
            result['previews'] = [render_case(template, case) for case in template['generation_strategy']['valid_cases'][:5]]
        return result

    def worker(draft_id, data):
        try:
            def progress(status, message):
                store.update(draft_id, status, message)
            if data.get('manual'):
                progress('validating', '正在穷举模板候选组合')
                plan = data['manual']
                artifact = compile_isolated(plan['template'], plan['analysis'])
                result = {'status': 'ready', 'message': '模板已通过程序验证，请预览后入库', 'plan': plan, 'artifact': artifact}
            else:
                images = []
                for field, label in (('image', '原题截图，仅用于识题'), ('diagram', '学生示意图，需判断是否适合参数变化')):
                    image = data.get(field)
                    if image:
                        images.append((label, image['mime'], (directory() / image['filename']).read_bytes()))
                result = agent.generate(data['source'], data['rules'], images, progress, data.get('previous'))
            data.update(plan=result['plan'], artifact=result['artifact'])
            if result['artifact']:
                limit = data['source'].get('answer_limit') or '3'
                require(limit in ('1', '2', '3') and result['artifact']['template']['answer_count'] <= int(limit), '模板答案数量超过本次允许数量')
                data['artifact_hash'] = fingerprint(result['artifact']['template'], result['artifact']['analysis'])
            store.update(draft_id, result['status'], result['message'], data)
        except Exception as exc:
            # Never put remote response bodies, credentials, or student data in errors/logs.
            message = str(exc) if isinstance(exc, CompileError) else '处理失败，输入已保存，请稍后重试'
            logger.warning('AI authoring failed: draft=%s type=%s', draft_id, type(exc).__name__)
            store.update(draft_id, 'failed', message, data)
        finally:
            slots.release()

    @bp.errorhandler(CompileError)
    def bad_request(exc):
        return jsonify(error=str(exc)), 400

    @bp.route('/admin/ai-questions')
    @admin
    def page():
        paper_id = request.args.get('paper_id', type=int)
        paper = deps['get_exam_paper_by_id'](paper_id) if paper_id else None
        if not paper:
            abort(404)
        session.setdefault('ai_csrf', secrets.token_urlsafe(32))
        selected_draft = ''
        template_id = request.args.get('template_id', type=int)
        if template_id:
            conn = deps['get_db_connection']()
            cursor = conn.cursor(dictionary=True)
            try:
                cursor.execute('SELECT id FROM ai_question_drafts WHERE template_id=%s AND paper_id=%s', (template_id, paper_id))
                linked = cursor.fetchone()
                selected_draft = linked['id'] if linked else ''
            finally:
                cursor.close()
                conn.close()
        return render_template('admin_ai_questions.html', paper=paper, csrf=session['ai_csrf'],
                               selected_draft=selected_draft, configured=agent.configured(), default_rules=store.rules(agent.DEFAULT_RULES))

    @bp.route('/admin/ai-questions/settings', methods=['POST'])
    @admin
    def settings():
        rules = (request.get_json(silent=True) or {}).get('rules')
        require(isinstance(rules, str) and 1 <= len(rules) <= 12000, '生成规范须为 1 到 12000 字')
        store.rules(agent.DEFAULT_RULES, rules)
        return jsonify(ok=True)

    @bp.route('/admin/ai-questions/drafts', methods=['GET', 'POST'])
    @admin
    def drafts():
        if request.method == 'GET':
            return jsonify(drafts=[serialize(row) for row in store.list(paper_id=request.args.get('paper_id', type=int))])
        require(request.content_length is None or request.content_length <= 14 * 1024 * 1024, '上传内容过大')
        paper_id = request.form.get('paper_id', type=int)
        require(bool(paper_id and deps['get_exam_paper_by_id'](paper_id)), '请选择有效题库')
        if not slots.acquire(blocking=False):
            return jsonify(error='已有生成任务，请稍后再试'), 429
        created_files = []
        try:
            source = {key: request.form.get(key, '').strip() for key in
                      ('text', 'requirements', 'stage', 'difficulty', 'knowledge_point', 'keep_questions', 'answer_limit')}
            require(sum(len(x) for x in source.values()) <= 14000, '输入文字过长')
            require(source['difficulty'] in ('', 'easy', 'medium', 'hard'), '难度设置错误')
            require(source['answer_limit'] in ('', '1', '2', '3'), '答案数量设置错误')
            data = {'source': source, 'rules': store.rules(agent.DEFAULT_RULES)}
            parent_id = request.form.get('parent_id', '')
            if parent_id:
                parent = get_draft(parent_id)
                require(parent['paper_id'] == paper_id, '只能在同一题库继续修改草稿')
                require(parent['status'] not in ('queued', 'generating', 'validating'), '原任务尚未完成')
                original = parent['data']
                data.update(parent_id=parent_id, image=original.get('image'), diagram=original.get('diagram'),
                            previous=original.get('plan'))
                data['source'] = dict(original['source'], **{key: value for key, value in source.items() if value})
            for field in ('image', 'diagram'):
                image = upload(field)
                if image:
                    data[field] = image
                    created_files.append(image['filename'])
            raw_manual = request.form.get('manual', '').strip()
            if raw_manual:
                require(len(raw_manual.encode()) <= 200000, '模板资料过大')
                try:
                    manual = json.loads(raw_manual)
                except ValueError as exc:
                    raise CompileError('请输入标准 JSON，包含 template 和 analysis') from exc
                require(isinstance(manual, dict) and set(manual) == {'template', 'analysis'}, '模板资料需包含 template 和 analysis')
                data['manual'] = manual
            else:
                require(agent.configured(), '尚未配置 AI_API_KEY 和 AI_MODEL，可先导入模板验证')
                require(bool(data.get('image') or data['source']['text'] or data.get('previous')), '请上传题目图片或输入题目文字')
            draft_id = store.create(paper_id, data)
            try:
                executor.submit(worker, draft_id, data)
            except Exception:
                store.update(draft_id, 'failed', '任务未启动，请重新生成')
                raise
            return jsonify(id=draft_id), 202
        except Exception:
            slots.release()
            for filename in created_files:
                (directory() / filename).unlink(missing_ok=True)
            raise

    @bp.route('/admin/ai-questions/drafts/<draft_id>')
    @admin
    def draft(draft_id):
        return jsonify(serialize(get_draft(draft_id)))

    @bp.route('/admin/ai-questions/drafts/<draft_id>/image/<kind>')
    @admin
    def draft_image(draft_id, kind):
        if kind not in ('image', 'diagram'):
            abort(404)
        image = get_draft(draft_id)['data'].get(kind)
        if not image:
            abort(404)
        response = send_from_directory(str(directory()), image['filename'], mimetype=image['mime'])
        response.headers['Cache-Control'] = 'private, no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    @bp.after_request
    def private_response(response):
        response.headers['Cache-Control'] = 'private, no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    @bp.route('/admin/ai-questions/drafts/<draft_id>/import', methods=['POST'])
    @admin
    def import_draft(draft_id):
        body = request.get_json(silent=True) or {}
        require(body.get('teacher_confirmed') is True, '请先确认题意、物理模型和图示')
        conn = deps['get_db_connection']()
        require(conn is not None, '数据库暂不可用')
        cursor = conn.cursor(dictionary=True)
        try:
            # Same exam-row locks as first student entry and exam arrangement changes.
            cursor.execute('SELECT paper_id FROM ai_question_drafts WHERE id=%s', (draft_id,))
            first = cursor.fetchone()
            require(bool(first), '草稿不存在')
            paper_id = first['paper_id']
            cursor.execute('SELECT id,status FROM exams WHERE paper_id=%s ORDER BY id FOR UPDATE', (paper_id,))
            locked_exams = cursor.fetchall()
            cursor.execute('SELECT * FROM ai_question_drafts WHERE id=%s FOR UPDATE', (draft_id,))
            row = store.decode(cursor.fetchone())
            if row['status'] == 'imported':
                conn.commit()
                return jsonify(ok=True, template_id=row['template_id'], already_imported=True)
            require(row['status'] == 'ready', '只有通过验证的草稿可以入库')
            data = row['data']
            artifact = data.get('artifact')
            require(artifact and hmac.compare_digest(str(body.get('artifact_hash', '')), data['artifact_hash']), '草稿版本已变化，请重新预览')
            require(data['artifact_hash'] == fingerprint(artifact['template'], artifact['analysis']), '验证资料已变化，请重新编译')
            cursor.execute('SELECT id FROM exam_papers WHERE id=%s FOR UPDATE', (paper_id,))
            require(bool(cursor.fetchone()), '题库不存在')
            # Use current locking reads, NOT a repeatable-read snapshot taken before
            # waiting for a student's first-entry transaction to release the exam lock.
            for exam in locked_exams:
                require(exam['status'] != 'archived', '题库有关联归档考试，请在新的测试题库中入库')
                for table in ('exam_user_questions', 'user_responses'):
                    cursor.execute('SELECT exam_id FROM ' + table + ' WHERE exam_id=%s LIMIT 1 FOR UPDATE', (exam['id'],))
                    require(not cursor.fetchone(), '题库已有学生进入考试，请在新的测试题库中入库')
            template = json.loads(json.dumps(artifact['template']))
            diagram = data.get('diagram')
            if diagram:
                require(body.get('diagram_confirmed') is True, '请确认示意图不含冲突的固定数值')
                import shutil
                filename = 'ai_' + diagram['filename']
                shutil.copyfile(directory() / diagram['filename'], Path(deps['get_upload_folder_path']()) / filename)
                template['image_filename'] = filename
                template['text'] = deps['build_problem_image_html'](filename, template['name']) + template['text']
            template['generation_strategy']['compiled_signature'] = signature(template)
            strategy_json = json.dumps(template['generation_strategy'], ensure_ascii=False, allow_nan=False)
            require(len(strategy_json.encode()) < 64000, '模板超过存储上限')
            cursor.execute('''INSERT INTO problem_templates
                (template_name,problem_text,variables,solution_formula,answer_count,answer_units,
                 difficulty,image_filename,paper_id,knowledge_point,generation_strategy)
                 VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                (template['name'], template['text'], template['variables'], template['formula'], template['answer_count'],
                 template['answer_units'], data['source'].get('difficulty') or 'medium', template['image_filename'] or None,
                 paper_id, (artifact['analysis'].get('knowledge_point') or '')[:50], strategy_json))
            template_id = cursor.lastrowid
            cursor.execute("UPDATE ai_question_drafts SET status='imported',message=%s,template_id=%s WHERE id=%s",
                           ('已加入题库', template_id, draft_id))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()
            conn.close()
        # Import is durable even if Redis/prewarm fails; duplicate clicks return the same ID.
        try:
            deps['invalidate_exam_paper_cache'](paper_id)
            deps['schedule_exam_paper_prewarm'](paper_id)
        except Exception:
            logger.warning('AI import cache refresh pending: template=%s', template_id)
        return jsonify(ok=True, template_id=template_id)

    return bp
