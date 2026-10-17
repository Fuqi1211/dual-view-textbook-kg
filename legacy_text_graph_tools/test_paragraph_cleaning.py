"""段落清理的无损边界测试。"""
import importlib.util
import inspect
from pathlib import Path
import unittest


MODULE = Path(__file__).with_name('paragraph_cleaning.py')
CANDIDATES = [
    {'id': 'chapter', 'marker': '第1章', 'title': '基础', 'label': '第1章 基础'},
    {'id': 'section', 'marker': '1.1', 'title': '定义', 'label': '1.1 定义'},
]


class CleaningTests(unittest.TestCase):
    def clean(self, text, candidates=CANDIDATES, **kwargs):
        self.assertTrue(MODULE.exists(), 'Lossless paragraph cleaning is not implemented')
        spec = importlib.util.spec_from_file_location('paragraph_cleaning', MODULE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for parameter in kwargs:
            self.assertIn(parameter, inspect.signature(module.clean_spans).parameters)
        spans = module.clean_spans(text, candidates, 'chapter', **kwargs)
        cursor = 0
        for span in spans:
            self.assertEqual(span['start'], cursor)
            self.assertGreater(span['end'], span['start'])
            cursor = span['end']
        self.assertEqual(cursor, len(text))
        self.assertEqual(''.join(text[s['start']:s['end']] for s in spans), text)
        return spans

    def pieces(self, text, spans, kind):
        return [text[s['start']:s['end']] for s in spans if s['kind'] == kind]

    def test_heading_echo_splits_without_blank_and_keeps_questions_and_sentence_mentions(self):
        text = '基础\n正文甲。\n1.1 定义\n正文提到 1.1 定义 但不是标题。\n\n1. 问题是什么？\n'
        spans = self.clean(text)
        self.assertEqual(self.pieces(text, spans, 'source_fragment'), ['基础\n', '1.1 定义\n'])
        self.assertEqual(self.pieces(text, spans, 'paragraph'), ['正文甲。\n', '正文提到 1.1 定义 但不是标题。\n', '1. 问题是什么？\n'])
        fragments = [s for s in spans if s['kind'] == 'source_fragment']
        self.assertEqual([s['matched_heading_ids'] for s in fragments], [['chapter'], ['section']])
        later = '正文\n基础\n定义\n'
        self.assertEqual(self.pieces(later, self.clean(later), 'paragraph'), [later])

    def test_known_atx_heading_is_fragment_but_unknown_bare_atx_stays_searchable(self):
        text = '前文\n## 1.1 定义 ##\n后文\n# 额外标题\n尾文'
        spans = self.clean(text)
        fragments = [s for s in spans if s['kind'] == 'source_fragment']
        self.assertEqual([s['fragment_kind'] for s in fragments], ['markdown_heading'])
        self.assertEqual([s['matched_heading_ids'] for s in fragments], [['section']])
        self.assertEqual(self.pieces(text, spans, 'paragraph'), ['前文\n', '后文\n# 额外标题\n尾文'])
        self.assertIn('potential_heading_review', spans[-1]['quality_flags'])
        structured = '正文\n## 6.4.1 通用缓存组织结构\n尾文\n'
        self.assertEqual(self.pieces(structured, self.clean(structured), 'source_fragment'),
                         ['## 6.4.1 通用缓存组织结构\n'])

    def test_indented_and_tab_fences_protect_blank_lines_and_heading_text(self):
        for indent in ['    ', '\t']:
            text = indent + '```python\n# 1.1 定义\n\nprint(1)\n' + indent + '```\n\n尾'
            spans = self.clean(text)
            paras = [s for s in spans if s['kind'] == 'paragraph']
            self.assertEqual([s['content_kind'] for s in paras], ['fenced_code', 'text'])
            self.assertEqual(self.pieces(text, spans, 'source_fragment'), [])
            self.assertIn('\n\nprint(1)', self.pieces(text, spans, 'paragraph')[0])

    def test_display_math_protects_blank_lines_and_empty_controls_are_fragments(self):
        text = '$$\na+b\n\nc+d\n$$\n\n\\[\nx+y\n\nz\n\\]\n\n$$\n\\tag{1} \\label{eq:x}\n$$\n'
        spans = self.clean(text)
        paras = [s for s in spans if s['kind'] == 'paragraph']
        self.assertEqual([s['content_kind'] for s in paras], ['math', 'math'])
        fragments = [s for s in spans if s['kind'] == 'source_fragment']
        self.assertEqual(len(fragments), 1)
        self.assertEqual(fragments[0]['fragment_kind'], 'empty_math')
        self.assertIn('empty_math_review', fragments[0]['quality_flags'])

    def test_images_only_are_fragments_but_inline_images_stay_searchable(self):
        text = '![图1](images/a.png)\n![图2](images/b.png)\n\n文字 ![图](images/c.png) 继续。'
        spans = self.clean(text)
        fragments = [s for s in spans if s['kind'] == 'source_fragment']
        self.assertEqual([s['fragment_kind'] for s in fragments], ['image_only'])
        self.assertEqual(self.pieces(text, spans, 'paragraph'), ['文字 ![图](images/c.png) 继续。'])
        self.assertEqual(self.pieces(text, spans, 'source_fragment'), ['![图1](images/a.png)\n![图2](images/b.png)\n'])

    def test_table_requires_header_delimiter_and_code_wins(self):
        text = '| 标题 | 值 |\n| --- | :---: |\n| 甲 | 1 |\n\n这只是 a | b。\n\n```\n| a | b |\n| --- | --- |\n```'
        spans = self.clean(text)
        paras = [s for s in spans if s['kind'] == 'paragraph']
        self.assertEqual([s['content_kind'] for s in paras], ['table', 'text', 'fenced_code'])

    def test_long_unclosed_and_ocr_text_is_preserved_with_review_flags(self):
        long_text = '人/入 1/IO ' + '长' * 4100
        spans = self.clean(long_text)
        self.assertEqual(self.pieces(long_text, spans, 'paragraph'), [long_text])
        self.assertIn('long_block_review', spans[0]['quality_flags'])
        for text, flag in [('```python\n\n# 基础\n', 'unclosed_fence_review'), ('\\[\na\n\n', 'unclosed_math_review')]:
            spans = self.clean(text)
            self.assertEqual(self.pieces(text, spans, 'paragraph'), [text])
            self.assertIn(flag, spans[0]['quality_flags'])

    def test_blank_prefix_unicode_separators_and_empty_field_are_lossless(self):
        self.assertEqual(self.clean(''), [])
        text = ' \n\t\n基础\n甲\u2028\u2028乙\n\n'
        spans = self.clean(text)
        self.assertEqual(self.pieces(text, spans, 'source_fragment'), ['基础\n'])
        self.assertEqual(self.pieces(text, spans, 'paragraph'), ['甲\u2028\u2028乙\n'])

    def test_math_with_trailing_prose_does_not_swallow_later_heading(self):
        text = '$$ x+y $$ 这是正文。\n1.1 定义\n后文'
        spans = self.clean(text)
        self.assertEqual(self.pieces(text, spans, 'source_fragment'), ['1.1 定义\n'])
        self.assertEqual(self.pieces(text, spans, 'paragraph'), ['$$ x+y $$ 这是正文。\n', '后文'])
        self.assertIn('mixed_or_unmatched_math_review', spans[0]['quality_flags'])

    def test_empty_math_layout_controls_are_not_invented_as_equation_content(self):
        text = '$$\n\\begin{aligned}\n\\tag{1}\\label{x}\\qquad\n\\end{aligned}\n$$'
        spans = self.clean(text)
        self.assertEqual([s['kind'] for s in spans], ['source_fragment'])
        self.assertEqual(spans[0]['fragment_kind'], 'empty_math')

    def test_verified_ocr_heading_matches_whole_line_only_outside_code_and_math(self):
        title = '6.4.1 分层缓存组织结构'
        text = ('段落引言。\n6.4.1   分层缓存组织结构\n相邻正文。\n'
                '正文说 6.4.1 分层缓存组织结构。\n\n'
                '```\n6.4.1 分层缓存组织结构\n```\n\n'
                '$$\n6.4.1 分层缓存组织结构\n$$\n\n'
                '6.4.2 其他编号不猜测\n')
        spans = self.clean(text, extra_heading_texts=[title])
        fragments = [s for s in spans if s['kind'] == 'source_fragment']
        self.assertEqual(self.pieces(text, spans, 'source_fragment'), ['6.4.1   分层缓存组织结构\n'])
        self.assertEqual(fragments[0]['fragment_kind'], 'ocr_heading')
        self.assertEqual(fragments[0]['matched_heading_ids'], [])
        self.assertEqual([s['content_kind'] for s in spans if s['kind'] == 'paragraph'],
                         ['text', 'text', 'fenced_code', 'math', 'text'])
        self.assertEqual(self.pieces(text, self.clean(text), 'source_fragment'), [])

    def test_ocr_heading_metadata_cannot_remove_program_body_or_bare_text(self):
        text = ('程序骨架如下：\nint main()   \nstatements   \nreturn 0;\n\n'
                'C++PrimerPlus（第6版）中文版\n1.5 seconds elapsed\n'
                '6.4.1 这是正文句。\n6.4.2 这是问题？\n6.4.3 f(x) = x;\n')
        extras = [line.strip() for line in text.splitlines() if line.strip()]
        spans = self.clean(text, extra_heading_texts=extras)
        self.assertEqual(self.pieces(text, spans, 'source_fragment'), [])
        self.assertEqual(len(self.pieces(text, spans, 'paragraph')), 2)
        self.assertIn('int main()   \nstatements   \nreturn 0;',
                      self.pieces(text, spans, 'paragraph')[0])

    def test_extra_heading_needs_structural_marker_and_full_line_match(self):
        titles = ['第十二章 存储器', '第3节 地址', '第二篇 结构', '第4部 基础',
                  '附录 A 指令表', 'A.3 常量表', '6.4.1 通用的高速缓存存储器组织结构',
                  'Chapter 2 Memory', 'Appendix A Tables', '2.3 Memory hierarchy']
        text = '\n'.join(titles) + '\n正文提到 ' + titles[6] + '，仍是正文。\n'
        spans = self.clean(text, extra_heading_texts=titles)
        fragments = [s for s in spans if s['kind'] == 'source_fragment']
        self.assertEqual(self.pieces(text, spans, 'source_fragment'), [t + '\n' for t in titles])
        self.assertTrue(all(s['fragment_kind'] == 'ocr_heading' and not s['matched_heading_ids']
                            for s in fragments))
        self.assertEqual(len(self.pieces(text, spans, 'paragraph')), 1)

    def test_c_preprocessor_directives_are_not_markdown_headings(self):
        text = '# include <stdio.h>\n# define X 1\n# if X\nint main()\n# endif\n'
        spans = self.clean(text, extra_heading_texts=text.splitlines())
        self.assertEqual(self.pieces(text, spans, 'source_fragment'), [])
        self.assertEqual(self.pieces(text, spans, 'paragraph'), [text])

    def test_ocr_atx_markup_does_not_remove_obvious_code_examples(self):
        text = ('下面是函数头：\n\n## int main()\n\n下面是函数调用：\n\n'
                '## simon(3);\n\n## return 0;\n\n## count = 2;\n\n'
                '## # include <stdio.h>\n')
        spans = self.clean(text)
        self.assertEqual(self.pieces(text, spans, 'source_fragment'), [])
        self.assertEqual(len(self.pieces(text, spans, 'paragraph')), 7)


if __name__ == '__main__':
    unittest.main()
