import unittest
from audit_heading_ownership import HeadingIndex


class HeadingAuditTests(unittest.TestCase):
    def setUp(self):
        self.headings=[{'id':'h1','type':'chapter','label':'第1章 基础','title':'基础','marker':'第1章'},
                       {'id':'s1','type':'section','label':'1.1 编码','title':'编码','marker':'1.1'},
                       {'id':'h2','type':'chapter','label':'第2章 运行','title':'运行','marker':'第2章'}]
        self.rows=[{'type':'text','text':'第1章 基础','text_level':1,'page_idx':1},
                   {'type':'text','text':'1.1 编码','text_level':1,'page_idx':2},
                   {'type':'text','text':'正文','page_idx':2},
                   {'type':'text','text':'第2章 运行','text_level':1,'page_idx':3},
                   {'type':'text','text':'运行正文','page_idx':3}]
        self.parents={'s1':'h1'}

    def test_direct_and_ancestor_are_distinct(self):
        index=HeadingIndex(self.headings,self.rows,self.parents)
        self.assertEqual(index.evaluate('s1',[2])['status'],'ocr_direct_owner_supported')
        self.assertEqual(index.evaluate('h1',[2])['status'],'ocr_ancestor_coverage_supported')

    def test_wrong_chapter_conflict(self):
        index=HeadingIndex(self.headings,self.rows,self.parents)
        self.assertEqual(index.evaluate('h1',[4])['status'],'ownership_conflict_candidate')

    def test_unknown_heading_prevents_false_pass(self):
        self.rows.insert(2,{'type':'text','text':'1.2 未入树标题','text_level':1,'page_idx':2})
        index=HeadingIndex(self.headings,self.rows,self.parents)
        self.assertEqual(index.evaluate('s1',[3])['status'],'unresolved_heading_evidence')

    def test_duplicate_heading_locations_not_resolved_by_declared_page(self):
        self.rows.insert(0,dict(self.rows[1]))
        self.headings[1]['pdf_page_start']=3
        index=HeadingIndex(self.headings,self.rows,self.parents)
        self.assertEqual(index.anchors['s1']['status'],'ambiguous_ocr_heading')

    def test_multiblock_crosses_section(self):
        index=HeadingIndex(self.headings,self.rows,self.parents)
        self.assertEqual(index.evaluate('s1',[2,4])['status'],'crosses_heading_boundary_candidate')

    def test_unmapped_subheading_blocks_section_but_not_chapter(self):
        self.rows.insert(2,{'type':'text','text':'深入讨论','text_level':1,'page_idx':2})
        index=HeadingIndex(self.headings,self.rows,self.parents)
        self.assertEqual(index.evaluate('s1',[3])['status'],'unresolved_heading_evidence')
        self.assertEqual(index.evaluate_chapter('s1',[3])['status'],'ocr_chapter_supported')

    def test_unmapped_new_chapter_blocks_chapter_support(self):
        self.rows.insert(2,{'type':'text','text':'第3章 未入树章节','text_level':1,'page_idx':2})
        index=HeadingIndex(self.headings,self.rows,self.parents)
        self.assertEqual(index.evaluate_chapter('s1',[3])['status'],'unresolved_chapter_evidence')

    def test_unique_short_chapter_title_is_valid_ocr_anchor(self):
        self.rows[3]['text']='运 行'
        index=HeadingIndex(self.headings,self.rows,self.parents)
        self.assertEqual(index.evaluate_chapter('h2',[4])['status'],'ocr_chapter_supported')


if __name__=='__main__':unittest.main()
