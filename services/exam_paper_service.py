def paper_has_unfinished_exam(cursor, paper_id):
    """Return whether a published exam still has a future or running batch."""
    cursor.execute(
        """
        SELECT EXISTS(
            SELECT 1
            FROM exams e
            WHERE e.paper_id = %s
              AND e.status = 'published'
              AND (
                  EXISTS(
                      SELECT 1
                      FROM exam_batches b
                      WHERE b.exam_id = e.id
                        AND b.status = 'active'
                        AND (b.end_time IS NULL OR b.end_time > NOW())
                  )
                  OR (
                      NOT EXISTS(
                          SELECT 1 FROM exam_batches any_batch
                          WHERE any_batch.exam_id = e.id
                      )
                      AND (e.end_time IS NULL OR e.end_time > NOW())
                  )
              )
            LIMIT 1
        ) AS locked
        """,
        (paper_id,),
    )
    return bool((cursor.fetchone() or {}).get('locked'))


def ensure_exam_paper_enabled(cursor, paper_id):
    """Lock and enable an exam paper, returning True only when it changed."""
    cursor.execute(
        "SELECT id, is_enabled FROM exam_papers WHERE id = %s FOR UPDATE",
        (paper_id,),
    )
    paper = cursor.fetchone()
    if not paper:
        raise ValueError('所选题库不存在。')
    if bool(paper.get('is_enabled')):
        return False
    cursor.execute(
        "UPDATE exam_papers SET is_enabled = TRUE WHERE id = %s",
        (paper_id,),
    )
    return True
