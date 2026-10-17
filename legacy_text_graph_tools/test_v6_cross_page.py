# -*- coding: utf-8 -*-
import unittest
from build_layout_ownership_v6 import resolve_cross_page
class CrossPageTests(unittest.TestCase):
    def pair(self,indent=0,right=498):
        af={'left':100,'right':500,'em':10,'top':70,'bottom':700,'single_column':True};bf=dict(af,left=80,right=480)
        a={'pdf_page':1,'pixel_supported':True,'text':'数据传','ink_bbox':[100,690,right,700],'page_size':[600,800],'page_frame':af,'special':False}
        b={'pdf_page':2,'pixel_supported':True,'text':'输过程','ink_bbox':[80+indent,70,480,80],'page_size':[600,800],'page_frame':bf,'special':False,'list_start':False}
        return a,b
    def test_small_ink_margin_difference_is_continuation(self):
        a,b=self.pair(indent=7);self.assertEqual(resolve_cross_page(a,b,[])[0],'continue')
    def test_indented_new_paragraph_is_not_cleared(self):
        a,b=self.pair(indent=20);self.assertEqual(resolve_cross_page(a,b,[])[0],'unknown')
    def test_short_line_requires_obstruction(self):
        a,b=self.pair(right=350);self.assertEqual(resolve_cross_page(a,b,[])[0],'unknown')
        self.assertEqual(resolve_cross_page(a,b,[{'page_idx':0,'type':'image','bbox':[620,800,900,880]}])[0],'continue')
    def test_terminal_sentence_is_not_cleared(self):
        a,b=self.pair();a['text']='完成。';self.assertEqual(resolve_cross_page(a,b,[])[0],'unknown')
if __name__=='__main__':unittest.main()
