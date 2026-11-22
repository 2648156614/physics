"""Per-paper mandatory questions plus an additional random quota."""
import json
import re
from services.exam_assignment_service import select_exam_template_ids


def public_plan(plan):
    if isinstance(plan, str):
        plan = json.loads(plan) if plan else []
    return [dict(paper_id=int(row['paper_id']),
                 mandatory_ids=sorted(int(value) for value in row.get('mandatory_ids', [])),
                 random_count=int(row.get('random_count', 0))) for row in (plan or [])]


def normalize_plan(raw, load_templates, existing=None):
    try:
        source = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(source, list) or not source:
            raise ValueError('请至少选择一个题库')
        result = []
        seen = set()
        def integer(value):
            if isinstance(value, bool) or not re.fullmatch(r'\d+', str(value)):
                raise ValueError('题库、题目编号和随机数量必须为整数')
            return int(value)

        for row in source:
            paper_id = integer(row['paper_id'])
            random_count = integer(row.get('random_count', 0))
            if not isinstance(row.get('mandatory_ids', []), list):
                raise ValueError('指定题目必须为题目编号列表')
            ids = [integer(value) for value in row.get('mandatory_ids', [])]
            if paper_id <= 0 or any(value <= 0 for value in ids) or paper_id in seen or len(ids) != len(set(ids)):
                raise ValueError('题库重复、指定题重复或随机数量无效')
            if not ids and random_count == 0:
                raise ValueError('已选题库至少指定一道题或随机抽一道题')
            result.append(dict(paper_id=paper_id, mandatory_ids=sorted(ids), random_count=random_count))
            seen.add(paper_id)
        result.sort(key=lambda item: item['paper_id'])
        # Preserve frozen/retired versions on metadata-only edits to an existing exam.
        if existing and public_plan(existing) == result:
            return json.loads(existing) if isinstance(existing, str) else existing
        for row in result:
            templates = load_templates(row['paper_id'])
            pool = sorted(int(item['id']) for item in templates if item.get('status') in (None, 'active'))
            if not set(row['mandatory_ids']).issubset(pool):
                raise ValueError('指定题已失效或不属于所选题库，请重新选择')
            if row['random_count'] > len(pool) - len(row['mandatory_ids']):
                raise ValueError('随机题数超过该题库剩余未指定的题目数量')
            row['pool_ids'] = pool
        return result
    except (KeyError, TypeError, json.JSONDecodeError, OverflowError) as error:
        raise ValueError('选题方案格式无效，请重新选择') from error


def plan_count(plan):
    return sum(len(row['mandatory_ids']) + row['random_count'] for row in plan)


def select_plan_questions(plan, available_ids, exam_id, user_id, secret):
    available = set(int(value) for value in available_ids)
    selected = []
    for row in plan:
        pool = set(row['pool_ids'])
        mandatory = set(row['mandatory_ids'])
        if not pool.issubset(available) or not mandatory.issubset(pool):
            raise ValueError('考试固定题目缺失，请联系管理员')
        remaining = pool - mandatory
        if row['random_count'] > len(remaining):
            raise ValueError('考试剩余题目不足，请联系管理员')
        selected.extend(sorted(mandatory))
        selected.extend(select_exam_template_ids(remaining, row['random_count'], exam_id, user_id, secret))
    if len(set(selected)) != len(selected):
        raise ValueError('考试选题方案中出现重复题目')
    return select_exam_template_ids(selected, len(selected), exam_id, user_id, f'{secret}:question-order')


def freeze_plan(cursor, exam_id, plan, enable_paper):
    # Called under the same exam-row lock as legacy pool changes.
    all_ids = []
    for row in plan:
        enable_paper(cursor, row['paper_id'])
        cursor.execute('SELECT name FROM exam_papers WHERE id = %s', (row['paper_id'],))
        paper = cursor.fetchone()
        if not paper:
            raise ValueError('所选题库不存在')
        row['paper_name'] = paper['name']
        ids = row['pool_ids']
        if not ids:
            raise ValueError('所选题库没有有效题目')
        placeholders = ','.join(['%s'] * len(ids))
        cursor.execute(f"SELECT id FROM problem_templates WHERE paper_id = %s AND id IN ({placeholders}) AND COALESCE(status, 'active') = 'active' FOR UPDATE",
                       [row['paper_id']] + ids)
        found = {int(item['id']) for item in cursor.fetchall()}
        if found != set(ids):
            raise ValueError('保存期间题库内容已变化，请刷新后重新选择')
        all_ids.extend(ids)
    cursor.execute('DELETE FROM exam_question_pool WHERE exam_id = %s', (exam_id,))
    cursor.executemany('INSERT INTO exam_question_pool (exam_id,template_id) VALUES (%s,%s)',
                       [(exam_id, value) for value in all_ids])
    cursor.execute('UPDATE exams SET question_plan = %s WHERE id = %s',
                   (json.dumps(plan, ensure_ascii=False), exam_id))
    return len(all_ids)


def describe_plan(plan):
    rows = json.loads(plan) if isinstance(plan, str) else plan
    return '、'.join(row.get('paper_name') or f"题库 {row['paper_id']}" for row in rows)
