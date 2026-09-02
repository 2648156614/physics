import os
import queue
import threading
import time
from datetime import datetime, timedelta
from functools import wraps

import mysql.connector
from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

from config import (
    AVATAR_ALLOWED_EXTENSIONS,
    AVATAR_FOLDER,
    DEFAULT_AVATAR,
    LOGIN_AUDIT_BATCH_SIZE,
    LOGIN_AUDIT_CLEANUP_INTERVAL_SECONDS,
    LOGIN_AUDIT_FLUSH_INTERVAL_SECONDS,
    LOGIN_AUDIT_QUEUE_MAXSIZE,
    LOGIN_AUDIT_RETENTION_DAYS,
    LOGIN_LOCK_MINUTES,
    MAX_LOGIN_FAILURES,
    SESSION_IDLE_TIMEOUT_SECONDS,
    UPGRADE_LEGACY_PASSWORD_ON_LOGIN,
)
from db import get_db_connection
from extensions import logger
from utils import get_client_ip, is_strong_password


auth_bp = Blueprint('auth', __name__)

last_login_audit_cleanup_ts = 0
login_audit_cleanup_lock = threading.Lock()
login_audit_cleanup_running = False
login_audit_queue = queue.Queue(maxsize=max(1, LOGIN_AUDIT_QUEUE_MAXSIZE))
login_audit_worker_lock = threading.Lock()
login_audit_worker_started = False


def get_avatar_choices():
    avatars_path = os.path.join(current_app.root_path, AVATAR_FOLDER)
    if not os.path.isdir(avatars_path):
        return [DEFAULT_AVATAR]
    avatars = []
    for filename in os.listdir(avatars_path):
        _, ext = os.path.splitext(filename)
        if ext.lower() in AVATAR_ALLOWED_EXTENSIONS:
            avatars.append(filename)
    if DEFAULT_AVATAR not in avatars:
        avatars.append(DEFAULT_AVATAR)
    return sorted(set(avatars))


def is_password_hash(value):
    if not value:
        return False
    return value.startswith('pbkdf2:') or value.startswith('scrypt:') or value.startswith('argon2:')


def get_display_name(user):
    name = (user.get('name') if isinstance(user, dict) else None) or ''
    name = str(name).strip()
    return name or user.get('username')


def clear_exam_session_state():
    for key in ('current_problem', 'attempt_count', 'verification_url'):
        session.pop(key, None)


def cleanup_expired_login_audits():
    global last_login_audit_cleanup_ts, login_audit_cleanup_running
    now_ts = int(time.time())
    if now_ts - last_login_audit_cleanup_ts < LOGIN_AUDIT_CLEANUP_INTERVAL_SECONDS:
        return
    with login_audit_cleanup_lock:
        if login_audit_cleanup_running:
            return
        login_audit_cleanup_running = True

    def _run_cleanup():
        global last_login_audit_cleanup_ts, login_audit_cleanup_running
        conn = None
        cursor = None
        try:
            conn = get_db_connection()
            if not conn:
                return
            cursor = conn.cursor()
            total_deleted = 0
            max_batches = 20
            batch_size = 1000
            for _ in range(max_batches):
                cursor.execute(
                    """
                    DELETE FROM user_login_audit_logs
                    WHERE login_time < DATE_SUB(NOW(), INTERVAL %s DAY)
                    LIMIT %s
                    """,
                    (LOGIN_AUDIT_RETENTION_DAYS, batch_size)
                )
                deleted = cursor.rowcount or 0
                if deleted <= 0:
                    break
                total_deleted += deleted
                conn.commit()
            last_login_audit_cleanup_ts = int(time.time())
            if total_deleted:
                logger.info("登录审计日志后台清理完成，删除 %s 条", total_deleted)
        except mysql.connector.Error as err:
            if conn:
                conn.rollback()
            logger.warning("登录审计日志后台清理失败: %s", err)
        finally:
            if cursor:
                cursor.close()
            if conn:
                conn.close()
            with login_audit_cleanup_lock:
                login_audit_cleanup_running = False

    threading.Thread(target=_run_cleanup, daemon=True).start()


def record_login_audit(cursor, user_id, login_status):
    cursor.execute(
        """
        INSERT INTO user_login_audit_logs (
            user_id, username, login_status, ip_address, user_agent
        ) VALUES (%s, %s, %s, %s, %s)
        """,
        (
            user_id,
            request.form.get('username', '').strip()[:50],
            login_status,
            get_client_ip(),
            (request.headers.get('User-Agent') or '')[:512]
        )
    )


def _build_login_audit_payload(user_id, login_status):
    return (
        user_id,
        request.form.get('username', '').strip()[:50],
        login_status,
        get_client_ip(),
        (request.headers.get('User-Agent') or '')[:512]
    )


def _insert_login_audit_batch(cursor, batch_payload):
    cursor.executemany(
        """
        INSERT INTO user_login_audit_logs (
            user_id, username, login_status, ip_address, user_agent
        ) VALUES (%s, %s, %s, %s, %s)
        """,
        batch_payload
    )


def _start_login_audit_worker_once():
    global login_audit_worker_started
    if login_audit_worker_started:
        return
    with login_audit_worker_lock:
        if login_audit_worker_started:
            return

        def _worker():
            while True:
                payload = login_audit_queue.get()
                if payload is None:
                    login_audit_queue.task_done()
                    continue

                batch = [payload]
                deadline = time.time() + LOGIN_AUDIT_FLUSH_INTERVAL_SECONDS
                while len(batch) < LOGIN_AUDIT_BATCH_SIZE:
                    timeout = max(0, deadline - time.time())
                    if timeout <= 0:
                        break
                    try:
                        next_payload = login_audit_queue.get(timeout=timeout)
                    except queue.Empty:
                        break
                    if next_payload is None:
                        login_audit_queue.task_done()
                        continue
                    batch.append(next_payload)

                conn = None
                cursor = None
                try:
                    conn = get_db_connection()
                    if conn:
                        cursor = conn.cursor()
                        _insert_login_audit_batch(cursor, batch)
                        conn.commit()
                except mysql.connector.Error as err:
                    if conn:
                        conn.rollback()
                    logger.warning("登录审计异步写入失败: %s", err)
                finally:
                    if cursor:
                        cursor.close()
                    if conn:
                        conn.close()
                    for _ in batch:
                        login_audit_queue.task_done()

        threading.Thread(target=_worker, daemon=True).start()
        login_audit_worker_started = True


def enqueue_login_audit(user_id, login_status, cursor=None):
    payload = _build_login_audit_payload(user_id, login_status)
    _start_login_audit_worker_once()
    try:
        login_audit_queue.put_nowait(payload)
    except queue.Full:
        if cursor is not None:
            record_login_audit(cursor, user_id, login_status)
        else:
            logger.warning("登录审计队列已满，且无可用游标，跳过本次审计写入。")


def login_required(f=None, *, db_check=False):
    def decorator(func):
        @wraps(func)
        def decorated_function(*args, **kwargs):
            user_id = session.get('user_id')
            if not user_id:
                flash('请先登录！', 'danger')
                return redirect(url_for('auth.login'))

            if db_check:
                conn = get_db_connection()
                if not conn:
                    flash('系统繁忙，请稍后重试。', 'danger')
                    return redirect(url_for('auth.login'))
                cursor = conn.cursor(dictionary=True)
                try:
                    cursor.execute("SELECT id FROM users WHERE id = %s LIMIT 1", (user_id,))
                    user_exists = cursor.fetchone()
                finally:
                    cursor.close()
                    conn.close()

                if not user_exists:
                    session.clear()
                    flash('当前账号状态异常，请重新登录。', 'danger')
                    return redirect(url_for('auth.login'))

            return func(*args, **kwargs)

        return decorated_function

    if f is None:
        return decorator
    return decorator(f)


@auth_bp.before_app_request
def enforce_session_idle_timeout():
    if request.endpoint in {'auth.login', 'auth.logout', 'login', 'logout', 'static'}:
        return None

    user_id = session.get('user_id')
    if not user_id:
        return None

    now_ts = int(time.time())
    last_activity = session.get('last_activity_ts')
    if last_activity and (now_ts - int(last_activity)) > SESSION_IDLE_TIMEOUT_SECONDS:
        session.clear()
        flash('由于超过1小时未操作，账号已自动锁定，请重新登录。', 'warning')
        return redirect(url_for('auth.login'))

    session.permanent = True
    session['last_activity_ts'] = now_ts
    return None


@auth_bp.route('/register', methods=['GET', 'POST'])
def register():
    abort(404)


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    cleanup_expired_login_audits()
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        now = datetime.now()

        conn = get_db_connection()
        if not conn:
            flash('系统繁忙，请稍后重试', 'danger')
            return render_template('login.html'), 503
        cursor = conn.cursor(dictionary=True)
        try:
            cursor.execute(
                """
                SELECT
                    id, username, name, password, avatar_filename, password_changed,
                    failed_login_attempts, login_locked_until, selected_paper_id
                FROM users
                WHERE username = %s
                """,
                (username,)
            )
            user = cursor.fetchone()

            if user:
                lock_until = user.get('login_locked_until')
                if lock_until and lock_until > now:
                    enqueue_login_audit(user['id'], 'locked', cursor=cursor)
                    remaining_seconds = int((lock_until - now).total_seconds())
                    remaining_minutes = max(1, (remaining_seconds + 59) // 60)
                    flash(f'该账号已被临时锁定，请在约 {remaining_minutes} 分钟后重试。', 'danger')
                    return render_template('login.html')

            if user and is_password_hash(user['password']):
                password_ok = check_password_hash(user['password'], password)
            else:
                password_ok = user is not None and user['password'] == password

            if user and password_ok:
                previous_user_id = session.get('user_id')
                if previous_user_id != user['id']:
                    clear_exam_session_state()

                if UPGRADE_LEGACY_PASSWORD_ON_LOGIN and not is_password_hash(user['password']):
                    new_hash = generate_password_hash(password)
                    cursor.execute("UPDATE users SET password = %s WHERE id = %s", (new_hash, user['id']))
                    user['password'] = new_hash

                if (user.get('failed_login_attempts') or 0) > 0 or user.get('login_locked_until'):
                    cursor.execute(
                        "UPDATE users SET failed_login_attempts = 0, login_locked_until = NULL WHERE id = %s",
                        (user['id'],)
                    )
                enqueue_login_audit(user['id'], 'success', cursor=cursor)
                conn.commit()

                session['user_id'] = user['id']
                session['username'] = user['username']
                session['display_name'] = get_display_name(user)
                session['avatar_filename'] = user.get('avatar_filename') or DEFAULT_AVATAR
                session['is_admin'] = (user['username'] == 'admin')
                preferred_paper_id = user.get('selected_paper_id')
                if preferred_paper_id:
                    session['selected_exam_paper_id'] = preferred_paper_id
                else:
                    session.pop('selected_exam_paper_id', None)
                session.pop('session_token', None)
                session.permanent = True
                session['last_activity_ts'] = int(time.time())

                if not user.get('password_changed', True):
                    session['show_password_modal'] = True
                if not is_strong_password(password):
                    session['show_password_modal'] = True
                    flash('当前密码强度不足，请尽快修改为强密码。', 'warning')

                flash('登录成功！', 'success')
                response = redirect(url_for('dashboard'))
                response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
                return response

            if user:
                failed_attempts = (user.get('failed_login_attempts') or 0) + 1
                lock_until = None
                if failed_attempts > MAX_LOGIN_FAILURES:
                    lock_until = now + timedelta(minutes=LOGIN_LOCK_MINUTES)
                    cursor.execute(
                        """
                        UPDATE users
                        SET failed_login_attempts = %s, login_locked_until = %s
                        WHERE id = %s
                        """,
                        (failed_attempts, lock_until, user['id'])
                    )
                    enqueue_login_audit(user['id'], 'locked', cursor=cursor)
                    conn.commit()
                    flash(
                        f'账号或密码连续错误超过 {MAX_LOGIN_FAILURES} 次，账号已锁定 {LOGIN_LOCK_MINUTES} 分钟。',
                        'danger'
                    )
                    return render_template('login.html')

                cursor.execute(
                    "UPDATE users SET failed_login_attempts = %s WHERE id = %s",
                    (failed_attempts, user['id'])
                )
                enqueue_login_audit(user['id'], 'failed', cursor=cursor)
                conn.commit()

            if not user:
                enqueue_login_audit(None, 'failed', cursor=cursor)

            flash('用户名或密码错误！', 'danger')
        except mysql.connector.Error as err:
            conn.rollback()
            current_app.logger.error("登录流程数据库错误: %s", err)
            flash('登录失败，请稍后重试。', 'danger')
        finally:
            cursor.close()
            conn.close()

    return render_template('login.html')


@auth_bp.route('/logout')
def logout():
    session.clear()
    flash('您已成功退出。', 'success')
    return redirect(url_for('auth.login'))


@auth_bp.route('/user/password', methods=['POST'])
@login_required(db_check=True)
def update_password():
    current_password = request.form.get('current_password', '')
    new_password = request.form.get('new_password', '')
    confirm_password = request.form.get('confirm_password', '')

    if not new_password or new_password != confirm_password:
        flash('新密码与确认密码不一致', 'danger')
        return redirect(request.referrer or url_for('dashboard'))
    if not is_strong_password(new_password):
        flash('新密码必须为强密码（至少8位，包含小写字母、数字和特殊字符，且不含空格）', 'danger')
        return redirect(request.referrer or url_for('dashboard'))

    conn = get_db_connection()
    if not conn:
        flash('数据库连接失败', 'danger')
        return redirect(request.referrer or url_for('dashboard'))

    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT password FROM users WHERE id = %s", (session['user_id'],))
        user = cursor.fetchone()
        if not user:
            flash('用户不存在', 'danger')
            return redirect(url_for('auth.login'))

        stored_password = user['password']
        if is_password_hash(stored_password):
            password_ok = check_password_hash(stored_password, current_password)
        else:
            password_ok = stored_password == current_password

        if not password_ok:
            flash('当前密码不正确', 'danger')
            return redirect(request.referrer or url_for('dashboard'))

        new_hash = generate_password_hash(new_password)
        cursor.execute(
            "UPDATE users SET password = %s, password_changed = TRUE WHERE id = %s",
            (new_hash, session['user_id'])
        )
        conn.commit()
        session.pop('show_password_modal', None)
        flash('密码修改成功', 'success')
        return redirect(request.referrer or url_for('dashboard'))
    finally:
        cursor.close()
        conn.close()


@auth_bp.route('/user/name', methods=['POST'])
@login_required(db_check=True)
def update_name():
    new_name = request.form.get('name', '').strip()
    if not new_name:
        flash('姓名不能为空', 'danger')
        return redirect(request.referrer or url_for('dashboard'))

    conn = get_db_connection()
    if not conn:
        flash('数据库连接失败', 'danger')
        return redirect(request.referrer or url_for('dashboard'))
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE users SET name = %s WHERE id = %s", (new_name, session['user_id']))
        conn.commit()
        session['display_name'] = new_name
        flash('姓名修改成功', 'success')
        return redirect(request.referrer or url_for('dashboard'))
    finally:
        cursor.close()
        conn.close()


@auth_bp.route('/user/avatar', methods=['POST'])
@login_required(db_check=True)
def update_avatar():
    avatar_filename = request.form.get('avatar_filename', '')
    available_avatars = get_avatar_choices()
    if avatar_filename not in available_avatars:
        flash('请选择有效的头像', 'danger')
        return redirect(request.referrer or url_for('dashboard'))

    conn = get_db_connection()
    if not conn:
        flash('数据库连接失败', 'danger')
        return redirect(request.referrer or url_for('dashboard'))
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE users SET avatar_filename = %s WHERE id = %s", (avatar_filename, session['user_id']))
        conn.commit()
        session['avatar_filename'] = avatar_filename
        flash('头像已更新', 'success')
        return redirect(request.referrer or url_for('dashboard'))
    finally:
        cursor.close()
        conn.close()
