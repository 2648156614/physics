"""Recoverable server-side deadline processing; no student browser is required."""
import logging
import threading
from datetime import datetime
from services.choice_exam_service import hand_in

logger=logging.getLogger(__name__)
_schedule_changed=threading.Event()

def notify_schedule_changed():
    _schedule_changed.set()


def process_expired(connect, after_submit, limit=100, pace=None):
    conn=connect();cur=conn.cursor(dictionary=True)
    try:
        cur.execute('''SELECT s.exam_id,s.user_id FROM exam_batch_students s
            JOIN exams e ON e.id=s.exam_id
            JOIN exam_batches b ON b.id=s.batch_id AND b.exam_id=e.id
            JOIN exam_auto_submit_settings cfg ON cfg.id=1
            LEFT JOIN exam_grading_done done ON done.exam_id=s.exam_id AND done.user_id=s.user_id
            WHERE s.is_active=TRUE AND e.status='published' AND b.status='active'
              AND COALESCE(b.end_time,e.end_time)>=cfg.enabled_after
              AND COALESCE(b.end_time,e.end_time)<=CURRENT_TIMESTAMP
              AND done.exam_id IS NULL
              AND EXISTS (SELECT 1 FROM exam_user_questions q WHERE q.exam_id=s.exam_id AND q.user_id=s.user_id)
            ORDER BY COALESCE(b.end_time,e.end_time),s.exam_id,s.user_id LIMIT %s''',(limit,))
        candidates=cur.fetchall()
    finally:
        cur.close();conn.close()
    completed=0
    for row in candidates:
        if pace and pace():break
        try:
            if hand_in(connect,row['exam_id'],row['user_id'],only_if_expired=True):
                completed+=1
                after_submit(row['user_id'],row['exam_id'])
        except Exception:
            logger.exception('自动交卷失败，将重试: exam_id=%s user_id=%s',row['exam_id'],row['user_id'])
    return len(candidates),completed


def next_deadline(connect):
    conn=connect();cur=conn.cursor(dictionary=True)
    try:
        cur.execute("""SELECT MIN(COALESCE(b.end_time,e.end_time)) AS end_time
            FROM exam_batches b JOIN exams e ON e.id=b.exam_id
            JOIN exam_auto_submit_settings cfg ON cfg.id=1
            WHERE e.status='published' AND b.status='active'
              AND COALESCE(b.end_time,e.end_time)>=cfg.enabled_after
              AND COALESCE(b.end_time,e.end_time)>CURRENT_TIMESTAMP""")
        return (cur.fetchone() or {}).get('end_time')
    finally:
        cur.close();conn.close()


def start_worker(connect, after_submit, pause_seconds=0.25):
    stop=threading.Event()
    def run():
        while not stop.is_set():
            _schedule_changed.clear()
            try:
                # Catch up once on startup/deadline/configuration change, not on a timer poll.
                while not stop.is_set():
                    count,completed=process_expired(connect,after_submit,limit=10,
                        pace=lambda:stop.wait(pause_seconds))
                    if completed:logger.info('后台已判分 %s 人',completed)
                    if count<10 or not completed:break
                deadline=next_deadline(connect)
                delay=max(0,(deadline-datetime.now()).total_seconds())+0.1 if deadline else None
                if count and completed<count:
                    delay=min(delay,30) if delay is not None else 30  # transient failure retry only
                _schedule_changed.wait(delay)
            except Exception:
                logger.exception('后台判分任务异常，稍后重试')
                _schedule_changed.wait(30)
    threading.Thread(target=run,name='exam-deadline-grading',daemon=True).start()
    return stop
