from services.exam_assignment_service import select_exam_template_ids


def test_selection_is_stable_for_same_student_and_exam():
    template_ids = list(range(1, 21))

    first = select_exam_template_ids(template_ids, 8, 4, 12, 'secret')
    second = select_exam_template_ids(reversed(template_ids), 8, 4, 12, 'secret')

    assert first == second
    assert len(first) == 8
    assert len(set(first)) == 8


def test_students_receive_independent_selections_and_orders():
    template_ids = list(range(1, 31))

    first_student = select_exam_template_ids(template_ids, 12, 7, 101, 'secret')
    second_student = select_exam_template_ids(template_ids, 12, 7, 102, 'secret')

    assert first_student != second_student
    assert set(first_student).issubset(template_ids)
    assert set(second_student).issubset(template_ids)


def test_selection_count_is_capped_by_available_templates():
    assert len(select_exam_template_ids([3, 5, 7], 20, 1, 1, 'secret')) == 3
