from pathlib import Path
import fitz
import openpyxl
import sqlite3

root = Path(__file__).resolve().parent
def read_pdf(path):
    with fitz.open(path) as document:
        return "\n".join(page.get_text() for page in document)

def run():
    paths = sorted((root / "benchmark-data/documents").glob("*.pdf"))
    index = sqlite3.connect(":memory:")
    index.execute("CREATE VIRTUAL TABLE docs USING fts5(name, body)")
    for p in paths:
        body = "".join(read_pdf(p).split())
        index.execute("INSERT INTO docs VALUES (?,?)", (p.stem[:3], " ".join(body[i:i+2] for i in range(len(body)-1))))
    workbook = openpyxl.load_workbook(next((root / "benchmark-data").glob("*.xlsx")), read_only=True)
    rows = iter(workbook.active.values)
    next(rows)
    for row in list(rows)[:3]:
        terms = [row[2][i:i+2] for i in range(len(row[2])-1) if row[2][i:i+2].isalnum()]
        top = [r[0] for r in index.execute("SELECT name FROM docs WHERE docs MATCH ? ORDER BY rank LIMIT 5", (" OR ".join(terms),))]
        print(row[0], "expected", row[5][:3], "top5", top)
    workbook.close()

if __name__ == "__main__":
    run()

