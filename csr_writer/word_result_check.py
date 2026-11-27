"""Read the saved Word tables and compare displayed cells against both selected and independent evidence."""
from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

from .io_utils import load_json_unique
from .project_table_match import CAPTION, data_rows, find_layout


def check_word(path,out,report,ledger,qc):
    doc=Document(path)
    tables=[]
    heading=caption=""
    for node in doc.element.body:
        if node.tag==qn("w:p"):
            p=Paragraph(node,doc)
            for b in node.findall(qn("w:bookmarkStart")):
                name=b.get(qn("w:name"),"")
                if name.startswith("_Ref_Sec_"):
                    heading=name[9:].replace("_",".");caption=""
            if CAPTION.match(p.text):
                caption=p.text
        elif node.tag==qn("w:tbl"):
            rows=[[c.text for c in r.cells] for r in Table(node,doc).rows]
            if caption and heading.startswith("14"):
                tables.append((caption,heading,rows))
            caption=""
    outrows=[]
    for entry in ledger:
        pick=entry.get("selected")
        if not pick:
            continue
        src,sheet=pick["source"],pick["sheet"]
        meta=report.get("derived_sources",{}).get(src) or report["sources"][src]
        raw=load_json_unique(out/meta["extraction"])["sheets"][sheet]["rows"]
        lay=find_layout(raw,computed=src in report.get("derived_sources",{}))
        want=[lay.columns,*[r for _,r in data_rows(raw,lay)[0]]]
        hits=[(sid,r) for cap,sid,r in tables if cap==lay.title]
        item=dict(number=entry["number"],caption=lay.title,adopted=pick,sections=[sid for sid,_ in hits],
                  cells=[],word_matches_source=len(hits)==1,independent=entry["independent"])
        oracle={(c["row"],c.get("column")):c.get("independent") for c in qc["tables"].get(entry["number"],{}).get("checks",[]) if "row" in c}
        if len(hits)==1:
            sid,got=hits[0]
            item["word_matches_source"]=got==want
            for i,r in enumerate(want[1:],1):
                for j,v in enumerate(r[1:],1):
                    seen=got[i][j] if i<len(got) and j<len(got[i]) else None
                    expected=oracle.get((r[0],want[0][j]))
                    from .project_reconcile import same_value
                    item["cells"].append(dict(section=sid,word_row=i+1,word_column=j+1,row=r[0],column=want[0][j],
                         printed=seen,source=v,independent=expected,word_matches_independent=same_value(seen,expected) if expected is not None else None))
        outrows.append(item)
    return outrows
