import re


def extract_student_id_suffix(student_id):
    """提取学号末四位，不足四位左侧补 0，保证始终返回 4 位。"""
    raw_value = str(student_id or '').strip()
    if not raw_value:
        return '0000'

    cleaned_digits = re.sub(r'\D', '', raw_value)
    source = cleaned_digits if cleaned_digits else re.sub(r'\s+', '', raw_value)
    if not source:
        return '0000'

    return source[-4:].rjust(4, '0')


def build_initial_password(student_id):
    """根据学号生成初始密码：@ncst + 学号后四位。"""
    suffix = extract_student_id_suffix(student_id)
    return f"@ncst{suffix}"


def is_strong_password(password):
    """强密码校验：长度>=8，且包含小写字母、数字、特殊字符。"""
    if not password:
        return False
    if len(password) < 8:
        return False
    if not re.search(r'[a-z]', password):
        return False
    if not re.search(r'\d', password):
        return False
    if not re.search(r'[^A-Za-z0-9]', password):
        return False
    if re.search(r'\s', password):
        return False
    return True
