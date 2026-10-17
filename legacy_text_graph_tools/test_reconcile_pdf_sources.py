# -*- coding: utf-8 -*-
import unittest
from reconcile_pdf_sources import aligned_line_ranges
class AlignmentTests(unittest.TestCase):
    def test_short_complete_record_has_unambiguous_source(self):
        self.assertEqual(aligned_line_ranges('作者',['作者']),[(0,0,2)])
    def test_escaped_markdown_character_does_not_break_location(self):
        self.assertEqual(aligned_line_ranges('数字：0\\~9。',['数字：0~9。']),[(0,0,8)])
    def test_list_item_is_located_in_its_own_lines(self):
        self.assertEqual(aligned_line_ranges('第二条文献内容',['第一条文献内容','第二条','文献内容']),[(1,0,3),(2,3,7)])
    def test_repeated_short_line_requires_complete_alignment(self):
        self.assertEqual(aligned_line_ranges('甲乙甲乙',['甲乙','甲乙']),[(0,0,2),(1,2,4)])
    def test_wrong_list_item_pointer_is_rejected(self):
        self.assertIsNone(aligned_line_ranges('第二条文献',['第一条文献']))
    def test_duplicate_record_in_block_is_ambiguous(self):
        self.assertIsNone(aligned_line_ranges('甲乙',['甲乙','甲乙']))
    def test_printed_hyphenation_does_not_lose_citation(self):
        self.assertEqual(aligned_line_ranges('Occam razor',['Oc-','cam razor']),[(0,0,2),(1,2,11)])
if __name__=='__main__':unittest.main()
