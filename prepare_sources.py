from pathlib import Path
import re
import fitz

ROOT = Path(__file__).resolve().parent
SOURCES = {
    "职工带薪年休假条例": "https://xzfg.moj.gov.cn/front/law/detail?LawID=208",
    "企业职工带薪年休假实施办法": "https://rsj.sjz.gov.cn/atm/7/20220324094537932.pdf",
}

def prepare():
    output = ROOT / "source-documents"
    output.mkdir(exist_ok=True)
    for title, url in SOURCES.items():
        with fitz.open(ROOT / "knowledge" / (title + ".pdf")) as pdf:
            lines = [line.strip() for page in pdf for line in page.get_text().splitlines()]
        text = "".join(line for line in lines if line and not re.fullmatch(r"[-－]?\s*\d+\s*[-－]?", line))
        matches = list(re.finditer(r"第[一二三四五六七八九十]+条", text))
        if not matches:
            raise ValueError(f"{title}未提取到条款")
        # Only treat sequential article numbers as headings; references can repeat.
        numbers = "一 二 三 四 五 六 七 八 九 十 十一 十二 十三 十四 十五 十六 十七 十八 十九".split()
        starts = []
        for match in matches:
            if len(starts) < len(numbers) and match.group() == "第" + numbers[len(starts)] + "条":
                starts.append(match)
        for i, match in enumerate(starts):
            end = starts[i+1].start() if i+1 < len(starts) else len(text)
            name = f"{title}－{match.group()}"
            (output / (name + ".md")).write_text(
                f"# {name}\n\n来源：{url}\n\n{text[match.start():end]}\n", encoding="utf-8")
        print(title, len(starts))

if __name__ == "__main__":
    prepare()
