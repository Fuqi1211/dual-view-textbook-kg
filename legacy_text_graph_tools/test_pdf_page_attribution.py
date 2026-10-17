import unittest
from extract_pdf_line_geometry import attribute_line_pages

def ln(text,y,cross=False):
    return {'bbox':[10,y,90,y+10],'spans':[{'type':'text','content':text,'cross_page':cross}]}

class PageAttributionTests(unittest.TestCase):
    def test_cross_page_lines_use_matching_original_page(self):
        a,b,c=ln('start',800),ln('continuation',50,True),ln('next',70,True)
        pages=[{'preproc_blocks':[{'lines':[a]}]}, {'preproc_blocks':[{'lines':[ln('continuation',50),ln('next',70)]}]}]
        self.assertEqual(attribute_line_pages([a,b,c],0,pages),[0,1,1])
    def test_multiple_page_record_is_resolved_by_source_lines(self):
        pages=[{'preproc_blocks':[{'lines':[ln(str(i),50)]}]} for i in range(3)]
        self.assertEqual(attribute_line_pages([ln('0',50),ln('1',50,True),ln('2',50,True)],0,pages),[0,1,2])
    def test_missing_cross_page_evidence_is_never_guessed(self):
        self.assertEqual(attribute_line_pages([ln('missing',50,True)],0,[{},{}]),[None])

if __name__=='__main__':unittest.main()
