# -*- coding: utf-8 -*-
import re
LIST=re.compile(r'^\s*(?:[•●◆■·]|\(?\d{1,3}(?:[.．](?!\d)|[、)])|[（(][一二三四五六七八九十\d]+[）)])')
END=re.compile(r'[。！？!?；;][”’"）)]*\s*$')
def decide_boundary(previous,current,left,right,em,step,across_page=False):
    if not all(x.get('pixel_supported') and not x.get('special') for x in (previous,current)):
        return 'unknown','missing_pdf_pixels_or_nonprose_line'
    a=previous['ink_bbox'];b=current['ink_bbox']
    indent=(b[0]-left)/em;short=(right-a[2])/em
    if not .65<=(b[3]-b[1])/max(1,a[3]-a[1])<=1.55:return 'unknown','font_or_geometry_change'
    if LIST.match(current['text']) and current.get('list_start'):
        return 'split','pdf_position_and_visible_list_marker'
    if current.get('list_start'):
        return 'unknown','list_marker_uncertain_do_not_join'
    if 1.15<=indent<=3.5 and (short>=1.1 or END.search(previous['text'])):
        return 'split','pdf_first_line_indent'
    gap=b[1]-a[1]
    if not across_page and gap>step*1.8 and short>1.5 and END.search(previous['text']):
        return 'split','pdf_paragraph_spacing'
    if abs(indent)<=.6 and short<=1.25 and (across_page or .65*step<=gap<=1.5*step):
        return 'continue','pdf_flush_continuation_after_full_line'
    return 'unknown','insufficient_paragraph_boundary_evidence'
