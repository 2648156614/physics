from datetime import datetime


EXAM_STUDENT_STATUS_LABELS = {
    'not_open': '未到考试时间',
    'not_started': '未开始',
    'in_progress': '答题中',
    'completed': '已完成',
    'absent': '缺考',
    'ended_incomplete': '考试结束未完成',
}


def classify_exam_student_status(
    *, entered, completed_count, total_problems, start_time, end_time, now=None
):
    """Classify one student's state within a specific exam batch."""
    now = now or datetime.now()
    if total_problems > 0 and completed_count >= total_problems:
        return 'completed'
    if end_time and now >= end_time:
        return 'ended_incomplete' if entered else 'absent'
    if entered:
        return 'in_progress'
    if start_time and now < start_time:
        return 'not_open'
    return 'not_started'
