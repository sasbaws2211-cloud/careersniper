"""The free job search tracker: a real Excel workbook, built here with no extra library (an .xlsx is a zip of small XML files).

Three sheets: Applications (one row per job, a status list, a days-since-applied formula and highlighting for follow-ups that are due), Summary (counts and rates
that update themselves) and How to use. It opens in Excel and Google Sheets, and nothing in it needs a macro.
"""
from __future__ import annotations

import io
import zipfile
from xml.sax.saxutils import escape

STATUSES = ("Saved", "Applied", "Interviewing", "Offer", "Rejected", "Withdrawn")
COLUMNS = (("Company", 24), ("Job title", 28), ("Link to the posting", 34), ("Where you found it", 20), ("Date applied", 14), ("Status", 15), ("Next step", 28),
           ("Follow up on", 14), ("Salary or range", 18), ("Contact", 24), ("Notes", 42), ("Days since applied", 14))
ROWS = 500                      # rows set up with the list, the date format and the formula
LAST = ROWS + 1
NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

# cell styles (the index is the position in cellXfs below)
S_DEFAULT, S_HEADER, S_DATE, S_WRAP, S_BOLD, S_TITLE, S_PERCENT, S_SMALL = range(8)


def _col(n: int) -> str:
    s = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _text(ref: str, value: str, style: int = S_DEFAULT) -> str:
    return f'<c r="{ref}" s="{style}" t="inlineStr"><is><t xml:space="preserve">{escape(value)}</t></is></c>'


def _formula(ref: str, formula: str, style: int = S_DEFAULT) -> str:
    return f'<c r="{ref}" s="{style}"><f>{escape(formula)}</f></c>'


def _empty(ref: str, style: int) -> str:
    return f'<c r="{ref}" s="{style}"/>'


def _row(n: int, cells: list[str], height: float | None = None) -> str:
    ht = f' ht="{height}" customHeight="1"' if height else ""
    return f'<row r="{n}"{ht}>' + "".join(cells) + "</row>"


def _sheet(body: str, cols: str = "", views: str = "", after: str = "") -> str:
    views = views or '<sheetView workbookViewId="0"/>'
    return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<worksheet xmlns="{NS}" xmlns:r="{REL}">'
            f'<sheetViews>{views}</sheetViews><sheetFormatPr defaultRowHeight="15"/>{cols}<sheetData>{body}</sheetData>{after}'
            '<pageMargins left="0.7" right="0.7" top="0.75" bottom="0.75" header="0.3" footer="0.3"/></worksheet>')


def applications_sheet() -> str:
    cols = "<cols>" + "".join(f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>' for i, (_, w) in enumerate(COLUMNS)) + "</cols>"
    rows = [_row(1, [_text(f"{_col(i)}1", name, S_HEADER) for i, (name, _) in enumerate(COLUMNS)], 32)]
    for r in range(2, LAST + 1):
        cells = []
        for i in range(len(COLUMNS)):
            ref = f"{_col(i)}{r}"
            if i == 11:
                cells.append(_formula(ref, f'IF(E{r}="","",TODAY()-E{r})'))
            elif i in (4, 7):
                cells.append(_empty(ref, S_DATE))
            elif i in (6, 10):
                cells.append(_empty(ref, S_WRAP))
            else:
                cells.append(_empty(ref, S_DEFAULT))
        rows.append(_row(r, cells))
    views = '<sheetView workbookViewId="0" tabSelected="1"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/><selection pane="bottomLeft" activeCell="A2" sqref="A2"/></sheetView>'
    last_col = _col(len(COLUMNS) - 1)
    after = (f'<autoFilter ref="A1:{last_col}{LAST}"/>'
             # a follow-up that is due (today or earlier) on a job still in play is highlighted
             f'<conditionalFormatting sqref="H2:H{LAST}"><cfRule type="expression" dxfId="0" priority="1"><formula>AND($H2&lt;&gt;"",$H2&lt;=TODAY(),OR($F2="Applied",$F2="Interviewing"))</formula></cfRule></conditionalFormatting>'
             f'<dataValidations count="1"><dataValidation type="list" allowBlank="1" showInputMessage="1" showErrorMessage="1" errorTitle="Pick a status" '
             f'error="Choose one of the statuses in the list." promptTitle="Status" prompt="Pick from the list." sqref="F2:F{LAST}"><formula1>"{",".join(STATUSES)}"</formula1></dataValidation></dataValidations>')
    return _sheet("".join(rows), cols, views, after)


def summary_sheet() -> str:
    rng = f"Applications!$F$2:$F${LAST}"
    rows = [_row(1, [_text("A1", "Your job search at a glance", S_TITLE)], 28),
            _row(3, [_text("A3", "Status", S_HEADER), _text("B3", "Jobs", S_HEADER)])]
    for i, status in enumerate(STATUSES):
        n = 4 + i
        rows.append(_row(n, [_text(f"A{n}", status), _formula(f"B{n}", f'COUNTIF({rng},A{n})')]))
    end = 4 + len(STATUSES)
    first, last = 4, end - 1
    rows.append(_row(end, [_text(f"A{end}", "Total", S_BOLD), _formula(f"B{end}", f"SUM(B{first}:B{last})", S_BOLD)]))
    applied = f"SUM(B5:B{last})"      # everything past 'Saved'
    r = end + 2
    rows.append(_row(r, [_text(f"A{r}", "Applied so far", S_BOLD), _formula(f"B{r}", applied, S_BOLD)]))
    rows.append(_row(r + 1, [_text(f"A{r + 1}", "Reached an interview or offer", S_DEFAULT), _formula(f"B{r + 1}", "B6+B7")]))
    rows.append(_row(r + 2, [_text(f"A{r + 2}", "Interview rate", S_DEFAULT), _formula(f"B{r + 2}", f'IF(B{r}=0,"",B{r + 1}/B{r})', S_PERCENT)]))
    rows.append(_row(r + 3, [_text(f"A{r + 3}", "Follow-ups due now", S_DEFAULT),
                             _formula(f"B{r + 3}", f'SUMPRODUCT((Applications!$H$2:$H${LAST}<>"")*(Applications!$H$2:$H${LAST}<=TODAY())*((Applications!$F$2:$F${LAST}="Applied")+(Applications!$F$2:$F${LAST}="Interviewing")))')]))
    rows.append(_row(r + 5, [_text(f"A{r + 5}", "These numbers update by themselves from the Applications sheet. The interview rate is interviews and offers divided by jobs you have applied to.", S_SMALL)]))
    cols = '<cols><col min="1" max="1" width="38" customWidth="1"/><col min="2" max="2" width="12" customWidth="1"/></cols>'
    return _sheet("".join(rows), cols)


HOW = (
    ("How to use this tracker", S_TITLE),
    ("1. On the Applications sheet, add one row for each job you are interested in. Start with the company and job title.", S_DEFAULT),
    ("2. Paste the link to the posting, so you can find the exact job again. Note where you found it (a company career page, a referral, a job board).", S_DEFAULT),
    ("3. Fill in the date you applied and pick a status from the list: Saved, Applied, Interviewing, Offer, Rejected or Withdrawn.", S_DEFAULT),
    ("4. Write the next step and a follow-up date. A follow-up that is due turns yellow while the job is Applied or Interviewing.", S_DEFAULT),
    ("5. Keep the name and email of the person you spoke to, and short notes on each conversation, salary and what to ask next.", S_DEFAULT),
    ("6. Look at the Summary sheet now and then. It counts your jobs by status and shows how many applications lead to interviews.", S_DEFAULT),
    ("", S_DEFAULT),
    ("Tips", S_BOLD),
    ("- Follow up about a week after applying, then again a week later, and then let it go.", S_DEFAULT),
    ("- Save the job description somewhere safe. Postings are often taken down once the role is filled.", S_DEFAULT),
    ("- Use the filter arrows in the header row to see only one status, such as everything you are waiting to hear back on.", S_DEFAULT),
    ("- In Google Sheets: File, then Import, then Upload this file. The status list and formulas carry over.", S_DEFAULT),
    ("", S_DEFAULT),
    ("Made by CareerSniper. Free to use and share.", S_SMALL),
)


def how_sheet() -> str:
    rows = [_row(i + 1, [_text(f"A{i + 1}", t, s)] if t else [], 28 if s == S_TITLE else None) for i, (t, s) in enumerate(HOW)]
    return _sheet("".join(rows), '<cols><col min="1" max="1" width="120" customWidth="1"/></cols>')


STYLES = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="{NS}">
<numFmts count="1"><numFmt numFmtId="164" formatCode="dd mmm yyyy"/></numFmts>
<fonts count="6">
<font><sz val="11"/><name val="Calibri"/><family val="2"/></font>
<font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/><family val="2"/></font>
<font><b/><sz val="11"/><name val="Calibri"/><family val="2"/></font>
<font><b/><sz val="16"/><color rgb="FF1D4ED8"/><name val="Calibri"/><family val="2"/></font>
<font><i/><sz val="10"/><color rgb="FF64748B"/><name val="Calibri"/><family val="2"/></font>
<font><sz val="11"/><name val="Calibri"/><family val="2"/></font>
</fonts>
<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF1D4ED8"/><bgColor indexed="64"/></patternFill></fill></fills>
<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="8">
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1" applyAlignment="1"><alignment vertical="center" wrapText="1"/></xf>
<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1" applyAlignment="1"><alignment horizontal="left"/></xf>
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>
<xf numFmtId="0" fontId="2" fillId="0" borderId="0" xfId="0" applyFont="1"/>
<xf numFmtId="0" fontId="3" fillId="0" borderId="0" xfId="0" applyFont="1"/>
<xf numFmtId="9" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
<xf numFmtId="0" fontId="4" fillId="0" borderId="0" xfId="0" applyFont="1"/>
</cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
<dxfs count="1"><dxf><font><b/><color rgb="FF92400E"/></font><fill><patternFill><bgColor rgb="FFFDE68A"/></patternFill></fill></dxf></dxfs>
</styleSheet>"""

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/worksheets/sheet3.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>"""

ROOT_RELS = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="{REL}/officeDocument" Target="xl/workbook.xml"/></Relationships>"""

WORKBOOK = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="{NS}" xmlns:r="{REL}"><bookViews><workbookView activeTab="0"/></bookViews>
<sheets><sheet name="Applications" sheetId="1" r:id="rId1"/><sheet name="Summary" sheetId="2" r:id="rId2"/><sheet name="How to use" sheetId="3" r:id="rId3"/></sheets>
<calcPr calcId="191029" fullCalcOnLoad="1"/></workbook>"""

WORKBOOK_RELS = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="{REL}/worksheet" Target="worksheets/sheet1.xml"/>
<Relationship Id="rId2" Type="{REL}/worksheet" Target="worksheets/sheet2.xml"/>
<Relationship Id="rId3" Type="{REL}/worksheet" Target="worksheets/sheet3.xml"/>
<Relationship Id="rId4" Type="{REL}/styles" Target="styles.xml"/></Relationships>"""

FILENAME = "job-search-tracker.xlsx"
MEDIA = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_cached: bytes | None = None


def build() -> bytes:
    """The workbook as bytes. It never changes, so it is built once."""
    global _cached
    if _cached is None:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("[Content_Types].xml", CONTENT_TYPES)
            z.writestr("_rels/.rels", ROOT_RELS)
            z.writestr("xl/workbook.xml", WORKBOOK)
            z.writestr("xl/_rels/workbook.xml.rels", WORKBOOK_RELS)
            z.writestr("xl/styles.xml", STYLES)
            z.writestr("xl/worksheets/sheet1.xml", applications_sheet())
            z.writestr("xl/worksheets/sheet2.xml", summary_sheet())
            z.writestr("xl/worksheets/sheet3.xml", how_sheet())
        _cached = buf.getvalue()
    return _cached
