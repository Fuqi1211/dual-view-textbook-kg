# -*- coding: utf-8 -*-
import unittest
from pdf_paragraph_rules import decide_boundary
def line(box,text,**kw):return dict(ink_bbox=box,text=text,pixel_supported=True)|kw
class PDFParagraphRuleTests(unittest.TestCase):
    def test_indented_new_paragraph_after_short_previous_line(self):
        self.assertEqual(decide_boundary(line([40,20,250,30],'上一自然段。'),line([56,34,430,44],'下一个自然段'),40,435,10,14)[0],'split')
    def test_normal_line_wrap_does_not_create_paragraph(self):
        self.assertEqual(decide_boundary(line([56,20,431,30],'尚未写完的'),line([41,34,432,44],'同一自然段'),40,435,10,14)[0],'continue')
    def test_page_break_does_not_create_paragraph(self):
        self.assertEqual(decide_boundary(line([41,620,431,630],'之间的区'),line([40,55,432,65],'别主要在于'),40,435,10,14,True)[0],'continue')
    def test_next_page_indented_paragraph_must_stay_separate(self):
        self.assertEqual(decide_boundary(line([41,620,431,630],'一段到此结束。'),line([57,55,432,65],'下一自然段'),40,435,10,14,True)[0],'split')
    def test_large_vertical_gap_with_full_stop_is_new_paragraph(self):
        self.assertEqual(decide_boundary(line([40,20,230,30],'上一段。'),line([40,50,432,60],'下一段'),40,435,10,14)[0],'split')
    def test_missing_pixel_support_never_auto_changes(self):
        self.assertEqual(decide_boundary(line([40,20,230,30],'上一段。'),line([56,34,432,44],'下一段',pixel_supported=False),40,435,10,14)[0],'unknown')
    def test_display_formula_is_not_prose_boundary_evidence(self):
        self.assertEqual(decide_boundary(line([40,20,430,30],'公式如下'),line([180,34,280,44],'x=y',special=True),40,435,10,14)[0],'unknown')
    def test_ip_address_is_not_a_numbered_list_item(self):
        self.assertNotEqual(decide_boundary(line([40,20,430,30],'（4）'),line([40,34,320,44],'255.252.0.0',list_start=True),40,435,10,14)[0],'split')
    def test_lost_bullet_cannot_trigger_join(self):
        self.assertEqual(decide_boundary(line([40,20,430,30],'一项'),line([40,34,432,44],'另一项',list_start=True),40,435,10,14)[0],'unknown')
if __name__=='__main__':unittest.main()
