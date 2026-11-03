"""Native-resolution thermal receipts with Thai shaping and a clear typographic grid."""
from __future__ import annotations

import math
import unicodedata
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from PIL import Image, ImageChops, ImageDraw

from .dataset import ROOT
from .care import advice_for_record

FONT_PATH = ROOT / 'assets' / 'fonts' / 'NotoSansThai.ttf'


def clean_text(value):
    value = str(value or '')
    return unicodedata.normalize('NFC', ''.join(c if not unicodedata.category(c).startswith('C') else ' ' for c in value)).strip()


class ThaiFont:
    """Shape with HarfBuzz; hint and rasterize each glyph at the printer's own pixels."""
    def __init__(self, size, font_path=FONT_PATH, weight=500):
        import freetype
        import uharfbuzz as hb
        self.hb, self.ft = hb, freetype
        self.data = font_path.read_bytes()
        self.font = hb.Font(hb.Face(self.data))
        self.font.scale = (size * 64, size * 64)
        self.font.set_variations({'wght':weight, 'wdth':100})
        hb.ot_font_set_funcs(self.font)
        self.face = freetype.Face(str(font_path))
        axes = self.face.get_variation_info().axes
        self.face.set_var_design_coords([weight if axis.tag == 'wght' else axis.default for axis in axes])
        self.face.set_pixel_sizes(0, size)
        self.ascent = math.ceil(self.face.size.ascender / 64)
        self.height = math.ceil((self.face.size.ascender - self.face.size.descender) / 64) + 3

    def glyphs(self, text):
        import regex
        pen = 0.
        for run in regex.findall(r'\p{Thai}+|[^\p{Thai}]+', text):
            buffer = self.hb.Buffer()
            buffer.add_str(run)
            buffer.guess_segment_properties()
            self.hb.shape(self.font, buffer)
            for info, position in zip(buffer.glyph_infos, buffer.glyph_positions):
                yield info.codepoint, pen + position.x_offset / 64, position.y_offset / 64, position.x_advance / 64
                pen += position.x_advance / 64

    def width(self, text):
        return sum(item[3] for item in self.glyphs(text))

    def draw(self, image, text, x, y):
        coverage = Image.new('L', (image.width, self.height), 0)
        for glyph_id, dx, dy, _ in self.glyphs(text):
            self.face.load_glyph(glyph_id, self.ft.FT_LOAD_RENDER | self.ft.FT_LOAD_TARGET_MONO | self.ft.FT_LOAD_MONOCHROME)
            glyph = self.face.glyph
            bitmap = glyph.bitmap
            if not bitmap.width or not bitmap.rows: continue
            raw = bytes(bitmap.buffer)
            pitch = abs(bitmap.pitch)
            row_bytes = (bitmap.width + 7) // 8
            rows = [raw[i*pitch:i*pitch+row_bytes] for i in range(bitmap.rows)]
            if bitmap.pitch < 0: rows.reverse()
            mask = Image.frombytes('1', (bitmap.width, bitmap.rows), b''.join(rows)).convert('L')
            left = round(x + dx + glyph.bitmap_left)
            top = round(self.ascent - dy - glyph.bitmap_top)
            layer = Image.new('L', coverage.size, 0)
            layer.paste(mask, (left, top))
            coverage = ImageChops.lighter(coverage, layer)
        image.paste(0, (0, round(y), image.width, round(y)+self.height), coverage)


@lru_cache(maxsize=1)
def thai_break_parser():
    import budoux
    return budoux.load_default_thai_parser()


def wrap_text(text, font, width):
    """Prefer Thai phrase/space boundaries, falling back only for an oversized token."""
    import regex
    text = clean_text(text)
    if not text: return
    clusters = regex.findall(r'\X', text)
    boundaries = set()
    offset = 0
    for phrase in thai_break_parser().parse(text):
        offset += len(phrase); boundaries.add(offset)
    boundaries.update(match.end() for match in regex.finditer(r'\s+', text))
    preferred = set()
    offset = 0
    for i, cluster in enumerate(clusters, 1):
        offset += len(cluster)
        if offset in boundaries and (i == len(clusters) or clusters[i][0] not in ',.;:!?)]}%'):
            preferred.add(i)
    start = 0
    while start < len(clusters):
        end = start
        while end < len(clusters) and font.width(''.join(clusters[start:end+1])) <= width:
            end += 1
        if end == start: raise ValueError('ตัวอักษรใหญ่กว่าพื้นที่พิมพ์')
        if end == len(clusters): cut = end
        else: cut = max((i for i in preferred if start < i <= end), default=end)
        line = ''.join(clusters[start:cut]).strip()
        if line: yield line
        start = cut
        while start < len(clusters) and clusters[start].isspace(): start += 1


def receipt_layout(row, width=384):
    """Return drawing operations; every text column is measured in native pixels."""
    if not 192 <= width <= 576 or width % 8:
        raise ValueError('ความกว้างใบผลตรวจต้องเป็น 192–576 จุด และหาร 8 ลงตัว')
    fonts = dict(brand=ThaiFont(26,weight=700), title=ThaiFont(23,weight=600),
                 result=ThaiFont(29,weight=700), body=ThaiFont(22,weight=550),
                 strong=ThaiFont(22,weight=650), small=ThaiFont(19,weight=500))
    margin = 16 if width >= 320 else 12
    right = width - margin
    content = right - margin
    layout = []
    y = 10

    def text(value, style='body', align='left', x=None, available=None):
        nonlocal y
        font = fonts[style]
        x = margin if x is None else x
        available = content if available is None else available
        for line in wrap_text(value,font,available):
            left = x + (available-font.width(line))/2 if align == 'center' else x
            layout.append(dict(kind='text',style=style,text=line,x=left,y=y,max_width=available))
            y += font.height

    def space(pixels=8):
        nonlocal y
        y += pixels

    def rule(heavy=False):
        nonlocal y
        space(9)
        layout.append(dict(kind='rule',x=margin,y=y,right=right-1,thickness=2 if heavy else 1))
        y += 12

    def pair(label,value):
        nonlocal y
        label_font = fonts['small']
        label_width = min(84,content*.30)
        value_x = margin + label_width + 8
        value_width = right - value_x
        lines = list(wrap_text(value,fonts['body'],value_width))
        label_y = y + (fonts['body'].height-label_font.height)//2
        layout.append(dict(kind='text',style='small',text=label,x=margin,y=label_y,max_width=label_width))
        for line in lines:
            layout.append(dict(kind='text',style='body',text=line,x=value_x,y=y,max_width=value_width))
            y += fonts['body'].height

    prediction = row['prediction']
    best = prediction['candidates'][0]
    captured = datetime.fromisoformat(row['captured_at'].replace('Z','+00:00'))
    if captured.tzinfo is None: captured = captured.replace(tzinfo=timezone.utc)
    captured = captured.astimezone(timezone(timedelta(hours=7)))

    text('AI PLANT DEVICE','brand','center')
    text('ใบผลวิเคราะห์พืช','title','center')
    if row['id'].startswith('TEST-'):
        space(3); text('ใบตัวอย่าง / TEST PRINT','small','center')
    rule(True)
    pair('วันที่',captured.strftime('%d/%m/%Y'))
    pair('เวลา',captured.strftime('%H:%M:%S')+' น.')
    pair('รหัส',row['id'][:12])
    rule()

    text('ผลวิเคราะห์เบื้องต้น','small','center')
    space(3)
    text('ยังระบุไม่ได้' if prediction['uncertain'] else best['disease_th'],'result','center')
    text('พืช: '+best['crop_th'],'body','center')
    if prediction['uncertain']:
        space(3);text('โปรดถ่ายภาพใหม่ให้ใบชัดเจน','small','center')
    rule(True)

    header = 'กลุ่มที่ใกล้เคียง' if prediction['uncertain'] else 'ผลที่ใกล้เคียง'
    score_font = fonts['strong']
    score_width = max(78,math.ceil(score_font.width('100.0%')))
    name_x = margin + 24
    score_x = right - score_width
    name_width = score_x - 12 - name_x
    if name_width < 110:
        # Narrow calibration widths retain a readable stacked layout.
        text(header,'strong')
        for index,item in enumerate(prediction['candidates'][:3],1):
            text(f'{index}. {item["disease_th"]}','body')
            text(f'คะแนนโมเดล {item["score"]*100:.1f}%','strong');space(5)
    else:
        header_top = y
        text(header,'strong',available=score_x-margin-12)
        caption = 'คะแนน'
        layout.append(dict(kind='text',style='small',text=caption,x=right-fonts['small'].width(caption),
                           y=header_top+3,max_width=score_width))
        for index,item in enumerate(prediction['candidates'][:3],1):
            top = y
            lines = list(wrap_text(item['disease_th'],fonts['body'],name_width))
            layout.append(dict(kind='text',style='small',text=str(index),x=margin,y=top+2,max_width=20))
            for line in lines:
                layout.append(dict(kind='text',style='body',text=line,x=name_x,y=y,max_width=name_width))
                y += fonts['body'].height
            score = f'{item["score"]*100:.1f}%'
            layout.append(dict(kind='text',style='strong',text=score,x=right-score_font.width(score),y=top,max_width=score_width))
            space(7)
    rule()

    text('จุดตรวจและการประเมิน','strong')
    location = {'simulated':'พิกัดจำลอง','manual':'พิกัดผู้ใช้ระบุ'}.get(row['location_source'],'แหล่งพิกัดไม่ทราบ')
    text(location,'small')
    text(f'{row["latitude"]:.6f}, {row["longitude"]:.6f}','body')
    space(5)
    severity = {'unassessed':'ยังไม่ประเมิน','mild':'เล็กน้อย','severe':'รุนแรง'}.get(row['severity'],'ยังไม่ประเมิน')
    text('ระดับอาการ: '+severity,'body')
    text('ประเมินโดยผู้ตรวจ','small')
    if row.get('note'):
        space(8);text('หมายเหตุ','strong');text(row['note'][:500],'body')
    rule()
    care = advice_for_record(row)
    text('คำแนะนำดูแลเบื้องต้น','strong')
    if care['urgent']:
        text('ผู้ตรวจระบุรุนแรง ติดต่อเจ้าหน้าที่โดยเร็ว','small')
    if care['status'] in ('uncertain', 'unsupported'):
        text('ยังไม่เลือกวิธีจัดการเฉพาะโรค','small')
    for index, line in enumerate(care['receipt_lines'][:2 if width < 320 else 3], 1):
        text(f'{index}. {line}','small')
    text('ดูรายละเอียดและอ้างอิงในแอป','small')
    basis = 'ฉบับคู่มือปัจจุบัน' if care['basis'] == 'current_reference' else 'ฉบับเก็บพร้อมผลตรวจ'
    text(basis+' '+care['knowledge_version'],'small')
    rule()
    text('คะแนน AI ไม่ใช่โอกาสยืนยันโรค','small')
    text('ควรตรวจอาการจริงประกอบ','small')
    if prediction.get('model_id'):
        space(4);text('Model: '+prediction['model_id'][:12],'small')
    text('เวลาประเทศไทย UTC+07','small')
    if y > 4096: raise ValueError('ใบผลตรวจยาวเกินไป กรุณาย่อหมายเหตุ')
    return fonts,layout,y+16


def render_receipt(row, width=384):
    fonts,layout,height = receipt_layout(row,width)
    image = Image.new('L',(width,height),255)
    draw = ImageDraw.Draw(image)
    for item in layout:
        if item['kind']=='rule':
            draw.line((item['x'],item['y'],item['right'],item['y']),fill=0,width=item['thickness'])
        else:
            fonts[item['style']].draw(image,item['text'],item['x'],item['y'])
    # Glyphs are already binary at native resolution: no scaling or dithering.
    return image.convert('1',dither=Image.Dither.NONE)


def escpos_commands(image, fragment_height=64, feed_lines=4):
    from escpos.printer import Dummy
    commands = [b'\x1b@', b'\x1ba\x00']
    for top in range(0,image.height,fragment_height):
        printer = Dummy()
        printer.profile.profile_data = {**printer.profile.profile_data,'media':{'width':{'pixels':image.width}}}
        printer.image(image.crop((0,top,image.width,min(top+fragment_height,image.height))),
                      impl='bitImageRaster',fragment_height=fragment_height)
        commands.append(printer.output)
    commands.append(b'\n'*feed_lines)
    return commands
