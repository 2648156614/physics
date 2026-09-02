from flask import Blueprint


def create_exam_blueprint(deps):
    globals().update(deps)
    bp = Blueprint('exam', __name__)

    # 主应用路由
    @bp.route('/')
    def home():
        if 'user_id' in session:
            return redirect(url_for('dashboard'))
        return render_template('home.html')
    
    
    @bp.route('/dashboard')
    @login_required
    def dashboard():
        try:
            selected_exam_id = resolve_selected_exam_id(request.args.get('exam_id', type=int))
            available_exams = [] if session.get('username') == 'admin' else get_user_available_exams(session['user_id'])
            exam_access = get_user_exam_access(session['user_id'], selected_exam_id)
            available_papers = get_enabled_exam_papers()
            selected_paper_id = exam_access.get('paper_id') or resolve_selected_exam_paper_id()
            selected_paper = get_exam_paper_by_id(selected_paper_id) if selected_paper_id else None
            selected_exam = get_exam_by_id(selected_exam_id) if selected_exam_id else None
            has_available_papers = bool(available_papers)
    
            if not has_available_papers:
                flash('教师没有布置题目。', 'info')
            elif session.get('username') != 'admin' and not selected_paper_id:
                flash('当前暂无可用题库，请联系管理员开启试卷。', 'warning')
    
            display_mapping = get_problem_display_info(selected_paper_id) if selected_paper_id else {}
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
        """统计信息页面"""
        conn = None
        try:
            if 'user_id' not in session:
                flash('请先登录', 'warning')
                return redirect(url_for('auth.login'))
    
            conn = get_db_connection()
            if not conn:
                flash('数据库连接失败', 'danger')
                return redirect(url_for('dashboard'))
    
            cursor = conn.cursor(dictionary=True)
            selected_exam_id = resolve_selected_exam_id()
            selected_exam = get_exam_by_id(selected_exam_id) if selected_exam_id else None
            selected_paper_id = selected_exam['paper_id'] if selected_exam else resolve_selected_exam_paper_id()
            response_scope = "exam_id = %s" if selected_exam_id else "paper_id = %s"
            response_scope_param = selected_exam_id if selected_exam_id else selected_paper_id
    
            # 总体统计 - 添加错误处理
            cursor.execute(f"""
                WITH attempt_summary AS (
                    SELECT
                        template_id,
                        attempt_count,
                        COUNT(*) AS total_answers,
                        SUM(CASE WHEN is_correct THEN 1 ELSE 0 END) AS correct_answers,
                        MAX(time_taken) AS time_taken
                    FROM user_responses
                    WHERE user_id = %s AND {response_scope}
                    GROUP BY template_id, attempt_count
                )
                SELECT
                    COUNT(*) AS total_count,
                    SUM(CASE WHEN correct_answers = total_answers THEN 1 ELSE 0 END) AS correct_count,
                    AVG(time_taken) AS avg_time
                FROM attempt_summary
            """, (session['user_id'], response_scope_param))
    
            stats_result = cursor.fetchone()
    
            # 处理空数据的情况
            if not stats_result or stats_result['total_count'] is None:
                stats = {
                    'total_count': 0,
                    'correct_count': 0,
                    'avg_time': 0
                }
            else:
                # 转换Decimal类型为float
                stats = {
                    'total_count': int(stats_result['total_count']),
                    'correct_count': int(stats_result['correct_count'] or 0),
                    'avg_time': float(stats_result['avg_time'] or 0)
                }
    
            # 各题目统计 - 添加错误处理
            cursor.execute(f"""
                WITH attempt_summary AS (
                    SELECT
                        template_id,
                        attempt_count,
                        COUNT(*) AS total_answers,
                        SUM(CASE WHEN is_correct THEN 1 ELSE 0 END) AS correct_answers,
                        MAX(time_taken) AS time_taken
                    FROM user_responses
                    WHERE user_id = %s AND {response_scope}
                    GROUP BY template_id, attempt_count
                )
                SELECT
                    t.template_name,
                    COUNT(*) AS total,
                    SUM(CASE WHEN s.correct_answers = s.total_answers THEN 1 ELSE 0 END) AS correct,
                    AVG(s.time_taken) AS avg_time
                FROM attempt_summary s
                JOIN problem_templates t ON s.template_id = t.id
                GROUP BY s.template_id, t.template_name
                ORDER BY t.id
            """, (session['user_id'], response_scope_param))
    
            problem_stats_result = cursor.fetchall()
    
            # 转换problem_stats中的Decimal类型
            problem_stats = []
            for stat in problem_stats_result:
                problem_stats.append({
                    'template_name': stat['template_name'],
                    'total': int(stat['total']),
                    'correct': int(stat['correct'] or 0),
                    'avg_time': float(stat['avg_time'] or 0)
                })
    
            # 计算正确率
            accuracy = 0
            if stats['total_count'] > 0:
                accuracy = round(stats['correct_count'] / stats['total_count'] * 100, 1)
    
            print(
                f"[DEBUG] 统计数据: total_count={stats['total_count']}, correct_count={stats['correct_count']}, accuracy={accuracy}%")
            print(f"[DEBUG] 题目统计: {len(problem_stats)} 条记录")
    
            return render_template('stats.html',
                                   accuracy=accuracy,
                                   correct_count=stats['correct_count'],
                                   total_count=stats['total_count'],
                                   avg_time=round(stats['avg_time'], 1),
                                   problem_stats=problem_stats,
                                   username=session['username'],
                               selected_paper_id=selected_paper_id,
                               selected_exam_id=selected_exam_id)
    
        except Exception as e:
            print(f"[统计页面错误] {str(e)}")
            import traceback
            print(f"[详细错误] {traceback.format_exc()}")
            flash(f'获取统计信息失败: {str(e)}', 'danger')
            return redirect(url_for('dashboard'))
        finally:
            if conn and conn.is_connected():
                conn.close()
    
    
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
        """获取答题历史"""
        conn = None
        try:
            conn = get_db_connection()
            cursor = conn.cursor(dictionary=True)
            selected_exam_id = resolve_selected_exam_id()
            selected_exam = get_exam_by_id(selected_exam_id) if selected_exam_id else None
            selected_paper_id = selected_exam['paper_id'] if selected_exam else resolve_selected_exam_paper_id()
            response_filter = "r.exam_id = %s" if selected_exam_id else "COALESCE(r.paper_id, t.paper_id) = %s"
            response_filter_param = selected_exam_id if selected_exam_id else selected_paper_id
    
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
    
            # 根据显示序号获取实际ID
            paper_id = exam_access.get('paper_id') or resolve_selected_exam_paper_id()
            display_mapping = get_problem_display_info(paper_id)
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
        debug_request_logs = os.getenv('DEBUG_REQUEST_LOGS', '0').lower() in ('1', 'true', 'yes', 'on')
        if debug_request_logs:
            print(f"\n=== Ajax问题页面开始 ===")
            print(f"显示序号: {problem_id}")
        selected_exam_id = resolve_selected_exam_id(request.args.get('exam_id', type=int))
        exam_access = get_user_exam_access(session['user_id'], selected_exam_id)
        if not exam_access['allowed']:
            flash(exam_access['message'], 'warning')
            return redirect(url_for('dashboard'))
    
        # 根据显示序号获取实际ID
        paper_id = exam_access.get('paper_id') or resolve_selected_exam_paper_id()
        display_mapping = get_problem_display_info(paper_id)
        display_to_actual = {
            info['display_number']: actual_id
            for actual_id, info in display_mapping.items()
        }
        total_problems = len(display_mapping)
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
            session['current_problem']['user_id'] = session['user_id']
    
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
    
        is_completed = is_problem_completed(session['user_id'], actual_id, paper_id, selected_exam_id)
    
        return render_template('problem_ajax.html',
                               problem=problem_data,
                               problem_id=problem_id,  # 传递显示序号到模板
                               total_attempts=total_attempts,
                               answer_count=answer_count,
                               username=session['username'],
                               total_problems=total_problems,
                               display_mapping=display_mapping,
                               display_to_actual=display_to_actual,
                               is_completed=is_completed)
    
    
    @bp.route('/api/submit/<int:problem_id>', methods=['POST'])  # 保持参数名为 problem_id
    @login_required
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
    
            data = request.get_json()
            logger.info("提交答案: problem_id=%s", problem_id)
            logger.debug("提交数据详情: problem_id=%s payload=%s", problem_id, data)
    
            if not data:
                return jsonify({'success': False, 'message': '无效的请求数据'})
    
            # 根据显示序号获取实际ID
            paper_id = exam_access.get('paper_id') or resolve_selected_exam_paper_id()
            display_mapping = get_problem_display_info(paper_id)
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
            try:
                switch_count = int(data.get('switch_count', 0) or 0)
            except (TypeError, ValueError):
                switch_count = 0
            switch_count = max(0, min(switch_count, 10000))
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
                # 即使保存失败，也继续处理，但记录警告
                session['current_problem']['save_failed'] = True
    
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
    
                # 答错时生成新题目（不再限制尝试次数）
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
                        'new_correct_answers': new_problem_data['correct_answers'],
                        'new_problem_text': new_problem_data['problem_text'],
                        'token': new_token
                    })
    
                    logger.info("已生成新题目: problem_id=%s actual_id=%s", problem_id, actual_id)
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
            return jsonify({'success': False, 'message': '请输入有效的数字格式'})
        except KeyError as e:
            logger.error("缺少必要字段: %s", e)
            return jsonify({'success': False, 'message': f'缺少必要字段: {str(e)}'})
        except Exception as e:
            logger.exception("服务器错误: %s", e)
            return jsonify({'success': False, 'message': f'服务器错误: {str(e)}'})

    # ===== 极简点选兜底：学生手动纠正刚提交的错因粗类 =====
    @bp.route('/api/correct_error_category', methods=['POST'])
    def correct_error_category():
        """允许学生在答错后手动把该条记录的错因纠正为 5 大粗类之一（用于补全无法自动识别的盲区，如公式记错）。"""
        user_id = session.get('user_id')
        if not user_id:
            return jsonify({'success': False, 'message': '未登录'}), 401

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
