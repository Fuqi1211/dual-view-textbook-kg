import unittest

from audit_paragraph_boundaries import SourceIndex, normalize, evaluate_match, text_value


class BoundaryAuditTests(unittest.TestCase):
    def test_real_graph_text_store_is_a_list(self):
        graph={'paragraph_texts':[{'id':'t1','text':'abc'}]}
        self.assertEqual(text_value(graph,{'text_ref':{'canonical_text_id':'t1','start':1,'end':3}}),'bc')

    def test_matching_preserves_distinct_physical_copies(self):
        rows = [dict(type='text', text='这是同一段。', page_idx=p) for p in (1, 8)]
        result = SourceIndex(rows, ['这是同一段。']).locate('这是同一段。')
        self.assertEqual(len(result), 2)
        self.assertEqual([r['row_indices'] for r in result], [[0], [1]])

    def test_inside_block_is_not_full_paragraph(self):
        index = SourceIndex([dict(type='text', text='第一句。第二句。', page_idx=1)], ['第二句。'])
        match = index.locate('第二句。')[0]
        self.assertEqual(match['match_scope'], 'within_ocr_block')
        self.assertEqual(evaluate_match(match, index.records)[0], 'boundary_review_required')

    def test_exact_full_block_does_not_claim_pdf_verified(self):
        index = SourceIndex([dict(type='text', text='原书的一段。', page_idx=1)], ['原书的一段。'])
        match = index.locate('原书的一段。')[0]
        self.assertEqual(match['match_scope'], 'full_ocr_block')
        self.assertEqual(evaluate_match(match, index.records)[0], 'ocr_block_agreement_only')

    def test_joined_blocks_are_explicitly_detected(self):
        index = SourceIndex([dict(type='text',text='前半句，',page_idx=1),
                             dict(type='text',text='后半句。',page_idx=2)], ['前半句，后半句。'])
        match = index.locate('前半句，后半句。')[0]
        self.assertEqual(match['match_scope'], 'multiple_ocr_blocks')
        self.assertEqual(match['row_indices'], [0, 1])

    def test_header_cannot_bridge_two_body_fragments(self):
        index = SourceIndex([dict(type='text',text='前半',page_idx=1),
                             dict(type='header',text='页眉',page_idx=2),
                             dict(type='text',text='后半',page_idx=2)], ['前半后半'])
        match = index.locate('前半后半')[0]
        self.assertEqual(match['row_indices'], [0,2])
        self.assertEqual(match['match_scope'], 'multiple_ocr_blocks')

    def test_punctuation_and_hyphens_are_not_erased(self):
        self.assertNotEqual(normalize('A-B'), normalize('AB'))
        self.assertNotEqual(normalize('1.5'), normalize('15'))

    def test_metadata_is_not_natural_prose(self):
        index = SourceIndex([dict(type='text',text='2.1 标题',text_level=1,page_idx=1)], ['2.1 标题'])
        match = index.locate('2.1 标题')[0]
        status, reasons = evaluate_match(match, index.records)
        self.assertEqual(status, 'boundary_review_required')
        self.assertIn('ocr_title_used_as_paragraph', reasons)

    def test_no_forced_match_for_ocr_correction(self):
        index = SourceIndex([dict(type='text',text='送人ID2',page_idx=1)], ['送入ID2'])
        self.assertEqual(index.locate('送入ID2'), [])


if __name__ == '__main__':
    unittest.main()
