import csv
import base64
import io
import json
import math
import os
import re
import threading
import time
import uuid
from decimal import Decimal
from datetime import datetime

import mysql.connector
from flask import render_template, request, redirect, url_for, session, flash, jsonify, Response
from openpyxl import load_workbook
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash

from config import (
    DEFAULT_AVATAR,
    DEFAULT_EXAM_PAPER_NAME,
    POOL_LOW_WATER,
    POOL_REFILL_BATCH,
    POOL_TARGET,
)
from db import get_db_connection
from extensions import RedisError, app, logger
from services.redis_cache_service import (
    cache_problem_with_token,
    delete_cache_patterns,
    delete_problem_pool,
    delete_problem_tokens_by_template,
    get_pool_size,
    get_problem_by_token,
    pop_problem_from_pool,
    push_problem_to_pool,
)
from services import question_generation_service
from routes.auth import auth_bp, get_avatar_choices, login_required
from routes.admin import create_admin_blueprint
from routes.exam import create_exam_blueprint
from routes.question import create_question_blueprint
from routes.student import create_student_blueprint
from utils import (
    allowed_excel_file,
    allowed_file,
    build_initial_password,
    format_datetime_local,
    is_mobile_device,
    is_touch_device,
    parse_exam_time,
)

app.register_blueprint(auth_bp)
problem_pool_refill_lock = threading.Lock()
problem_pool_refilling = set()
exam_paper_prewarm_lock = threading.Lock()
exam_paper_prewarming = set()
exam_metadata_cache_lock = threading.Lock()
exam_metadata_cache = {}
EXAM_METADATA_CACHE_TTL_SECONDS = 30


def get_exam_metadata_cache(key):
    now = time.time()
    with exam_metadata_cache_lock:
        item = exam_metadata_cache.get(key)
        if not item:
            return None
        expires_at, value = item
        if expires_at <= now:
            exam_metadata_cache.pop(key, None)
            return None
        return value


def set_exam_metadata_cache(key, value, ttl=EXAM_METADATA_CACHE_TTL_SECONDS):
    with exam_metadata_cache_lock:
        exam_metadata_cache[key] = (time.time() + ttl, value)


def clear_exam_metadata_cache():
    with exam_metadata_cache_lock:
        exam_metadata_cache.clear()


def get_or_create_default_exam_paper(cursor):
    """获取或创建默认题库。"""
    cursor.execute("SELECT id FROM exam_papers WHERE name = %s LIMIT 1", (DEFAULT_EXAM_PAPER_NAME,))
    row = cursor.fetchone()
    if row:
        return row['id'] if isinstance(row, dict) else row[0]

    cursor.execute(
        """
        INSERT INTO exam_papers (name, description, is_enabled)
        VALUES (%s, %s, TRUE)
        """,
        (DEFAULT_EXAM_PAPER_NAME, '系统默认题库')
    )
    return cursor.lastrowid


def get_exam_papers(include_disabled=True):
    cache_key = ('exam_papers', bool(include_disabled))
    cached = get_exam_metadata_cache(cache_key)
    if cached is not None:
        return [dict(paper) for paper in cached]

    conn = get_db_connection()
    if not conn:
        return []
    cursor = conn.cursor(dictionary=True)
    try:
        query = "SELECT id, name, description, is_enabled, created_at FROM exam_papers"
        params = []
        if not include_disabled:
            query += " WHERE is_enabled = TRUE"
        query += " ORDER BY created_at DESC, id DESC"
        cursor.execute(query, params)
        papers = cursor.fetchall()
        set_exam_metadata_cache(cache_key, [dict(paper) for paper in papers])
        return papers
    finally:
        cursor.close()
        conn.close()


def get_enabled_exam_papers():
    return get_exam_papers(include_disabled=False)


def get_exam_paper_by_id(paper_id):
    if not paper_id:
        return None
    cache_key = ('exam_paper', int(paper_id))
    cached = get_exam_metadata_cache(cache_key)
    if cached is not None:
        return dict(cached) if cached else None

    conn = get_db_connection()
    if not conn:
        return None
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT id, name, description, is_enabled, created_at FROM exam_papers WHERE id = %s", (paper_id,))
        paper = cursor.fetchone()
        set_exam_metadata_cache(cache_key, dict(paper) if paper else None)
        return paper
    finally:
        cursor.close()
        conn.close()


def get_user_exam_access(user_id, exam_id=None):
    """Only exam batches control access now."""
    now = datetime.now()
    if session.get('username') == 'admin':
        return {
            'allowed': True,
            'server_time': now,
            'exam_start_time': None,
            'exam_end_time': None,
            'teacher_name': None,
            'exam_id': None,
            'paper_id': None,
            'exam_name': None,
            'status': 'open',
            'message': None,
        }

    available_exams = get_user_available_exams(user_id)
    if not available_exams:
        return {
            'allowed': False,
            'server_time': now,
            'exam_start_time': None,
            'exam_end_time': None,
            'teacher_name': None,
            'exam_id': None,
            'paper_id': None,
            'exam_name': None,
            'status': 'error',
            'message': '当前没有可参加的考试，请联系管理员。',
        }

    selected_exam_id = None
    if exam_id is not None:
        try:
            selected_exam_id = int(exam_id)
        except (TypeError, ValueError):
            selected_exam_id = None
    if selected_exam_id is None:
        selected_exam_id = session.get('selected_exam_id')
        try:
            selected_exam_id = int(selected_exam_id) if selected_exam_id else None
        except (TypeError, ValueError):
            selected_exam_id = None

    available_map = {int(exam['id']): exam for exam in available_exams}
    if selected_exam_id not in available_map:
        selected_exam_id = next((int(exam['id']) for exam in available_exams if exam.get('access_allowed')), int(available_exams[0]['id']))

    selected_exam = available_map.get(selected_exam_id)
    if not selected_exam:
        return {
            'allowed': False,
            'server_time': now,
            'exam_start_time': None,
            'exam_end_time': None,
            'teacher_name': None,
            'exam_id': None,
            'paper_id': None,
            'exam_name': None,
            'status': 'error',
            'message': '未找到可用考试，请返回重新选择。',
        }

    session['selected_exam_id'] = selected_exam_id
    session['selected_exam_paper_id'] = selected_exam['paper_id']

    if not selected_exam.get('access_allowed'):
        return {
            'allowed': False,
            'server_time': now,
            'exam_start_time': selected_exam.get('start_time'),
            'exam_end_time': selected_exam.get('end_time'),
            'teacher_name': None,
            'exam_id': selected_exam_id,
            'paper_id': selected_exam.get('paper_id'),
            'exam_name': selected_exam.get('name'),
            'status': selected_exam.get('access_status', 'error'),
            'message': '当前考试暂不可进入，请返回考试列表重新选择。',
        }

    return {
        'allowed': True,
        'server_time': now,
        'exam_start_time': selected_exam.get('start_time'),
        'exam_end_time': selected_exam.get('end_time'),
        'teacher_name': None,
        'exam_id': selected_exam_id,
        'paper_id': selected_exam.get('paper_id'),
        'exam_name': selected_exam.get('name'),
        'status': 'open',
        'message': None,
    }


def get_teacher_exam_groups():
    conn = get_db_connection()
    if not conn:
        return []
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            """
            SELECT
                COALESCE(NULLIF(teacher_name, ''), '未分配') AS teacher_name,
                COUNT(*) AS student_count,
                MIN(exam_start_time) AS exam_start_time,
                MIN(exam_end_time) AS exam_end_time,
                COUNT(DISTINCT COALESCE(CAST(exam_start_time AS CHAR), '')) AS start_schedule_count,
                COUNT(DISTINCT COALESCE(CAST(exam_end_time AS CHAR), '')) AS end_schedule_count
            FROM users
            WHERE username != 'admin'
            GROUP BY COALESCE(NULLIF(teacher_name, ''), '未分配')
            ORDER BY
                CASE WHEN MIN(exam_start_time) IS NULL THEN 1 ELSE 0 END,
                MIN(exam_start_time) ASC,
                teacher_name ASC
            """
        )
        return cursor.fetchall()
    finally:
        cursor.close()
        conn.close()


def resolve_selected_exam_paper_id(preferred_paper_id=None, include_disabled_for_admin=False):
    """解析当前用户选中的题库。"""
    enabled_papers = get_enabled_exam_papers()
    enabled_ids = {paper['id'] for paper in enabled_papers}
    paper_id = preferred_paper_id or session.get('selected_exam_paper_id')

    if paper_id:
        try:
            paper_id = int(paper_id)
        except (TypeError, ValueError):
            paper_id = None

    if paper_id and paper_id in enabled_ids:
        session['selected_exam_paper_id'] = paper_id
        return paper_id

    if paper_id and include_disabled_for_admin and session.get('username') == 'admin':
        paper = get_exam_paper_by_id(paper_id)
        if paper:
            session['selected_exam_paper_id'] = paper['id']
            return paper['id']

    if enabled_papers:
        session['selected_exam_paper_id'] = enabled_papers[0]['id']
        return enabled_papers[0]['id']

    session.pop('selected_exam_paper_id', None)
    return None


def get_selected_exam_paper(include_disabled_for_admin=False):
    paper_id = resolve_selected_exam_paper_id(include_disabled_for_admin=include_disabled_for_admin)
    if not paper_id:
        return None
    return get_exam_paper_by_id(paper_id)


def get_exam_by_id(exam_id):
    if not exam_id:
        return None
    try:
        exam_id = int(exam_id)
    except (TypeError, ValueError):
        return None

    cache_key = ('exam', exam_id)
    cached = get_exam_metadata_cache(cache_key)
    if cached is not None:
        return cached

    conn = get_db_connection()
    if not conn:
        return None
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            """
            SELECT e.*, ep.name AS paper_name, ep.is_enabled AS paper_enabled
            FROM exams e
            JOIN exam_papers ep ON ep.id = e.paper_id
            WHERE e.id = %s
            """,
            (exam_id,)
        )
        exam = cursor.fetchone()
        result = dict(exam) if exam else None
        set_exam_metadata_cache(cache_key, result)
        return result
    except mysql.connector.Error as err:
        if getattr(err, 'errno', None) == 1146:
            return None
        raise
    finally:
        cursor.close()
        conn.close()


def get_all_exams(limit=100):
    conn = get_db_connection()
    if not conn:
        return []
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            """
            SELECT e.*, ep.name AS paper_name,
                   (
                       SELECT COUNT(*)
                       FROM exam_assignments a
                       WHERE a.exam_id = e.id
                   ) AS assignment_count
            FROM exams e
            JOIN exam_papers ep ON ep.id = e.paper_id
            ORDER BY e.created_at DESC, e.id DESC
            LIMIT %s
            """,
            (limit,)
        )
        return cursor.fetchall()
    except mysql.connector.Error as err:
        if getattr(err, 'errno', None) == 1146:
            return []
        raise
    finally:
        cursor.close()
        conn.close()


def get_user_available_exams(user_id, include_unavailable=True):
    """Return exams assigned to this user by student, class, major, or course number."""
    conn = get_db_connection()
    if not conn:
        return []
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT id, class_name, major, teacher_name FROM users WHERE id = %s",
            (user_id,)
        )
        user = cursor.fetchone()
        if not user:
            return []

        now = datetime.now()
        cursor.execute(
            """
            SELECT DISTINCT e.*, ep.name AS paper_name, ep.description AS paper_description
            FROM exams e
            JOIN exam_papers ep ON ep.id = e.paper_id
            JOIN exam_assignments a ON a.exam_id = e.id
            WHERE e.status = 'published'
              AND ep.is_enabled = TRUE
              AND (
                    (a.assign_type = 'student' AND a.assign_value = %s)
                 OR (a.assign_type = 'class' AND a.assign_value = %s)
                 OR (a.assign_type = 'major' AND a.assign_value = %s)
                 OR (a.assign_type = 'course' AND a.assign_value = %s)
              )
            ORDER BY e.start_time DESC, e.id DESC
            """,
            (
                str(user_id),
                (user.get('class_name') or '').strip(),
                (user.get('major') or '').strip(),
                (user.get('teacher_name') or '').strip(),
            )
        )
        exams = [dict(row) for row in cursor.fetchall()]
        for exam in exams:
            start_time = exam.get('start_time')
            end_time = exam.get('end_time')
            if start_time and now < start_time:
                exam['access_status'] = 'not_started'
                exam['access_allowed'] = False
            elif end_time and now >= end_time:
                exam['access_status'] = 'ended'
                exam['access_allowed'] = False
            else:
                exam['access_status'] = 'open'
                exam['access_allowed'] = True
        if include_unavailable:
            return exams
        return [exam for exam in exams if exam.get('access_allowed')]
    except mysql.connector.Error as err:
        if getattr(err, 'errno', None) == 1146:
            return []
        raise
    finally:
        cursor.close()
        conn.close()


def user_can_access_exam(user_id, exam_id):
    return any(int(exam['id']) == int(exam_id) for exam in get_user_available_exams(user_id))


def resolve_selected_exam_id(preferred_exam_id=None):
    if session.get('username') == 'admin':
        return None

    available_exams = get_user_available_exams(session.get('user_id'))
    if not available_exams:
        session.pop('selected_exam_id', None)
        return None

    exam_id = preferred_exam_id or session.get('selected_exam_id')
    try:
        exam_id = int(exam_id) if exam_id else None
    except (TypeError, ValueError):
        exam_id = None

    available_ids = {int(exam['id']) for exam in available_exams}
    if exam_id in available_ids:
        session['selected_exam_id'] = exam_id
        selected = next(exam for exam in available_exams if int(exam['id']) == exam_id)
        session['selected_exam_paper_id'] = selected['paper_id']
        return exam_id

    open_exam = next((exam for exam in available_exams if exam.get('access_allowed')), available_exams[0])
    session['selected_exam_id'] = open_exam['id']
    session['selected_exam_paper_id'] = open_exam['paper_id']
    return open_exam['id']


def get_problem_templates_by_paper(paper_id=None, enabled_only=True):
    """Return problem templates, optionally filtered by paper and enabled paper status."""
    cache_key = ('problem_templates_by_paper', paper_id, bool(enabled_only))
    cached = get_exam_metadata_cache(cache_key)
    if cached is not None:
        return [dict(template) for template in cached]

    conn = get_db_connection()
    if not conn:
        return []
    cursor = conn.cursor(dictionary=True)
    try:
        query = ["SELECT pt.id, pt.template_name, pt.paper_id FROM problem_templates pt"]
        conditions = []
        params = []

        if paper_id is not None:
            conditions.append("pt.paper_id = %s")
            params.append(paper_id)
        elif enabled_only:
            query.append("JOIN exam_papers ep ON ep.id = pt.paper_id")
            conditions.append("ep.is_enabled = TRUE")

        if conditions:
            query.append("WHERE " + " AND ".join(conditions))
        query.append("ORDER BY pt.id")
        cursor.execute(" ".join(query), params)
        templates = cursor.fetchall()
        set_exam_metadata_cache(cache_key, [dict(template) for template in templates])
        return templates
    finally:
        cursor.close()
        conn.close()


def get_enabled_exam_paper_ids():
    """返回所有已开启题库 ID。"""
    return [paper['id'] for paper in get_enabled_exam_papers()]


def build_enabled_paper_filter(alias, selected_paper_id=None):
    """构建仅统计已开启题库的 SQL 过滤条件。"""
    enabled_paper_ids = get_enabled_exam_paper_ids()

    if selected_paper_id is not None:
        if selected_paper_id in enabled_paper_ids:
            return f" AND {alias}.paper_id = %s", [selected_paper_id]
        return " AND 1 = 0", []

    if not enabled_paper_ids:
        return " AND 1 = 0", []

    placeholders = ', '.join(['%s'] * len(enabled_paper_ids))
    return f" AND {alias}.paper_id IN ({placeholders})", enabled_paper_ids


def get_exam_paper_stats(paper_id):
    conn = get_db_connection()
    if not conn:
        return {'completed_count': 0, 'completed_all': False, 'total_time': 0}
    cursor = conn.cursor(dictionary=True)
    try:
        total_problems = get_total_problem_count(paper_id)
        cursor.execute(
            """
            SELECT
                COUNT(DISTINCT r.template_id) AS completed_count,
                COALESCE(SUM(r.time_taken), 0) AS total_time
            FROM user_responses r
            JOIN problem_templates t ON r.template_id = t.id
            WHERE r.user_id = %s
              AND COALESCE(r.paper_id, t.paper_id) = %s
              AND r.is_correct = TRUE
              AND (r.template_id, r.attempt_count) IN (
                  SELECT r2.template_id, r2.attempt_count
                  FROM user_responses r2
                  JOIN problem_templates t2 ON r2.template_id = t2.id
                  WHERE r2.user_id = %s
                    AND COALESCE(r2.paper_id, t2.paper_id) = %s
                  GROUP BY r2.template_id, r2.attempt_count
                  HAVING SUM(CASE WHEN r2.is_correct THEN 1 ELSE 0 END) = COUNT(*)
              )
            """,
            (session['user_id'], paper_id, session['user_id'], paper_id)
        )
        row = cursor.fetchone() or {}
        completed_count = int(row.get('completed_count') or 0)
        return {
            'completed_count': completed_count,
            'completed_all': total_problems > 0 and completed_count >= total_problems,
            'total_time': float(row.get('total_time') or 0),
        }
    finally:
        cursor.close()
        conn.close()


def save_uploaded_file(file):
    """保存上传的文件"""
    if file and file.filename != '' and allowed_file(file.filename):
        # 生成安全的文件名
        filename = secure_filename(file.filename)
        # 确保文件名唯一
        base, ext = os.path.splitext(filename)
        counter = 1
        upload_folder = get_upload_folder_path()
        os.makedirs(upload_folder, exist_ok=True)
        while os.path.exists(os.path.join(upload_folder, filename)):
            filename = f"{base}_{counter}{ext}"
            counter += 1

        # 保存文件
        file_path = os.path.join(upload_folder, filename)
        file.save(file_path)
        return filename
    return None


def get_upload_folder_path():
    upload_folder = app.config['UPLOAD_FOLDER']
    if os.path.isabs(upload_folder):
        return upload_folder
    return os.path.join(app.root_path, upload_folder)


def build_unique_image_filename(filename):
    filename = secure_filename(filename or '')
    if not filename:
        filename = f"imported_{int(time.time())}.png"
    base, ext = os.path.splitext(filename)
    if not ext:
        ext = '.png'
    candidate = f"{base}{ext}"
    counter = 1
    upload_folder = get_upload_folder_path()
    while os.path.exists(os.path.join(upload_folder, candidate)):
        candidate = f"{base}_{counter}{ext}"
        counter += 1
    return candidate


def save_imported_image(image_payload):
    if not image_payload:
        return None
    original_filename = image_payload.get('filename') or ''
    if not allowed_file(original_filename):
        return None
    content_base64 = image_payload.get('content_base64') or ''
    if not content_base64:
        return None
    image_bytes = base64.b64decode(content_base64)
    filename = build_unique_image_filename(original_filename)
    upload_folder = get_upload_folder_path()
    os.makedirs(upload_folder, exist_ok=True)
    with open(os.path.join(upload_folder, filename), 'wb') as image_file:
        image_file.write(image_bytes)
    return filename


def invalidate_problem_cache(template_id):
    """编辑题目后清理模板缓存、题目池缓存和已生成题目缓存。"""
    try:
        template_id = int(template_id)
    except (TypeError, ValueError):
        return 0

    clear_exam_metadata_cache()
    question_generation_service.clear_template_cache(template_id)

    deleted_count = 0
    try:
        deleted_count += delete_problem_pool(template_id)
        deleted_count += delete_problem_tokens_by_template(template_id)
    except RedisError as err:
        logger.warning("清理题目缓存失败: template_id=%s error=%s", template_id, err)

    return deleted_count


def invalidate_exam_paper_cache(paper_id=None):
    """清理某个题库相关的模板缓存、题目池和已生成题目缓存。"""
    clear_exam_metadata_cache()
    template_ids = []
    if paper_id is not None:
        conn = get_db_connection()
        if conn:
            cursor = conn.cursor(dictionary=True)
            try:
                cursor.execute("SELECT id FROM problem_templates WHERE paper_id = %s", (paper_id,))
                template_ids = [row['id'] for row in cursor.fetchall()]
            finally:
                cursor.close()
                conn.close()

    deleted_count = 0
    if template_ids:
        for template_id in template_ids:
            deleted_count += invalidate_problem_cache(template_id)
    else:
        question_generation_service.clear_template_cache()
        deleted_count += delete_cache_patterns(("exam:pool:*", "exam:problem:*"))

    return deleted_count


def refill_problem_pool(template_id, count):
    """生成题目并补充到池中"""
    created = 0
    for _ in range(count):
        problem_data = question_generation_service.generate_problem_from_template(template_id)
        if problem_data:
            push_problem_to_pool(template_id, problem_data)
            created += 1
    return created


def ensure_problem_pool(template_id):
    """低水位补货（小批量）"""
    current_size = get_pool_size(template_id)
    if current_size < POOL_LOW_WATER:
        refill_problem_pool(template_id, POOL_REFILL_BATCH)


def schedule_problem_pool_refill(template_id):
    """Refill a low problem pool in the background so page loads do not block."""
    try:
        template_id = int(template_id)
    except (TypeError, ValueError):
        return

    try:
        if get_pool_size(template_id) >= POOL_LOW_WATER:
            return
    except Exception as err:
        logger.warning("检查题目池水位失败: template_id=%s error=%s", template_id, err)
        return

    with problem_pool_refill_lock:
        if template_id in problem_pool_refilling:
            return
        problem_pool_refilling.add(template_id)

    def _run():
        try:
            refill_problem_pool(template_id, POOL_REFILL_BATCH)
        except Exception as err:
            logger.warning("后台补充题目池失败: template_id=%s error=%s", template_id, err)
        finally:
            with problem_pool_refill_lock:
                problem_pool_refilling.discard(template_id)

    threading.Thread(target=_run, daemon=True, name=f"problem-pool-refill-{template_id}").start()


def fetch_problem_from_pool(template_id):
    """Get a problem quickly; refill the pool asynchronously when it is low."""
    problem_data = pop_problem_from_pool(template_id)
    if not problem_data:
        result = generate_and_cache_problem(template_id)
        schedule_problem_pool_refill(template_id)
        return result

    token = uuid.uuid4().hex
    cache_problem_with_token(token, problem_data)
    schedule_problem_pool_refill(template_id)
    return token, problem_data


def generate_and_cache_problem(template_id):
    """直接生成题目并写入Redis，作为池为空时的兜底"""
    problem_data = question_generation_service.generate_problem_from_template(template_id)
    if not problem_data:
        return None, None
    token = uuid.uuid4().hex
    cache_problem_with_token(token, problem_data)
    return token, problem_data


def _problem_var_fingerprint(problem_data):
    var_values = (problem_data or {}).get('var_values') or {}
    if not var_values:
        return None
    return json.dumps(var_values, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def fetch_distinct_problem(template_id, previous_problem_data=None, max_attempts=5):
    """Fetch a retry problem and avoid returning the same variable set when possible."""
    previous_fingerprint = _problem_var_fingerprint(previous_problem_data)
    if previous_fingerprint is None:
        return fetch_problem_from_pool(template_id)

    last_token = None
    last_problem_data = None
    for attempt in range(max_attempts):
        if attempt == 0:
            token, problem_data = fetch_problem_from_pool(template_id)
        else:
            token, problem_data = generate_and_cache_problem(template_id)

        if not problem_data:
            continue

        last_token = token
        last_problem_data = problem_data
        if _problem_var_fingerprint(problem_data) != previous_fingerprint:
            return token, problem_data

    logger.warning(
        "未能生成变量不同的新题，使用最后一次结果: template_id=%s previous_vars=%s",
        template_id,
        previous_fingerprint,
    )
    return last_token, last_problem_data


def prewarm_pools(paper_id=None, enabled_only=True):
    print(f"[PREWARM] 开始预热题目池 paper_id={paper_id or 'enabled'}")
    conn = get_db_connection()
    if not conn:
        print("[PREWARM] 数据库连接失败，跳过预热")
        return

    cursor = conn.cursor(dictionary=True)
    try:
        query = [
            """
            SELECT pt.id
            FROM problem_templates pt
            JOIN exam_papers ep ON ep.id = pt.paper_id
            """
        ]
        conditions = []
        params = []
        if paper_id is not None:
            conditions.append("pt.paper_id = %s")
            params.append(paper_id)
        elif enabled_only:
            conditions.append("ep.is_enabled = TRUE")
        if conditions:
            query.append("WHERE " + " AND ".join(conditions))
        query.append("ORDER BY pt.id")
        cursor.execute(" ".join(query), params)
        template_ids = [row['id'] for row in cursor.fetchall()]
    finally:
        cursor.close()
        conn.close()

    for template_id in template_ids:
        pool_size = get_pool_size(template_id)
        if pool_size < POOL_TARGET:
            to_add = POOL_TARGET - pool_size
            print(f"[PREWARM] 模板 {template_id} 补货 {to_add} 道")
            refill_problem_pool(template_id, to_add)
        else:
            print(f"[PREWARM] 模板 {template_id} 池已满足，当前 {pool_size}")
    print(f"[PREWARM] 预热完成 paper_id={paper_id or 'enabled'} templates={len(template_ids)}")


def schedule_exam_paper_prewarm(paper_id):
    try:
        paper_id = int(paper_id)
    except (TypeError, ValueError):
        return False

    with exam_paper_prewarm_lock:
        if paper_id in exam_paper_prewarming:
            return False
        exam_paper_prewarming.add(paper_id)

    def _run():
        try:
            prewarm_pools(paper_id=paper_id, enabled_only=False)
        except Exception as err:
            logger.warning("题库后台预热失败: paper_id=%s error=%s", paper_id, err)
        finally:
            with exam_paper_prewarm_lock:
                exam_paper_prewarming.discard(paper_id)

    threading.Thread(target=_run, daemon=True, name=f"exam-paper-prewarm-{paper_id}").start()
    return True


def is_correct(user_answer, correct_answer):
    """判断答案是否正确（允许1%误差，支持科学计数法范围）"""
    logger.debug("is_correct 输入: user_answer=%r, correct_answer=%r", user_answer, correct_answer)

    # 处理None值
    if user_answer is None or correct_answer is None:
        logger.debug("is_correct 答案为空，返回 False")
        return False

    # 转换为浮点数
    try:
        user_float = float(user_answer)
        correct_float = float(correct_answer)
    except (ValueError, TypeError) as e:
        logger.debug("is_correct 数值转换失败: %s", e)
        return False

    # 处理特殊情况：两个都是0
    if user_float == 0 and correct_float == 0:
        logger.debug("is_correct 两者均为0，返回 True")
        return True

    # 处理特殊情况：其中一个为0，另一个不为0
    if user_float == 0 or correct_float == 0:
        # 如果其中一个为0，则要求完全相等（因为0的1%还是0）
        result = user_float == correct_float
        logger.debug("is_correct 存在0值，直接比较结果: %s", result)
        return result

    # 计算相对误差（百分比）
    relative_error = abs((user_float - correct_float) / correct_float) * 100

    # 动态误差阈值（根据数量级调整）
    base_tolerance = 1.0  # 基础1%误差

    # 对于非常大或非常小的数，稍微放宽误差限制
    magnitude = math.log10(abs(correct_float))
    if magnitude > 10:  # 大于10^10
        adjusted_tolerance = min(base_tolerance * 1.5, 2.0)
    elif magnitude < -10:  # 小于10^-10
        adjusted_tolerance = min(base_tolerance * 1.5, 2.0)
    else:
        adjusted_tolerance = base_tolerance

    # 检查相对误差
    result = relative_error <= adjusted_tolerance

    logger.debug(
        "is_correct 计算结果: user=%s correct=%s relative_error=%.6f%% tolerance=%s result=%s",
        f"{user_float:.2e}",
        f"{correct_float:.2e}",
        relative_error,
        adjusted_tolerance,
        result
    )

    return result


# 新增辅助函数：科学计数法格式化
def format_scientific(value, precision=2):
    """将数值格式化为科学计数法字符串"""
    if value == 0:
        return "0.00 × 10⁰"

    is_negative = value < 0
    abs_value = abs(value)

    # 计算指数
    exponent = math.floor(math.log10(abs_value))
    mantissa = abs_value / (10 ** exponent)

    # 调整到标准形式 (1 ≤ mantissa < 10)
    if mantissa >= 10:
        mantissa /= 10
        exponent += 1
    elif mantissa < 1:
        mantissa *= 10
        exponent -= 1

    # 格式化
    sign = '-' if is_negative else ''
    mantissa_str = f"{mantissa:.{precision}f}"

    # 获取上标数字
    superscript_digits = {
        '0': '⁰', '1': '¹', '2': '²', '3': '³', '4': '⁴',
        '5': '⁵', '6': '⁶', '7': '⁷', '8': '⁸', '9': '⁹'
    }

    exp_str = str(exponent)
    sup_exp = ''
    if exp_str[0] == '-':
        sup_exp += '⁻'
        exp_str = exp_str[1:]
    for digit in exp_str:
        sup_exp += superscript_digits.get(digit, digit)

    return f"{sign}{mantissa_str} × 10{sup_exp}"


# 新增函数：用于生成正确答案的科学计数法提示
def get_scientific_hint(correct_answers):
    """生成科学计数法格式的正确答案提示"""
    if not correct_answers or not isinstance(correct_answers, list):
        return "正确答案: 暂无"

    formatted_answers = []
    for answer in correct_answers:
        try:
            formatted = format_scientific(float(answer))
            formatted_answers.append(formatted)
        except:
            formatted_answers.append(str(answer))

    if len(formatted_answers) == 1:
        return f"正确答案: {formatted_answers[0]}"
    else:
        parts = []
        for i, answer in enumerate(formatted_answers):
            parts.append(f"答案{i + 1} = {answer}")
        return f"正确答案: {', '.join(parts)}"


def get_problem_display_info(paper_id=None, enabled_only=True):
    """获取题目的显示信息（支持按题库隔离显示序号）。"""
    cache_key = ('problem_display_info', paper_id, bool(enabled_only))
    cached = get_exam_metadata_cache(cache_key)
    if cached is not None:
        return {int(actual_id): dict(info) for actual_id, info in cached.items()}

    templates = get_problem_templates_by_paper(paper_id=paper_id, enabled_only=enabled_only)

    display_mapping = {}
    for display_number, template in enumerate(templates, 1):
        display_mapping[template['id']] = {
            'display_number': display_number,
            'template_name': template['template_name'],
            'actual_id': template['id'],
            'paper_id': template.get('paper_id')
        }

    set_exam_metadata_cache(cache_key, {int(actual_id): dict(info) for actual_id, info in display_mapping.items()})
    return display_mapping


def build_display_to_actual_map(paper_id=None):
    """生成显示序号到实际ID的映射，便于前端查找"""
    mapping = get_problem_display_info(paper_id)
    display_to_actual = {}

    for actual_id, info in mapping.items():
        display_number = info['display_number']
        display_to_actual[display_number] = actual_id

    return display_to_actual


def get_display_number(actual_id, paper_id=None):
    """根据实际ID获取显示序号"""
    mapping = get_problem_display_info(paper_id)
    if actual_id in mapping:
        return mapping[actual_id]['display_number']
    return actual_id  # 回退到实际ID


def get_actual_id(display_number, paper_id=None):
    """根据显示序号获取实际ID"""
    mapping = get_problem_display_info(paper_id)
    for actual_id, info in mapping.items():
        if info['display_number'] == display_number:
            return actual_id
    return None  # 找不到对应的实际ID


def has_full_correct_attempt(cursor, user_id, template_id):
    """判断是否存在所有答案均正确的作答记录"""
    cursor.execute("""
        SELECT attempt_count
        FROM user_responses
        WHERE user_id = %s AND template_id = %s
        GROUP BY attempt_count
        HAVING SUM(CASE WHEN is_correct THEN 1 ELSE 0 END) = COUNT(*)
        LIMIT 1
    """, (user_id, template_id))
    return cursor.fetchone() is not None


def get_latest_full_correct_attempt(cursor, user_id, template_id):
    """获取最近一次所有答案均正确的作答统计"""
    cursor.execute("""
        SELECT attempt_count
        FROM user_responses
        WHERE user_id = %s AND template_id = %s
        GROUP BY attempt_count
        HAVING SUM(CASE WHEN is_correct THEN 1 ELSE 0 END) = COUNT(*)
        ORDER BY attempt_count DESC
        LIMIT 1
    """, (user_id, template_id))
    attempt_row = cursor.fetchone()

    if not attempt_row:
        return None

    attempt_count = attempt_row['attempt_count']
    cursor.execute("""
        SELECT time_taken, attempt_count,
               CASE WHEN is_correct THEN 100 ELSE 0 END as score
        FROM user_responses
        WHERE user_id = %s AND template_id = %s AND attempt_count = %s
        LIMIT 1
    """, (user_id, template_id, attempt_count))
    return cursor.fetchone()


def is_problem_completed(user_id, template_id, paper_id=None, exam_id=None):
    """检查指定题目是否已被用户正确完成"""
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        if exam_id is not None:
            cursor.execute("""
                SELECT attempt_count
                FROM user_responses
                WHERE user_id = %s AND template_id = %s AND exam_id = %s
                GROUP BY attempt_count
                HAVING SUM(CASE WHEN is_correct THEN 1 ELSE 0 END) = COUNT(*)
                LIMIT 1
            """, (user_id, template_id, exam_id))
            return cursor.fetchone() is not None
        if paper_id is None:
            return has_full_correct_attempt(cursor, user_id, template_id)
        cursor.execute("""
            SELECT attempt_count
            FROM user_responses
            WHERE user_id = %s AND template_id = %s AND paper_id = %s
            GROUP BY attempt_count
            HAVING SUM(CASE WHEN is_correct THEN 1 ELSE 0 END) = COUNT(*)
            LIMIT 1
        """, (user_id, template_id, paper_id))
        return cursor.fetchone() is not None
    finally:
        cursor.close()
        conn.close()


def get_completion_status_map(user_id, paper_id, template_ids, exam_id=None):
    """批量获取题目完成状态，避免逐题查询导致的 N+1 问题。"""
    if not template_ids:
        return {}

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        placeholders = ', '.join(['%s'] * len(template_ids))
        params = [user_id, *template_ids]
        paper_filter = ""

        if exam_id is not None:
            paper_filter = "AND exam_id = %s"
            params.insert(1, exam_id)
        elif paper_id is not None:
            paper_filter = "AND paper_id = %s"
            params.insert(1, paper_id)

        cursor.execute(f"""
            SELECT t.template_id
            FROM (
                SELECT
                    template_id,
                    attempt_count,
                    SUM(CASE WHEN is_correct THEN 1 ELSE 0 END) = COUNT(*) AS full_correct
                FROM user_responses
                WHERE user_id = %s
                  {paper_filter}
                  AND template_id IN ({placeholders})
                GROUP BY template_id, attempt_count
            ) AS t
            WHERE t.full_correct = 1
            GROUP BY t.template_id
        """, params)

        completed_ids = {int(row['template_id']) for row in cursor.fetchall()}
        return {int(template_id): (int(template_id) in completed_ids) for template_id in template_ids}
    finally:
        cursor.close()
        conn.close()


def get_latest_attempt_count(user_id, template_id, paper_id=None, exam_id=None):
    """获取用户在某题上的最大 attempt_count，避免会话重置导致编号冲突"""
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        query = """
            SELECT COALESCE(MAX(attempt_count), 0) AS latest_attempt_count
            FROM user_responses
            WHERE user_id = %s AND template_id = %s
        """
        params = [user_id, template_id]
        if exam_id is not None:
            query += " AND exam_id = %s"
            params.append(exam_id)
        elif paper_id is not None:
            query += " AND paper_id = %s"
            params.append(paper_id)
        cursor.execute(query, params)
        row = cursor.fetchone() or {}
        return int(row.get('latest_attempt_count') or 0)
    finally:
        cursor.close()
        conn.close()


def get_total_problem_count(paper_id=None):
    """动态获取题目总数（仅统计已开启题库）。"""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        enabled_paper_ids = get_enabled_exam_paper_ids()
        if paper_id is not None:
            if paper_id not in enabled_paper_ids:
                return 0
            cursor.execute("SELECT COUNT(*) FROM problem_templates WHERE paper_id = %s", (paper_id,))
        else:
            if not enabled_paper_ids:
                return 0
            placeholders = ', '.join(['%s'] * len(enabled_paper_ids))
            cursor.execute(
                f"SELECT COUNT(*) FROM problem_templates WHERE paper_id IN ({placeholders})",
                enabled_paper_ids
            )
        row = cursor.fetchone()
        return int(row[0] if row else 0)
    finally:
        cursor.close()
        conn.close()


def classify_error_type(user_answer, correct_answer, is_correct):
    """根据用户答案与正确答案的差异推断错误类型"""
    if is_correct:
        return '正确'

    if user_answer is None:
        return '未作答'

    try:
        user_float = float(user_answer)
        correct_float = float(correct_answer)
    except (TypeError, ValueError):
        return '格式错误'

    if correct_float == 0:
        return '计算错误' if user_float != 0 else '正确'

    import math
    # 1) 符号相反（数值一样但正负相反，如 -10 写成 10）：正负号/读题失误，归计算粗心
    if abs(user_float + correct_float) < 1e-6 and abs(user_float) > 1e-6:
        return '计算误差'
    # 2) 数量级偏差（差 10 的整数次幂，如 0.5 写成 5 或 100 写成 10）：单位/数量级换算错误
    if user_float != 0:
        ratio = abs(user_float / correct_float)
        log10 = math.log10(ratio)
        if abs(log10) >= 1 and abs(log10 - round(log10)) < 0.1:
            return '精度或单位偏差'

    relative_error = abs((user_float - correct_float) / correct_float) * 100

    if relative_error > 50:
        return '概念错误'
    if relative_error > 5:
        return '计算误差'
    return '精度或单位偏差'


# ===== 学情分析：错误粗类映射 / 知识点推断 / 画像统计（供画像闭环复用） =====
# 5 个种子粗类（写入 error_tags_library，is_system=1）
ERROR_CATEGORY_SEED = [
    ('calc_careless', '计算粗心', '计算类失误，如算错、漏算、抄错数', 1),
    ('concept_confused', '概念混淆', '对物理概念、定律理解存在偏差', 2),
    ('unit_error', '单位换算', '单位、量纲或数量级换算错误', 3),
    ('misread', '审题不清', '未作答、格式错误或读题有偏差', 4),
    ('formula_wrong', '公式记错', '记错或套错公式（后续由 AI/自述补充）', 5),
]

# 5 个粗类固定顺序（雷达图坐标轴）
ERROR_CATEGORY_ORDER = ['计算粗心', '概念混淆', '单位换算', '审题不清', '公式记错']


def map_error_to_category(error_type):
    """将 classify_error_type 返回的细类映射到 5 个粗类标签；正确/未知返回 None。"""
    if error_type in ('计算误差', '计算错误'):
        return '计算粗心'
    if error_type == '概念错误':
        return '概念混淆'
    if error_type == '精度或单位偏差':
        return '单位换算'
    if error_type in ('格式错误', '未作答'):
        return '审题不清'
    return None


def infer_knowledge_label(template_name):
    """根据题目名称推断知识点标签（集中定义，供 repair_database 回填与路由复用）。"""
    name = (template_name or '').strip()
    rules = [
        ('电磁学', ['电磁', '磁场', '磁铁', '感应', '线圈', '电流', '电压', '电阻', '电荷', '安培', '法拉第']),
        ('力学', ['力学', '受力', '牛顿', '加速度', '速度', '位移', '动量', '机械', '弹簧', '摩擦', '圆周', '功', '能量']),
        ('热学', ['热', '温度', '内能', '热量', '压强', '气体']),
        ('光学', ['光', '透镜', '折射', '反射', '干涉', '衍射']),
        ('振动与波', ['波', '振动', '频率', '波长', '声', '共振']),
    ]
    for label, keywords in rules:
        if any(keyword in name for keyword in keywords):
            return label
    return '综合分析'


def build_radar_svg(categories, values, max_value=None, size=260):
    """生成 5 轴雷达图的 SVG 字符串（零前端依赖，避免 CDN 不可用风险）。"""
    import math
    n = len(categories)
    if n < 3:
        return ''
    cx = cy = size // 2
    R = size // 2 - 36
    if max_value is None or max_value <= 0:
        max_value = max(values) if values else 1
        max_value = max(max_value, 1)
    step = 2 * math.pi / n
    start = -math.pi / 2  # 顶部开始

    def point(angle, radius):
        return (cx + radius * math.cos(angle), cy + radius * math.sin(angle))

    parts = [f'<svg viewBox="0 0 {size} {size}" width="{size}" height="{size}" '
             f'xmlns="http://www.w3.org/2000/svg" role="img" aria-label="错因雷达图">']
    for ring in (0.33, 0.66, 1.0):
        pts = ' '.join(f'{x:.1f},{y:.1f}' for x, y in
                        (point(start + i * step, R * ring) for i in range(n)))
        parts.append(f'<polygon points="{pts}" fill="none" stroke="#dee2e6" stroke-width="1"/>')
    for i, cat in enumerate(categories):
        ang = start + i * step
        x, y = point(ang, R)
        parts.append(f'<line x1="{cx}" y1="{cy}" x2="{x:.1f}" y2="{y:.1f}" stroke="#dee2e6" stroke-width="1"/>')
        lx, ly = point(ang, R + 20)
        anchor = 'middle'
        if lx < cx - 5:
            anchor = 'end'
        elif lx > cx + 5:
            anchor = 'start'
        parts.append(f'<text x="{lx:.1f}" y="{ly:.1f}" font-size="11" text-anchor="{anchor}" fill="#495057">{cat}</text>')
    data_pts = []
    for i, val in enumerate(values):
        ang = start + i * step
        r = R * min(val / max_value, 1.0)
        data_pts.append(point(ang, r))
    data_str = ' '.join(f'{x:.1f},{y:.1f}' for x, y in data_pts)
    parts.append(f'<polygon points="{data_str}" fill="rgba(13,110,253,0.25)" stroke="#0d6efd" stroke-width="2"/>')
    for (x, y), val in zip(data_pts, values):
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="#0d6efd"/>')
        parts.append(f'<text x="{x:.1f}" y="{y - 6:.1f}" font-size="10" text-anchor="middle" fill="#0d6efd">{val}</text>')
    parts.append('</svg>')
    return ''.join(parts)


def get_student_profile(user_id, paper_id=None):
    """返回学生学情画像数据（纯本地规则，基于 error_category 与 knowledge_point）。"""
    conn = get_db_connection()
    if not conn:
        return None
    cursor = conn.cursor(dictionary=True)
    try:
        pf = ""
        params = [user_id]
        if paper_id:
            pf = " AND ur.paper_id = %s"
            params.append(paper_id)

        cursor.execute(f"""
            SELECT
                COUNT(*) AS total_answers,
                SUM(CASE WHEN ur.is_correct THEN 1 ELSE 0 END) AS correct_answers,
                SUM(CASE WHEN ur.is_correct = FALSE THEN 1 ELSE 0 END) AS wrong_answers
            FROM user_responses ur
            WHERE ur.user_id = %s {pf}
        """, params)
        overall = cursor.fetchone() or {}
        total = overall.get('total_answers') or 0
        correct = overall.get('correct_answers') or 0
        wrong = overall.get('wrong_answers') or 0
        accuracy = round(correct / total * 100, 1) if total else None

        cursor.execute(f"""
            SELECT COALESCE(error_category, '未分类') AS category, COUNT(*) AS cnt
            FROM user_responses ur
            WHERE ur.user_id = %s AND ur.is_correct = FALSE {pf}
            GROUP BY error_category
            ORDER BY cnt DESC
        """, params)
        category_dist = {r['category']: r['cnt'] for r in cursor.fetchall()}

        cursor.execute(f"""
            SELECT pt.knowledge_point AS knowledge_point,
                   COUNT(*) AS total,
                   SUM(CASE WHEN ur.is_correct THEN 1 ELSE 0 END) AS correct,
                   SUM(CASE WHEN ur.is_correct = FALSE THEN 1 ELSE 0 END) AS wrong
            FROM user_responses ur
            JOIN problem_templates pt ON pt.id = ur.template_id
            WHERE ur.user_id = %s {pf}
            GROUP BY pt.knowledge_point
        """, params)
        knowledge_stats = []
        for row in cursor.fetchall():
            t = row.get('total') or 0
            c = row.get('correct') or 0
            cr = round(c / t * 100, 1) if t else 0
            main_err = None
            if row.get('wrong'):
                me_params = [user_id, row['knowledge_point']]
                if paper_id:
                    me_params.append(paper_id)
                cursor.execute(f"""
                    SELECT COALESCE(error_category, '未分类') AS ec, COUNT(*) AS cnt
                    FROM user_responses ur2
                    JOIN problem_templates pt2 ON pt2.id = ur2.template_id
                    WHERE ur2.user_id = %s AND ur2.is_correct = FALSE
                      AND pt2.knowledge_point = %s {pf}
                    GROUP BY error_category ORDER BY cnt DESC LIMIT 1
                """, me_params)
                me = cursor.fetchone()
                main_err = me['ec'] if me else None
            knowledge_stats.append({
                'knowledge_point': row['knowledge_point'] or '未分类',
                'total': t, 'correct': c, 'wrong': row.get('wrong') or 0,
                'correct_rate': cr, 'main_error': main_err,
            })
        knowledge_stats.sort(key=lambda x: (x['correct_rate'], -x['wrong']))

        cursor.execute(f"""
            SELECT ur.is_correct, ur.error_type, ur.error_category,
                   ur.response_time, pt.template_name, pt.knowledge_point
            FROM user_responses ur
            JOIN problem_templates pt ON pt.id = ur.template_id
            WHERE ur.user_id = %s {pf}
            ORDER BY ur.response_time DESC
            LIMIT 15
        """, params)
        recent = cursor.fetchall()

        return {
            'total': total, 'correct': correct, 'wrong': wrong, 'accuracy': accuracy,
            'category_dist': category_dist, 'knowledge_stats': knowledge_stats, 'recent': recent,
        }
    finally:
        cursor.close()
        conn.close()


def get_class_analytics(paper_id=None):
    """返回班级级学情看板数据（纯本地规则）。"""
    conn = get_db_connection()
    if not conn:
        return None
    cursor = conn.cursor(dictionary=True)
    try:
        pf = ""
        p = []
        if paper_id:
            pf = " AND ur.paper_id = %s"
            p.append(paper_id)

        cursor.execute(f"""
            SELECT COALESCE(error_category, '未分类') AS category, COUNT(*) AS cnt
            FROM user_responses ur
            WHERE ur.is_correct = FALSE {pf}
            GROUP BY error_category ORDER BY cnt DESC
        """, p)
        category_totals = cursor.fetchall()

        cursor.execute(f"""
            SELECT pt.knowledge_point AS knowledge_point,
                   COUNT(*) AS total,
                   SUM(CASE WHEN ur.is_correct THEN 1 ELSE 0 END) AS correct,
                   SUM(CASE WHEN ur.is_correct = FALSE THEN 1 ELSE 0 END) AS wrong
            FROM user_responses ur
            JOIN problem_templates pt ON pt.id = ur.template_id
            WHERE 1=1 {pf}
            GROUP BY pt.knowledge_point
        """, p)
        knowledge_stats = []
        for r in cursor.fetchall():
            t = r['total'] or 0
            c = r['correct'] or 0
            cr = round(c / t * 100, 1) if t else 0
            knowledge_stats.append({
                'knowledge_point': r['knowledge_point'] or '未分类',
                'total': t, 'correct': c, 'wrong': r['wrong'] or 0, 'correct_rate': cr,
            })
        knowledge_stats.sort(key=lambda x: (x['correct_rate'], -x['wrong']))

        cursor.execute(f"""
            SELECT u.id AS user_id, u.username, u.name, u.class_name,
                   COUNT(*) AS total,
                   SUM(CASE WHEN ur.is_correct THEN 1 ELSE 0 END) AS correct
            FROM user_responses ur
            JOIN users u ON u.id = ur.user_id
            WHERE 1=1 {pf}
            GROUP BY u.id
        """, p)
        students = cursor.fetchall()
        for s in students:
            t = s['total'] or 0
            c = s['correct'] or 0
            s['accuracy'] = round(c / t * 100, 1) if t else 0
        students.sort(key=lambda x: -x['accuracy'])

        return {'category_totals': category_totals, 'knowledge_stats': knowledge_stats, 'students': students}
    finally:
        cursor.close()
        conn.close()


def save_user_response(user_id, template_id, paper_id, problem_text, user_answers, correct_answers, is_correct_list,
                       attempt_count, time_taken, error_types=None, switch_count=0, exam_id=None):
    """保存用户答题记录（支持多答案）"""
    print(f"\n=== 保存答题记录开始 ===")
    print(f"用户ID: {user_id}")
    print(f"模板ID: {template_id}")
    print(f"用户答案: {user_answers}")
    print(f"正确答案: {correct_answers}")
    print(f"是否正确: {is_correct_list}")
    print(f"尝试次数: {attempt_count}")
    print(f"用时: {time_taken}秒")
    print(f"答案数量: {len(user_answers)}")

    conn = None
    try:
        # 1. 获取数据库连接
        conn = get_db_connection()
        if not conn:
            print("❌ 数据库连接失败")
            return (False, [])

        cursor = conn.cursor()
        print("✅ 数据库连接成功")

        # 2. 验证用户和模板是否存在
        cursor.execute("SELECT id FROM users WHERE id = %s", (user_id,))
        user_exists = cursor.fetchone()
        if not user_exists:
            print(f"❌ 用户ID {user_id} 不存在")
            return (False, [])
        print("✅ 用户存在")

        cursor.execute("SELECT id FROM problem_templates WHERE id = %s", (template_id,))
        template_exists = cursor.fetchone()
        if not template_exists:
            print(f"❌ 模板ID {template_id} 不存在")
            return (False, [])
        print("✅ 模板存在")

        # 3. 截断过长的problem_text
        truncated_problem_text = problem_text[:1000] + "..." if len(problem_text) > 1000 else problem_text
        print(f"✅ 问题文本已截断: {len(truncated_problem_text)} 字符")

        # 4. 保存每个答案的记录
        all_success = True
        saved_count = 0
        saved_ids = []

        if error_types is None:
            error_types = [
                classify_error_type(user_answer, correct_answer, is_correct)
                for user_answer, correct_answer, is_correct in zip(user_answers, correct_answers, is_correct_list)
            ]
        # 新增：根据细类错误映射 5 个粗类标签，用于学情画像
        error_categories = [map_error_to_category(et) for et in error_types]

        for i, (user_answer, correct_answer, is_correct, error_type, error_category) in enumerate(
                zip(user_answers, correct_answers, is_correct_list, error_types, error_categories)):
            try:
                print(
                    f"正在保存答案 {i + 1}: user_answer={user_answer}, correct_answer={correct_answer}, is_correct={is_correct}, error_type={error_type}, error_category={error_category}")

                cursor.execute("""
                    INSERT INTO user_responses
                    (user_id, template_id, problem_text, user_answer,
                     correct_answer, is_correct, error_type, error_category, attempt_count, time_taken, answer_index, paper_id, switch_count, exam_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (user_id, template_id, truncated_problem_text, user_answer,
                      correct_answer, is_correct, error_type, error_category, attempt_count, time_taken, i, paper_id, switch_count, exam_id))

                saved_count += 1
                saved_ids.append(cursor.lastrowid)
                print(f"✅ 答案 {i + 1} 保存成功 (id={cursor.lastrowid})")

            except mysql.connector.Error as err:
                print(f"❌ 答案 {i + 1} 保存失败: {err}")
                all_success = False
                break
            except Exception as e:
                print(f"❌ 答案 {i + 1} 保存异常: {str(e)}")
                all_success = False
                break

        # 5. 提交事务
        if all_success:
            conn.commit()
            print(f"✅ 事务提交成功，共保存 {saved_count} 个答案记录")
        else:
            conn.rollback()
            print("❌ 部分答案保存失败，事务已回滚")

        return (all_success, saved_ids)

    except mysql.connector.Error as err:
        print(f"❌ 数据库错误: {err}")
        print(f"错误代码: {err.errno}")
        print(f"SQL状态: {err.sqlstate}")
        if conn:
            conn.rollback()
        return False

    except Exception as e:
        print(f"❌ 保存过程中发生异常: {str(e)}")
        import traceback
        print(f"详细错误信息:\n{traceback.format_exc()}")
        if conn:
            conn.rollback()
        return False

    finally:
        if conn and conn.is_connected():
            conn.close()
            print("✅ 数据库连接已关闭")
        print("=== 保存答题记录结束 ===\n")


def repair_database():
    """修复数据库表结构"""
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        # 检查并添加缺失的列
        cursor.execute("SHOW COLUMNS FROM user_responses LIKE 'user_id'")
        if not cursor.fetchone():
            cursor.execute("ALTER TABLE user_responses ADD COLUMN user_id INT NOT NULL AFTER id")
            print("已添加 user_id 列")

        cursor.execute("SHOW COLUMNS FROM user_responses LIKE 'template_id'")
        if not cursor.fetchone():
            cursor.execute("ALTER TABLE user_responses ADD COLUMN template_id INT NOT NULL AFTER user_id")
            print("已添加 template_id 列")

        cursor.execute("SHOW TABLES LIKE 'exam_papers'")
        if not cursor.fetchone():
            cursor.execute("""
                CREATE TABLE exam_papers (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    name VARCHAR(100) NOT NULL UNIQUE,
                    description TEXT DEFAULT NULL,
                    is_enabled BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            print("已创建 exam_papers 表")

        cursor = conn.cursor(dictionary=True)
        default_paper_id = get_or_create_default_exam_paper(cursor)
        cursor.close()
        cursor = conn.cursor()

        cursor.execute("SHOW COLUMNS FROM problem_templates LIKE 'paper_id'")
        if not cursor.fetchone():
            cursor.execute("ALTER TABLE problem_templates ADD COLUMN paper_id INT DEFAULT NULL")
            print("已添加 problem_templates.paper_id 列")
        cursor.execute("UPDATE problem_templates SET paper_id = %s WHERE paper_id IS NULL", (default_paper_id,))

        cursor.execute("SHOW COLUMNS FROM user_responses LIKE 'paper_id'")
        if not cursor.fetchone():
            cursor.execute("ALTER TABLE user_responses ADD COLUMN paper_id INT DEFAULT NULL AFTER answer_index")
            print("已添加 user_responses.paper_id 列")
        cursor.execute("""
            UPDATE user_responses ur
            JOIN problem_templates pt ON pt.id = ur.template_id
            SET ur.paper_id = pt.paper_id
            WHERE ur.paper_id IS NULL
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS exams (
                id INT AUTO_INCREMENT PRIMARY KEY,
                name VARCHAR(150) NOT NULL,
                paper_id INT NOT NULL,
                exam_type VARCHAR(20) DEFAULT 'normal',
                start_time DATETIME NULL,
                end_time DATETIME NULL,
                status VARCHAR(20) DEFAULT 'published',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (paper_id) REFERENCES exam_papers(id)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS exam_assignments (
                id INT AUTO_INCREMENT PRIMARY KEY,
                exam_id INT NOT NULL,
                assign_type VARCHAR(20) NOT NULL,
                assign_value VARCHAR(100) NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (exam_id) REFERENCES exams(id) ON DELETE CASCADE,
                UNIQUE KEY uq_exam_assignment (exam_id, assign_type, assign_value)
            )
        """)

        cursor.execute("SHOW COLUMNS FROM user_responses LIKE 'exam_id'")
        if not cursor.fetchone():
            cursor.execute("ALTER TABLE user_responses ADD COLUMN exam_id INT DEFAULT NULL AFTER paper_id")
            print("Added user_responses.exam_id column")

        cursor.execute("SHOW COLUMNS FROM user_responses LIKE 'error_type'")
        if not cursor.fetchone():
            cursor.execute("ALTER TABLE user_responses ADD COLUMN error_type VARCHAR(50) DEFAULT '未知' AFTER is_correct")
            cursor.execute("UPDATE user_responses SET error_type = '正确' WHERE is_correct = TRUE")
            cursor.execute("UPDATE user_responses SET error_type = '计算误差' WHERE is_correct = FALSE")
            print("已添加 error_type 列")

        # 添加外键约束（如果不存在）
        cursor.execute("SHOW COLUMNS FROM user_responses LIKE 'switch_count'")
        if not cursor.fetchone():
            cursor.execute("ALTER TABLE user_responses ADD COLUMN switch_count INT NOT NULL DEFAULT 0 AFTER time_taken")
            print("Added user_responses.switch_count column")

        cursor.execute("""
            SELECT COUNT(*) FROM information_schema.table_constraints
            WHERE table_name = 'user_responses' 
            AND constraint_name = 'user_responses_ibfk_1'
        """)
        if cursor.fetchone()[0] == 0:
            cursor.execute("""
                ALTER TABLE user_responses
                ADD FOREIGN KEY (user_id) REFERENCES users(id)
            """)
            print("已添加 user_id 外键约束")

        cursor.execute("""
            SELECT COUNT(*) FROM information_schema.table_constraints
            WHERE table_name = 'user_responses' 
            AND constraint_name = 'user_responses_ibfk_2'
        """)
        if cursor.fetchone()[0] == 0:
            cursor.execute("""
                ALTER TABLE user_responses
                ADD FOREIGN KEY (template_id) REFERENCES problem_templates(id)
            """)
            print("已添加 template_id 外键约束")

        # ===== 学情分析闭环：新增列 / 表 / 种子标签（幂等） =====
        # user_responses 增加 error_category 粗类列，并回填历史数据
        cursor.execute("SHOW COLUMNS FROM user_responses LIKE 'error_category'")
        if not cursor.fetchone():
            cursor.execute("ALTER TABLE user_responses ADD COLUMN error_category VARCHAR(30) DEFAULT NULL AFTER error_type")
            print("已添加 user_responses.error_category 列")
            cursor.execute("""
                UPDATE user_responses
                SET error_category = CASE error_type
                    WHEN '计算误差' THEN '计算粗心'
                    WHEN '计算错误' THEN '计算粗心'
                    WHEN '概念错误' THEN '概念混淆'
                    WHEN '精度或单位偏差' THEN '单位换算'
                    WHEN '格式错误' THEN '审题不清'
                    WHEN '未作答' THEN '审题不清'
                    ELSE NULL END
            """)
            print("已回填 error_category 历史数据")

        # problem_templates 增加 knowledge_point 显式知识点列，并回填
        cursor.execute("SHOW COLUMNS FROM problem_templates LIKE 'knowledge_point'")
        if not cursor.fetchone():
            cursor.execute("ALTER TABLE problem_templates ADD COLUMN knowledge_point VARCHAR(50) DEFAULT NULL")
            print("已添加 problem_templates.knowledge_point 列")
            cursor.execute("SELECT id, template_name FROM problem_templates WHERE knowledge_point IS NULL OR knowledge_point = ''")
            for tid, tname in cursor.fetchall():
                cursor.execute("UPDATE problem_templates SET knowledge_point = %s WHERE id = %s",
                               (infer_knowledge_label(tname), tid))
            print("已回填 knowledge_point 历史数据")

        cursor.execute("SHOW COLUMNS FROM problem_templates LIKE 'generation_strategy'")
        if not cursor.fetchone():
            cursor.execute("ALTER TABLE problem_templates ADD COLUMN generation_strategy TEXT DEFAULT NULL")
            print("已添加 problem_templates.generation_strategy 列")
        cursor.execute("""
            SELECT id, template_name, problem_text, variables, solution_formula, answer_count
            FROM problem_templates
            WHERE generation_strategy IS NULL OR generation_strategy = ''
        """)
        templates_without_strategy = cursor.fetchall()
        for template_row in templates_without_strategy:
            template_id, template_name, problem_text, variables, solution_formula, answer_count = template_row
            generation_strategy = question_generation_service.infer_generation_strategy(
                template_name,
                problem_text,
                variables,
                solution_formula,
                answer_count,
            )
            if generation_strategy:
                cursor.execute(
                    "UPDATE problem_templates SET generation_strategy = %s WHERE id = %s",
                    (generation_strategy, template_id),
                )
        if templates_without_strategy:
            print(f"已为 {len(templates_without_strategy)} 道题补充 generation_strategy")

        # 高频错因字典表（系统种子标签，第二步 AI 追加 is_system=0 的行）
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS error_tags_library (
                id INT AUTO_INCREMENT PRIMARY KEY,
                code VARCHAR(30) NOT NULL,
                name VARCHAR(50) NOT NULL,
                definition VARCHAR(255) DEFAULT NULL,
                is_system TINYINT NOT NULL DEFAULT 1,
                sort_order INT NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE KEY uq_error_tag_code (code)
            )
        """)
        for code, name, definition, sort_order in ERROR_CATEGORY_SEED:
            cursor.execute("""
                INSERT INTO error_tags_library (code, name, definition, is_system, sort_order)
                VALUES (%s, %s, %s, 1, %s)
                ON DUPLICATE KEY UPDATE name = VALUES(name), definition = VALUES(definition), sort_order = VALUES(sort_order)
            """, (code, name, definition, sort_order))
        print("已确保 error_tags_library 种子标签")

        conn.commit()
    except mysql.connector.Error as err:
        print(f"修复数据库失败: {err}")
        conn.rollback()
    finally:
        cursor.close()
        conn.close()


def initialize_database():
    """初始化数据库 - 使用新的图片管理方式，包含答案单位字段"""
    conn = get_db_connection()
    cursor = conn.cursor()

    # 创建用户表（如果不存在）- 添加完成状态字段
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INT AUTO_INCREMENT PRIMARY KEY,
        username VARCHAR(50) NOT NULL UNIQUE,
        name VARCHAR(100) DEFAULT NULL,
        major VARCHAR(100) DEFAULT NULL,
        class_name VARCHAR(100) DEFAULT NULL,
        teacher_name VARCHAR(100) DEFAULT NULL,
        exam_start_time DATETIME NULL,
        exam_end_time DATETIME NULL,
        password VARCHAR(255) NOT NULL,
        avatar_filename VARCHAR(255) DEFAULT 'default.svg',
        password_changed BOOLEAN DEFAULT TRUE,
        current_session_token VARCHAR(64) DEFAULT NULL,
        failed_login_attempts INT DEFAULT 0,
        login_locked_until DATETIME NULL,
        completed_all BOOLEAN DEFAULT FALSE,
        completed_at TIMESTAMP NULL,
        total_score INT DEFAULT 0,
        total_time FLOAT DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS user_login_audit_logs (
        id BIGINT AUTO_INCREMENT PRIMARY KEY,
        user_id INT NULL,
        username VARCHAR(50) NOT NULL,
        login_status VARCHAR(20) NOT NULL,
        ip_address VARCHAR(45) DEFAULT NULL,
        user_agent VARCHAR(512) DEFAULT NULL,
        login_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_login_time (login_time),
        INDEX idx_username_login_time (username, login_time),
        INDEX idx_user_id_login_time (user_id, login_time),
        CONSTRAINT fk_login_audit_user
            FOREIGN KEY (user_id) REFERENCES users(id)
            ON DELETE SET NULL
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS exam_papers (
        id INT AUTO_INCREMENT PRIMARY KEY,
        name VARCHAR(100) NOT NULL UNIQUE,
        description TEXT DEFAULT NULL,
        is_enabled BOOLEAN DEFAULT TRUE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS exams (
        id INT AUTO_INCREMENT PRIMARY KEY,
        name VARCHAR(150) NOT NULL,
        paper_id INT NOT NULL,
        exam_type VARCHAR(20) DEFAULT 'normal',
        start_time DATETIME NULL,
        end_time DATETIME NULL,
        status VARCHAR(20) DEFAULT 'published',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (paper_id) REFERENCES exam_papers(id)
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS exam_assignments (
        id INT AUTO_INCREMENT PRIMARY KEY,
        exam_id INT NOT NULL,
        assign_type VARCHAR(20) NOT NULL,
        assign_value VARCHAR(100) NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (exam_id) REFERENCES exams(id) ON DELETE CASCADE,
        UNIQUE KEY uq_exam_assignment (exam_id, assign_type, assign_value)
    )
    """)

    default_paper_id = get_or_create_default_exam_paper(cursor)

    # 创建问题模板表（如果不存在）- 添加图片文件名字段和答案单位字段
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS problem_templates (
        id INT AUTO_INCREMENT PRIMARY KEY,
        template_name VARCHAR(100) NOT NULL,
        problem_text TEXT NOT NULL,
        variables TEXT NOT NULL,
        solution_formula TEXT NOT NULL,
        answer_count INT DEFAULT 1,
        answer_units TEXT,  -- 新增：答案单位字段
        difficulty VARCHAR(20) DEFAULT 'medium',
        image_filename VARCHAR(255) NULL,
        paper_id INT DEFAULT NULL,
        knowledge_point VARCHAR(50) DEFAULT NULL,
        generation_strategy TEXT DEFAULT NULL,
        FOREIGN KEY (paper_id) REFERENCES exam_papers(id)
    )
    """)

    # 创建用户答题记录表（确保包含所有必要字段）
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS user_responses (
        id INT AUTO_INCREMENT PRIMARY KEY,
        user_id INT NOT NULL,
        template_id INT NOT NULL,
        problem_text TEXT NOT NULL,
        user_answer FLOAT NOT NULL,
        correct_answer FLOAT NOT NULL,
        is_correct BOOLEAN NOT NULL,
        error_type VARCHAR(50) DEFAULT '未知',
        error_category VARCHAR(30) DEFAULT NULL,
        attempt_count INT NOT NULL,
        time_taken FLOAT NOT NULL,
        switch_count INT NOT NULL DEFAULT 0,
        response_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        answer_index INT DEFAULT 0,
        paper_id INT DEFAULT NULL,
        exam_id INT DEFAULT NULL,
        FOREIGN KEY (user_id) REFERENCES users(id),
        FOREIGN KEY (template_id) REFERENCES problem_templates(id),
        FOREIGN KEY (paper_id) REFERENCES exam_papers(id),
        FOREIGN KEY (exam_id) REFERENCES exams(id)
    )
    """)

    # 创建防伪记录表
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS verification_records (
        id INT AUTO_INCREMENT PRIMARY KEY,
        user_id INT NOT NULL,
        verification_code VARCHAR(20) NOT NULL UNIQUE,
        verification_data JSON NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )
    """)

    # 插入电磁学题目模板 - 包含答案单位信息
    templates = [
        # 题目1：闭合圆形线圈的感应电流（无图片）
        {
            'name': '闭合圆形线圈的感应电流',
            'text': r"""
                <div class="math-formula">
                    <h5>题目描述：</h5>
                    <p>用导线制成一半径为 \( r = __r__ \, \text{cm} \) 的闭合圆形线圈，其电阻 \( R = __R__ \, \Omega \)，均匀磁场垂直于线圈平面。</p>
                    <p>欲使电路中有一稳定的感应电流 \( i = __i__ \, \text{A} \)，求 \( B \) 的变化率 \( \frac{dB}{dt} \)。</p>
                    <div class="problem-hint-static">
                        <h5>解题提示：</h5>
                        <p>法拉第电磁感应定律：\( \varepsilon = -\frac{d\Phi}{dt} \)</p>
                        <p>磁通量：\( \Phi = B \cdot S = B \cdot \pi r^2 \)</p>
                        <p>感应电流：\( i = \frac{\varepsilon}{R} \)</p>
                    </div>
                </div>
            """,
            'variables': 'r,R,i',
            'formula': "i * R / (pi * (r/100)**2)",
            'answer_count': 1,
            'answer_units': 'T/s',  # 磁感应强度变化率，单位：特斯拉/秒
            'image_filename': None
        },

        # 题目2：高铁电磁感应问题（无图片）
        {
            'name': '高铁电磁感应问题',
            'text': r"""
            <div class="math-formula">
            <h5>题目描述：</h5>
            <p>中国是目前世界上高速铁路运行里程最长的国家，已知"复兴号"高铁长度为 L = __L__ m，车厢高 h = __h__ m，正常行驶速度 v = __v__ km/h。</p>
            <p>假设地面附近地磁场的水平分量约为 B = __B__ μT，将列车视为一整块导体，只考虑地磁场的水平分量。</p>
            <p>则"复兴号"列车在自西向东正常行驶的过程中，求车顶与车底之间的电势差大小。</p>
            <div class="problem-hint-static">
                        <h5>解题提示：</h5>
                <p>1. 速度单位换算：km/h → m/s</p>
                <p>2. 磁场单位换算：μT → T</p>
                <p>3. 导体在磁场中运动产生的感应电动势：ε = BLv</p>
                <p>4. 最终结果单位转换为微伏(μV)：1 V = 10⁶ μV</p>
            </div>
            </div>
            """,
            'variables': 'L,h,v,B',
            'formula': "B * L * (v / 3.6)",
            'answer_count': 1,
            'answer_units': 'μV',  # 电势差，单位：微伏
            'image_filename': None
        },

        # 题目3：等边三角形金属框转动电动势（有图片）
        {
            'name': '等边三角形金属框转动电动势',
            'text': r"""
                <div class="math-formula">
                    <h5>题目描述：</h5>
                    <p>如图所示，等边三角形的金属框，边长为 \( l = __l__ \, \text{m} \)，放在均匀磁场 \( B = __B__ \, \text{T} \) 中。</p>
                    <p>\( ab \) 边平行于磁感强度 \( B \)，当金属框绕 \( ab \) 边以角速度 \( \omega = __omega__ \, \text{rad/s} \) 转动时：</p>
                    <ol>
                        <li>求 \( bc \) 边上沿 \( bc \) 的电动势</li>
                        <li>求 \( ca \) 边上沿 \( ca \) 的电动势</li>
                        <li>求金属框内的总电动势</li>
                    </ol>
                    <div class="problem-hint-static">
                        <h5>解题提示：</h5>
                        <p>动生电动势：\( \varepsilon = \int (\vec{v} \times \vec{B}) \cdot d\vec{l} \)</p>
                        <p>考虑各边的运动情况和磁场方向</p>
                    </div>
                </div>
            """,
            'variables': 'l,B,omega',
            'formula': "(3/8) * B * omega * l**2, -(3/8) * B * omega * l**2,0",
            'answer_count': 3,
            'answer_units': 'V,V,V',  # 三个电动势，单位都是伏特
            'image_filename': 'problem3.png'
        },

        # 题目4：动生电动势与感生电动势（有图片）
        {
            'name': '动生电动势与感生电动势',
            'text': r"""
                <div class="math-formula">
                    <h5>题目描述：</h5>
                    <p>导体 \( AC \) 以速度 \( v = __v__ \, \text{m/s} \) 运动。</p>
                    <p>设 \( AC = __AC__ \, \text{cm} \)，均匀磁场随时间的变化率 \( \frac{dB}{dt} = __dBdt__ \, \text{T/s} \)。</p>
                    <p>某一时刻 \( B = __B__ \, \text{T} \)，\( x = __x__ \, \text{cm} \)，求：</p>
                    <ol>
                        <li>这时动生电动势的大小</li>
                        <li>总感应电动势的大小</li>
                        <li>动生电动势随 \( AC \) 运动的变化趋势（增大填1，减小填-1）</li>
                    </ol>
                    <div class="problem-hint-static">
                        <h5>解题提示：</h5>
                        <p>动生电动势：导体切割磁感线产生</p>
                        <p>感生电动势：磁场变化产生</p>
                        <p>总电动势为两者之和</p>
                    </div>
                </div>
            """,
            'variables': 'v,AC,dBdt,B,x',
            'formula': "B * v * (AC/100), (B * v * (AC/100)) + (dBdt * (x/100) * (AC/100)), 1",
            'answer_count': 3,
            'answer_units': 'V,V,-',  # 前两个是伏特，第三个是无量纲
            'image_filename': 'problem4.png'
        },

        # 题目5：折形金属导线运动电势差（有图片）
        {
            'name': '折形金属导线运动电势差',
            'text': r"""
                <div class="math-formula">
                    <h5>题目描述：</h5>
                    <p>\( aOc \) 为一折成 \( 30^\circ \) 角的金属导线（\( aO = Oc = L = __L__ \, \text{m} \)），位于 \( xy \) 平面中。</p>
                    <p>其中 \( aO \) 段与 \( x \) 轴夹角为 \( 30^\circ \)，\( Oc \) 段与 \( x \) 轴夹角为 \( 30^\circ \)，两段在 \( O \) 点相接。</p>
                    <p>磁感强度为 \( B = __B__ \, \text{T} \) 的匀强磁场垂直于 \( xy \) 平面。</p>
                    <ol>
                        <li>当 \( aOc \) 以速度 \( v = __v__ \, \text{m/s} \) 沿 \( x \) 轴正向运动时，导线上 \( a, c \) 两点间电势差 \( U_{ac} \)</li>
                        <li>当 \( aOc \) 以速度 \( v \) 沿 \( y \) 轴正向运动时，判断 \( a, c \) 两点电势高低（a点高填1，c点高填-1）</li>
                    </ol>
                    <div class="problem-hint-static">
                        <h5>解题提示：</h5>
                        <p>动生电动势公式：\( \varepsilon = \int (\vec{v} \times \vec{B}) \cdot d\vec{l} \)</p>
                        <p>考虑不同运动方向时各段的电动势，注意30度角的影响</p>
                        <p>总电势差为各段电动势的代数和</p>
                    </div>
                </div>
            """,
            'variables': 'L,B,v',
            'formula': "B * v * L /2 , -1",
            'answer_count': 2,
            'answer_units': 'V,-',  # 第一个是伏特，第二个是无量纲
            'image_filename': 'problem5.png'
        },

        # 题目6：磁铁插入线圈的感应现象（无图片）
        {
            'name': '磁铁插入线圈的感应现象',
            'text': r"""
                <div class="math-formula">
                    <h5>题目描述：</h5>
                    <p>将磁铁插入闭合电路线圈，一次是迅速地插入，另一次是缓慢地插入。</p>
                    <ol>
                        <li>两次插入过程中，线圈中感应电荷量是否相同？（相同填1，不同填0）</li>
                        <li>两次插入过程中，手推磁铁所做的功是否相同？（相同填1，不同填0）</li>
                    </ol>
                    <div class="problem-hint-static">
                        <h5>解题提示：</h5>
                        <p>感应电荷量：\( q = \frac{\Delta\Phi}{R} \)</p>
                        <p>做功与功率和时间有关</p>
                    </div>
                </div>
            """,
            'variables': '',
            'formula': "1, 0",
            'answer_count': 2,
            'answer_units': '-,-',  # 两个都是无量纲
            'image_filename': None
        },

        # 题目7：双圆线圈的感应电流（有图片）
        {
            'name': '双圆线圈的感应电流',
            'text': r"""
                <div class="math-formula">
                    <h5>题目描述：</h5>
                    <p>电阻为 \( R = __R__ \, \Omega \) 的闭合线圈折成半径分别为 \( a = __a__ \, \text{cm} \) 和 \( 2a \) 的两个圆，</p>
                    <p>将其置于与两圆平面垂直的匀强磁场内，磁感应强度按 \( B = B_0 \sin(\omega t) \) 的规律变化。</p>
                    <p>已知 \( B_0 = __B0__ \, \text{T} \)，\( \omega = __omega__ \, \text{rad/s} \)，求线圈中感应电流的最大值。</p>
                    <div class="problem-hint-static">
                        <h5>解题提示：</h5>
                        <p>法拉第电磁感应定律</p>
                        <p>总电动势为两个线圈电动势之和</p>
                        <p>感应电流最大值</p>
                    </div>
                </div>
            """,
            'variables': 'R,a,B0,omega',
            'formula': "(pi * omega * B0 / R) * ((a/100)**2 + (2*a/100)**2)",
            'answer_count': 1,
            'answer_units': 'A',  # 电流，单位：安培
            'image_filename': 'problem7.png'
        },
    ]

    # 插入模板到数据库 - 包含答案单位信息
    for template in templates:
        cursor.execute("SELECT id FROM problem_templates WHERE template_name = %s", (template['name'],))
        if not cursor.fetchone():
            # 如果有图片文件名，在problem_text中插入图片HTML
            problem_text = question_generation_service.normalize_problem_placeholders(template['text'])
            if template.get('image_filename'):
                img_html = f'''
                <div class="text-center mb-3">
                    <img src="/static/images/{template['image_filename']}" 
                         alt="{template['name']}" class="problem-image img-fluid">
                    <div class="image-caption text-muted">图：{template['name']}</div>
                </div>
                '''
                problem_text = img_html + problem_text

            # 插入包含答案单位的数据
            cursor.execute("""
                INSERT INTO problem_templates 
                (template_name, problem_text, variables, solution_formula, 
                 answer_count, answer_units, image_filename, paper_id, generation_strategy)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                template['name'],
                problem_text,
                question_generation_service.normalize_variable_specs(template['variables']),
                template['formula'],
                template['answer_count'],
                template.get('answer_units', ''),
                template.get('image_filename'),
                default_paper_id,
                question_generation_service.infer_generation_strategy(
                    template['name'],
                    problem_text,
                    question_generation_service.normalize_variable_specs(template['variables']),
                    template['formula'],
                    template['answer_count'],
                )
            ))
            print(f"✅ 插入题目: {template['name']}, 答案单位: {template.get('answer_units', '无')}")

    conn.commit()
    cursor.close()
    conn.close()

    print("✅ 数据库初始化完成，所有题目已添加答案单位")


def ensure_user_columns():
    """确保用户表包含必要字段"""
    conn = get_db_connection()
    if not conn:
        print("用户表检查失败：数据库连接失败")
        return
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SHOW COLUMNS FROM users")
        existing_columns = {col['Field'] for col in cursor.fetchall()}
        cursor.execute("SHOW COLUMNS FROM users LIKE 'password'")
        password_column = cursor.fetchone()
        if password_column and 'varchar(255)' not in password_column['Type'].lower():
            cursor.execute("ALTER TABLE users MODIFY COLUMN password VARCHAR(255) NOT NULL")
            print("已扩展 password 列长度")
        if 'name' not in existing_columns:
            cursor.execute("ALTER TABLE users ADD COLUMN name VARCHAR(100) DEFAULT NULL AFTER username")
            print("已添加 name 列")
        if 'major' not in existing_columns:
            cursor.execute("ALTER TABLE users ADD COLUMN major VARCHAR(100) DEFAULT NULL AFTER name")
            print("已添加 major 列")
        if 'class_name' not in existing_columns:
            cursor.execute("ALTER TABLE users ADD COLUMN class_name VARCHAR(100) DEFAULT NULL AFTER major")
            print("已添加 class_name 列")
        if 'teacher_name' not in existing_columns:
            cursor.execute("ALTER TABLE users ADD COLUMN teacher_name VARCHAR(100) DEFAULT NULL AFTER class_name")
            print("已添加 teacher_name 列")
        if 'exam_start_time' not in existing_columns:
            cursor.execute("ALTER TABLE users ADD COLUMN exam_start_time DATETIME NULL AFTER teacher_name")
            print("已添加 exam_start_time 列")
        if 'exam_end_time' not in existing_columns:
            cursor.execute("ALTER TABLE users ADD COLUMN exam_end_time DATETIME NULL AFTER exam_start_time")
            print("已添加 exam_end_time 列")
        if 'avatar_filename' not in existing_columns:
            cursor.execute("ALTER TABLE users ADD COLUMN avatar_filename VARCHAR(255) DEFAULT 'default.svg' AFTER password")
            print("已添加 avatar_filename 列")
        if 'password_changed' not in existing_columns:
            cursor.execute("ALTER TABLE users ADD COLUMN password_changed BOOLEAN DEFAULT TRUE AFTER avatar_filename")
            cursor.execute("UPDATE users SET password_changed = TRUE")
            print("已添加 password_changed 列")
        if 'current_session_token' not in existing_columns:
            cursor.execute("ALTER TABLE users ADD COLUMN current_session_token VARCHAR(64) DEFAULT NULL AFTER password_changed")
            print("已添加 current_session_token 列")
        if 'failed_login_attempts' not in existing_columns:
            cursor.execute("ALTER TABLE users ADD COLUMN failed_login_attempts INT DEFAULT 0 AFTER current_session_token")
            cursor.execute("UPDATE users SET failed_login_attempts = 0 WHERE failed_login_attempts IS NULL")
            print("已添加 failed_login_attempts 列")
        if 'login_locked_until' not in existing_columns:
            cursor.execute("ALTER TABLE users ADD COLUMN login_locked_until DATETIME NULL AFTER failed_login_attempts")
            print("已添加 login_locked_until 列")
        if 'selected_paper_id' not in existing_columns:
            cursor.execute("ALTER TABLE users ADD COLUMN selected_paper_id INT DEFAULT NULL AFTER login_locked_until")
            print("已添加 selected_paper_id 列")
        conn.commit()
    except mysql.connector.Error as err:
        print(f"更新用户表失败: {err}")
        conn.rollback()
    finally:
        cursor.close()
        conn.close()


def ensure_performance_indexes():
    """为高并发登录/答题场景补充必要复合索引。"""
    conn = get_db_connection()
    if not conn:
        print("索引检查失败：数据库连接失败")
        return

    cursor = conn.cursor(dictionary=True)
    try:
        index_definitions = [
            (
                'problem_templates',
                'idx_problem_templates_paper_id',
                "CREATE INDEX idx_problem_templates_paper_id ON problem_templates (paper_id)"
            ),
            (
                'user_responses',
                'idx_user_responses_user_paper_template_attempt',
                "CREATE INDEX idx_user_responses_user_paper_template_attempt ON user_responses (user_id, paper_id, template_id, attempt_count)"
            ),
            (
                'user_responses',
                'idx_user_responses_user_template_attempt_correct',
                "CREATE INDEX idx_user_responses_user_template_attempt_correct ON user_responses (user_id, template_id, attempt_count, is_correct)"
            ),
            (
                'user_responses',
                'idx_user_responses_user_response_time',
                "CREATE INDEX idx_user_responses_user_response_time ON user_responses (user_id, response_time)"
            ),
            (
                'user_responses',
                'idx_user_responses_exam_user_template_attempt',
                "CREATE INDEX idx_user_responses_exam_user_template_attempt ON user_responses (exam_id, user_id, template_id, attempt_count)"
            ),
            (
                'exam_assignments',
                'idx_exam_assignments_lookup',
                "CREATE INDEX idx_exam_assignments_lookup ON exam_assignments (assign_type, assign_value, exam_id)"
            ),
            (
                'exams',
                'idx_exams_status_time',
                "CREATE INDEX idx_exams_status_time ON exams (status, start_time, end_time)"
            ),
            (
                'users',
                'idx_users_login_lock_status',
                "CREATE INDEX idx_users_login_lock_status ON users (username, login_locked_until, failed_login_attempts)"
            ),
            (
                'users',
                'idx_users_teacher_exam_start',
                "CREATE INDEX idx_users_teacher_exam_start ON users (teacher_name, exam_start_time, exam_end_time)"
            ),
        ]

        for table_name, index_name, ddl in index_definitions:
            cursor.execute(
                """
                SELECT 1
                FROM information_schema.statistics
                WHERE table_schema = DATABASE()
                  AND table_name = %s
                  AND index_name = %s
                LIMIT 1
                """,
                (table_name, index_name)
            )
            exists = cursor.fetchone() is not None
            if exists:
                continue

            try:
                cursor.execute(ddl)
                print(f"已创建索引: {index_name}")
            except mysql.connector.Error as err:
                print(f"创建索引失败 {index_name}: {err}")

        conn.commit()
    except mysql.connector.Error as err:
        print(f"检查索引失败: {err}")
        conn.rollback()
    finally:
        cursor.close()
        conn.close()


def create_admin_user():
    """创建管理员用户"""
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        # 检查管理员用户是否已存在
        cursor.execute("SELECT id FROM users WHERE username = 'admin'")
        if not cursor.fetchone():
            # 创建管理员用户
            default_admin_password = '@admin123'
            admin_password = generate_password_hash(default_admin_password)
            cursor.execute(
                "INSERT INTO users (username, name, password, password_changed, avatar_filename) "
                "VALUES (%s, %s, %s, %s, %s)",
                ('admin', '管理员', admin_password, True, DEFAULT_AVATAR)
            )
            conn.commit()
            print(f"管理员用户创建成功: admin / {default_admin_password}")
        else:
            print("管理员用户已存在")
    except Exception as e:
        print(f"创建管理员用户失败: {e}")
    finally:
        cursor.close()
        conn.close()


def ensure_default_images():
    """确保默认图片文件存在"""
    import os

    default_images = {
        'problem3.png': '等边三角形金属框示意图',
        'problem4.png': '导体AC运动示意图',
        'problem5.png': '折形金属导线示意图',
        'problem7.png': '双圆线圈示意图'
    }

    images_path = os.path.join(app.root_path, 'static', 'images')
    os.makedirs(images_path, exist_ok=True)

    # 检查默认图片是否存在
    for filename, description in default_images.items():
        file_path = os.path.join(images_path, filename)
        if not os.path.exists(file_path):
            print(f"⚠️ 注意: 默认图片缺失: {filename} - {description}")
            print(f"请将图片文件放置到: {file_path}")

    print("✅ 默认图片检查完成")


def verify_image_consistency():
    """验证图片一致性"""
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    # 检查所有有图片的题目
    cursor.execute("""
        SELECT id, template_name, image_filename 
        FROM problem_templates 
        WHERE image_filename IS NOT NULL
    """)

    templates_with_images = cursor.fetchall()

    print("=== 图片一致性检查 ===")
    missing_images = []
    for template in templates_with_images:
        image_path = os.path.join(app.config['UPLOAD_FOLDER'], template['image_filename'])
        if os.path.exists(image_path):
            print(f"✅ 题目 '{template['template_name']}' 图片存在: {template['image_filename']}")
        else:
            print(f"❌ 题目 '{template['template_name']}' 图片缺失: {template['image_filename']}")
            missing_images.append({
                'template_name': template['template_name'],
                'image_filename': template['image_filename']
            })

    if missing_images:
        print(f"\n⚠️ 总计缺失 {len(missing_images)} 个图片文件:")
        for missing in missing_images:
            print(f"   - {missing['template_name']}: {missing['image_filename']}")

    cursor.close()
    conn.close()
    return len(missing_images) == 0


def update_user_completion_status(user_id):
    """更新用户完成状态 - 使用动态题目总数"""
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        selected_paper_id = resolve_selected_exam_paper_id()
        if not selected_paper_id:
            return False

        # 动态获取题目总数
        total_problems = get_total_problem_count(selected_paper_id)

        # 检查是否完成所有题目
        cursor.execute("""
            SELECT COUNT(DISTINCT template_id) as completed_count
            FROM (
                SELECT ur.template_id, ur.attempt_count
                FROM user_responses ur
                JOIN problem_templates t ON ur.template_id = t.id
                WHERE ur.user_id = %s AND COALESCE(ur.paper_id, t.paper_id) = %s
                GROUP BY template_id, attempt_count
                HAVING SUM(CASE WHEN is_correct THEN 1 ELSE 0 END) = COUNT(*)
            ) as completed_attempts
        """, (user_id, selected_paper_id))
        completed_count = cursor.fetchone()['completed_count']

        # 计算总分和总用时（仅统计完全正确的尝试）
        cursor.execute("""
            SELECT
                COUNT(*) as total_score,
                SUM(time_taken) as total_time
            FROM user_responses ur
            JOIN problem_templates t ON ur.template_id = t.id
            WHERE (ur.user_id, ur.template_id, ur.attempt_count) IN (
                SELECT ur2.user_id, ur2.template_id, ur2.attempt_count
                FROM user_responses ur2
                JOIN problem_templates t2 ON ur2.template_id = t2.id
                WHERE ur2.user_id = %s AND COALESCE(ur2.paper_id, t2.paper_id) = %s
                GROUP BY ur2.template_id, ur2.attempt_count
                HAVING SUM(CASE WHEN ur2.is_correct THEN 1 ELSE 0 END) = COUNT(*)
            )
            AND ur.is_correct = TRUE
        """, (user_id, selected_paper_id))
        stats = cursor.fetchone()

        completed_all = completed_count >= total_problems  # 使用动态总数

        # 更新用户表
        if completed_all:
            cursor.execute("""
                UPDATE users 
                SET completed_all = TRUE,
                    completed_at = NOW(),
                    total_score = %s,
                    total_time = %s
                WHERE id = %s
            """, (stats['total_score'] or 0, stats['total_time'] or 0, user_id))
        else:
            cursor.execute("""
                UPDATE users 
                SET completed_all = FALSE,
                    total_score = %s,
                    total_time = %s
                WHERE id = %s
            """, (stats['total_score'] or 0, stats['total_time'] or 0, user_id))

        conn.commit()
        return completed_all

    except Exception as e:
        print(f"更新用户完成状态失败: {e}")
        conn.rollback()
        return False
    finally:
        cursor.close()
        conn.close()

def update_user_completion_status(user_id, exam_id=None):
    """Update legacy completion fields, scoped to exam_id when available."""
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        selected_exam = get_exam_by_id(exam_id) if exam_id else None
        selected_paper_id = selected_exam['paper_id'] if selected_exam else resolve_selected_exam_paper_id()
        if not selected_paper_id:
            return False

        total_problems = get_total_problem_count(selected_paper_id)
        response_scope = "ur.exam_id = %s" if exam_id else "COALESCE(ur.paper_id, t.paper_id) = %s"
        response_scope_2 = "ur2.exam_id = %s" if exam_id else "COALESCE(ur2.paper_id, t2.paper_id) = %s"
        scope_param = exam_id if exam_id else selected_paper_id

        cursor.execute(f"""
            SELECT COUNT(DISTINCT template_id) as completed_count
            FROM (
                SELECT ur.template_id, ur.attempt_count
                FROM user_responses ur
                JOIN problem_templates t ON ur.template_id = t.id
                WHERE ur.user_id = %s AND {response_scope}
                GROUP BY template_id, attempt_count
                HAVING SUM(CASE WHEN is_correct THEN 1 ELSE 0 END) = COUNT(*)
            ) as completed_attempts
        """, (user_id, scope_param))
        completed_count = cursor.fetchone()['completed_count']

        cursor.execute(f"""
            SELECT
                COUNT(*) as total_score,
                SUM(time_taken) as total_time
            FROM user_responses ur
            JOIN problem_templates t ON ur.template_id = t.id
            WHERE (ur.user_id, ur.template_id, ur.attempt_count) IN (
                SELECT ur2.user_id, ur2.template_id, ur2.attempt_count
                FROM user_responses ur2
                JOIN problem_templates t2 ON ur2.template_id = t2.id
                WHERE ur2.user_id = %s AND {response_scope_2}
                GROUP BY ur2.template_id, ur2.attempt_count
                HAVING SUM(CASE WHEN ur2.is_correct THEN 1 ELSE 0 END) = COUNT(*)
            )
            AND ur.is_correct = TRUE
        """, (user_id, scope_param))
        stats = cursor.fetchone()

        completed_all = total_problems > 0 and completed_count >= total_problems
        if completed_all:
            cursor.execute("""
                UPDATE users
                SET completed_all = TRUE,
                    completed_at = NOW(),
                    total_score = %s,
                    total_time = %s
                WHERE id = %s
            """, (stats['total_score'] or 0, stats['total_time'] or 0, user_id))
        else:
            cursor.execute("""
                UPDATE users
                SET completed_all = FALSE,
                    total_score = %s,
                    total_time = %s
                WHERE id = %s
            """, (stats['total_score'] or 0, stats['total_time'] or 0, user_id))

        conn.commit()
        return completed_all
    except Exception as e:
        print(f"更新用户完成状态失败: {e}")
        conn.rollback()
        return False
    finally:
        cursor.close()
        conn.close()


def update_all_users_completion_status():
    """为所有用户刷新完成状态，返回更新数量"""
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        cursor.execute("SELECT id FROM users")
        users = cursor.fetchall()
    finally:
        cursor.close()
        conn.close()

    for user in users:
        update_user_completion_status(user['id'])

    return len(users)


def get_completion_stats(paper_id=None):
    """获取完成情况统计（仅统计已开启题库）。"""
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    total_problems = get_total_problem_count(paper_id)
    response_filter, response_params = build_enabled_paper_filter('ur', paper_id)
    response_filter_ur2, response_params_ur2 = build_enabled_paper_filter('ur2', paper_id)

    cursor.execute(f"""
        WITH completed AS (
            SELECT user_id, COUNT(DISTINCT template_id) AS completed_count, COALESCE(SUM(time_taken), 0) AS total_time
            FROM user_responses ur
            WHERE ur.is_correct = TRUE {response_filter}
              AND (ur.user_id, ur.template_id, ur.attempt_count) IN (
                SELECT user_id, template_id, attempt_count
                FROM user_responses ur2
                WHERE ur2.is_correct IN (TRUE, FALSE) {response_filter_ur2}
                GROUP BY user_id, template_id, attempt_count
                HAVING SUM(CASE WHEN is_correct THEN 1 ELSE 0 END) = COUNT(*)
              )
            GROUP BY user_id
        )
        SELECT
            COUNT(*) AS total_students,
            SUM(CASE WHEN COALESCE(c.completed_count, 0) >= %s THEN 1 ELSE 0 END) AS completed_count,
            ROUND(SUM(CASE WHEN COALESCE(c.completed_count, 0) >= %s THEN 1 ELSE 0 END) / COUNT(*) * 100, 1) AS completion_rate,
            AVG(COALESCE(c.completed_count, 0)) AS avg_score,
            AVG(COALESCE(c.total_time, 0)) AS avg_time
        FROM users u
        LEFT JOIN completed c ON c.user_id = u.id
        WHERE u.username != 'admin'
    """, response_params + response_params_ur2 + [total_problems, total_problems])
    stats = cursor.fetchone() or {}

    cursor.execute(f"""
        WITH completed AS (
            SELECT user_id, COUNT(DISTINCT template_id) AS completed_count
            FROM user_responses ur
            WHERE ur.is_correct = TRUE {response_filter}
              AND (ur.user_id, ur.template_id, ur.attempt_count) IN (
                SELECT user_id, template_id, attempt_count
                FROM user_responses ur2
                WHERE ur2.is_correct IN (TRUE, FALSE) {response_filter_ur2}
                GROUP BY user_id, template_id, attempt_count
                HAVING SUM(CASE WHEN is_correct THEN 1 ELSE 0 END) = COUNT(*)
              )
            GROUP BY user_id
        )
        SELECT COUNT(*) AS today_completions
        FROM users u
        LEFT JOIN completed c ON c.user_id = u.id
        WHERE u.username != 'admin' AND COALESCE(c.completed_count, 0) >= %s AND DATE(u.completed_at) = CURDATE()
    """, response_params + response_params_ur2 + [total_problems])
    today_stats = cursor.fetchone() or {}
    cursor.close()
    conn.close()
    return {'stats': stats, 'today_stats': today_stats, 'top_students': []}


def get_students_by_completion(completed=True, limit=None, offset=0, paper_id=None, filters=None):
    """按完成状态获取学生列表（仅统计已开启题库）。"""
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    total_problems = get_total_problem_count(paper_id)
    response_filter, response_params = build_enabled_paper_filter('ur', paper_id)
    response_filter_ur2, response_params_ur2 = build_enabled_paper_filter('ur2', paper_id)
    switch_filter, switch_params = build_enabled_paper_filter('sx', paper_id)
    filters = filters or {}
    student_filters = []
    student_params = []

    keyword = (filters.get('keyword') or '').strip()
    class_name = (filters.get('class_name') or '').strip()
    major = (filters.get('major') or '').strip()
    teacher_name = (filters.get('teacher_name') or '').strip()

    if keyword:
        student_filters.append("(u.username LIKE %s OR u.name LIKE %s)")
        student_params.extend([f"%{keyword}%", f"%{keyword}%"])
    if class_name:
        student_filters.append("u.class_name LIKE %s")
        student_params.append(f"%{class_name}%")
    if major:
        student_filters.append("u.major LIKE %s")
        student_params.append(f"%{major}%")
    if teacher_name:
        student_filters.append("u.teacher_name LIKE %s")
        student_params.append(f"%{teacher_name}%")

    student_filter_sql = ""
    if student_filters:
        student_filter_sql = " AND " + " AND ".join(student_filters)

    query = f"""
        WITH completed AS (
            SELECT user_id, COUNT(DISTINCT template_id) AS total_score, COALESCE(SUM(time_taken), 0) AS total_time
            FROM user_responses ur
            WHERE ur.is_correct = TRUE {response_filter}
              AND (ur.user_id, ur.template_id, ur.attempt_count) IN (
                SELECT user_id, template_id, attempt_count
                FROM user_responses ur2
                WHERE 1=1 {response_filter_ur2}
                GROUP BY user_id, template_id, attempt_count
                HAVING SUM(CASE WHEN is_correct THEN 1 ELSE 0 END) = COUNT(*)
            )
            GROUP BY user_id
        ),
        switch_stats AS (
            SELECT user_id, COALESCE(SUM(attempt_switch_count), 0) AS total_switch_count
            FROM (
                SELECT sx.user_id, sx.template_id, sx.attempt_count, MAX(COALESCE(sx.switch_count, 0)) AS attempt_switch_count
                FROM user_responses sx
                WHERE 1=1 {switch_filter}
                GROUP BY sx.user_id, sx.template_id, sx.attempt_count
            ) per_attempt
            GROUP BY user_id
        )
        SELECT u.id, u.username, u.name, u.major, u.class_name, u.teacher_name, u.exam_start_time, u.exam_end_time,
               CASE WHEN COALESCE(c.total_score, 0) >= %s AND %s > 0 THEN TRUE ELSE FALSE END AS completed_all,
               u.completed_at, COALESCE(c.total_score, 0) AS total_score, COALESCE(c.total_time, 0) AS total_time,
               COALESCE(s.total_switch_count, 0) AS total_switch_count, u.created_at
        FROM users u
        LEFT JOIN completed c ON c.user_id = u.id
        LEFT JOIN switch_stats s ON s.user_id = u.id
        WHERE u.username != 'admin' {student_filter_sql}
        HAVING completed_all = %s
        ORDER BY total_score DESC, total_time ASC, created_at ASC
    """
    full_params = response_params + response_params_ur2 + switch_params + [total_problems, total_problems] + student_params + [completed]
    if limit:
        query += " LIMIT %s OFFSET %s"
        full_params.extend([limit, offset])
    cursor.execute(query, full_params)
    students = cursor.fetchall()
    cursor.close()
    conn.close()
    return students
def get_class_comparison_stats(paper_id=None):
    """获取班级对比统计数据（用于管理端分析）"""
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    total_problems = get_total_problem_count(paper_id)
    response_filter = ""
    params = []
    if paper_id is not None:
        response_filter = " AND ur.paper_id = %s"
        params.append(paper_id)
    cursor.execute(f"""
        WITH completed AS (
            SELECT user_id, COUNT(DISTINCT template_id) AS total_score, COALESCE(SUM(time_taken), 0) AS total_time
            FROM user_responses ur
            WHERE ur.is_correct = TRUE {response_filter}
              AND (ur.user_id, ur.template_id, ur.attempt_count) IN (
                SELECT user_id, template_id, attempt_count
                FROM user_responses ur2
                WHERE 1=1 {response_filter.replace('ur.', 'ur2.')}
                GROUP BY user_id, template_id, attempt_count
                HAVING SUM(CASE WHEN is_correct THEN 1 ELSE 0 END) = COUNT(*)
              )
            GROUP BY user_id
        )
        SELECT COALESCE(NULLIF(TRIM(u.class_name), ''), '未分班') AS class_name,
               COUNT(*) AS student_count,
               SUM(CASE WHEN COALESCE(c.total_score, 0) >= %s AND %s > 0 THEN 1 ELSE 0 END) AS completed_count,
               ROUND(SUM(CASE WHEN COALESCE(c.total_score, 0) >= %s AND %s > 0 THEN 1 ELSE 0 END) / COUNT(*) * 100, 1) AS completion_rate,
               ROUND(AVG(COALESCE(c.total_score, 0)), 1) AS avg_score,
               ROUND(AVG(COALESCE(c.total_time, 0)), 1) AS avg_time
        FROM users u
        LEFT JOIN completed c ON c.user_id = u.id
        WHERE u.username != 'admin'
        GROUP BY COALESCE(NULLIF(TRIM(u.class_name), ''), '未分班')
        ORDER BY completion_rate DESC, avg_score DESC
    """, params + params + [total_problems, total_problems, total_problems, total_problems])
    class_stats = cursor.fetchall()
    cursor.close()
    conn.close()
    return class_stats
def diagnose_database_issue():
    """诊断数据库问题"""
    print("\n=== 数据库诊断开始 ===")

    try:
        conn = get_db_connection()
        if not conn:
            print("❌ 数据库连接失败")
            return False

        cursor = conn.cursor(dictionary=True)

        # 检查表是否存在
        cursor.execute("SHOW TABLES LIKE 'user_responses'")
        if not cursor.fetchone():
            print("❌ user_responses 表不存在")
            return False

        # 检查表结构
        cursor.execute("DESCRIBE user_responses")
        columns = cursor.fetchall()
        print("✅ user_responses 表结构:")
        for col in columns:
            print(f"  - {col['Field']} ({col['Type']})")

        # 检查外键约束
        cursor.execute("""
            SELECT TABLE_NAME, COLUMN_NAME, CONSTRAINT_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME
            FROM information_schema.KEY_COLUMN_USAGE
            WHERE TABLE_NAME = 'user_responses' AND REFERENCED_TABLE_NAME IS NOT NULL
        """)
        foreign_keys = cursor.fetchall()
        print("✅ 外键约束:")
        for fk in foreign_keys:
            print(f"  - {fk['COLUMN_NAME']} -> {fk['REFERENCED_TABLE_NAME']}.{fk['REFERENCED_COLUMN_NAME']}")

        cursor.close()
        conn.close()
        print("✅ 数据库诊断完成")
        return True

    except Exception as e:
        print(f"❌ 数据库诊断失败: {str(e)}")
        return False


@app.context_processor
def inject_device_status():
    """向所有模板注入设备状态"""
    return {
        'is_mobile': is_mobile_device(),
        'is_touch': is_touch_device(),
        'display_name': session.get('display_name'),
        'avatar_filename': session.get('avatar_filename', DEFAULT_AVATAR),
        'is_admin': session.get('username') == 'admin' if 'username' in session else False,
        'available_avatars': get_avatar_choices(),
        'show_password_modal': session.pop('show_password_modal', False)
    }


@app.route('/health')
def health_check():
    """健康检查端点"""
    try:
        # 检查数据库连接
        conn = get_db_connection()
        if conn and conn.is_connected():
            conn.close()
            return jsonify({'status': 'healthy', 'database': 'connected'})
        else:
            return jsonify({'status': 'unhealthy', 'database': 'disconnected'}), 500
    except Exception as e:
        return jsonify({'status': 'unhealthy', 'error': str(e)}), 500


@app.route('/api/exam/status')
@login_required
def exam_system_status():
    """考试系统状态监控"""
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    # 获取系统统计
    cursor.execute("SELECT COUNT(*) as active_users FROM users WHERE completed_all = FALSE")
    active_users = cursor.fetchone()

    cursor.execute("SELECT COUNT(*) as total_attempts FROM user_responses WHERE DATE(response_time) = CURDATE()")
    today_attempts = cursor.fetchone()

    cursor.close()
    conn.close()

    return jsonify({
        'active_users': active_users['active_users'],
        'today_attempts': today_attempts['total_attempts'],
        'server_time': datetime.now().isoformat(),
        'status': 'operational'
    })


def register_legacy_endpoint_alias(endpoint, blueprint_name, blueprint_endpoint=None):
    blueprint_endpoint = blueprint_endpoint or endpoint
    target_endpoint = f"{blueprint_name}.{blueprint_endpoint}"
    rule = next(app.url_map.iter_rules(target_endpoint))
    methods = rule.methods - {'HEAD', 'OPTIONS'}
    app.add_url_rule(
        rule.rule,
        endpoint=endpoint,
        view_func=app.view_functions[target_endpoint],
        methods=methods,
    )


admin_bp = create_admin_blueprint(globals())
app.register_blueprint(admin_bp)
for rule in list(app.url_map.iter_rules()):
    if rule.endpoint.startswith('admin.'):
        legacy_endpoint = rule.endpoint.split('.', 1)[1]
        register_legacy_endpoint_alias(legacy_endpoint, 'admin')


question_bp = create_question_blueprint(globals())
app.register_blueprint(question_bp)
for rule in list(app.url_map.iter_rules()):
    if rule.endpoint.startswith('question.'):
        legacy_endpoint = rule.endpoint.split('.', 1)[1]
        register_legacy_endpoint_alias(legacy_endpoint, 'question')


student_bp = create_student_blueprint(globals())
app.register_blueprint(student_bp)
for rule in list(app.url_map.iter_rules()):
    if rule.endpoint.startswith('student.'):
        legacy_endpoint = rule.endpoint.split('.', 1)[1]
        register_legacy_endpoint_alias(legacy_endpoint, 'student')


exam_bp = create_exam_blueprint(globals())
app.register_blueprint(exam_bp)
for legacy_endpoint in (
        'home',
        'dashboard',
        'select_exam',
        'select_exam_paper',
        'statistics',
        'debug_images',
        'reload_templates',
        'history',
        'debug_history',
        'refresh_problem',
        'problem_ajax',
        'api_submit',
):
    register_legacy_endpoint_alias(legacy_endpoint, 'exam')


if __name__ == '__main__':
    # 确保图片目录存在
    os.makedirs(get_upload_folder_path(), exist_ok=True)


    # 初始化数据库
    initialize_database()

    # 确保用户表字段完整
    ensure_user_columns()

    # 为高并发场景补齐关键索引
    ensure_performance_indexes()

    # 确保默认图片存在
    ensure_default_images()

    # 创建管理员用户
    create_admin_user()

    # 修复可能存在的表结构问题
    repair_database()
    ensure_performance_indexes()

    # 运行数据库诊断
    print("运行数据库诊断...")
    diagnose_database_issue()

    # 验证图片一致性
    print("验证图片一致性...")
    images_ok = verify_image_consistency()
    if not images_ok:
        print("⚠️ 警告: 部分图片文件缺失，请检查以上列表")

    prewarm_flag = os.getenv('PREWARM_ON_START', '0').lower() in ('1', 'true', 'yes', 'on')
    if prewarm_flag:
        prewarm_pools()
    else:
        print("[PREWARM] 跳过预热，未满足 PREWARM 或端口条件")

    app.run(host='0.0.0.0', port=5000, debug=False)
