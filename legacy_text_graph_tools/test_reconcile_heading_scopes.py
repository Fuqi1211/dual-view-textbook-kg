# -*- coding: utf-8 -*-
import unittest
from reconcile_heading_scopes import numbered_title
class TitleTests(unittest.TestCase):
    def test_chinese_chapter_number(self):self.assertEqual(numbered_title('第一章 绪论'),('chapter','1','绪论'))
    def test_two_digit_chinese_chapter(self):self.assertEqual(numbered_title('第十二章 人工智能的争论与展望'),('chapter','12','人工智能的争论与展望'))
    def test_spaced_numbered_heading(self):self.assertEqual(numbered_title('6. 1. 2 I/O方式概述'),('section','6.1.2','I/O方式概述'))
    def test_local_item_not_structural_section(self):self.assertIsNone(numbered_title('1.第一代微处理器'))
    def test_ocr_io_local_item_not_section(self):self.assertIsNone(numbered_title('2.1/0设备'))
    def test_ip_address_is_not_heading(self):self.assertIsNone(numbered_title('255.255.0.0'))
if __name__=='__main__':unittest.main()
