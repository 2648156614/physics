"""Minimal error recovery that works even when normal page context fails."""
from flask import jsonify, make_response, request, session, url_for
from services.generation_errors import TemplateGenerationError


def build_seb_quit_url(exam, get_shared_settings):
    if exam.get('seb_config_mode') == 'shared':
        token = get_shared_settings().get('seb_quit_token')
        endpoint = 'exam.seb_shared_quit'
    else:
        token = exam.get('seb_quit_token')
        endpoint = 'exam.seb_quit'
    return url_for(endpoint, token=token, _external=True) if token else None


def install_error_recovery(app, deps):
    @app.errorhandler(500)
    @app.errorhandler(TemplateGenerationError)
    def server_error(error):
        status = 503 if isinstance(error, TemplateGenerationError) else 500
        message = ('当前题目无法生成或验算，请联系监考教师。' if status == 503 else
                   '服务暂时异常，请稍后重试或联系监考教师。')
        if status == 503:
            app.logger.error('题目验算失败: %s', error)
        # Keep API callers on their existing JSON failure path. Never retry POSTs.
        if request.path.startswith('/api/'):
            response = make_response(jsonify(success=False, message=message), status)
        else:
            state = session.get('current_problem') or {}
            exam_id = state.get('exam_id') or session.get('selected_exam_id')
            recovery = session.get('seb_error_recovery') or {}
            quit_url = (recovery.get('quit_url') if session.get('user_id') and exam_id
                        and recovery.get('exam_id') == exam_id else None)
            if session.get('user_id') and exam_id and not quit_url:
                try:
                    exam = deps['get_exam_by_id'](exam_id)
                    if deps['is_seb_exam_required'](exam):
                        quit_url = build_seb_quit_url(exam, deps['get_shared_seb_settings'])
                except Exception:
                    app.logger.warning('错误页无法读取 SEB 退出配置', exc_info=True)
            back_url = url_for('dashboard')
            number = state.get('display_number')
            can_resume = bool(session.get('user_id') and isinstance(number, int) and number > 0)
            if can_resume:
                back_url = url_for('problem_ajax', problem_id=number)
            # Bypass context processors: one of them may have caused the failure.
            html = app.jinja_env.get_template('server_error.html').render(
                back_url=back_url, can_resume=can_resume, quit_url=quit_url,
                generation_error=(status == 503))
            response = make_response(html, status)
        response.headers['Cache-Control'] = 'no-store'
        return response
