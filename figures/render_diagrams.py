#!/usr/bin/env python3
"""Render minimal B&W Agent Framework + Workflow Overview PNGs for slides."""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent
NOTE = Path.home() / "Desktop/DeeCamp/Note"


def font(size: int, bold: bool = False):
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold
        else "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/Library/Fonts/Arial.ttf",
    ]
    for p in candidates:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default()


def box(draw, xy, text_lines, *, title=None):
    x0, y0, x1, y1 = xy
    draw.rectangle(xy, outline="#111", width=2)
    f, fb = font(14), font(14, bold=True)
    y = y0 + 10
    if title:
        draw.text((x0 + 12, y), title, fill="#111", font=fb)
        y += 22
    for line in text_lines:
        draw.text((x0 + 12, y), line, fill="#222", font=f)
        y += 18


def arrow_h(draw, x0, y, x1):
    draw.line([(x0, y), (x1, y)], fill="#111", width=2)
    draw.polygon([(x1, y), (x1 - 10, y - 5), (x1 - 10, y + 5)], fill="#111")


def arrow_v(draw, x, y0, y1):
    draw.line([(x, y0), (x, y1)], fill="#111", width=2)
    draw.polygon([(x, y1), (x - 5, y1 - 10), (x + 5, y1 - 10)], fill="#111")


def center_text(draw, text, cx, y, f, fill="#111"):
    bb = draw.textbbox((0, 0), text, font=f)
    draw.text((cx - (bb[2] - bb[0]) / 2, y), text, fill=fill, font=f)


def render_framework():
    W, H = 1400, 820
    im = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(im)
    center_text(d, "Agent Framework", W / 2, 18, font(24, bold=True))

    box(d, (40, 310, 170, 410), ["task request", "confirmation"], title="User")

    d.rectangle([220, 70, 760, 145], fill="#111", outline="#111", width=2)
    d.text((400, 88), "Agent", fill="#fff", font=font(18, bold=True))
    d.text((245, 118),
           "intent · skill routing · context reuse · confirm · organize",
           fill="#ddd", font=font(13))

    box(d, (220, 170, 760, 245),
        ["curation · processing · handoff · continue from results"],
        title="1. Request Interpretation")
    box(d, (220, 265, 760, 385),
        ["entry: curation / processing / handoff pipelines",
         "analysis: scatac-fragment-qc · scrna-qc · multiome-qc · map-to-ccre",
         "corpus: resource-setup · download-validate · tokenize · fm-handoff"],
        title="2. Skill Selection")
    box(d, (220, 405, 760, 500),
        ["validate inputs → resolve paths → show exact command",
         "→ wait for confirmation → launch staged job (--stage)"],
        title="3. Execution Control")
    box(d, (220, 520, 760, 595),
        ["Pi shell · conda envs · object reuse · provenance · resume"],
        title="4. Session Management")

    for y0, y1 in [(145, 170), (245, 265), (385, 405), (500, 520)]:
        arrow_v(d, 490, y0, y1)

    d.line([(170, 350), (220, 350)], fill="#666", width=1)
    d.text((175, 330), "clarify", fill="#555", font=font(11))

    d.rectangle([820, 170, 1340, 500], outline="#111", width=2)
    d.text((890, 190), "Execution Environment", fill="#111", font=font(16, bold=True))
    for i, line in enumerate([
        "Pi coding agent",
        "skills/  +  scripts/",
        "conda: snapatac2 · scanpy · muon · curator",
        "SnapATAC2 · scanpy · muon",
        "inputs · prior results",
        "--stage CLI scripts",
    ]):
        d.text((860, 240 + i * 28), line, fill="#222", font=font(14))

    arrow_h(d, 760, 350, 820)
    d.text((770, 328), "confirm", fill="#555", font=font(11))

    d.line([(1080, 500), (1080, 575), (490, 575), (490, 595)], fill="#111", width=2)
    d.polygon([(490, 595), (485, 585), (495, 585)], fill="#111")
    d.text((700, 555), "results & states", fill="#555", font=font(11))

    d.rectangle([220, 620, 1340, 780], outline="#111", width=2)
    center_text(d, "Organized Outputs", 780, 640, font(16, bold=True))
    labels = ["catalog / QC", "h5ad / h5mu", "cell × cCRE", "data cards", "FM corpus"]
    x = 260
    for lab in labels:
        d.rectangle([x, 685, x + 170, 750], outline="#111", width=2)
        center_text(d, lab, x + 85, 710, font(13))
        x += 200

    d.line([(220, 700), (100, 410)], fill="#666", width=1)
    d.text((115, 540), "feedback", fill="#555", font=font(11))

    NOTE.mkdir(parents=True, exist_ok=True)
    path = OUT / "agent_framework.png"
    im.save(path, "PNG")
    im.save(NOTE / "agent_framework_overview.png", "PNG")
    print("wrote", path)
    return path


def render_workflow():
    W, H = 1500, 760
    im = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(im)
    center_text(d, "Workflow Overview", W / 2, 16, font(24, bold=True))

    for x, name in [
        (30, "1. Inputs & Curation"),
        (520, "2. Modality Processing"),
        (1010, "3. Representation & Handoff"),
    ]:
        d.rectangle([x, 60, x + 460, 720], outline="#111", width=2)
        d.rectangle([x, 60, x + 460, 96], fill="#111", outline="#111", width=2)
        center_text(d, name, x + 230, 70, font(15, bold=True), fill="#fff")

    # --- Panel 1 ---
    d.text((50, 120), "Inputs", fill="#111", font=font(13, bold=True))
    for i, lab in enumerate(["GEO/SRA", "ENCODE", "10x/CXG", "fragments"]):
        x = 50 + i * 105
        d.rectangle([x, 145, x + 95, 180], outline="#111", width=1)
        center_text(d, lab, x + 47, 155, font(12))
    arrow_v(d, 250, 180, 210)

    d.text((50, 220), "Curation pipeline", fill="#111", font=font(13, bold=True))
    steps = [
        "Discovery → Metadata → File manifest",
        "Eligibility / routing",
        "Resource setup (cCRE vocab)",
        "Download + validate",
    ]
    y = 250
    for s in steps:
        d.rectangle([50, y, 450, y + 36], outline="#111", width=1)
        center_text(d, s, 250, y + 9, font(13))
        if s != steps[-1]:
            arrow_v(d, 250, y + 36, y + 48)
        y += 48

    d.text((50, 470), "Outputs", fill="#111", font=font(13, bold=True))
    for i, lab in enumerate(["catalog / manifest", "review queue"]):
        x = 50 + i * 210
        d.rectangle([x, 495, x + 190, 535], outline="#111", width=1)
        center_text(d, lab, x + 95, 508, font(12))

    # --- Panel 2 ---
    d.text((540, 120), "Branch by modality", fill="#111", font=font(13, bold=True))
    branches = [
        (145, 95, "7A  scATAC fragments (preferred)",
         ["SnapATAC2: import → TSSe → QC → spectral",
          "→ Leiden → doublet → map-to-ccre"]),
        (255, 72, "7B  scATAC peak matrix (fallback)",
         ["load → filter → LSI/cluster → approximate cCRE map"]),
        (342, 95, "7C  Multiome (RNA + ATAC)",
         ["barcode pair-check → RNA QC (scanpy)",
          "→ ATAC QC (SnapATAC2) → paired-pass ∩"]),
        (452, 72, "7D  scRNA reference (not in ATAC corpus)",
         ["scanpy QC → cluster → markers / label transfer"]),
    ]
    for y, h, title_s, lines in branches:
        d.rectangle([540, y, 960, y + h], outline="#111", width=1)
        d.text((555, y + 10), title_s, fill="#111", font=font(13, bold=True))
        for j, line in enumerate(lines):
            d.text((555, y + 34 + j * 18), line, fill="#333", font=font(12))

    d.text((540, 555), "Outputs", fill="#111", font=font(13, bold=True))
    for i, lab in enumerate(["atac.h5ad", "rna.h5ad", "multiome.h5mu"]):
        x = 540 + i * 140
        d.rectangle([x, 580, x + 125, 620], outline="#111", width=1)
        center_text(d, lab, x + 62, 593, font(12))

    # --- Panel 3 ---
    d.text((1030, 120), "Unified representation", fill="#111", font=font(13, bold=True))
    steps3a = ["map-to-ccre (GRCh38)", "TF-IDF / LSI baseline", "tokenize cell sentence"]
    y = 150
    for s in steps3a:
        d.rectangle([1030, y, 1450, y + 36], outline="#111", width=1)
        center_text(d, s, 1240, y + 9, font(13))
        if s != steps3a[-1]:
            arrow_v(d, 1240, y + 36, y + 48)
        y += 48

    d.text((1030, 320), "Package for FM", fill="#111", font=font(13, bold=True))
    steps3b = ["data cards + QC reports", "train / val / test split", "FM-ready corpus"]
    y = 350
    for s in steps3b:
        d.rectangle([1030, y, 1450, y + 36], outline="#111", width=1)
        center_text(d, s, 1240, y + 9, font(13))
        if s != steps3b[-1]:
            arrow_v(d, 1240, y + 36, y + 48)
        y += 48

    d.text((1030, 555), "Outputs", fill="#111", font=font(13, bold=True))
    for i, lab in enumerate(["matrices", "tokens", "MANIFEST"]):
        x = 1030 + i * 140
        d.rectangle([x, 580, x + 125, 620], outline="#111", width=1)
        center_text(d, lab, x + 62, 593, font(12))

    arrow_h(d, 490, 360, 520)
    arrow_h(d, 980, 360, 1010)

    path = OUT / "workflow_overview.png"
    im.save(path, "PNG")
    im.save(NOTE / "workflow_overview.png", "PNG")
    print("wrote", path)
    return path


if __name__ == "__main__":
    render_framework()
    render_workflow()
