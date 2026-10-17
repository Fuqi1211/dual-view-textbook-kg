import unittest
from build_physical_graphs import split_at_markers, join_prose, normalized_positions, can_merge


class PhysicalGraphTests(unittest.TestCase):
    def test_three_visual_numbered_paragraphs_partition_without_loss(self):
        s='1.从上层收到数据。第一段。2.超时。第二段。3.收到ACK。第三段。'
        spans=split_at_markers(s,['2.超时','3.收到ACK'])
        self.assertEqual(len(spans),3)
        self.assertEqual(''.join(s[a:b] for a,b in spans),s)
        self.assertTrue(s[spans[1][0]:spans[1][1]].startswith('2.超时'))

    def test_missing_or_repeated_marker_does_not_guess(self):
        self.assertEqual(split_at_markers('没有标记',['2.超时']),[(0,4)])
        self.assertEqual(split_at_markers('2.超时又说2.超时',['2.超时']),[(0,10)])

    def test_cross_page_cjk_not_joined_with_space(self):
        self.assertEqual(join_prose('之间的区\n','别主要在于'), '之间的区别主要在于')

    def test_english_words_need_space(self):
        self.assertEqual(join_prose('the\n','system'), 'the system')

    def test_exact_raw_character_mapping(self):
        text=' a\\_b， c '
        normalized,positions=normalized_positions(text)
        self.assertEqual(normalized,'a_b,c')
        self.assertEqual([text[p] for p in positions],list('a_b，c'))

    def test_cannot_merge_over_formula_or_title(self):
        a={'type':'paragraph','heading_id':'h','ownership_status':'anchored','text':'连续的未完'}
        b={'type':'paragraph','heading_id':'h','ownership_status':'anchored','text':'句子。'}
        self.assertFalse(can_merge(a,b,[{'type':'equation'}]))
        self.assertFalse(can_merge(a,b,[{'type':'text','text_level':1}]))
        self.assertTrue(can_merge(a,b,[{'type':'page_number'},{'type':'header'}]))

    def test_cannot_merge_across_heading_or_unknown_scope(self):
        a={'type':'paragraph','heading_id':'h','ownership_status':'anchored','text':'连续的未完'}
        b={'type':'paragraph','heading_id':'h2','ownership_status':'anchored','text':'句子。'}
        self.assertFalse(can_merge(a,b,[]))
        b['heading_id']='h';b['ownership_status']='unresolved'
        self.assertFalse(can_merge(a,b,[]))

if __name__=='__main__':unittest.main()
