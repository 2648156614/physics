from datetime import datetime


def parse_exam_time(value):
    """解析后台 datetime-local 输入，返回服务器本地时间。"""
    value = (value or '').strip()
    if not value:
        return None
    for fmt in ('%Y-%m-%dT%H:%M', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M'):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    raise ValueError('时间格式不正确，请使用页面时间选择器设置。')


def format_datetime_local(value):
    if not value:
        return ''
    if isinstance(value, str):
        try:
            value = parse_exam_time(value)
        except ValueError:
            return ''
    return value.strftime('%Y-%m-%dT%H:%M')
