from functools import wraps
from time import perf_counter
import json

from flask import Blueprint, make_response

from services.seb_exam_service import SEB_CONFIG_KEY_HEADER, build_finish_summary, validate_exam_config_request
from services.submission_receipt_service import receipt_key, answer_fingerprint, restore_receipt_state


def create_exam_blueprint(deps):
    globals().update(deps)
    bp = Blueprint('exam', __name__)

    def serialize_submission(view):
        """Allow only one in-flight submission per student and displayed problem."""
        @wraps(view)
        def wrapped(problem_id, *args, **kwargs):
            user_id = session.get('user_id')
            current_state = session.get('current_problem') or {}
            exam_id = current_state.get('exam_id') or session.get('selected_exam_id') or 'none'
            payload = request.get_json(silent=True) or {}
            submission_id = payload.get('submission_id')
            key = None
            if submission_id:
                try:
                    key = receipt_key(user_id, submission_id)
                except ValueError as err:
                    return jsonify(success=False, message=str(err)), 400
            lock_name = f"answer-submit:{user_id}:{exam_id}:{problem_id}"
            try:
                lock_token = acquire_distributed_lock(lock_name, ttl_seconds=30)
            except RedisError as err:
                logger.error("提交锁连接 Redis 失败: user_id=%s error=%s", user_id, err)
                return jsonify({
                    'success': False,
                    'message': '提交服务暂时不可用，请稍后重试。',
                }), 503
            if not lock_token:
                return jsonify({
                    'success': False,
                    'submission_in_progress': True,
                    'message': '答案正在提交，请勿重复点击。',
                }), 409
            try:
                if key:
                    fingerprint = answer_fingerprint(payload, exam_id, problem_id)
                    try:
                        previous = redis_client.get(key)
                        if previous:
                            previous = json.loads(previous)
                            if previous.get('fingerprint') != fingerprint:
                                return jsonify(success=False, message='提交内容已变化，请勿重复提交。'), 409
                            if previous.get('state') == 'done':
                                restore_receipt_state(previous, session)
                                return jsonify(previous['data']), previous['status']
                            return jsonify(success=False, submission_in_progress=True, message='正在核对提交结果，请勿重复提交。'), 409
                        redis_client.setex(key, 3600, json.dumps({'state': 'pending', 'fingerprint': fingerprint}))
                    except (RedisError, ValueError):
                        return jsonify(success=False, message='提交服务暂时不可用，答案尚未提交，请稍后重试。'), 503
                source_token = current_state.get('token')
                response = make_response(view(problem_id, *args, **kwargs))
                if key:
                    receipt = dict(state='done', fingerprint=fingerprint, data=response.get_json(),
                                   status=response.status_code, source_token=source_token,
                                   current_problem=session.get('current_problem'))
                    try:
                        redis_client.setex(key, 3600, json.dumps(receipt))
                    except RedisError:
                        logger.error('无法缓存提交回执: user_id=%s', user_id)
                return response
            finally:
                release_distributed_lock(lock_name, lock_token)

        return wrapped

    @bp.route('/api/submissions/<submission_id>')
    @login_required
    def submission_status(submission_id):
        try:
            key = receipt_key(session['user_id'], submission_id)
            raw = redis_client.get(key)
            receipt = json.loads(raw) if raw else {'state': 'unknown'}
            if receipt.get('state') == 'done':
                restore_receipt_state(receipt, session)
                body = dict(state='done', data=receipt['data'], status=receipt['status'])
            else:
                body = dict(state=receipt.get('state', 'unknown'))
            response = jsonify(body)
            response.headers['Cache-Control'] = 'no-store'
            return response
        except ValueError as err:
            return jsonify(success=False, message=str(err)), 400
        except RedisError:
            return jsonify(success=False, message='暂时无法核对提交结果，请稍后再次核对。'), 503


    def seb_guard(exam, api=False):
        valid, message = validate_seb_exam_request(
            exam,
            request.url,
            request.headers.get(SEB_CONFIG_KEY_HEADER),
        )
        if valid:
            return None
        if api:
            return jsonify({
                'success': False,
                'seb_required': True,
                'message': message,
            }), 403
        return render_template('seb_required.html', exam=exam, message=message), 403

    def secure_exam_progress(exam_id, paper_id):
        display_mapping = get_user_exam_problem_display_info(
            session['user_id'], exam_id, paper_id
        )
        actual_ids = [item['actual_id'] for item in display_mapping.values()]
        completion_map = get_completion_status_map(
            session['user_id'], paper_id, actual_ids, exam_id
        ) if actual_ids else {}
        completed_count = sum(1 for completed in completion_map.values() if completed)
        return (
            display_mapping,
            completion_map,
            build_finish_summary(completed_count, len(display_mapping)),
        )

    def shared_guard():
        valid, message = validate_exam_config_request(
            get_shared_seb_settings(), request.url, request.headers.get(SEB_CONFIG_KEY_HEADER))
        if not valid:
            return render_template('seb_required.html', exam=None, message=message), 403
        return None

    @bp.route('/seb/start')
    def seb_shared_start():
        blocked = shared_guard()
        if blocked:
            return blocked
        session.clear()
        session['pending_shared_seb'] = True
        response = redirect(url_for('auth.login'))
        response.headers['Cache-Control'] = 'no-store'
        return response

    @bp.route('/seb/exams')
    @login_required
    def seb_exams():
        blocked = shared_guard()
        if blocked:
            return blocked
        exams = [item for item in get_user_available_exams(session['user_id'])
                 if is_seb_exam_required(item) and item.get('seb_config_mode') == 'shared']
        open_exams = [item for item in exams if item.get('access_allowed')]
        if len(open_exams) == 1:
            response = redirect(url_for('exam.seb_continue', exam_id=open_exams[0]['id']))
            response.headers['Cache-Control'] = 'no-store'
            return response
        response = make_response(render_template('seb_exams.html', exams=exams))
        response.headers['Cache-Control'] = 'no-store'
        return response

    @bp.route('/seb/exit/<token>')
    def seb_shared_quit(token):
        import hmac
        settings = get_shared_seb_settings()
        if not hmac.compare_digest(token, settings.get('seb_quit_token') or ''):
            return render_template('seb_finished.html', exam_name=None, invalid=True), 404
        session.clear()
        return render_template('seb_finished.html', exam_name='本场考试', invalid=False)

    @bp.route('/seb/test/<int:exam_id>')
    def seb_test(exam_id):
        exam = get_exam_by_id(exam_id)
        if not exam or not is_seb_exam_required(exam):
            return render_template('seb_required.html', exam=exam, message='该考试未启用安全考试模式。'), 404
        blocked = seb_guard(exam)
        if blocked:
            return blocked
        response = make_response(render_template('seb_test.html', exam=exam))
        response.headers['Cache-Control'] = 'no-store'
        return response


    @bp.route('/seb/start/<int:exam_id>')
    def seb_start(exam_id):
        exam = get_exam_by_id(exam_id)
        if not exam or not is_seb_exam_required(exam):
            return render_template('seb_required.html', exam=exam, message='该考试未启用安全考试模式。'), 404
        blocked = seb_guard(exam)
        if blocked:
            return blocked
        # A fresh SEB launch always asks for credentials on shared computers.
        # Clear the old student's identity and question state only after the
        # entry URL has passed SEB validation.
        session.clear()
        session['pending_seb_exam_id'] = exam_id
        response = redirect(url_for('auth.login'))
        response.headers['Cache-Control'] = 'no-store'
        return response

    @bp.route('/seb/continue/<int:exam_id>')
    @login_required
    def seb_continue(exam_id):
        exam = get_exam_by_id(exam_id)
        available_exams = get_user_available_exams(session['user_id'])
        if not exam or not any(int(item['id']) == exam_id for item in available_exams):
            session.pop('pending_seb_exam_id', None)
            flash('您不在该考试的参考名单中。', 'danger')
            return redirect(url_for('dashboard'))
        blocked = seb_guard(exam)
        if blocked:
            return blocked
        exam_access = get_user_exam_access(session['user_id'], exam_id, available_exams=available_exams)
        if not exam_access['allowed']:
            flash(exam_access['message'], 'warning')
            return redirect(url_for('dashboard', exam_id=exam_id))
        session['selected_exam_id'] = exam_id
        session['selected_exam_paper_id'] = exam['paper_id']
        session['selected_exam_batch_id'] = exam_access.get('batch_id')
        session.pop('pending_seb_exam_id', None)
        session.pop('pending_shared_seb', None)
        # Select the first unfinished question in the answer-page request.
        # This avoids loading/persisting the assignment twice across workers.
        session['seb_resume_exam_id'] = exam_id
        return redirect(url_for('problem_ajax', problem_id=1))

    @bp.route('/seb/quit/<token>')
    def seb_quit(token):
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True) if conn else None
        exam = None
        try:
            if cursor:
                cursor.execute(
                    'SELECT id, name FROM exams WHERE seb_quit_token = %s LIMIT 1',
                    (token,),
                )
                exam = cursor.fetchone()
        finally:
            if cursor:
                cursor.close()
            if conn:
                conn.close()
        if not exam:
            return render_template('seb_finished.html', exam_name=None, invalid=True), 404
        exam_name = exam['name']
        session.clear()
        return render_template('seb_finished.html', exam_name=exam_name, invalid=False)

    # 主应用路由
    @bp.route('/')
    def home():
        if 'user_id' in session:
            return redirect(url_for('dashboard'))
        return redirect(url_for('auth.login'))
    
    
    @bp.route('/dashboard')
    @login_required
    def dashboard():
        try:
            available_exams = [] if session.get('username') == 'admin' else get_user_available_exams(session['user_id'])
            selected_exam_id = resolve_selected_exam_id(
                request.args.get('exam_id', type=int),
                available_exams=available_exams,
            )
            exam_access = get_user_exam_access(
                session['user_id'],
                selected_exam_id,
                available_exams=available_exams,
            )
            available_papers = get_enabled_exam_papers()
            selected_paper_id = exam_access.get('paper_id') or resolve_selected_exam_paper_id()
            selected_paper = get_exam_paper_by_id(selected_paper_id) if selected_paper_id else None
            selected_exam = get_exam_by_id(selected_exam_id) if selected_exam_id else None
            has_available_papers = bool(available_papers)
    
            if not has_available_papers:
                flash('教师没有布置题目。', 'info')
            elif session.get('username') != 'admin' and not selected_paper_id:
                flash('当前暂无可用题库，请联系管理员开启试卷。', 'warning')
    
            display_mapping = (
                get_user_exam_problem_display_info(
                    session['user_id'], selected_exam_id, selected_paper_id, persist=False
                )
                if selected_paper_id else {}
            )
            total_problems = len(display_mapping)
            actual_ids = [item['actual_id'] for item in display_mapping.values()]
            completion_map = (
                get_completion_status_map(session['user_id'], selected_paper_id, actual_ids, selected_exam_id)
                if selected_paper_id and actual_ids else {}
            )
            completed_count = sum(1 for completed in completion_map.values() if completed)
            completed_all = total_problems > 0 and completed_count >= total_problems
    
            current_display_number = 1
            if selected_paper_id and total_problems and not completed_all:
                for display_info in display_mapping.values():
                    actual_id = display_info['actual_id']
                    if not completion_map.get(actual_id, False):
                        current_display_number = display_info['display_number']
                        break
    
            session.pop('attempt_count', None)
            session.pop('current_problem', None)
    
            return render_template(
                'dashboard.html',
                display_name=session.get('display_name', session.get('username')),
                is_admin=session.get('username') == 'admin',
                current_problem=current_display_number,
                completed_count=completed_count,
                completed_all=completed_all,
                total_problems=total_problems,
                display_mapping=display_mapping,
                available_papers=available_papers,
                selected_paper=selected_paper,
                selected_paper_id=selected_paper_id,
                available_exams=available_exams,
                selected_exam=selected_exam,
                seb_required=is_seb_exam_required(selected_exam),
                selected_exam_id=selected_exam_id,
                has_available_papers=has_available_papers,
                exam_access=exam_access
            )
    
        except mysql.connector.Error as err:
            print(f"数据库查询错误: {err}")
            flash('数据库查询错误', 'danger')
            return redirect(url_for('home'))
    
    
    @bp.route('/select_exam', methods=['POST'])
    @login_required(db_check=True)
    def select_exam():
        exam_id = request.form.get('exam_id', type=int)
        if not exam_id or not user_can_access_exam(session['user_id'], exam_id):
            flash('考试不可用，请重新选择。', 'danger')
            return redirect(url_for('dashboard'))

        exam = get_exam_by_id(exam_id)
        if not exam:
            flash('考试不存在或已关闭。', 'danger')
            return redirect(url_for('dashboard'))

        session['selected_exam_id'] = exam_id
        session['selected_exam_paper_id'] = exam['paper_id']
        session.pop('current_problem', None)
        flash(f"已切换到考试：{exam['name']}", 'success')
        return redirect(url_for('dashboard', exam_id=exam_id))


    @bp.route('/select_exam_paper', methods=['POST'])
    @login_required(db_check=True)
    def select_exam_paper():
        paper_id = request.form.get('paper_id', type=int)
        selected = get_exam_paper_by_id(paper_id)
        if not selected or not selected.get('is_enabled'):
            flash('所选题库不可用，请重新选择。', 'danger')
            return redirect(url_for('dashboard'))
    
        session['selected_exam_paper_id'] = paper_id
        conn = get_db_connection()
        if conn:
            cursor = conn.cursor()
            try:
                cursor.execute("UPDATE users SET selected_paper_id = %s WHERE id = %s", (paper_id, session['user_id']))
                conn.commit()
            except mysql.connector.Error as err:
                # 兼容旧数据库：users 表可能还没有 selected_paper_id 列
                if getattr(err, 'errno', None) == 1054 and "selected_paper_id" in str(err):
                    try:
                        ensure_user_columns()
                        cursor.execute("UPDATE users SET selected_paper_id = %s WHERE id = %s", (paper_id, session['user_id']))
                        conn.commit()
                    except mysql.connector.Error:
                        conn.rollback()
                else:
                    conn.rollback()
                    raise
            finally:
                cursor.close()
                conn.close()
    
        flash(f"已切换到题库：{selected['name']}", 'success')
        return redirect(url_for('dashboard'))
    
    
    @bp.route('/stats')
    @login_required
    def statistics():
        """兼容旧统计入口，统一进入学生本人的详细答题数据。"""
        return redirect(url_for('student_answer_details'))
    
    
    @bp.route('/debug/images')
    def debug_images():
        import os
        static_path = os.path.join(app.root_path, 'static', 'images')
        files = os.listdir(static_path) if os.path.exists(static_path) else []
        return jsonify({
            'static_folder': app.static_folder,
            'images_path': static_path,
            'files': files
        })
    
    
    @bp.route('/debug/reload_templates', methods=['GET'])
    def reload_templates():
        template_cache_ts = question_generation_service.clear_template_cache()
    
        deleted_count = delete_cache_patterns(("exam:pool:*", "exam:problem:*"))
    
        return jsonify({
            'success': True,
            'message': '模板缓存、Redis题目池和已生成题目缓存已清空',
            'deleted_cache_count': deleted_count,
            'timestamp': template_cache_ts
        })
    
    
    @bp.route('/history')
    @login_required
    def history():
        """获取主页当前所选题库的答题历史。"""
        conn = None
        selected_paper_id = None
        selected_exam_id = None
        try:
            conn = get_db_connection()
            cursor = conn.cursor(dictionary=True)
            selected_paper_id = resolve_selected_exam_paper_id()
            selected_exam_id = session.get('selected_exam_id')
            if not selected_paper_id:
                return render_template(
                    'history.html',
                    responses=[],
                    stats=None,
                    username=session['username'],
                    selected_paper_id=None,
                    selected_exam_id=selected_exam_id,
                )
            response_filter = "COALESCE(r.paper_id, t.paper_id) = %s"
            response_filter_param = selected_paper_id
    
            # 获取答题记录
            cursor.execute(f"""
                SELECT 
                    r.id,
                    t.template_name,
                    r.problem_text,
                    r.user_answer,
                    r.correct_answer,
                    r.is_correct,
                    r.attempt_count,
                    r.response_time,
                    r.time_taken,
                    t.id as template_id
                FROM user_responses r
                JOIN problem_templates t ON r.template_id = t.id
                WHERE r.user_id = %s AND {response_filter}
                ORDER BY r.response_time DESC
            """, (session['user_id'], response_filter_param))
    
            responses = cursor.fetchall()
    
            # 格式化时间
            for response in responses:
                if response['response_time']:
                    response['formatted_time'] = response['response_time'].strftime('%Y-%m-%d %H:%M:%S')
                else:
                    response['formatted_time'] = '未知时间'
    
            # 获取统计信息
            cursor.execute(f"""
                SELECT 
                    COUNT(*) as total,
                    SUM(CASE WHEN is_correct = TRUE THEN 1 ELSE 0 END) as correct_count,
                    AVG(time_taken) as avg_time
                FROM user_responses r
                JOIN problem_templates t ON r.template_id = t.id
                WHERE r.user_id = %s AND {response_filter}
            """, (session['user_id'], response_filter_param))
    
            stats_result = cursor.fetchone()
    
            # 处理统计信息
            stats = None
            if stats_result and stats_result['total']:
                stats = {
                    'total': int(stats_result['total']),
                    'correct': int(stats_result['correct_count'] or 0),
                    'avg_time': float(stats_result['avg_time'] or 0)
                }
    
            return render_template('history.html',
                                   responses=responses,
                                   stats=stats,
                                   username=session['username'],
                                   selected_paper_id=selected_paper_id,
                                   selected_exam_id=selected_exam_id)
    
        except Exception as e:
            print(f"[系统错误] 获取答题历史失败: {str(e)}")
            flash('获取答题历史失败，请稍后再试', 'danger')
            return render_template('history.html',
                                   responses=[],
                                   stats=None,
                                   username=session['username'],
                                   selected_paper_id=selected_paper_id)
        finally:
            if conn:
                conn.close()
    
    
    @bp.route('/debug/history')
    @login_required
    def debug_history():
        """调试历史记录"""
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
    
        # 检查用户记录
        cursor.execute("SELECT COUNT(*) as count FROM user_responses WHERE user_id = %s", (session['user_id'],))
        user_count = cursor.fetchone()
    
        # 检查表结构
        cursor.execute("DESCRIBE user_responses")
        table_structure = cursor.fetchall()
    
        cursor.close()
        conn.close()
    
        return jsonify({
            'user_id': session['user_id'],
            'user_response_count': user_count['count'],
            'table_structure': table_structure
        })
    
    
    @bp.route('/refresh_problem/<int:problem_id>', methods=['POST'])
    @login_required
    def refresh_problem(problem_id):
        """刷新单个题目"""
        try:
            selected_exam_id = resolve_selected_exam_id(request.args.get('exam_id', type=int))
            exam_access = get_user_exam_access(session['user_id'], selected_exam_id)
            if not exam_access['allowed']:
                return jsonify({
                    'success': False,
                    'message': exam_access['message'],
                    'exam_blocked': True,
                    'exam_status': exam_access.get('status')
                }), 403
            exam = get_exam_by_id(selected_exam_id)
            blocked = seb_guard(exam, api=True)
            if blocked:
                return blocked
    
            # 根据显示序号获取实际ID
            paper_id = exam_access.get('paper_id') or resolve_selected_exam_paper_id()
            display_mapping = get_user_exam_problem_display_info(session['user_id'], selected_exam_id, paper_id)
            display_to_actual = {
                info['display_number']: actual_id
                for actual_id, info in display_mapping.items()
            }
            actual_id = display_to_actual.get(problem_id)
            if actual_id is None:
                return jsonify({'success': False, 'message': '无效的题目编号'})
    
            current_problem_data = None
            current_problem_state = session.get('current_problem') or {}
            if current_problem_state.get('display_number') == problem_id:
                current_problem_data = get_problem_by_token(current_problem_state.get('token'))

            token, problem_data = fetch_distinct_problem(actual_id, current_problem_data)
    
            if not problem_data or not token:
                return jsonify({'success': False, 'message': '题目生成失败'})
    
            # 更新session中的题目状态，确保前端显示的新题和下次提交校验使用同一个token。
            session['current_problem'] = {
                'display_number': problem_id,
                'actual_id': actual_id,
                'paper_id': paper_id,
                'exam_id': selected_exam_id,
                'batch_id': exam_access.get('batch_id'),
                'user_id': session['user_id'],
                'token': token,
                'total_attempts': 0,
                'answered_correctly': False,
                'start_time': time.time(),
            }
            session.modified = True
    
            return jsonify({
                'success': True,
                'message': '题目刷新成功',
                'token': token,
                'new_var_values': problem_data.get('var_values', {}),
                'new_display_var_values': problem_data.get('display_var_values', {}),
                'new_correct_answers': problem_data.get('correct_answers', []),
                'new_problem_text': problem_data.get('problem_text', ''),
                'answer_units': problem_data.get('answer_units', []),
            })
        except Exception as e:
            return jsonify({'success': False, 'message': str(e)})
    
    
    @bp.route('/problem_ajax/<int:problem_id>')  # 保持参数名为 problem_id
    @login_required
    def problem_ajax(problem_id):
        """支持Ajax的问题页面 - 内部将problem_id作为显示序号使用"""
        request_started = perf_counter()
        stage_started = request_started
        timings = []

        def mark_stage(name):
            nonlocal stage_started
            now = perf_counter()
            timings.append((name, (now - stage_started) * 1000))
            stage_started = now

        debug_request_logs = os.getenv('DEBUG_REQUEST_LOGS', '0').lower() in ('1', 'true', 'yes', 'on')
        if debug_request_logs:
            print(f"\n=== Ajax问题页面开始 ===")
            print(f"显示序号: {problem_id}")
        available_exams = (
            None if session.get('username') == 'admin'
            else get_user_available_exams(session['user_id'])
        )
        selected_exam_id = resolve_selected_exam_id(
            request.args.get('exam_id', type=int), available_exams=available_exams
        )
        resume_exam_id = session.get('seb_resume_exam_id')
        if resume_exam_id and selected_exam_id != resume_exam_id:
            session.pop('seb_resume_exam_id', None)
            flash('该考试当前不可访问，请联系管理员。', 'danger')
            return redirect(url_for('dashboard'))
        exam_access = get_user_exam_access(
            session['user_id'], selected_exam_id, available_exams=available_exams
        )
        if not exam_access['allowed']:
            flash(exam_access['message'], 'warning')
            return redirect(url_for('dashboard'))
        exam = get_exam_by_id(selected_exam_id)
        blocked = seb_guard(exam)
        if blocked:
            return blocked
        mark_stage('access')
    
        # 根据显示序号获取实际ID
        paper_id = exam_access.get('paper_id') or resolve_selected_exam_paper_id()
        display_mapping = get_user_exam_problem_display_info(session['user_id'], selected_exam_id, paper_id)
        display_to_actual = {
            info['display_number']: actual_id
            for actual_id, info in display_mapping.items()
        }
        total_problems = len(display_mapping)
        mark_stage('assignment')
        completion_map = None
        if resume_exam_id and resume_exam_id == selected_exam_id and is_seb_exam_required(exam):
            actual_ids = [item['actual_id'] for item in display_mapping.values()]
            completion_map = get_completion_status_map(
                session['user_id'], paper_id, actual_ids, selected_exam_id
            ) if actual_ids else {}
            ordered_questions = sorted(display_mapping.values(), key=lambda item: item['display_number'])
            problem_id = next(
                (item['display_number'] for item in ordered_questions
                 if not completion_map.get(item['actual_id'], False)),
                ordered_questions[-1]['display_number'] if ordered_questions else 1,
            )
        mark_stage('resume')
        actual_id = display_to_actual.get(problem_id)
    
        # 1. 验证题目序号
        if problem_id < 1 or problem_id > total_problems:
            flash('无效的题目编号', 'danger')
            return redirect(url_for('dashboard'))
    
        # 2. 初始化或获取题目数据
        problem_data = None
        problem_token = None
    
        current_problem_state = session.get('current_problem') or {}
        if current_problem_state.get('display_number') == problem_id:
            state_matches_current_user = (
                current_problem_state.get('user_id') == session['user_id'] and
                current_problem_state.get('paper_id') == paper_id and
                current_problem_state.get('exam_id') == selected_exam_id and
                current_problem_state.get('batch_id') == exam_access.get('batch_id') and
                current_problem_state.get('actual_id') == actual_id
            )
            if state_matches_current_user:
                problem_token = current_problem_state.get('token')
                problem_data = get_problem_by_token(problem_token)
            else:
                problem_token = None
                problem_data = None
                session.pop('current_problem', None)
            if problem_data and int(problem_data.get('template_id', -1)) != int(actual_id):
                if debug_request_logs:
                    print(
                        f"会话题目实际ID不匹配，丢弃旧题目: cached={problem_data.get('template_id')} current={actual_id}"
                    )
                problem_data = None
                problem_token = None
                session.pop('current_problem', None)
            if not problem_data:
                if debug_request_logs:
                    print("缓存中的题目已过期或不匹配，重新获取新题目")

        if not problem_data:
            if debug_request_logs:
                print(f"生成/获取新题目... 实际ID: {actual_id}")
            problem_token, problem_data = fetch_problem_from_pool(actual_id)
    
            if not problem_data:
                problem_token, problem_data = generate_and_cache_problem(actual_id)
    
            if not problem_data:
                flash('题目生成失败', 'danger')
                return redirect(url_for('dashboard'))
    
            session['current_problem'] = {
                'display_number': problem_id,
                'actual_id': actual_id,
                'paper_id': paper_id,
                'exam_id': selected_exam_id,
                'batch_id': exam_access.get('batch_id'),
                'user_id': session['user_id'],
                'token': problem_token,
                'total_attempts': 0,
                'answered_correctly': False,
                'start_time': time.time()
            }
            if debug_request_logs:
                print(f"新题目生成成功，答案数量: {problem_data.get('answer_count', 1)}")
        else:
            # 确保最小状态存在
            session['current_problem'].setdefault('total_attempts', 0)
            session['current_problem'].setdefault('answered_correctly', False)
            session['current_problem'].setdefault('start_time', time.time())
            session['current_problem'].setdefault('token', problem_token)
            session['current_problem']['actual_id'] = actual_id
            session['current_problem']['paper_id'] = paper_id
            session['current_problem']['exam_id'] = selected_exam_id
            session['current_problem']['batch_id'] = exam_access.get('batch_id')
            session['current_problem']['user_id'] = session['user_id']
        mark_stage('problem')
    
        # 3. 检查是否已经完成
        if session['current_problem'].get('answered_correctly', False):
            if debug_request_logs:
                print(f"题目 {problem_id} 已完成")
    
        # 4. 渲染Ajax模板
        total_attempts = session['current_problem'].get('total_attempts', 0)
        answer_count = problem_data.get('answer_count', 1)
    
        # 确保token写回session（兼容旧数据）
        session['current_problem']['token'] = problem_token or session['current_problem'].get('token')
        session.modified = True
    
        if debug_request_logs:
            print(f"渲染Ajax模板，显示序号: {problem_id}, 实际ID: {actual_id}")
            print(f"答案数量: {answer_count}, 累计尝试: {total_attempts}")
            print(f"当前题目参数: {problem_data['var_values']}")
            print(f"=== Ajax问题页面结束 ===\n")
    
        is_completed = (
            completion_map.get(actual_id, False) if completion_map is not None
            else is_problem_completed(session['user_id'], actual_id, paper_id, selected_exam_id)
        )
        seb_required = is_seb_exam_required(exam)
        if seb_required and session.get('seb_entry_recorded_exam_id') != selected_exam_id:
            if record_secure_exam_entry(
                session['user_id'], selected_exam_id, exam_access.get('batch_id')
            ):
                session['seb_entry_recorded_exam_id'] = selected_exam_id
                session.modified = True
        mark_stage('progress')
    
        response = make_response(render_template('problem_ajax.html',
                               problem=problem_data,
                               problem_id=problem_id,  # 传递显示序号到模板
                               total_attempts=total_attempts,
                               answer_count=answer_count,
                               username=session['username'],
                               total_problems=total_problems,
                               display_mapping=display_mapping,
                               display_to_actual=display_to_actual,
                               is_completed=is_completed,
                               resumed_problem_url=(
                                   url_for('problem_ajax', problem_id=problem_id)
                                   if completion_map is not None else None
                               ),
                               seb_required=seb_required))
        session.pop('seb_resume_exam_id', None)
        mark_stage('template')
        total_ms = (perf_counter() - request_started) * 1000
        response.headers['Server-Timing'] = ', '.join(
            f'{name};dur={duration:.1f}' for name, duration in timings
        ) + f', total;dur={total_ms:.1f}'
        if total_ms >= 1000:
            logger.warning(
                '答题页加载较慢: user_id=%s exam_id=%s problem_id=%s timings=%s',
                session['user_id'], selected_exam_id, problem_id,
                response.headers['Server-Timing'],
            )
        return response
    
    
    @bp.route('/api/submit/<int:problem_id>', methods=['POST'])  # 保持参数名为 problem_id
    @login_required
    @serialize_submission
    def api_submit(problem_id):
        """API接口：提交答案 - 内部将problem_id作为显示序号使用"""
        try:
            current_problem_state = session.get('current_problem') or {}
            selected_exam_id = resolve_selected_exam_id(current_problem_state.get('exam_id') or session.get('selected_exam_id'))
            exam_access = get_user_exam_access(session['user_id'], selected_exam_id)
            if not exam_access['allowed']:
                return jsonify({
                    'success': False,
                    'message': exam_access['message'],
                    'exam_blocked': True,
                    'server_time': exam_access['server_time'].strftime('%Y-%m-%d %H:%M:%S'),
                'exam_start_time': exam_access['exam_start_time'].strftime('%Y-%m-%d %H:%M:%S')
                    if exam_access.get('exam_start_time') else None,
                'exam_end_time': exam_access['exam_end_time'].strftime('%Y-%m-%d %H:%M:%S')
                    if exam_access.get('exam_end_time') else None,
                'exam_status': exam_access.get('status')
            }), 403
            exam = get_exam_by_id(selected_exam_id)
            blocked = seb_guard(exam, api=True)
            if blocked:
                return blocked
    
            data = request.get_json()
            logger.info("提交答案: problem_id=%s", problem_id)
            logger.debug("提交数据详情: problem_id=%s payload=%s", problem_id, data)
    
            if not data:
                return jsonify({'success': False, 'message': '无效的请求数据'})
    
            # 根据显示序号获取实际ID
            paper_id = exam_access.get('paper_id') or resolve_selected_exam_paper_id()
            display_mapping = get_user_exam_problem_display_info(session['user_id'], selected_exam_id, paper_id)
            total_problems = len(display_mapping)
            display_to_actual = {
                info['display_number']: actual_id
                for actual_id, info in display_mapping.items()
            }
            actual_id = display_to_actual.get(problem_id)
            # Validate that the cached problem belongs to this user, paper, and display number.
            if (not current_problem_state or
                    current_problem_state.get('display_number') != problem_id or
                    current_problem_state.get('user_id') != session['user_id'] or
                    current_problem_state.get('paper_id') != paper_id or
                    current_problem_state.get('exam_id') != selected_exam_id or
                    current_problem_state.get('batch_id') != exam_access.get('batch_id') or
                    current_problem_state.get('actual_id') != actual_id):
                session.pop('current_problem', None)
                session.modified = True
                return jsonify({'success': False, 'message': '会话已过期，请重新开始答题'})
    
            problem_token = session['current_problem'].get('token')
            problem_data = get_problem_by_token(problem_token)
            if not problem_data:
                return jsonify({'success': False, 'message': '题目已过期，请刷新后重试'})
            if int(problem_data.get('template_id', -1)) != int(actual_id):
                session.pop('current_problem', None)
                session.modified = True
                return jsonify({'success': False, 'message': '题目缓存与当前题号不匹配，请刷新后重试'})
    
            correct_answers = problem_data.get('correct_answers', [])
            answer_count = problem_data.get('answer_count', 1)
            time_taken = float(data.get('time_taken', 0))
            # 保留历史字段兼容性，后续答题不再采集切屏次数。
            switch_count = 0
            user_id = session['user_id']
            template_id = problem_data['template_id']
            problem_text = problem_data['problem_text']
    
            # 如果题目已完成，阻止重复作答
            if is_problem_completed(user_id, actual_id, paper_id, selected_exam_id):
                return jsonify({
                    'success': False,
                    'message': '该题已完成，无需重复作答',
                    'already_completed': True
                })
    
            logger.info(
                "处理提交: user_id=%s problem_id=%s actual_id=%s template_id=%s answer_count=%s attempts=%s time_taken=%.2fs",
                user_id,
                problem_id,
                actual_id,
                template_id,
                answer_count,
                session['current_problem']['total_attempts'],
                time_taken,
            )
            logger.debug("正确答案详情: %s", correct_answers)
    
            # 验证答题时间的合理性
            if time_taken < 0:
                logger.warning("无效的答题时间: %.2fs，重置为0", time_taken)
                time_taken = 0
            elif time_taken > 86400:  # 超过24小时
                logger.warning("答题时间异常长: %.2fs，限制为3600秒", time_taken)
                time_taken = 3600
            elif time_taken < 1:  # 少于1秒（可能有问题）
                logger.warning("答题时间过短: %.2fs，可能计时器有问题", time_taken)
            elif time_taken > 3600:  # 超过1小时
                logger.info("答题时间较长: %.2fs", time_taken)
    
            logger.info("最终记录用时: %.2fs", time_taken)
    
            # 获取用户答案
            user_answers = []
            for i in range(answer_count):
                if answer_count == 1:
                    user_answer = float(data.get('answer', 0))
                    user_answers.append(user_answer)
                else:
                    user_answer = float(data.get(f'answer{i + 1}', 0))
                    user_answers.append(user_answer)
    
            logger.debug("用户答案详情: %s", user_answers)
    
            # 验证每个答案
            is_correct_list = []
            all_correct = True
    
            for i, (user_answer, correct_answer) in enumerate(zip(user_answers, correct_answers)):
                # 使用不同的变量名避免冲突
                answer_is_correct = is_correct(user_answer, correct_answer)
                is_correct_list.append(answer_is_correct)
                if not answer_is_correct:
                    all_correct = False
                logger.debug(
                    "答案校验: index=%s user=%s correct=%s result=%s",
                    i + 1,
                    user_answer,
                    correct_answer,
                    answer_is_correct,
                )
    
            error_types = [
                classify_error_type(user_answer, correct_answer, is_correct)
                for user_answer, correct_answer, is_correct in zip(user_answers, correct_answers, is_correct_list)
            ]
    
            # 计算 attempt_count：基于数据库最大值递增，避免会话重置导致 attempt_count 重复
            latest_attempt_count = get_latest_attempt_count(user_id, template_id, paper_id, selected_exam_id)
            total_attempts = latest_attempt_count + 1
            session['current_problem']['total_attempts'] = total_attempts
    
            # 保存答题记录 - 使用更新后的累计尝试次数
            save_success, response_ids = save_user_response(
                user_id, template_id, paper_id, problem_text, user_answers,
                correct_answers, is_correct_list, total_attempts, time_taken, error_types, switch_count, selected_exam_id
            )
    
            if not save_success:
                logger.error("保存答题记录失败: user_id=%s template_id=%s", user_id, template_id)
                session['current_problem']['total_attempts'] = latest_attempt_count
                return jsonify(success=False, saved=False, message='答案未能保存，当前输入已保留，请稍后重新提交。'), 503
    
            # 生成正确答案消息
            correct_answer_message = "正确答案: "
            if answer_count == 1:
                correct_answer_message += f"{correct_answers[0]:.2f}"
            else:
                correct_parts = []
                for i, correct_answer in enumerate(correct_answers):
                    correct_parts.append(f"答案{i + 1} = {correct_answer:.2f}")
                correct_answer_message += ", ".join(correct_parts)
    
            # 更新会话状态
            new_problem_data = None
            response_data = {}
    
            if all_correct:
                # 回答正确后更新用户完成状态
                completed_all = update_user_completion_status(user_id, selected_exam_id)
    
                session['current_problem']['answered_correctly'] = True
                next_problem_id = problem_id + 1 if problem_id < total_problems else None
    
                # 构建成功消息
                message = f'🎉 回答正确！用时 {time_taken:.1f}秒，尝试 {total_attempts} 次。'
    
                response_data.update({
                    'correct': True,
                    'message': message,
                    'time_taken': time_taken,
                    'total_attempts': total_attempts,
                    'next_problem': next_problem_id,
                    'completed_all': completed_all if problem_id >= total_problems else False
                })
    
                # 如果完成所有题目，生成验证链接
                if completed_all and problem_id >= total_problems:
                    verification_url = f"/api/user/{user_id}/completion"
                    session['verification_url'] = verification_url
                    response_data['verification_url'] = verification_url
                    response_data['completion_message'] = '恭喜您完成了所有题目！'
            else:
                session['current_problem']['answered_correctly'] = False
                next_problem_id = problem_id
    
                # 答错时从 Redis 题目池抽取新题（不再限制尝试次数）
                new_token, new_problem_data = fetch_distinct_problem(actual_id, problem_data)
    
                if new_problem_data:
                    # 更新session中的题目数据和正确答案
                    session['current_problem']['token'] = new_token
                    session['current_problem']['start_time'] = time.time()
    
                    # 构建错误消息
                    message = f'❌ 答案不正确！用时 {time_taken:.1f}秒。{correct_answer_message}。已为您生成新题目，请重新作答。'
    
                    response_data.update({
                        'correct': False,
                        'message': message,
                        'correct_answers': correct_answers,
                        'total_attempts': total_attempts,
                        'next_problem': next_problem_id,
                        'new_problem_generated': True,
                        'new_var_values': new_problem_data['var_values'],
                        'new_display_var_values': new_problem_data.get('display_var_values', {}),
                        'new_correct_answers': new_problem_data['correct_answers'],
                        'new_problem_text': new_problem_data['problem_text'],
                        'token': new_token
                    })

                    logger.info("已从题目池获取新题: problem_id=%s actual_id=%s", problem_id, actual_id)
                    logger.debug(
                        "新题目详情: var_values=%s correct_answers=%s",
                        new_problem_data['var_values'],
                        new_problem_data['correct_answers']
                    )
                else:
                    # 题目生成失败
                    message = f'❌ 答案不正确！{correct_answer_message}。题目刷新失败，请重试。'
                    response_data.update({
                        'correct': False,
                        'message': message,
                        'correct_answers': correct_answers,
                        'total_attempts': total_attempts,
                        'next_problem': next_problem_id,
                        'new_problem_generated': False
                    })
    
            # 确保session被修改
            session.modified = True
    
            # 基础响应数据
            response_data.update({
                'success': True,
                'save_success': save_success,
                'response_ids': response_ids,
                'user_answers': user_answers,
                'answer_count': answer_count,
                'problem_id': problem_id,
                'actual_id': actual_id
            })
    
            logger.info(
                "提交处理完成: user_id=%s problem_id=%s actual_id=%s correct=%s total_attempts=%s",
                user_id,
                problem_id,
                actual_id,
                response_data.get('correct'),
                response_data.get('total_attempts'),
            )
            logger.debug("返回数据详情: %s", response_data)
            return jsonify(response_data)
    
        except ValueError as e:
            logger.error("数值转换错误: %s", e)
            saved = bool(locals().get('save_success'))
            return jsonify(success=False, saved=saved, message='答案已保存，但页面更新失败，请联系监考教师。' if saved else '请输入有效的数字格式'), 500 if saved else 400
        except KeyError as e:
            logger.error("缺少必要字段: %s", e)
            saved = bool(locals().get('save_success'))
            return jsonify(success=False, saved=saved, message='答案已保存，但页面更新失败，请联系监考教师。' if saved else '答题状态已失效，请联系监考教师协助恢复。'), 500 if saved else 400
        except Exception as e:
            logger.exception("服务器错误: %s", e)
            saved = bool(locals().get('save_success'))
            return jsonify(success=False, saved=saved,
                           message='答案已保存，但页面更新失败，请联系监考教师协助恢复。' if saved else '服务暂时异常，当前输入已保留，请稍后重试。'), 500

    @bp.route('/api/exam/secure-finish-summary', methods=['POST'])
    @login_required
    def secure_finish_summary():
        current_problem_state = session.get('current_problem') or {}
        exam_id = resolve_selected_exam_id(
            current_problem_state.get('exam_id') or session.get('selected_exam_id')
        )
        exam_access = get_user_exam_access(session['user_id'], exam_id)
        if not exam_access['allowed'] and exam_access.get('status') != 'ended':
            return jsonify({'success': False, 'message': exam_access['message']}), 403
        exam = get_exam_by_id(exam_id)
        if not is_seb_exam_required(exam):
            return jsonify({'success': False, 'message': '该考试未启用安全考试模式。'}), 400
        blocked = seb_guard(exam, api=True)
        if blocked:
            return blocked
        display_mapping, _, summary = secure_exam_progress(exam_id, exam_access['paper_id'])
        if current_problem_state.get('display_number') != len(display_mapping):
            return jsonify({'success': False, 'message': '请先进入最后一题页面再确认完成。'}), 400
        return jsonify({'success': True, **summary})

    @bp.route('/api/exam/secure-finish', methods=['POST'])
    @login_required
    def secure_finish():
        current_problem_state = session.get('current_problem') or {}
        exam_id = resolve_selected_exam_id(
            current_problem_state.get('exam_id') or session.get('selected_exam_id')
        )
        exam_access = get_user_exam_access(session['user_id'], exam_id)
        if not exam_access['allowed'] and exam_access.get('status') != 'ended':
            return jsonify({'success': False, 'message': exam_access['message']}), 403
        exam = get_exam_by_id(exam_id)
        if not is_seb_exam_required(exam):
            return jsonify({'success': False, 'message': '该考试未启用安全考试模式。'}), 400
        blocked = seb_guard(exam, api=True)
        if blocked:
            return blocked
        display_mapping, _, summary = secure_exam_progress(exam_id, exam_access['paper_id'])
        if current_problem_state.get('display_number') != len(display_mapping):
            return jsonify({'success': False, 'message': '请先进入最后一题页面再确认完成。'}), 400
        saved, summary = finish_secure_exam_session(
            session['user_id'],
            exam_id,
            exam_access.get('batch_id'),
            summary['completed_count'],
            summary['total_count'],
        )
        if not saved:
            return jsonify({'success': False, 'message': '完成状态保存失败，请重试。'}), 503
        session.pop('seb_entry_recorded_exam_id', None)
        session.pop('current_problem', None)
        session.modified = True
        return jsonify({
            'success': True,
            **summary,
            'quit_url': (url_for('exam.seb_shared_quit', token=get_shared_seb_settings()['seb_quit_token'], _external=True)
                         if exam.get('seb_config_mode') == 'shared' else
                         url_for('exam.seb_quit', token=exam['seb_quit_token'], _external=True)),
        })

    # ===== 极简点选兜底：学生手动纠正刚提交的错因粗类 =====
    @bp.route('/api/correct_error_category', methods=['POST'])
    def correct_error_category():
        """允许学生在答错后手动把该条记录的错因纠正为 5 大粗类之一（用于补全无法自动识别的盲区，如公式记错）。"""
        user_id = session.get('user_id')
        if not user_id:
            return jsonify({'success': False, 'message': '未登录'}), 401
        current_problem_state = session.get('current_problem') or {}
        exam_id = current_problem_state.get('exam_id') or session.get('selected_exam_id')
        exam = get_exam_by_id(exam_id)
        blocked = seb_guard(exam, api=True)
        if blocked:
            return blocked

        payload = request.get_json(silent=True) or {}
        response_ids = payload.get('response_ids') or []
        category = payload.get('error_category')
        if not isinstance(response_ids, list):
            response_ids = [response_ids]

        valid_categories = ('计算粗心', '概念混淆', '单位换算', '审题不清', '公式记错')
        if category not in valid_categories:
            return jsonify({'success': False, 'message': '无效的错因类别'}), 400
        if not response_ids:
            return jsonify({'success': False, 'message': '缺少记录标识'}), 400

        conn = get_db_connection()
        if not conn:
            return jsonify({'success': False, 'message': '数据库连接失败'}), 500
        try:
            cursor = conn.cursor()
            updated = 0
            for rid in response_ids:
                try:
                    rid = int(rid)
                except (TypeError, ValueError):
                    continue
                cursor.execute(
                    "UPDATE user_responses SET error_category = %s WHERE id = %s AND user_id = %s",
                    (category, rid, user_id))
                updated += cursor.rowcount
            conn.commit()
            logger.info("学生手动纠正错因: user_id=%s ids=%s -> %s (更新 %s 条)",
                        user_id, response_ids, category, updated)
            return jsonify({'success': True, 'updated': updated, 'error_category': category})
        except Exception as e:
            conn.rollback()
            logger.exception("纠正错因失败: %s", e)
            return jsonify({'success': False, 'message': f'服务器错误: {str(e)}'}), 500
        finally:
            if conn:
                conn.close()

    return bp
