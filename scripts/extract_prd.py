#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
extract_prd.py — 把 PRD 拆成「文字 + 图 + 图与说明文字的对应关系」

用途：交互评审前先取材。本脚本只做**确定性提取**，不做任何判断。
      「图好不好用」「组件选得对不对」属于语义判断，留给模型，不要塞进脚本。

依赖：Python 3.8+，**纯标准库**，不装任何第三方包。
      （跨平台硬要求：团队 Windows / Mac 都能直接跑）

用法：
    python3 extract_prd.py <PRD文件或目录> [-o 输出目录] [--context 3]

支持：.docx / .html / .htm / .md / .markdown / .xlsx
输出：
    <outdir>/content.md            正文（图位置用 <!-- FIGURE n --> 占位）
    <outdir>/figures/              抽出的图片
    <outdir>/figure-text-map.json  图与邻近文字的配对（本脚本的核心产出）
    <outdir>/manifest.json         概要统计

退出码：0 成功（可能有 warnings）；2 参数错误；3 没有可提取的文件

注意：
  - HTML 只能拿到静态内容。原型页里「点开才出现的标注层」静态解析不到，
    那种情况要用浏览器打开，不要指望本脚本。
  - 提取失败的文件不会中断流程，会写进 warnings。
"""

import argparse
import json
import os
import re
import struct
import sys
import zipfile
from pathlib import Path

SUPPORTED = {".docx", ".html", ".htm", ".md", ".markdown", ".xlsx"}

# 图注启发式：分强弱两档，避免「列表」因为含「表」字被误判成「表1」
STRONG_CAPTION = (
    r"^图\s*\d+", r"^表\s*\d+", r"^图\s*[一二三四五六七八九十]",
    r"^(figure|fig\.?)\s*\d+", r"^(table|tab\.?)\s*\d+",
)
WEAK_CAPTION = (
    r"截图", r"原型", r"界面", r"设计稿", r"示意", r"流程图", r"状态图",
    r"布局", r"表单", r"弹窗", r"详情页", r"列表页",
)
CAPTION_MAX_LEN = 120
WEAK_MAX_LEN = 40


def caption_score(text):
    """图注可能性打分：2=强特征 1=弱特征 0=不像"""
    if not text or len(text) > CAPTION_MAX_LEN:
        return 0
    for p in STRONG_CAPTION:
        if re.search(p, text, re.I):
            return 2
    if len(text) <= WEAK_MAX_LEN and any(re.search(p, text, re.I) for p in WEAK_CAPTION):
        return 1
    return 0


def judge_caption(before, after):
    """在前后候选里挑最像图注的一条。

    先全局找强特征（图注在图上方或下方都行），找不到才按位置偏好降级。
    返回 (text, confidence)
    """
    for group in (after, before):
        for t in group:
            if caption_score(t) == 2:
                return t, "high"
    for group in (after, before):
        for t in group:
            if caption_score(t) == 1:
                return t, "medium"
    if after:
        return short(after[0]), "medium"
    if before:
        return short(before[-1]), "medium"
    return "", "low"


# --------------------------------------------------------------------------
# 通用工具
# --------------------------------------------------------------------------

def local(tag):
    """取标签本地名：{ns}paragraph -> paragraph"""
    if isinstance(tag, str) and "}" in tag:
        return tag.split("}", 1)[1]
    return tag or ""


def attr_local(elem, name):
    for k, v in elem.attrib.items():
        if local(k) == name:
            return v
    return None


def short(text, limit=CAPTION_MAX_LEN):
    t = re.sub(r"\s+", " ", text or "").strip()
    return t[:limit] + ("…" if len(t) > limit else "")


def read_image_size(data):
    """从图片头部读宽高，纯标准库。读不到返回 None。"""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return list(struct.unpack(">II", data[16:24]))
        if data[:2] == b"\xff\xd8":  # JPEG
            i = 2
            while i < len(data) - 9:
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB):
                    h, w = struct.unpack(">HH", data[i + 5:i + 9])
                    return [w, h]
                seg = struct.unpack(">H", data[i + 2:i + 4])[0]
                i += 2 + seg
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return list(struct.unpack("<HH", data[6:10]))
    except Exception:
        pass
    return None


# --------------------------------------------------------------------------
# 统一序列：('text', str) | ('figure', index)
# 四种格式都先转成这个序列，配对和渲染只写一次。
# --------------------------------------------------------------------------

def pair_figures(sequence, figures, context):
    """给每张图找前后各 context 段文字，并判断最可能的图注。"""
    pos_of = {}
    for i, item in enumerate(sequence):
        if item[0] == "figure":
            pos_of[item[1]] = i

    for f in figures:
        pos = pos_of.get(f["index"])
        if pos is None:
            f["caption_before"], f["caption_after"] = [], []
        else:
            f["caption_before"] = [short(s[1]) for s in sequence[max(0, pos - context):pos] if s[0] == "text"]
            f["caption_after"] = [short(s[1]) for s in sequence[pos + 1:pos + 1 + context] if s[0] == "text"]

        text, conf = judge_caption(f["caption_before"], f["caption_after"])
        f["nearest_text"] = text
        f["confidence"] = conf
        f["caption_shared"] = False

    # 同一段说明被多张图共用 —— 必须标出来，不能默认归属
    # （规范要求：一条说明只对一张图，一对多本身就是 PRD 缺陷）
    counts = {}
    for f in figures:
        key = f.get("nearest_text")
        if key:
            counts[key] = counts.get(key, 0) + 1
    for f in figures:
        key = f.get("nearest_text")
        if key and counts.get(key, 0) > 1:
            f["caption_shared"] = True
            f["confidence"] = "low"
            f["note"] = f"同一段说明被 {counts[key]} 张图共用，归属需人工确认"

    return figures


def render_markdown(sequence):
    parts = []
    for kind, val in sequence:
        if kind == "text":
            parts.append(val)
        elif kind == "figure":
            parts.append(f"<!-- FIGURE {val:02d} -->")
        elif kind == "hr":
            parts.append("---")
    return "\n\n".join(parts)


# --------------------------------------------------------------------------
# DOCX
# --------------------------------------------------------------------------

def extract_docx(path, outdir, context):
    import xml.etree.ElementTree as ET

    figures, warnings, sequence = [], [], []
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        if "word/document.xml" not in names:
            raise ValueError("不是有效的 docx：找不到 word/document.xml")

        # rId -> word/media/...
        rels = {}
        if "word/_rels/document.xml.rels" in names:
            try:
                root = ET.fromstring(z.read("word/_rels/document.xml.rels"))
                for rel in root:
                    rid, target = rel.get("Id"), rel.get("Target") or ""
                    if rid and target:
                        target = target.lstrip("/")
                        if not target.startswith("word/"):
                            target = "word/" + target.replace("../", "")
                        rels[rid] = target
            except Exception as e:
                warnings.append(f"rels 解析失败：{e}")

        root = ET.fromstring(z.read("word/document.xml"))
        body = next((el for el in root.iter() if local(el.tag) == "body"), root)

        fig_no = 0

        def handle_table(tbl):
            rows = []
            for tr in tbl.iter():
                if local(tr.tag) != "tr":
                    continue
                cells = []
                for tc in tr:
                    if local(tc.tag) != "tc":
                        continue
                    txt = " ".join((t.text or "").strip() for t in tc.iter() if local(t.tag) == "t").strip()
                    cells.append(txt.replace("|", "\\|"))
                if cells:
                    rows.append(cells)
            if not rows:
                return
            width = max(len(r) for r in rows)
            rows = [r + [""] * (width - len(r)) for r in rows]
            md = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
            md += ["| " + " | ".join(r) + " |" for r in rows[1:]]
            sequence.append(("text", "\n".join(md)))

        def walk(container):
            nonlocal fig_no
            for node in container:
                name = local(node.tag)
                if name == "p":
                    text = "".join(t.text or "" for t in node.iter() if local(t.tag) == "t").strip()
                    rids = []
                    for el in node.iter():
                        if local(el.tag) == "blip":
                            rid = attr_local(el, "embed")
                            if rid:
                                rids.append(rid)
                        elif local(el.tag) == "imagedata":
                            rid = attr_local(el, "id")
                            if rid:
                                rids.append(rid)
                    for rid in rids:
                        fig_no += 1
                        media = rels.get(rid)
                        out_name, size = None, None
                        if media and media in names:
                            data = z.read(media)
                            ext = os.path.splitext(media)[1] or ".png"
                            out_name = f"figure-{fig_no:02d}{ext}"
                            (outdir / "figures").mkdir(parents=True, exist_ok=True)
                            (outdir / "figures" / out_name).write_bytes(data)
                            size = read_image_size(data)
                        else:
                            warnings.append(f"图 {fig_no}：rId {rid} 未找到媒体文件")
                        figures.append({
                            "index": fig_no,
                            "source": f"figures/{out_name}" if out_name else None,
                            "origin": media,
                            "alt": "",
                            "size": size,
                        })
                        sequence.append(("figure", fig_no))
                    if text:
                        sequence.append(("text", text))
                elif name == "tbl":
                    handle_table(node)
                elif name in ("sdt", "sdtContent", "txbxContent"):
                    walk(node)

        walk(body)

    return pair_figures(sequence, figures, context), sequence, warnings


# --------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------

class HTMLCollector:
    BLOCK_TAGS = {"p", "div", "li", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6",
                  "caption", "figcaption", "span", "label", "a", "pre", "blockquote"}
    SKIP_TAGS = {"script", "style", "head", "noscript"}

    def __init__(self):
        self.seq = []      # 混合序列：('text', s) 或 ('img', src)
        self._buf = []
        self._skip = 0

    def flush(self):
        text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
        self._buf = []
        if text:
            self.seq.append(("text", text))

    def start(self, tag, attrs):
        attrs = dict(attrs or {})
        if tag in self.SKIP_TAGS:
            self._skip += 1
            return
        if tag == "img":
            self.flush()
            src = attrs.get("src") or attrs.get("data-src") or ""
            self.seq.append(("img", (src, attrs.get("alt") or "")))

    def end(self, tag):
        if tag in self.SKIP_TAGS and self._skip:
            self._skip -= 1
            return
        if tag in self.BLOCK_TAGS:
            self.flush()

    def data(self, d):
        if not self._skip:
            self._buf.append(d)


def extract_html(path, outdir, context):
    from html.parser import HTMLParser

    raw = None
    for enc in ("utf-8", "gb18030", "big5", "latin-1"):
        try:
            raw = path.read_text(encoding=enc)
            break
        except UnicodeDecodeError:
            continue
    if raw is None:
        raise ValueError("无法解码 HTML（试过 utf-8 / gb18030 / big5）")

    c = HTMLCollector()
    p = HTMLParser(convert_charrefs=True)
    p.handle_starttag = c.start
    p.handle_startendtag = c.start
    p.handle_endtag = c.end
    p.handle_data = c.data
    p.feed(raw)
    p.close()
    c.flush()

    base, figures, warnings, sequence = path.parent, [], [], []
    fig_no = 0
    for kind, val in c.seq:
        if kind == "text":
            sequence.append(("text", val))
            continue

        src, alt = val
        fig_no += 1
        out_name, local_path = None, None
        if src and re.match(r"^data:", src, re.I):
            warnings.append(f"图 {fig_no} 是内联 base64，未导出（需在浏览器里看）")
        elif src and re.match(r"^[a-z]+://", src, re.I):
            warnings.append(f"图 {fig_no} 是远程地址，未下载：{src[:80]}")
        elif src:
            cand = (base / src).resolve()
            if cand.exists() and cand.is_file():
                ext = cand.suffix or ".png"
                out_name = f"figure-{fig_no:02d}{ext}"
                (outdir / "figures").mkdir(parents=True, exist_ok=True)
                try:
                    data = cand.read_bytes()
                    (outdir / "figures" / out_name).write_bytes(data)
                    local_path = f"figures/{out_name}"
                except Exception as e:
                    warnings.append(f"图 {fig_no} 复制失败：{e}")
                    out_name = None
            else:
                warnings.append(f"图 {fig_no} 引用的文件不存在：{src[:80]}")

        figures.append({
            "index": fig_no,
            "source": local_path,
            "origin": src,
            "alt": alt,
            "size": read_image_size((outdir / "figures" / out_name).read_bytes()) if out_name else None,
        })
        sequence.append(("figure", fig_no))

    return pair_figures(sequence, figures, context), sequence, warnings


# --------------------------------------------------------------------------
# Markdown
# --------------------------------------------------------------------------

def extract_md(path, outdir, context):
    raw = None
    for enc in ("utf-8", "gb18030", "latin-1"):
        try:
            raw = path.read_text(encoding=enc)
            break
        except UnicodeDecodeError:
            continue
    if raw is None:
        raise ValueError("无法解码 Markdown")

    base, figures, warnings, sequence = path.parent, [], [], []
    fig_no = 0
    pattern = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)[^)]*\)|<img[^>]+src=[\"']([^\"']+)[\"']", re.I)

    for para in raw.split("\n\n"):
        buf, last = [], 0
        for m in pattern.finditer(para):
            buf.append(para[last:m.start()])
            last = m.end()
            fig_no += 1
            alt, src = m.group(1) or "", m.group(2) or m.group(3) or ""
            out_name, size = None, None
            if src and re.match(r"^data:", src, re.I):
                warnings.append(f"图 {fig_no} 是内联 base64，未导出")
            elif src and re.match(r"^[a-z]+://", src, re.I):
                warnings.append(f"图 {fig_no} 是远程地址，未下载：{src[:80]}")
            elif src:
                cand = (base / src).resolve()
                if cand.exists() and cand.is_file():
                    ext = cand.suffix or ".png"
                    out_name = f"figure-{fig_no:02d}{ext}"
                    (outdir / "figures").mkdir(parents=True, exist_ok=True)
                    try:
                        data = cand.read_bytes()
                        (outdir / "figures" / out_name).write_bytes(data)
                        size = read_image_size(data)
                    except Exception as e:
                        warnings.append(f"图 {fig_no} 复制失败：{e}")
                else:
                    warnings.append(f"图 {fig_no} 引用的文件不存在：{src[:80]}")
            figures.append({
                "index": fig_no,
                "source": f"figures/{out_name}" if out_name else None,
                "origin": src,
                "alt": alt,
                "size": size,
            })
            sequence.append(("figure", fig_no))
        buf.append(para[last:])
        tail = "".join(buf).strip()
        if tail:
            sequence.append(("text", tail))

    return pair_figures(sequence, figures, context), sequence, warnings


# --------------------------------------------------------------------------
# XLSX
# --------------------------------------------------------------------------

def extract_xlsx(path, outdir, context):
    import xml.etree.ElementTree as ET

    warnings, sequence = [], []
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        shared = []
        if "xl/sharedStrings.xml" in names:
            try:
                root = ET.fromstring(z.read("xl/sharedStrings.xml"))
                for si in root:
                    shared.append("".join(t.text or "" for t in si.iter() if local(t.tag) == "t"))
            except Exception as e:
                warnings.append(f"sharedStrings 解析失败：{e}")

        for sheet in sorted(n for n in names if re.match(r"xl/worksheets/sheet\d+\.xml$", n)):
            try:
                root = ET.fromstring(z.read(sheet))
            except Exception as e:
                warnings.append(f"{sheet} 解析失败：{e}")
                continue
            rows = []
            for row in root.iter():
                if local(row.tag) != "row":
                    continue
                cells = []
                for c in row:
                    if local(c.tag) != "c":
                        continue
                    ctype, v = c.get("t"), None
                    for ch in c:
                        if local(ch.tag) == "v":
                            v = ch.text
                        elif local(ch.tag) == "is":
                            v = "".join(t.text or "" for t in ch.iter() if local(t.tag) == "t")
                    if v is None:
                        cells.append("")
                    elif ctype == "s":
                        try:
                            cells.append(shared[int(v)])
                        except Exception:
                            cells.append(v)
                    else:
                        cells.append(v)
                if any(str(x).strip() for x in cells):
                    rows.append([str(x).replace("|", "\\|") for x in cells])
            if rows:
                width = max(len(r) for r in rows)
                rows = [r + [""] * (width - len(r)) for r in rows]
                md = [f"### {sheet}", "| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
                md += ["| " + " | ".join(r) + " |" for r in rows[1:]]
                sequence.append(("text", "\n".join(md)))

    return [], sequence, warnings


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

EXTRACTORS = {
    ".docx": extract_docx,
    ".html": extract_html,
    ".htm": extract_html,
    ".md": extract_md,
    ".markdown": extract_md,
    ".xlsx": extract_xlsx,
}


def collect_targets(target, outdir=None):
    """收集待提取文件。

    三层排除：
      1. 输出目录自身（避免二次运行时把自己的产出当输入）
      2. 任何含 manifest.json 的目录（本脚本自己的历史输出，无论放在哪）
      3. Office 临时文件（~$ 开头）
    """
    if not target.is_dir():
        return [target] if target.is_file() else []

    out_res = outdir.resolve() if outdir else None
    skip_dirs = set()

    for p in target.rglob("*"):
        if p.is_file() and p.name == "manifest.json":
            skip_dirs.add(p.parent.resolve())

    def under(p, d):
        try:
            p.resolve().relative_to(d)
            return True
        except ValueError:
            return False

    picked = []
    for p in sorted(target.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in SUPPORTED or p.name.startswith("~$"):
            continue
        if out_res is not None and under(p, out_res):
            continue
        if any(under(p, d) for d in skip_dirs):
            continue
        picked.append(p)
    return picked


def _force_utf8_stdout():
    """Windows 控制台默认 cp936 / cp1252，打印 ✅ 会直接抛 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main():
    _force_utf8_stdout()
    ap = argparse.ArgumentParser(
        description="把 PRD 拆成文字 + 图 + 图文对应关系（纯标准库，无第三方依赖）")
    ap.add_argument("target", help="PRD 文件或目录")
    ap.add_argument("-o", "--outdir", default=None, help="输出目录（默认 ./prd-extract）")
    ap.add_argument("--context", type=int, default=3, help="图前后各取几段作说明文字候选（默认 3）")
    args = ap.parse_args()

    target = Path(args.target).expanduser()
    if not target.exists():
        print(f"错误：路径不存在：{target}", file=sys.stderr)
        return 2

    outdir = Path(args.outdir).expanduser() if args.outdir else Path.cwd() / "prd-extract"
    outdir.mkdir(parents=True, exist_ok=True)

    files = collect_targets(target, outdir)
    if not files:
        print(f"错误：没找到可提取的文件（支持 {' / '.join(sorted(SUPPORTED))}）", file=sys.stderr)
        return 3

    manifest = {"target": str(target), "files": [], "warnings": []}
    multi = len(files) > 1
    used_names = set()

    for f in files:
        fn = EXTRACTORS.get(f.suffix.lower())
        if not fn:
            manifest["warnings"].append(f"跳过不支持的文件：{f.name}")
            continue
        try:
            figures, sequence, warnings = fn(f, outdir, args.context)
        except Exception as e:
            msg = f"{f.name} 提取失败：{type(e).__name__}: {e}"
            manifest["warnings"].append(msg)
            print(f"⚠️  {msg}", file=sys.stderr)
            continue

        # 同名文件（如多个 content.md）必须错开，否则互相覆盖
        stem = re.sub(r"[^\w\-]+", "_", f.stem)[:40] or "prd"
        base_stem, n = stem, 2
        while stem in used_names:
            stem = f"{base_stem}-{n}"
            n += 1
        used_names.add(stem)

        content_name = f"content-{stem}.md" if multi else "content.md"
        high = sum(1 for x in figures if x["confidence"] == "high")
        low = sum(1 for x in figures if x["confidence"] == "low")
        header = (f"# 提取自：{f.name}\n\n"
                  f"> 类型 {f.suffix.lstrip('.')}｜图 {len(figures)} 张"
                  f"｜图注高置信 {high} 张｜未匹配 {low} 张\n\n")
        (outdir / content_name).write_text(header + render_markdown(sequence), encoding="utf-8")

        map_name = f"figure-text-map-{stem}.json" if multi else "figure-text-map.json"
        (outdir / map_name).write_text(json.dumps({
            "file": f.name,
            "type": f.suffix.lstrip("."),
            "content_md": content_name,
            "figures": figures,
            "warnings": warnings,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

        manifest["files"].append({
            "file": f.name,
            "content_md": content_name,
            "figure_text_map": map_name,
            "figures": len(figures),
            "high_confidence_captions": high,
            "low_confidence_captions": low,
            "warnings": len(warnings),
        })
        manifest["warnings"].extend(f"{f.name}: {w}" for w in warnings)
        print(f"✅ {f.name}：{len(figures)} 张图（高置信图注 {high}）→ {content_name} / {map_name}")

    (outdir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    total = sum(x["figures"] for x in manifest["files"])
    low = sum(x["low_confidence_captions"] for x in manifest["files"])
    print(f"\n输出目录：{outdir}")
    print(f"共 {len(manifest['files'])} 个文件、{total} 张图")
    if low:
        print(f"⚠️  {low} 张图没匹配到说明文字 —— 这些必须回去问人，不要猜")
    if manifest["warnings"]:
        print(f"⚠️  {len(manifest['warnings'])} 条警告，详见 manifest.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
