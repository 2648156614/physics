"""Transactional choice drafts and an irreversible, idempotent exam hand-in."""
import json
from services.choice_question_service import choice_definition, encode_selection, generate_choice_problem


def ensure_schema(cursor):
    cursor.execute('''CREATE TABLE IF NOT EXISTS exam_choice_sessions (
        exam_id INT NOT NULL, user_id INT NOT NULL, submitted BOOLEAN NOT NULL DEFAULT FALSE,
        PRIMARY KEY (exam_id,user_id))''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS exam_choice_drafts (
        exam_id INT NOT NULL, user_id INT NOT NULL, template_id INT NOT NULL,
        selected_ids TEXT NOT NULL, time_taken DOUBLE NOT NULL DEFAULT 0,
        PRIMARY KEY (exam_id,user_id,template_id))''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS exam_auto_submit_settings (
        id INT PRIMARY KEY, enabled_after DATETIME NOT NULL)''')
    cursor.execute('INSERT IGNORE INTO exam_auto_submit_settings (id,enabled_after) VALUES (1,CURRENT_TIMESTAMP)')
    cursor.execute('''CREATE TABLE IF NOT EXISTS exam_grading_done (
        exam_id INT NOT NULL,user_id INT NOT NULL,PRIMARY KEY(exam_id,user_id))''')


def state(connect, exam_id, user_id):
    conn=connect();cur=conn.cursor(dictionary=True)
    try:
        cur.execute('SELECT submitted FROM exam_choice_sessions WHERE exam_id=%s AND user_id=%s',(exam_id,user_id))
        submitted=bool((cur.fetchone() or {}).get('submitted'))
        cur.execute('SELECT template_id,selected_ids FROM exam_choice_drafts WHERE exam_id=%s AND user_id=%s',(exam_id,user_id))
        return submitted,{int(row['template_id']):json.loads(row['selected_ids']) for row in cur.fetchall()}
    finally:
        cur.close();conn.close()


def lock_session(cur, exam_id, user_id):
    cur.execute('INSERT IGNORE INTO exam_choice_sessions (exam_id,user_id) VALUES (%s,%s)',(exam_id,user_id))
    cur.execute('SELECT submitted FROM exam_choice_sessions WHERE exam_id=%s AND user_id=%s FOR UPDATE',(exam_id,user_id))
    return bool(cur.fetchone()['submitted'])


def save_draft(connect, exam_id, user_id, template_id, selected, elapsed):
    conn=connect();cur=conn.cursor(dictionary=True)
    try:
        if lock_session(cur,exam_id,user_id):
            raise ValueError('已交卷，不能再修改答案')
        cur.execute('SELECT template_id FROM exam_user_questions WHERE exam_id=%s AND user_id=%s AND template_id=%s',(exam_id,user_id,template_id))
        if not cur.fetchone():
            raise ValueError('题目不属于当前考试')
        cur.execute('DELETE FROM exam_choice_drafts WHERE exam_id=%s AND user_id=%s AND template_id=%s',(exam_id,user_id,template_id))
        cur.execute('INSERT INTO exam_choice_drafts (exam_id,user_id,template_id,selected_ids,time_taken) VALUES (%s,%s,%s,%s,%s)',(exam_id,user_id,template_id,json.dumps(selected),elapsed))
        conn.commit()
    except Exception:
        conn.rollback();raise
    finally:
        cur.close();conn.close()


def hand_in(connect, exam_id, user_id, only_if_expired=False):
    conn=connect();cur=conn.cursor(dictionary=True)
    try:
        already_locked=lock_session(cur,exam_id,user_id)
        cur.execute('SELECT exam_id FROM exam_grading_done WHERE exam_id=%s AND user_id=%s',(exam_id,user_id))
        if cur.fetchone():
            conn.commit();return True
        if only_if_expired:
            cur.execute('''SELECT e.id FROM exams e
                JOIN exam_batch_students s ON s.exam_id=e.id
                JOIN exam_batches b ON b.id=s.batch_id AND b.exam_id=e.id
                JOIN exam_auto_submit_settings cfg ON cfg.id=1
                WHERE e.id=%s AND s.user_id=%s AND s.is_active=TRUE
                  AND e.status='published' AND b.status='active'
                  AND COALESCE(b.end_time,e.end_time)>=cfg.enabled_after
                  AND COALESCE(b.end_time,e.end_time)<=CURRENT_TIMESTAMP FOR UPDATE''',(exam_id,user_id))
            if not cur.fetchone():
                conn.rollback();return False
        cur.execute('''SELECT pt.*, q.batch_id FROM exam_user_questions q
            JOIN problem_templates pt ON pt.id=q.template_id
            WHERE q.exam_id=%s AND q.user_id=%s ORDER BY q.display_number''',(exam_id,user_id))
        templates=cur.fetchall()
        if not templates:
            raise ValueError('尚未进入考试，不能交卷')
        cur.execute('SELECT template_id,selected_ids,time_taken FROM exam_choice_drafts WHERE exam_id=%s AND user_id=%s',(exam_id,user_id))
        drafts={int(row['template_id']):row for row in cur.fetchall()}
        for template in templates:
            definition=choice_definition(template.get('generation_strategy'))
            if not definition:continue
            draft=drafts.get(int(template['id']))
            # Preserve legacy graded answers if the student never created a draft.
            cur.execute('SELECT 1 AS found FROM user_responses WHERE exam_id=%s AND user_id=%s AND template_id=%s LIMIT 1',(exam_id,user_id,template['id']))
            existing = cur.fetchone()
            if existing and (already_locked or not draft):continue
            cur.fetchall()  # consume buffered/unbuffered cursor results
            selected=json.loads(draft['selected_ids']) if draft else []
            value=encode_selection(selected)
            problem=generate_choice_problem(template,definition,2)
            correct=problem['correct_answers'][0]
            cur.execute('SELECT COALESCE(MAX(attempt_count),0)+1 AS attempt FROM user_responses WHERE exam_id=%s AND user_id=%s AND template_id=%s',(exam_id,user_id,template['id']))
            attempt = cur.fetchone()['attempt']
            cur.execute('''INSERT INTO user_responses
                (user_id,template_id,problem_text,user_answer,correct_answer,is_correct,
                 attempt_count,time_taken,answer_index,paper_id,switch_count,exam_id,batch_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,0,%s,0,%s,%s)''',
                (user_id,template['id'],problem['problem_text'],value,correct,value==correct,attempt,
                 draft['time_taken'] if draft else 0,template['paper_id'],exam_id,template['batch_id']))
        cur.execute('UPDATE exam_choice_sessions SET submitted=TRUE WHERE exam_id=%s AND user_id=%s',(exam_id,user_id))
        cur.execute('INSERT IGNORE INTO exam_grading_done (exam_id,user_id) VALUES (%s,%s)',(exam_id,user_id))
        conn.commit()
        return True
    except Exception:
        conn.rollback();raise
    finally:
        cur.close();conn.close()


def lock_answers(connect, exam_id, user_id):
    """Manual hand-in closes editing without doing grading work on the request."""
    conn=connect();cur=conn.cursor(dictionary=True)
    try:
        lock_session(cur,exam_id,user_id)
        cur.execute('UPDATE exam_choice_sessions SET submitted=TRUE WHERE exam_id=%s AND user_id=%s',(exam_id,user_id))
        conn.commit()
    except Exception:
        conn.rollback();raise
    finally:
        cur.close();conn.close()
