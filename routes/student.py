from flask import Blueprint


def create_student_blueprint(deps):
    globals().update(deps)
    bp = Blueprint('student', __name__)

    @bp.route('/api/user/<int:user_id>/completion')
    def check_completion(user_id):
        """检查用户完成状态 - 公开API"""
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
    
        cursor.execute("""
            SELECT 
                u.username,
                u.completed_all,
                u.completed_at,
                u.total_score,
                u.total_time,
                COUNT(DISTINCT r.template_id) as completed_problems,
                MAX(r.response_time) as last_completion_time
            FROM users u
            LEFT JOIN user_responses r ON u.id = r.user_id AND r.is_correct = TRUE
            WHERE u.id = %s
            GROUP BY u.id
        """, (user_id,))
    
        result = cursor.fetchone()
        cursor.close()
        conn.close()
    
        if result:
            return jsonify({
                'user_id': user_id,
                'username': result['username'],
                'completed_all': result['completed_all'],
                'completed_problems': result['completed_problems'],
                'total_score': result['total_score'],
                'total_time': result['total_time'],
                'completed_at': result['completed_at'],
                'last_completion_time': result['last_completion_time'],
                'verified': True
            })
        else:
            return jsonify({
                'user_id': user_id,
                'error': '用户不存在',
                'verified': False
            }), 404
    
    
    # 管理员功能 - 学生完成情况
    @bp.route('/admin/students/<status>')
    @login_required
    def admin_students_by_status(status):
        """按完成状态查看学生列表"""
        if session.get('username') != 'admin':
            flash('权限不足', 'danger')
            return redirect(url_for('dashboard'))
    
        selected_paper_id = request.args.get('paper_id', type=int)
        keyword = (request.args.get('keyword') or '').strip()
        class_name = (request.args.get('class_name') or '').strip()
        major = (request.args.get('major') or '').strip()
        teacher_name = (request.args.get('teacher_name') or '').strip()
        has_filter = bool(keyword or class_name or major or teacher_name)
        completed = (status == 'completed')
        student_filters = {
            'keyword': keyword,
            'class_name': class_name,
            'major': major,
            'teacher_name': teacher_name,
        }
        students = (
            get_students_by_completion(completed=completed, paper_id=selected_paper_id, filters=student_filters)
            if has_filter else []
        )
        total_problems = get_total_problem_count(selected_paper_id)
        completion_stats = get_completion_stats(selected_paper_id)
        class_stats = get_class_comparison_stats(selected_paper_id)
        total_students = completion_stats['stats']['total_students'] or 0
        completed_students = completion_stats['stats']['completed_count'] or 0
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        try:
            cursor.execute("""
                SELECT DISTINCT TRIM(class_name) AS value
                FROM users
                WHERE username != 'admin' AND class_name IS NOT NULL AND TRIM(class_name) != ''
                ORDER BY value
            """)
            class_options = [row['value'] for row in cursor.fetchall()]
            cursor.execute("""
                SELECT DISTINCT TRIM(major) AS value
                FROM users
                WHERE username != 'admin' AND major IS NOT NULL AND TRIM(major) != ''
                ORDER BY value
            """)
            major_options = [row['value'] for row in cursor.fetchall()]
            cursor.execute("""
                SELECT DISTINCT TRIM(teacher_name) AS value
                FROM users
                WHERE username != 'admin' AND teacher_name IS NOT NULL AND TRIM(teacher_name) != ''
                ORDER BY value
            """)
            teacher_options = [row['value'] for row in cursor.fetchall()]
        finally:
            cursor.close()
            conn.close()
    
        status_text = '已完成' if completed else '未完成'
    
        return render_template('admin_students.html',
                               students=students,
                               status=status,
                               status_text=status_text,
                               selected_paper_id=selected_paper_id,
                               keyword=keyword,
                               class_name=class_name,
                               major=major,
                               teacher_name=teacher_name,
                               has_filter=has_filter,
                               class_options=class_options,
                               major_options=major_options,
                               teacher_options=teacher_options,
                               total_problems=total_problems,
                               total_students=total_students,
                               completed_students=completed_students,
                               class_stats=class_stats,
                               username=session['username'])
    
    
    @bp.route('/admin/users')
    @login_required
    def admin_user_management():
        """管理员用户管理页面：仅展示基础信息。"""
        if session.get('username') != 'admin':
            flash('权限不足', 'danger')
            return redirect(url_for('dashboard'))
    
        keyword = (request.args.get('keyword') or '').strip()
        class_name = (request.args.get('class_name') or '').strip()
        major = (request.args.get('major') or '').strip()
        has_user_filter = bool(keyword or class_name or major)
    
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        try:
            users = []
            if has_user_filter:
                query = """
                    SELECT id, username, name, major, class_name, teacher_name, exam_start_time, exam_end_time, created_at
                    FROM users
                    WHERE username != 'admin'
                """
                params = []

                if keyword:
                    query += " AND (username LIKE %s OR name LIKE %s)"
                    like = f"%{keyword}%"
                    params.extend([like, like])
                if class_name:
                    query += " AND class_name LIKE %s"
                    params.append(f"%{class_name}%")
                if major:
                    query += " AND major LIKE %s"
                    params.append(f"%{major}%")

                query += " ORDER BY created_at DESC, id DESC LIMIT 500"
                cursor.execute(query, params)
                users = cursor.fetchall()
    
            cursor.execute(
                """
                SELECT TRIM(teacher_name) AS name, COUNT(*) AS student_count
                FROM users
                WHERE username != 'admin'
                  AND teacher_name IS NOT NULL
                  AND TRIM(teacher_name) != ''
                GROUP BY TRIM(teacher_name)
                ORDER BY name
                """
            )
            teacher_groups = cursor.fetchall()
    
            cursor.execute(
                """
                SELECT TRIM(class_name) AS name, COUNT(*) AS student_count
                FROM users
                WHERE username != 'admin'
                  AND class_name IS NOT NULL
                  AND TRIM(class_name) != ''
                GROUP BY TRIM(class_name)
                ORDER BY name
                """
            )
            class_groups = cursor.fetchall()
    
            cursor.execute(
                """
                SELECT TRIM(major) AS name, COUNT(*) AS student_count
                FROM users
                WHERE username != 'admin'
                  AND major IS NOT NULL
                  AND TRIM(major) != ''
                GROUP BY TRIM(major)
                ORDER BY name
                """
            )
            major_groups = cursor.fetchall()
        finally:
            cursor.close()
            conn.close()
    
        return render_template(
            'admin_user_management.html',
            users=users,
            keyword=keyword,
            class_name=class_name,
            major=major,
            has_user_filter=has_user_filter,
            teacher_groups=teacher_groups,
            class_groups=class_groups,
            major_groups=major_groups
        )
    
    
    @bp.route('/admin/users/add', methods=['POST'])
    @login_required
    def admin_add_user():
        if session.get('username') != 'admin':
            flash('权限不足', 'danger')
            return redirect(url_for('dashboard'))
    
        username = (request.form.get('username') or '').strip()
        name = (request.form.get('name') or '').strip()
        major = (request.form.get('major') or '').strip()
        class_name = (request.form.get('class_name') or '').strip()
        teacher_name = (request.form.get('teacher_name') or '').strip()
        exam_start_time_raw = (request.form.get('exam_start_time') or '').strip()
        exam_end_time_raw = (request.form.get('exam_end_time') or '').strip()
    
        if not username or not name:
            flash('学号和姓名不能为空', 'danger')
            return redirect(url_for('admin_user_management'))
    
        try:
            exam_start_time = parse_exam_time(exam_start_time_raw)
            exam_end_time = parse_exam_time(exam_end_time_raw)
        except ValueError as err:
            flash(str(err), 'danger')
            return redirect(url_for('admin_user_management'))
        if exam_start_time and exam_end_time and exam_end_time <= exam_start_time:
            flash('结束时间必须晚于开始时间。', 'danger')
            return redirect(url_for('admin_user_management'))
    
        initial_password = build_initial_password(username)
        password_hash = generate_password_hash(initial_password)
    
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT id FROM users WHERE username = %s", (username,))
            if cursor.fetchone():
                flash('该学号已存在', 'danger')
                return redirect(url_for('admin_user_management'))
    
            cursor.execute(
                """
                INSERT INTO users (username, name, major, class_name, teacher_name, exam_start_time, exam_end_time, password, password_changed, avatar_filename)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, FALSE, %s)
                """,
                (username, name, major or None, class_name or None, teacher_name or None, exam_start_time, exam_end_time, password_hash, DEFAULT_AVATAR)
            )
            conn.commit()
            flash(f'账户已添加，初始密码：{initial_password}', 'success')
        except Exception as e:
            conn.rollback()
            flash(f'添加失败：{e}', 'danger')
        finally:
            cursor.close()
            conn.close()
    
        return redirect(url_for('admin_user_management'))
    
    
    @bp.route('/admin/users/<int:user_id>/edit', methods=['POST'])
    @login_required
    def admin_edit_user(user_id):
        if session.get('username') != 'admin':
            flash('权限不足', 'danger')
            return redirect(url_for('dashboard'))
    
        name = (request.form.get('name') or '').strip()
        major = (request.form.get('major') or '').strip()
        class_name = (request.form.get('class_name') or '').strip()
        username = (request.form.get('username') or '').strip()
        teacher_name = (request.form.get('teacher_name') or '').strip()
        exam_start_time_raw = (request.form.get('exam_start_time') or '').strip()
        exam_end_time_raw = (request.form.get('exam_end_time') or '').strip()
    
        if not username or not name:
            flash('学号和姓名不能为空', 'danger')
            return redirect(url_for('admin_user_management'))
    
        try:
            exam_start_time = parse_exam_time(exam_start_time_raw)
            exam_end_time = parse_exam_time(exam_end_time_raw)
        except ValueError as err:
            flash(str(err), 'danger')
            return redirect(url_for('admin_user_management'))
        if exam_start_time and exam_end_time and exam_end_time <= exam_start_time:
            flash('结束时间必须晚于开始时间。', 'danger')
            return redirect(url_for('admin_user_management'))
    
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                UPDATE users
                SET username = %s, name = %s, major = %s, class_name = %s, teacher_name = %s,
                    exam_start_time = %s, exam_end_time = %s
                WHERE id = %s AND username != 'admin'
                """,
                (username, name, major or None, class_name or None, teacher_name or None, exam_start_time, exam_end_time, user_id)
            )
            conn.commit()
            flash('用户信息更新成功', 'success')
        except mysql.connector.Error as err:
            conn.rollback()
            flash(f'更新失败：{err}', 'danger')
        finally:
            cursor.close()
            conn.close()
    
        return redirect(url_for('admin_user_management'))


    @bp.route('/admin/users/<int:user_id>/reset_password', methods=['POST'])
    @login_required
    def admin_reset_user_password(user_id):
        if session.get('username') != 'admin':
            flash('权限不足', 'danger')
            return redirect(url_for('dashboard'))

        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        try:
            cursor.execute("SELECT username, name FROM users WHERE id = %s AND username != 'admin'", (user_id,))
            user = cursor.fetchone()
            if not user:
                flash('用户不存在或不能重置管理员密码', 'danger')
                return redirect(request.referrer or url_for('admin_user_management'))

            initial_password = build_initial_password(user['username'])
            password_hash = generate_password_hash(initial_password)
            cursor.execute(
                """
                UPDATE users
                SET password = %s,
                    password_changed = FALSE,
                    failed_login_attempts = 0,
                    login_locked_until = NULL,
                    current_session_token = NULL
                WHERE id = %s AND username != 'admin'
                """,
                (password_hash, user_id)
            )
            conn.commit()
            display_name = user.get('name') or user['username']
            flash(f'{display_name} 的密码已重置为：{initial_password}', 'success')
        except Exception as e:
            conn.rollback()
            flash(f'重置密码失败：{e}', 'danger')
        finally:
            cursor.close()
            conn.close()

        return redirect(request.referrer or url_for('admin_user_management'))
    
    
    @bp.route('/admin/users/batch_update', methods=['POST'])
    @login_required
    def admin_batch_update_users():
        if session.get('username') != 'admin':
            flash('权限不足', 'danger')
            return redirect(url_for('dashboard'))
    
        filter_class_name = (request.form.get('filter_class_name') or '').strip()
        filter_major = (request.form.get('filter_major') or '').strip()
        new_teacher_name = (request.form.get('teacher_name') or '').strip()
        exam_start_time_raw = (request.form.get('exam_start_time') or '').strip()
        exam_end_time_raw = (request.form.get('exam_end_time') or '').strip()
        update_exam_time = request.form.get('update_exam_time') == '1'
    
        if not filter_class_name and not filter_major:
            flash('请至少填写一个筛选条件：班级或专业。', 'danger')
            return redirect(url_for('admin_user_management'))
        if not new_teacher_name and not update_exam_time:
            flash('请填写要设置的课程号，或勾选批量设置考试时间。', 'danger')
            return redirect(url_for('admin_user_management'))
    
        try:
            exam_start_time = parse_exam_time(exam_start_time_raw)
            exam_end_time = parse_exam_time(exam_end_time_raw)
        except ValueError as err:
            flash(str(err), 'danger')
            return redirect(url_for('admin_user_management'))
        if update_exam_time and exam_start_time and exam_end_time and exam_end_time <= exam_start_time:
            flash('结束时间必须晚于开始时间。', 'danger')
            return redirect(url_for('admin_user_management'))
    
        set_clauses = []
        params = []
        if new_teacher_name:
            set_clauses.append("teacher_name = %s")
            params.append(new_teacher_name)
        if update_exam_time:
            set_clauses.append("exam_start_time = %s")
            set_clauses.append("exam_end_time = %s")
            params.extend([exam_start_time, exam_end_time])
    
        where_clauses = ["username != 'admin'"]
        if filter_class_name:
            where_clauses.append("TRIM(COALESCE(class_name, '')) = %s")
            params.append(filter_class_name)
        if filter_major:
            where_clauses.append("TRIM(COALESCE(major, '')) = %s")
            params.append(filter_major)
    
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            sql = f"""
                UPDATE users
                SET {', '.join(set_clauses)}
                WHERE {' AND '.join(where_clauses)}
            """
            cursor.execute(sql, params)
            affected = cursor.rowcount
            conn.commit()
            if affected:
                flash(f'批量修改完成：已更新 {affected} 名学生。', 'success')
            else:
                flash('没有找到符合条件的学生。', 'warning')
        except Exception as err:
            conn.rollback()
            flash(f'批量修改失败：{err}', 'danger')
        finally:
            cursor.close()
            conn.close()
    
        return redirect(url_for('admin_user_management'))
    
    
    @bp.route('/admin/users/batch_delete', methods=['POST'])
    @login_required
    def admin_batch_delete_users():
        if session.get('username') != 'admin':
            flash('权限不足', 'danger')
            return redirect(url_for('dashboard'))
    
        delete_by = (request.form.get('delete_by') or '').strip()
        delete_value = (request.form.get('delete_value') or '').strip()
        allowed_fields = {
            'teacher_name': ('teacher_name', '课程号'),
            'class_name': ('class_name', '班级')
        }
    
        if delete_by not in allowed_fields:
            flash('请选择按课程号或班级删除。', 'danger')
            return redirect(url_for('admin_user_management'))
        if not delete_value:
            flash('请填写要删除的课程号或班级名称。', 'danger')
            return redirect(url_for('admin_user_management'))
    
        column_name, label = allowed_fields[delete_by]
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        try:
            cursor.execute(
                f"""
                SELECT id, username
                FROM users
                WHERE username != 'admin'
                  AND TRIM(COALESCE({column_name}, '')) = %s
                """,
                (delete_value,)
            )
            matched_users = cursor.fetchall()
            user_ids = [user['id'] for user in matched_users]
            if not user_ids:
                flash(f'没有找到{label}为“{delete_value}”的学生。', 'warning')
                return redirect(url_for('admin_user_management'))
    
            placeholders = ', '.join(['%s'] * len(user_ids))
            cursor.execute(f"DELETE FROM user_responses WHERE user_id IN ({placeholders})", user_ids)
            cursor.execute(f"DELETE FROM verification_records WHERE user_id IN ({placeholders})", user_ids)
            cursor.execute(f"DELETE FROM user_login_audit_logs WHERE user_id IN ({placeholders})", user_ids)
            cursor.execute(f"DELETE FROM users WHERE id IN ({placeholders}) AND username != 'admin'", user_ids)
            deleted_count = cursor.rowcount
            conn.commit()
            flash(f'已删除{label}为“{delete_value}”的学生 {deleted_count} 人，并清理相关答题记录。', 'success')
        except Exception as e:
            conn.rollback()
            flash(f'批量删除失败：{e}', 'danger')
        finally:
            cursor.close()
            conn.close()
    
        return redirect(url_for('admin_user_management'))
    
    
    @bp.route('/admin/users/<int:user_id>/delete', methods=['POST'])
    @login_required
    def admin_delete_user(user_id):
        if session.get('username') != 'admin':
            flash('权限不足', 'danger')
            return redirect(url_for('dashboard'))
    
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("DELETE FROM user_responses WHERE user_id = %s", (user_id,))
            cursor.execute("DELETE FROM verification_records WHERE user_id = %s", (user_id,))
            cursor.execute("DELETE FROM user_login_audit_logs WHERE user_id = %s", (user_id,))
            cursor.execute("DELETE FROM users WHERE id = %s AND username != 'admin'", (user_id,))
            conn.commit()
            flash('账户已删除', 'success')
        except Exception as e:
            conn.rollback()
            flash(f'删除失败：{e}', 'danger')
        finally:
            cursor.close()
            conn.close()
    
        return redirect(url_for('admin_user_management'))
    
    
    # 管理员功能 - 学生详细答题情况
    # infer_knowledge_label 已提升至 app.py 模块级（见 globals() 注入），此处直接复用

    def build_student_insight_summary(problem_stats, knowledge_stats, error_type_stats):
        """生成学生表现自动结论。"""
        solved = [stat for stat in problem_stats if stat.get('is_completed')]
        total = len(problem_stats)
        solved_count = len(solved)
    
        summary = []
        if total:
            summary.append(f'共完成 {solved_count}/{total} 道题，整体完成率 {round(solved_count / total * 100, 1)}%。')
    
        comparable = [item for item in knowledge_stats if item.get('attempted_templates')]
        if comparable:
            def normalized_avg_attempts(item, default_value):
                value = item.get('avg_attempts')
                return value if isinstance(value, (int, float)) else default_value
    
            best = max(
                comparable,
                key=lambda item: (
                    item.get('correct_rate', 0),
                    normalized_avg_attempts(item, 999) * -1,
                ),
            )
            weakest = min(
                comparable,
                key=lambda item: (
                    item.get('correct_rate', 0),
                    -normalized_avg_attempts(item, 0),
                ),
            )
            if best.get('label') == weakest.get('label'):
                summary.append(f"当前题库主要集中在{best['label']}，该知识点正确率为 {best.get('correct_rate', 0):.1f}%。")
            else:
                summary.append(f"你在 {best['label']} 题上表现最好，正确率 {best.get('correct_rate', 0):.1f}%；{weakest['label']} 仍是当前薄弱点，正确率 {weakest.get('correct_rate', 0):.1f}%。")
    
        top_error = next((item for item in error_type_stats if item.get('error_type') and item.get('error_type') != '正确'), None)
        if top_error and top_error.get('count'):
            summary.append(f"最近错题主要集中在“{top_error['error_type']}”类型，共出现 {top_error['count']} 次，建议优先复盘对应步骤。")
    
        if not summary:
            summary.append('当前作答数据较少，继续完成更多题目后可生成更稳定的学习结论。')
    
        return ' '.join(summary)
    
    @bp.route('/admin/student/<int:user_id>/details')
    @login_required
    def admin_student_details(user_id):
        """查看学生详细答题情况"""
        if session.get('username') != 'admin':
            flash('权限不足', 'danger')
            return redirect(url_for('dashboard'))
    
        selected_paper_id = resolve_selected_exam_paper_id(request.args.get('paper_id', type=int))
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
    
        try:
            # 获取学生基本信息
            cursor.execute(
                "SELECT username, name, class_name, completed_all, total_score, total_time FROM users WHERE id = %s",
                (user_id,)
            )
            student = cursor.fetchone()
    
            if not student:
                flash('学生不存在', 'danger')
                return redirect(url_for('admin_dashboard'))
    
            # 获取题目总数
            total_problems = get_total_problem_count(selected_paper_id)
            problem_template_filter, problem_template_params = build_enabled_paper_filter('t', selected_paper_id)
            response_filter, response_params = build_enabled_paper_filter('ur', selected_paper_id)
    
            # 每题作答状态：是否答对；答对时展示达到答对所需次数与累计时长
            cursor.execute(f"""
                WITH attempt_summary AS (
                    SELECT
                        template_id,
                        attempt_count,
                        COUNT(*) AS total_answers,
                        SUM(CASE WHEN is_correct THEN 1 ELSE 0 END) AS correct_answers,
                        MAX(time_taken) AS time_taken,
                        MAX(response_time) AS last_response_time,
                        CASE WHEN SUM(CASE WHEN is_correct THEN 1 ELSE 0 END) = COUNT(*) THEN 1 ELSE 0 END AS is_fully_correct
                    FROM user_responses ur
                    WHERE user_id = %s {response_filter}
                    GROUP BY template_id, attempt_count
                ),
                progress AS (
                    SELECT
                        template_id,
                        COUNT(*) AS total_attempts,
                        SUM(is_fully_correct) AS correct_attempts,
                        SUM(time_taken) AS total_time_spent,
                        MAX(last_response_time) AS last_attempt_time,
                        MAX(is_fully_correct) AS is_completed,
                        MIN(CASE WHEN is_fully_correct = 1 THEN attempt_count ELSE NULL END) AS attempts_to_correct
                    FROM attempt_summary
                    GROUP BY template_id
                )
                SELECT
                    t.id AS template_id,
                    t.template_name,
                    COALESCE(p.total_attempts, 0) AS total_attempts,
                    COALESCE(p.correct_attempts, 0) AS correct_attempts,
                    COALESCE(p.total_time_spent, 0) AS total_time_spent,
                    p.last_attempt_time,
                    COALESCE(p.is_completed, 0) AS is_completed,
                    p.attempts_to_correct,
                    (
                        SELECT SUM(a2.time_taken)
                        FROM attempt_summary a2
                        WHERE a2.template_id = t.id
                          AND p.attempts_to_correct IS NOT NULL
                          AND a2.attempt_count <= p.attempts_to_correct
                    ) AS cumulative_time_to_correct
                FROM problem_templates t
                LEFT JOIN progress p ON t.id = p.template_id
                WHERE 1 = 1 {problem_template_filter}
                ORDER BY t.id
            """, [user_id] + response_params + problem_template_params)
    
            problem_stats = cursor.fetchall()
            for stat in problem_stats:
                stat['knowledge_label'] = infer_knowledge_label(stat.get('template_name'))
                stat['correct_rate'] = 100.0 if stat.get('is_completed') else 0.0
            completed_problems_count = sum(1 for stat in problem_stats if stat.get('is_completed'))
    
            # 计算总体统计
            cursor.execute(f"""
                WITH attempt_summary AS (
                    SELECT
                        template_id,
                        attempt_count,
                        COUNT(*) AS total_answers,
                        SUM(CASE WHEN is_correct THEN 1 ELSE 0 END) AS correct_answers,
                        MAX(time_taken) AS time_taken
                    FROM user_responses ur
                    WHERE user_id = %s {response_filter}
                    GROUP BY template_id, attempt_count
                )
                SELECT
                    COUNT(*) as total_attempts,
                    SUM(CASE WHEN correct_answers = total_answers THEN 1 ELSE 0 END) as total_correct,
                    AVG(time_taken) as overall_avg_time
                FROM attempt_summary
            """, [user_id] + response_params)
    
            overall_stats = cursor.fetchone() or {}
            overall_stats.setdefault('total_attempts', 0)
            overall_stats.setdefault('total_correct', 0)
            overall_stats.setdefault('overall_avg_time', 0)
            if isinstance(overall_stats.get('overall_avg_time'), Decimal):
                overall_stats['overall_avg_time'] = float(overall_stats['overall_avg_time'])
    
            trend_points = []
            cumulative_correct = 0
            for index, stat in enumerate(problem_stats, start=1):
                cumulative_correct += 1 if stat.get('is_completed') else 0
                trend_points.append({
                    'label': f"第{get_display_number(stat.get('template_id'), selected_paper_id)}题",
                    'short_label': str(get_display_number(stat.get('template_id'), selected_paper_id)),
                    'correct_rate': round((cumulative_correct / index) * 100, 1),
                    'attempts': stat.get('total_attempts') or 0,
                    'completed': bool(stat.get('is_completed'))
                })
    
            knowledge_map = {}
            for stat in problem_stats:
                label = stat['knowledge_label']
                bucket = knowledge_map.setdefault(label, {
                    'label': label,
                    'total_templates': 0,
                    'attempted_templates': 0,
                    'completed_templates': 0,
                    'total_attempts': 0,
                    'sum_attempts_to_correct': 0,
                    'attempts_to_correct_count': 0,
                })
                bucket['total_templates'] += 1
                if (stat.get('total_attempts') or 0) > 0:
                    bucket['attempted_templates'] += 1
                if stat.get('is_completed'):
                    bucket['completed_templates'] += 1
                bucket['total_attempts'] += stat.get('total_attempts') or 0
                if stat.get('attempts_to_correct'):
                    bucket['sum_attempts_to_correct'] += stat['attempts_to_correct']
                    bucket['attempts_to_correct_count'] += 1
    
            knowledge_stats = []
            for item in knowledge_map.values():
                attempted = item['attempted_templates']
                completed = item['completed_templates']
                item['correct_rate'] = round((completed / attempted) * 100, 1) if attempted else 0
                item['avg_attempts'] = round(item['sum_attempts_to_correct'] / item['attempts_to_correct_count'], 1) if item['attempts_to_correct_count'] else None
                knowledge_stats.append(item)
            knowledge_stats.sort(key=lambda item: (-item['correct_rate'], -item['completed_templates'], item['label']))
    
            cursor.execute(f"""
                SELECT COALESCE(NULLIF(TRIM(error_type), ''), '未知') AS error_type, COUNT(*) AS count
                FROM user_responses ur
                WHERE user_id = %s AND is_correct = FALSE {response_filter}
                GROUP BY COALESCE(NULLIF(TRIM(error_type), ''), '未知')
                ORDER BY count DESC, error_type ASC
            """, [user_id] + response_params)
            error_type_stats = cursor.fetchall()
    
            cursor.execute(f"""
                SELECT
                    t.id AS template_id,
                    t.template_name,
                    COUNT(*) AS wrong_count,
                    MAX(ur.response_time) AS last_wrong_time,
                    SUM(CASE WHEN ur.error_type = '计算误差' THEN 1 ELSE 0 END) AS calc_error_count,
                    SUM(CASE WHEN ur.error_type = '精度或单位偏差' THEN 1 ELSE 0 END) AS unit_error_count,
                    SUM(CASE WHEN ur.error_type = '格式错误' THEN 1 ELSE 0 END) AS format_error_count
                FROM user_responses ur
                JOIN problem_templates t ON t.id = ur.template_id
                WHERE ur.user_id = %s AND ur.is_correct = FALSE {response_filter}
                GROUP BY t.id, t.template_name
                ORDER BY wrong_count DESC, t.id ASC
                LIMIT 6
            """, [user_id] + response_params)
            wrong_problem_stats = cursor.fetchall()
            for item in wrong_problem_stats:
                item['knowledge_label'] = infer_knowledge_label(item.get('template_name'))
    
            insight_summary = build_student_insight_summary(problem_stats, knowledge_stats, error_type_stats)
    
            return render_template('admin_student_details.html',
                                   student=student,
                                   problem_stats=problem_stats,
                                   overall_stats=overall_stats,
                                   completed_problems_count=completed_problems_count,
                                   total_problems=total_problems,
                                   trend_points=trend_points,
                                   knowledge_stats=knowledge_stats,
                                   error_type_stats=error_type_stats,
                                   wrong_problem_stats=wrong_problem_stats,
                                   insight_summary=insight_summary,
                                   username=session['username'],
                                   get_display_number=get_display_number,
                                   selected_paper_id=selected_paper_id)  # 传递函数到模板
        except Exception as e:
            print(f"获取学生详情失败: {str(e)}")
            import traceback
            print(f"详细错误: {traceback.format_exc()}")
            flash('获取学生详情失败', 'danger')
            return redirect(url_for('admin_dashboard'))
        finally:
            cursor.close()
            conn.close()
    
    
    # 学生端：我的学情画像（纯本地规则闭环）
    @bp.route('/student/profile')
    @login_required
    def student_profile():
        """展示当前学生的学情画像：错因雷达 + 薄弱知识点 + 最近作答。"""
        user_id = session.get('user_id')
        paper_id = request.args.get('paper_id', type=int)
        profile = get_student_profile(user_id, paper_id)

        if not profile or profile['total'] == 0:
            return render_template('student_profile.html',
                                   empty=True,
                                   username=session.get('username'),
                                   paper_id=paper_id)

        # 雷达图数据：按固定 5 类顺序取计数
        counts = [profile['category_dist'].get(cat, 0) for cat in ERROR_CATEGORY_ORDER]
        radar_svg = build_radar_svg(ERROR_CATEGORY_ORDER, counts)

        return render_template('student_profile.html',
                               empty=False,
                               profile=profile,
                               radar_svg=radar_svg,
                               category_order=ERROR_CATEGORY_ORDER,
                               total=profile['total'],
                               correct=profile['correct'],
                               wrong=profile['wrong'],
                               accuracy=profile['accuracy'],
                               username=session.get('username'),
                               paper_id=paper_id)


    # 教师端：班级学情看板（纯本地规则闭环）
    @bp.route('/admin/analytics')
    @login_required
    def admin_analytics():
        """展示班级整体学情：错因分布 + 薄弱知识点 + 学生正确率排行。"""
        if session.get('username') != 'admin':
            flash('权限不足', 'danger')
            return redirect(url_for('dashboard'))

        paper_id = request.args.get('paper_id', type=int)
        data = get_class_analytics(paper_id)
        if not data or not data.get('students'):
            flash('暂时没有学情数据，先让学生作答几道题吧', 'info')
            return redirect(url_for('admin_dashboard'))

        counts = [next((c['cnt'] for c in data['category_totals'] if c['category'] == cat), 0)
                  for cat in ERROR_CATEGORY_ORDER]
        radar_svg = build_radar_svg(ERROR_CATEGORY_ORDER, counts)

        return render_template('admin_analytics.html',
                               data=data,
                               radar_svg=radar_svg,
                               category_order=ERROR_CATEGORY_ORDER,
                               username=session.get('username'),
                               paper_id=paper_id)


    # 管理员功能 - 所有学生题目答题情况
    @bp.route('/admin/all_problems_stats')
    @login_required
    def admin_all_problems_stats():
        """查看所有学生对每道题的答题情况"""
        if session.get('username') != 'admin':
            flash('权限不足', 'danger')
            return redirect(url_for('dashboard'))
    
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
    
        try:
            selected_paper_id = resolve_selected_exam_paper_id(request.args.get('paper_id', type=int))
            selected_class_name = (request.args.get('class_name') or '').strip()
            selected_major = (request.args.get('major') or '').strip()
            problem_filter, problem_filter_params = build_enabled_paper_filter('t', selected_paper_id)
            response_filter, response_params = build_enabled_paper_filter('ur', selected_paper_id)
            response_filter_ur2, response_params_ur2 = build_enabled_paper_filter('ur2', selected_paper_id)
    
            # 加载班级筛选选项
            cursor.execute("""
                SELECT DISTINCT COALESCE(NULLIF(TRIM(class_name), ''), '未分班') AS class_name
                FROM users
                WHERE username != 'admin'
                ORDER BY class_name
            """)
            class_options = [row['class_name'] for row in cursor.fetchall()]
    
            cursor.execute("""
                SELECT DISTINCT COALESCE(NULLIF(TRIM(major), ''), '未设置专业') AS major
                FROM users
                WHERE username != 'admin'
                ORDER BY major
            """)
            major_options = [row['major'] for row in cursor.fetchall()]
    
            filters = ["u.username != 'admin'"]
            params = []
    
            if selected_class_name:
                if selected_class_name == '未分班':
                    filters.append("(u.class_name IS NULL OR TRIM(u.class_name) = '')")
                else:
                    filters.append("u.class_name = %s")
                    params.append(selected_class_name)
    
            if selected_major:
                if selected_major == '未设置专业':
                    filters.append("(u.major IS NULL OR TRIM(u.major) = '')")
                else:
                    filters.append("u.major = %s")
                    params.append(selected_major)
    
            filter_clause = ' AND '.join(filters)
    
            # 题目层级统计（首次正确率、总答题次数、最终答对人数、平均正确时长）
            cursor.execute(f"""
                WITH attempt_summary AS (
                    SELECT
                        ur.user_id,
                        ur.template_id,
                        ur.attempt_count,
                        COUNT(*) AS total_answers,
                        SUM(CASE WHEN ur.is_correct THEN 1 ELSE 0 END) AS correct_answers,
                        MAX(ur.time_taken) AS time_taken,
                        CASE WHEN SUM(CASE WHEN ur.is_correct THEN 1 ELSE 0 END) = COUNT(*) THEN 1 ELSE 0 END AS is_fully_correct
                    FROM user_responses ur
                    JOIN users u ON ur.user_id = u.id
                    WHERE {filter_clause} {response_filter}
                    GROUP BY ur.user_id, ur.template_id, ur.attempt_count
                ),
                user_problem_stats AS (
                    SELECT
                        user_id,
                        template_id,
                        COUNT(*) AS attempt_count,
                        MIN(attempt_count) AS first_attempt_no,
                        MIN(CASE WHEN is_fully_correct = 1 THEN attempt_count ELSE NULL END) AS first_correct_attempt_no,
                        MAX(is_fully_correct) AS final_is_correct
                    FROM attempt_summary
                    GROUP BY user_id, template_id
                )
                SELECT
                    t.id as template_id,
                    t.template_name,
                    COUNT(DISTINCT ups.user_id) as participant_students,
                    COALESCE(SUM(ups.attempt_count), 0) as total_attempts,
                    SUM(CASE WHEN first_attempt.is_fully_correct = 1 THEN 1 ELSE 0 END) as first_correct_students,
                    SUM(CASE WHEN ups.final_is_correct = 1 THEN 1 ELSE 0 END) as final_correct_students,
                    AVG(CASE
                        WHEN ups.first_correct_attempt_no IS NOT NULL AND correct_attempt.attempt_count = ups.first_correct_attempt_no
                        THEN correct_attempt.time_taken
                        ELSE NULL
                    END) as avg_correct_time
                FROM problem_templates t
                LEFT JOIN user_problem_stats ups ON t.id = ups.template_id
                LEFT JOIN attempt_summary first_attempt
                    ON first_attempt.user_id = ups.user_id
                   AND first_attempt.template_id = ups.template_id
                   AND first_attempt.attempt_count = ups.first_attempt_no
                LEFT JOIN attempt_summary correct_attempt
                    ON correct_attempt.user_id = ups.user_id
                   AND correct_attempt.template_id = ups.template_id
                   AND correct_attempt.attempt_count = ups.first_correct_attempt_no
                WHERE 1 = 1 {problem_filter}
                GROUP BY t.id, t.template_name
                ORDER BY t.id
            """, params + response_params + problem_filter_params)
    
            problem_stats_result = cursor.fetchall()
            problem_stats = []
    
            # 转换problem_stats中的Decimal类型，避免模板中tojson序列化失败
            for raw_stat in problem_stats_result:
                stat = dict(raw_stat)
                for key, value in stat.items():
                    if isinstance(value, Decimal):
                        stat[key] = float(value)
    
                participants = stat.get('participant_students') or 0
                first_correct_students = stat.get('first_correct_students') or 0
                if participants > 0:
                    stat['first_correct_rate'] = round((first_correct_students / participants) * 100, 1)
                else:
                    stat['first_correct_rate'] = 0
                problem_stats.append(stat)
    
            # 获取筛选后的学生总数（排除管理员）
            student_filters = ["u.username != 'admin'"]
            student_params = []
            if selected_class_name:
                if selected_class_name == '未分班':
                    student_filters.append("(u.class_name IS NULL OR TRIM(u.class_name) = '')")
                else:
                    student_filters.append("u.class_name = %s")
                    student_params.append(selected_class_name)
            if selected_major:
                if selected_major == '未设置专业':
                    student_filters.append("(u.major IS NULL OR TRIM(u.major) = '')")
                else:
                    student_filters.append("u.major = %s")
                    student_params.append(selected_major)
    
            cursor.execute(
                f"SELECT COUNT(*) as total FROM users u WHERE {' AND '.join(student_filters)}",
                student_params
            )
    
            total_students_result = cursor.fetchone()
            total_students = total_students_result['total'] if total_students_result else 0
    
            cursor.execute(
                f"""
                WITH completed AS (
                    SELECT user_id, COUNT(DISTINCT template_id) AS total_score, COALESCE(SUM(time_taken), 0) AS total_time
                    FROM user_responses ur
                    WHERE ur.is_correct = TRUE {response_filter}
                      AND (ur.user_id, ur.template_id, ur.attempt_count) IN (
                        SELECT user_id, template_id, attempt_count
                        FROM user_responses ur2
                        WHERE 1 = 1 {response_filter_ur2}
                        GROUP BY user_id, template_id, attempt_count
                        HAVING SUM(CASE WHEN is_correct THEN 1 ELSE 0 END) = COUNT(*)
                      )
                    GROUP BY user_id
                )
                SELECT
                    u.id,
                    u.username,
                    u.name,
                    COALESCE(NULLIF(TRIM(u.major), ''), '未设置专业') AS major,
                    COALESCE(NULLIF(TRIM(u.class_name), ''), '未分班') AS class_name,
                    CASE WHEN COALESCE(c.total_score, 0) >= %s AND %s > 0 THEN TRUE ELSE FALSE END AS completed_all,
                    COALESCE(c.total_score, 0) AS total_score,
                    COALESCE(c.total_time, 0) AS total_time,
                    u.created_at,
                    u.completed_at
                FROM users u
                LEFT JOIN completed c ON c.user_id = u.id
                WHERE {' AND '.join(student_filters)}
                ORDER BY u.class_name ASC, u.username ASC
                """,
                response_params + response_params_ur2 + [get_total_problem_count(selected_paper_id), get_total_problem_count(selected_paper_id)] + student_params
            )
            filtered_students = cursor.fetchall()
    
            # 汇总统计
            cursor.execute(f"""
                WITH attempt_summary AS (
                    SELECT
                        ur.user_id,
                        ur.template_id,
                        ur.attempt_count,
                        COUNT(*) AS total_answers,
                        SUM(CASE WHEN ur.is_correct THEN 1 ELSE 0 END) AS correct_answers,
                        MAX(ur.time_taken) AS time_taken,
                        CASE WHEN SUM(CASE WHEN ur.is_correct THEN 1 ELSE 0 END) = COUNT(*) THEN 1 ELSE 0 END AS is_fully_correct
                    FROM user_responses ur
                    JOIN users u ON ur.user_id = u.id
                    WHERE {filter_clause} {response_filter}
                    GROUP BY ur.user_id, ur.template_id, ur.attempt_count
                ),
                user_problem_stats AS (
                    SELECT
                        user_id,
                        template_id,
                        COUNT(*) AS attempt_count,
                        MIN(attempt_count) AS first_attempt_no,
                        MIN(CASE WHEN is_fully_correct = 1 THEN attempt_count ELSE NULL END) AS first_correct_attempt_no,
                        MAX(is_fully_correct) AS final_is_correct
                    FROM attempt_summary
                    GROUP BY user_id, template_id
                )
                SELECT
                    COALESCE(SUM(ups.attempt_count), 0) as total_attempts,
                    SUM(CASE WHEN first_attempt.is_fully_correct = 1 THEN 1 ELSE 0 END) as first_correct_students,
                    SUM(CASE WHEN ups.final_is_correct = 1 THEN 1 ELSE 0 END) as final_correct_students,
                    AVG(CASE
                        WHEN ups.first_correct_attempt_no IS NOT NULL AND correct_attempt.attempt_count = ups.first_correct_attempt_no
                        THEN correct_attempt.time_taken
                        ELSE NULL
                    END) as avg_correct_time
                FROM user_problem_stats ups
                LEFT JOIN attempt_summary first_attempt
                    ON first_attempt.user_id = ups.user_id
                   AND first_attempt.template_id = ups.template_id
                   AND first_attempt.attempt_count = ups.first_attempt_no
                LEFT JOIN attempt_summary correct_attempt
                    ON correct_attempt.user_id = ups.user_id
                   AND correct_attempt.template_id = ups.template_id
                   AND correct_attempt.attempt_count = ups.first_correct_attempt_no
            """, params + response_params)
            overall_stats = cursor.fetchone() or {}
            for key, value in list(overall_stats.items()):
                if isinstance(value, Decimal):
                    overall_stats[key] = float(value)
            overall_stats.setdefault('total_attempts', 0)
            overall_stats.setdefault('first_correct_students', 0)
            overall_stats.setdefault('final_correct_students', 0)
            overall_stats.setdefault('avg_correct_time', 0)
    
            total_participants = sum((stat.get('participant_students') or 0) for stat in problem_stats)
            total_first_correct = sum((stat.get('first_correct_students') or 0) for stat in problem_stats)
            overall_stats['first_correct_rate'] = round((total_first_correct / total_participants) * 100, 1) if total_participants else 0
    
            return render_template('admin_all_problems_stats.html',
                                   problem_stats=problem_stats,
                                   total_students=total_students,
                                   overall_stats=overall_stats,
                                   filtered_students=filtered_students,
                                   class_options=class_options,
                                   major_options=major_options,
                                   selected_class_name=selected_class_name,
                                   selected_major=selected_major,
                                   username=session['username'],
                                   selected_paper_id=selected_paper_id)
    
        except Exception as e:
            print(f"获取题目统计失败: {str(e)}")
            import traceback
            print(f"详细错误: {traceback.format_exc()}")
            flash(f'获取题目统计失败: {str(e)}', 'danger')
            return redirect(url_for('admin_dashboard'))
        finally:
            cursor.close()
            conn.close()


    @bp.route('/api/user/completion_status')
    @login_required
    def api_user_completion_status():
        """获取用户所有题目的完成状态"""
        try:
            selected_paper_id = resolve_selected_exam_paper_id()
            display_mapping = get_problem_display_info(selected_paper_id) if selected_paper_id else {}
            actual_ids = list(display_mapping.keys())
            completion_map = get_completion_status_map(session['user_id'], selected_paper_id, actual_ids)
            completion_status = {
                display_info['display_number']: completion_map.get(actual_id, False)
                for actual_id, display_info in display_mapping.items()
            }
    
            return jsonify({
                'success': True,
                'completion_status': completion_status
            })
    
        except Exception as e:
            print(f"获取完成状态失败: {str(e)}")
            return jsonify({
                'success': False,
                'message': f'获取完成状态失败: {str(e)}'
            })
    
    
    # 图片管理功能

    return bp
