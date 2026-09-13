"""Tiny original vector/text PDFs built without optional authoring packages."""
from pathlib import Path


def write_pdf(path: Path, pages=None, encrypted=False, media=(420, 595)):
    pages = pages or [("Sea Notes", "Morning light crossed the harbour. We opened a book beside the water."),
                      ("Island Walk", "A silver gull followed the path. This page carries the search word lighthouse."),
                      ("Coming Home", "At dusk the lighthouse guided us back. We kept the day between two pages.")]
    objects = []
    def add(value):
        objects.append(value.encode() if isinstance(value, str) else value)
        return len(objects)
    add("<< /Type /Catalog /Pages 2 0 R >>")
    add("pending")
    add("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    kids=[]
    for index,(title,body) in enumerate(pages):
        page_id=add("pending")
        color=("0.12 0.45 0.56", "0.28 0.52 0.26", "0.55 0.31 0.17")[index % 3]
        commands=f"q {color} rg 32 385 356 150 re f Q\nBT /F1 26 Tf 32 555 Td ({title}) Tj ET\n"
        for line,part in enumerate([body[i:i+54] for i in range(0,len(body),54)]):
            part=part.replace('\\','\\\\').replace('(','\\(').replace(')','\\)')
            commands+=f"BT /F1 12 Tf 32 {340-line*22} Td ({part}) Tj ET\n"
        commands+=f"BT /F1 10 Tf 32 28 Td (Original Reader fixture / {index+1}) Tj ET"
        stream=commands.encode()
        content=add(b"<< /Length "+str(len(stream)).encode()+b" >>\nstream\n"+stream+b"\nendstream")
        objects[page_id-1]=f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {media[0]} {media[1]}] /Resources << /Font << /F1 3 0 R >> >> /Contents {content} 0 R >>".encode()
        kids.append(f"{page_id} 0 R")
    objects[1]=f"<< /Type /Pages /Count {len(pages)} /Kids [{' '.join(kids)}] >>".encode()
    encrypt_id=add('<< /Filter /Standard /V 1 /R 2 /Length 40 /O <'+('00'*32)+'> /U <'+('ff'*32)+'> /P -4 >>') if encrypted else None
    output=bytearray(b"%PDF-1.4\n")
    offsets=[0]
    for number,value in enumerate(objects,1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode()+value+b"\nendobj\n")
    xref=len(output)
    output.extend(f"xref\n0 {len(objects)+1}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]: output.extend(f"{offset:010d} 00000 n \n".encode())
    extra=f" /Encrypt {encrypt_id} 0 R /ID [<{'42'*16}><{'42'*16}>]" if encrypt_id else ""
    output.extend(f"trailer\n<< /Size {len(objects)+1} /Root 1 0 R{extra} >>\nstartxref\n{xref}\n%%EOF\n".encode())
    path.write_bytes(output)
    return path
