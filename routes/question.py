import os
import re
from html import escape
from urllib.parse import quote

from flask import Blueprint, abort, send_from_directory


def build_problem_image_html(image_filename, template_name):
    url_filename = quote(image_filename or '')
    safe_title = escape(template_name or '题目图片', quote=True)
    cache_version = get_image_cache_version(image_filename)
    version_suffix = f'?v={cache_version}' if cache_version else ''
    return (
        f'<div class="text-center mb-3">'
        f'<img src="/problem_image/{url_filename}{version_suffix}" alt="{safe_title}" class="problem-image img-fluid">'
        f'<div class="image-caption text-muted">图：{safe_title}</div>'
        f'</div>'
    )


def get_image_cache_version(image_filename):
    if not image_filename:
        return ''
    image_path = os.path.join(get_upload_folder_path(), image_filename)
    try:
        return str(int(os.path.getmtime(image_path)))
    except OSError:
        return ''


def strip_problem_image_html(problem_text):
    if not problem_text:
        return problem_text

    image_block_pattern = re.compile(
        r'^\s*'
        r'(?:<div\s+class=["\']text-center\s+mb-3["\']>\s*'
        r'<img\b[^>]*\bclass=["\'][^"\']*problem-image[^"\']*["\'][^>]*>\s*'
        r'(?:<div\s+class=["\']image-caption\s+text-muted["\']>.*?</div>\s*)?'
        r'</div>\s*)+'
        r'(?:</div>\s*)*',
        re.IGNORECASE | re.DOTALL
    )
    return image_block_pattern.sub('', problem_text, count=1).lstrip()


def delete_image_file_if_unused(cursor, image_filename, current_template_id=None):
    if not image_filename:
        return False

    query = "SELECT COUNT(*) AS usage_count FROM problem_templates WHERE image_filename = %s"
    params = [image_filename]
    if current_template_id is not None:
        query += " AND id != %s"
        params.append(current_template_id)
    cursor.execute(query, params)
    usage = cursor.fetchone() or {}
    if int(usage.get('usage_count') or 0) > 0:
        return False

    image_path = os.path.join(get_upload_folder_path(), image_filename)
    if os.path.exists(image_path):
        os.remove(image_path)
        return True
    return False


def create_question_blueprint(deps):
    globals().update(deps)
    bp = Blueprint('question', __name__)

    @bp.route('/problem_image/<path:filename>')
    def problem_image_file(filename):
        if not filename or filename != os.path.basename(filename) or not allowed_file(filename):
            abort(404)
        response = send_from_directory(get_upload_folder_path(), filename, max_age=0)
        response.headers['Cache-Control'] = 'no-cache, must-revalidate'
        return response

    @bp.route('/admin/exam_papers', methods=['POST'])
    @login_required(db_check=True)
    def admin_create_exam_paper():
        if session.get('username') != 'admin':
            flash('权限不足', 'danger')
            return redirect(url_for('dashboard'))
        name = (request.form.get('name') or '').strip()
        description = (request.form.get('description') or '').strip()
        is_enabled = request.form.get('is_enabled') == '1'
        if not name:
            flash('题库名称不能为空', 'danger')
            return redirect(url_for('admin_dashboard'))
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("INSERT INTO exam_papers (name, description, is_enabled) VALUES (%s, %s, %s)", (name, description or None, is_enabled))
            new_paper_id = cursor.lastrowid
            conn.commit()
            clear_exam_metadata_cache()
            if is_enabled:
                schedule_exam_paper_prewarm(new_paper_id)
            flash('题库创建成功', 'success')
        except mysql.connector.Error as err:
            conn.rollback()
            flash(f'题库创建失败：{err}', 'danger')
        finally:
            cursor.close()
            conn.close()
        return redirect(url_for('admin_dashboard'))
    
    
    @bp.route('/admin/exam_papers/<int:paper_id>/toggle', methods=['POST'])
    @login_required(db_check=True)
    def admin_toggle_exam_paper(paper_id):
        if session.get('username') != 'admin':
            flash('权限不足', 'danger')
            return redirect(url_for('dashboard'))
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        try:
            cursor.execute("SELECT id, name, is_enabled FROM exam_papers WHERE id = %s", (paper_id,))
            paper = cursor.fetchone()
            if not paper:
                flash('题库不存在', 'danger')
                return redirect(url_for('admin_dashboard'))
            new_status = not bool(paper['is_enabled'])
            cursor.execute("UPDATE exam_papers SET is_enabled = %s WHERE id = %s", (new_status, paper_id))
            conn.commit()
            deleted_cache_count = invalidate_exam_paper_cache(paper_id)
            prewarm_started = False
            if new_status:
                prewarm_started = schedule_exam_paper_prewarm(paper_id)
            session.pop('current_problem', None)
            session.modified = True
            if prewarm_started:
                flash('已开始后台预热该题库题目池。', 'info')
            flash(f"题库《{paper['name']}》已{'开启' if new_status else '关闭'}，已清理 {deleted_cache_count} 条题目缓存。", 'success')
        finally:
            cursor.close()
            conn.close()
        return redirect(url_for('admin_dashboard', paper_id=paper_id))
    
    
    @bp.route('/admin/exam_papers/<int:paper_id>/export')
    @login_required(db_check=True)
    def admin_export_exam_paper(paper_id):
        if session.get('username') != 'admin':
            flash('权限不足', 'danger')
            return redirect(url_for('dashboard'))
    
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        try:
            cursor.execute("SELECT id, name, description, is_enabled, created_at FROM exam_papers WHERE id = %s", (paper_id,))
            paper = cursor.fetchone()
            if not paper:
                flash('题库不存在，无法导出。', 'danger')
                return redirect(url_for('admin_manage_problems'))
    
            cursor.execute(
                """
                SELECT template_name, problem_text, variables, solution_formula,
                       answer_count, answer_units, difficulty, image_filename
                FROM problem_templates
                WHERE paper_id = %s
                ORDER BY id
                """,
                (paper_id,)
            )
            templates = cursor.fetchall()
        finally:
            cursor.close()
            conn.close()
    
        upload_folder = get_upload_folder_path()
        exported_templates = []
        for template in templates:
            image_payload = None
            image_filename = template.get('image_filename')
            if image_filename:
                image_path = os.path.join(upload_folder, image_filename)
                if os.path.exists(image_path):
                    with open(image_path, 'rb') as image_file:
                        image_payload = {
                            'filename': image_filename,
                            'content_base64': base64.b64encode(image_file.read()).decode('ascii')
                        }
    
            exported_templates.append({
                'template_name': template.get('template_name'),
                'problem_text': template.get('problem_text'),
                'variables': template.get('variables'),
                'solution_formula': template.get('solution_formula'),
                'answer_count': template.get('answer_count'),
                'answer_units': template.get('answer_units'),
                'difficulty': template.get('difficulty'),
                'image_filename': image_filename,
                'image': image_payload
            })
    
        export_payload = {
            'export_format': 'physics_exam_paper_backup',
            'version': 1,
            'exported_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'paper': {
                'name': paper.get('name'),
                'description': paper.get('description'),
                'is_enabled': bool(paper.get('is_enabled')),
                'created_at': paper['created_at'].strftime('%Y-%m-%d %H:%M:%S') if paper.get('created_at') else None
            },
            'templates': exported_templates
        }
    
        safe_name = secure_filename(paper.get('name') or f"paper_{paper_id}") or f"paper_{paper_id}"
        filename = f"{safe_name}_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        content = json.dumps(export_payload, ensure_ascii=False, indent=2)
        response = Response(content, mimetype='application/json; charset=utf-8')
        response.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response
    
    
    @bp.route('/admin/exam_papers/import', methods=['POST'])
    @login_required(db_check=True)
    def admin_import_exam_paper():
        if session.get('username') != 'admin':
            flash('权限不足', 'danger')
            return redirect(url_for('dashboard'))
    
        upload_file = request.files.get('paper_file')
        replace_existing = request.form.get('replace_existing') == '1'
        if not upload_file or upload_file.filename == '':
            flash('请先选择题库备份 JSON 文件。', 'danger')
            return redirect(url_for('admin_manage_problems'))
    
        try:
            payload = json.loads(upload_file.read().decode('utf-8-sig'))
        except Exception as err:
            flash(f'读取题库备份失败：{err}', 'danger')
            return redirect(url_for('admin_manage_problems'))
    
        if payload.get('export_format') != 'physics_exam_paper_backup' or not isinstance(payload.get('paper'), dict):
            flash('文件格式不正确：请选择本系统导出的题库备份文件。', 'danger')
            return redirect(url_for('admin_manage_problems'))
    
        paper_payload = payload.get('paper') or {}
        templates_payload = payload.get('templates') or []
        paper_name = (paper_payload.get('name') or '').strip()
        if not paper_name:
            flash('导入失败：备份文件缺少题库名称。', 'danger')
            return redirect(url_for('admin_manage_problems'))
        if not isinstance(templates_payload, list):
            flash('导入失败：题目列表格式不正确。', 'danger')
            return redirect(url_for('admin_manage_problems'))
    
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        try:
            cursor.execute("SELECT id FROM exam_papers WHERE name = %s", (paper_name,))
            existing_paper = cursor.fetchone()
            if existing_paper and replace_existing:
                paper_id = existing_paper['id']
                cursor.execute("SELECT id FROM problem_templates WHERE paper_id = %s", (paper_id,))
                old_template_ids = [row['id'] for row in cursor.fetchall()]
                deleted_cache_count = 0
                if old_template_ids:
                    placeholders = ', '.join(['%s'] * len(old_template_ids))
                    cursor.execute(f"DELETE FROM user_responses WHERE template_id IN ({placeholders})", old_template_ids)
                    cursor.execute(f"DELETE FROM problem_templates WHERE id IN ({placeholders})", old_template_ids)
                    for template_id in old_template_ids:
                        deleted_cache_count += invalidate_problem_cache(template_id)
                cursor.execute(
                    "UPDATE exam_papers SET description = %s, is_enabled = %s WHERE id = %s",
                    (paper_payload.get('description'), bool(paper_payload.get('is_enabled', True)), paper_id)
                )
            else:
                deleted_cache_count = 0
                import_name = paper_name
                if existing_paper:
                    suffix = datetime.now().strftime('%Y%m%d_%H%M%S')
                    import_name = f"{paper_name}（导入 {suffix}）"
                cursor.execute(
                    "INSERT INTO exam_papers (name, description, is_enabled) VALUES (%s, %s, %s)",
                    (import_name, paper_payload.get('description'), bool(paper_payload.get('is_enabled', True)))
                )
                paper_id = cursor.lastrowid
    
            imported_count = 0
            for item in templates_payload:
                if not isinstance(item, dict):
                    continue
                template_name = (item.get('template_name') or '').strip()
                problem_text = item.get('problem_text') or ''
                solution_formula = item.get('solution_formula') or ''
                if not template_name or not problem_text or not solution_formula:
                    continue
    
                old_image_filename = item.get('image_filename')
                image_filename = save_imported_image(item.get('image'))
                final_image_filename = image_filename or old_image_filename
                if final_image_filename:
                    problem_text = build_problem_image_html(final_image_filename, template_name) + strip_problem_image_html(problem_text)
    
                cursor.execute(
                    """
                    INSERT INTO problem_templates
                    (template_name, problem_text, variables, solution_formula,
                     answer_count, answer_units, difficulty, image_filename, paper_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        template_name,
                        problem_text,
                        question_generation_service.normalize_variable_specs(item.get('variables') or ''),
                        item.get('solution_formula'),
                        int(item.get('answer_count') or 1),
                        item.get('answer_units') or '',
                        item.get('difficulty') or 'medium',
                        final_image_filename,
                        paper_id
                    )
                )
                imported_count += 1
    
            conn.commit()
            deleted_cache_count += invalidate_exam_paper_cache(paper_id)
            if bool(paper_payload.get('is_enabled', True)):
                schedule_exam_paper_prewarm(paper_id)
            session.pop('current_problem', None)
            session.modified = True
            flash(f'题库导入完成：已导入 {imported_count} 道题，已清理 {deleted_cache_count} 条题目缓存。', 'success')
        except Exception as err:
            conn.rollback()
            flash(f'题库导入失败：{err}', 'danger')
        finally:
            cursor.close()
            conn.close()
    
        return redirect(url_for('admin_manage_problems', paper_id=paper_id if 'paper_id' in locals() else None))
    
    
    @bp.route('/admin/add_problem', methods=['GET', 'POST'])
    @login_required
    def admin_add_problem():
        """添加新题目"""
        if session.get('username') != 'admin':
            flash('权限不足', 'danger')
            return redirect(url_for('dashboard'))
    
        if request.method == 'POST':
            try:
                template_name = request.form['template_name']
                problem_text = request.form['problem_text']
                variables = question_generation_service.normalize_variable_specs(request.form['variables'])
                solution_formula = request.form['solution_formula']
                problem_text = question_generation_service.normalize_problem_placeholders(problem_text)
                problem_text = question_generation_service.normalize_problem_image_urls(problem_text)
                answer_count = int(request.form.get('answer_count', 1))
                difficulty = request.form.get('difficulty', 'medium')
                answer_units = request.form.get('answer_units', '')
                paper_id = request.form.get('paper_id', type=int)
                knowledge_point = request.form.get('knowledge_point', '').strip()
                if not knowledge_point:
                    knowledge_point = infer_knowledge_label(template_name)

                # 处理图片上传
                image_filename = None
                if 'problem_image' in request.files:
                    file = request.files['problem_image']
                    if file and file.filename != '':
                        image_filename = save_uploaded_file(file)
                        if image_filename:
                            problem_text = build_problem_image_html(image_filename, template_name) + strip_problem_image_html(problem_text)
    
                conn = get_db_connection()
                cursor = conn.cursor()
    
                # 插入新题目模板
                cursor.execute("""
                    INSERT INTO problem_templates 
                    (template_name, problem_text, variables, solution_formula, answer_count, answer_units, difficulty, image_filename, paper_id, knowledge_point)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (template_name, problem_text, variables, solution_formula, answer_count, answer_units, difficulty, image_filename, paper_id, knowledge_point))
                new_template_id = cursor.lastrowid
    
                conn.commit()
                deleted_cache_count = invalidate_exam_paper_cache(paper_id)
                session.pop('current_problem', None)
                session.modified = True
                cursor.close()
                conn.close()
    
                flash(f'题目添加成功！已清理 {deleted_cache_count} 条题目缓存。', 'success')
                return redirect(url_for('admin_manage_problems'))
    
            except Exception as e:
                print(f"添加题目失败: {str(e)}")
                flash(f'添加题目失败: {str(e)}', 'danger')
    
        return render_template('admin_add_problem.html', exam_papers=get_exam_papers())
    
    
    @bp.route('/admin/manage_problems')
    @login_required
    def admin_manage_problems():
        """管理所有题目"""
        if session.get('username') != 'admin':
            flash('权限不足', 'danger')
            return redirect(url_for('dashboard'))
    
        selected_paper_id = request.args.get('paper_id', type=int)
        exam_papers = get_exam_papers()
    
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        try:
            if selected_paper_id:
                cursor.execute("SELECT * FROM problem_templates WHERE paper_id = %s ORDER BY id", (selected_paper_id,))
            else:
                cursor.execute("SELECT * FROM problem_templates ORDER BY id")
            templates = cursor.fetchall()
        finally:
            cursor.close()
            conn.close()
    
        display_mapping = get_problem_display_info(selected_paper_id, enabled_only=False)
        display_number_by_id = {
            int(actual_id): info['display_number']
            for actual_id, info in display_mapping.items()
        }
        for template in templates:
            template['display_number'] = display_number_by_id.get(int(template['id']), template['id'])
    
        return render_template('admin_manage_problems.html',
                               templates=templates,
                               display_mapping=display_mapping,
                               exam_papers=exam_papers,
                               selected_paper_id=selected_paper_id)
    
    
    @bp.route('/admin/edit_problem/<int:template_id>', methods=['GET', 'POST'])
    @login_required
    def admin_edit_problem(template_id):
        """编辑题目"""
        if session.get('username') != 'admin':
            flash('权限不足', 'danger')
            return redirect(url_for('dashboard'))
    
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
    
        if request.method == 'POST':
            try:
                template_name = request.form['template_name']
                problem_text = request.form['problem_text']
                variables = question_generation_service.normalize_variable_specs(request.form['variables'])
                solution_formula = request.form['solution_formula']
                problem_text = question_generation_service.normalize_problem_placeholders(problem_text)
                problem_text = question_generation_service.normalize_problem_image_urls(problem_text)
                answer_count = int(request.form.get('answer_count', 1))
                difficulty = request.form.get('difficulty', 'medium')
                answer_units = request.form.get('answer_units', '')
                paper_id = request.form.get('paper_id', type=int)
                knowledge_point = request.form.get('knowledge_point', '').strip()
                if not knowledge_point:
                    knowledge_point = infer_knowledge_label(template_name)
                remove_image = request.form.get('remove_image') == 'true'
                current_image = request.form.get('current_image', '')
                cursor.execute("SELECT image_filename FROM problem_templates WHERE id = %s", (template_id,))
                existing_template = cursor.fetchone() or {}
                old_image_filename = existing_template.get('image_filename')
    
                # 处理图片更新
                image_filename = old_image_filename or current_image or None
    
                if remove_image:
                    # 删除图片
                    image_filename = None
                    # 从问题文本中移除图片
                    problem_text = strip_problem_image_html(problem_text)
                elif 'problem_image' in request.files:
                    file = request.files['problem_image']
                    if file and file.filename != '':
                        # 上传新图片
                        image_filename = save_uploaded_file(file)
                        if image_filename:
                            problem_text = build_problem_image_html(image_filename, template_name) + strip_problem_image_html(problem_text)

                if image_filename:
                    problem_text = build_problem_image_html(image_filename, template_name) + strip_problem_image_html(problem_text)
    
                cursor.execute("""
                    UPDATE problem_templates 
                    SET template_name = %s, problem_text = %s, variables = %s, 
                        solution_formula = %s, answer_count = %s, answer_units = %s, difficulty = %s, image_filename = %s, paper_id = %s, knowledge_point = %s
                    WHERE id = %s
                """, (template_name, problem_text, variables, solution_formula, answer_count, answer_units, difficulty, image_filename, paper_id, knowledge_point,
                      template_id))
    
                conn.commit()
                deleted_old_image = False
                if old_image_filename and old_image_filename != image_filename:
                    deleted_old_image = delete_image_file_if_unused(cursor, old_image_filename, template_id)
                deleted_cache_count = invalidate_problem_cache(template_id)
                session.pop('current_problem', None)
                session.modified = True
                cursor.close()
                conn.close()
                extra_message = '\uFF0C\u65E7\u56FE\u7247\u6587\u4EF6\u5DF2\u5220\u9664' if deleted_old_image else ''
                flash(f'\u9898\u76EE\u66F4\u65B0\u6210\u529F\uFF01\u5DF2\u6E05\u7406 {deleted_cache_count} \u6761\u65E7\u7F13\u5B58{extra_message}\u3002', 'success')
                return redirect(url_for('admin_manage_problems'))
    
            except Exception as e:
                print(f"更新题目失败: {str(e)}")
                flash(f'更新题目失败: {str(e)}', 'danger')
    
        # 获取题目信息
        cursor.execute("SELECT * FROM problem_templates WHERE id = %s", (template_id,))
        template = cursor.fetchone()
        cursor.close()
        conn.close()
    
        if not template:
            flash('题目不存在', 'danger')
            return redirect(url_for('admin_manage_problems'))

        template['problem_text'] = question_generation_service.normalize_problem_image_urls(template.get('problem_text') or '')
    
        return render_template('admin_edit_problem.html', template=template, exam_papers=get_exam_papers())
    
    
    @bp.route('/admin/delete_problem/<int:template_id>')
    @login_required(db_check=True)
    def admin_delete_problem(template_id):
        """删除题目并清理相关图片"""
        if session.get('username') != 'admin':
            flash('权限不足', 'danger')
            return redirect(url_for('dashboard'))
    
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
    
        try:
            # 先获取题目的图片信息
            cursor.execute("SELECT image_filename FROM problem_templates WHERE id = %s", (template_id,))
            template = cursor.fetchone()
    
            # 删除相关的答题记录
            cursor.execute("DELETE FROM user_responses WHERE template_id = %s", (template_id,))
            # 删除题目模板
            cursor.execute("DELETE FROM problem_templates WHERE id = %s", (template_id,))
    
            conn.commit()
            deleted_cache_count = invalidate_problem_cache(template_id)
            session.pop('current_problem', None)
            session.modified = True
            print(f"ℹ️ 删除题目后已清理 {deleted_cache_count} 条旧缓存")
    
            # 删除题目后刷新所有用户的完成状态和统计
            try:
                updated_count = update_all_users_completion_status()
                print(f"ℹ️ 已刷新 {updated_count} 个用户的完成状态")
            except Exception as update_error:
                print(f"⚠️ 删除题目后刷新完成状态失败: {update_error}")
    
            # 如果题目有专属图片，检查并删除图片文件
            if template and template['image_filename']:
                try:
                    image_path = os.path.join(get_upload_folder_path(), template['image_filename'])
                    if os.path.exists(image_path):
                        # 检查是否还有其他题目使用这个图片
                        cursor.execute("SELECT COUNT(*) as usage_count FROM problem_templates WHERE image_filename = %s",
                                       (template['image_filename'],))
                        usage = cursor.fetchone()
                        if usage['usage_count'] == 0:
                            os.remove(image_path)
                            print(f"✅ 已删除未使用的图片: {template['image_filename']}")
                        else:
                            print(f"ℹ️ 图片 {template['image_filename']} 仍被其他题目使用，保留文件")
                except Exception as e:
                    print(f"⚠️ 删除图片文件失败: {e}")
    
            flash('题目删除成功！', 'success')
        except Exception as e:
            print(f"❌ 删除题目失败: {str(e)}")
            flash(f'删除题目失败: {str(e)}', 'danger')
        finally:
            cursor.close()
            conn.close()
    
        return redirect(url_for('admin_manage_problems'))
    
    
    @bp.route('/admin/update_all_status')
    @login_required(db_check=True)
    def update_all_status():
        """批量更新所有用户的完成状态"""
        if session.get('username') != 'admin':
            flash('权限不足', 'danger')
            return redirect(url_for('dashboard'))
    
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
    
        try:
            updated_count = update_all_users_completion_status()
            flash(f'成功更新 {updated_count} 个用户的完成状态', 'success')
    
        except Exception as e:
            print(f"批量更新状态失败: {e}")
            flash('更新状态失败', 'danger')
        finally:
            cursor.close()
            conn.close()
    
        return redirect(url_for('admin_dashboard'))
    
    
    # 用户完成状态API

    return bp
