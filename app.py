"""
Shipping Label Address Replacer - Web App
===========================================
Yeh Streamlit app har PDF mein "FROM" (sender) address block ko
khud-b-khud dhoond kar naye address se replace karti hai - chahe
label FedEx-style ho ("FROM:" ke sath) ya UPS/Amazon-style
(bina "FROM" label ke).

Run karne ke liye:
    streamlit run app.py
"""

import streamlit as st
import fitz  # PyMuPDF
import pytesseract
from PIL import Image, ImageDraw, ImageFont
import io
import os
import zipfile
import numpy as np
import platform

# ---- Tesseract path auto-detect (Windows / Linux / Mac) ----
if platform.system() == "Windows":
    default_path = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    if os.path.exists(default_path):
        pytesseract.pytesseract.tesseract_cmd = default_path


def get_font(size):
    candidates = [
        "DejaVuSans-Bold.ttf",
        "DejaVuSansMono-Bold.ttf",
        "Arial Bold.ttf",
        "arialbd.ttf",
        "Verdana Bold.ttf",
        "verdanab.ttf",
        "DejaVuSans.ttf",
        "arial.ttf",
    ]
    for name in candidates:
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


def wrap_line(line, font, draw, max_width):
    words = line.split()
    if not words:
        return [line]
    lines = []
    current = words[0]
    for word in words[1:]:
        candidate = current + " " + word
        if draw.textlength(candidate, font=font) <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def erase_horizontal_dividers(img, draw, search_top, search_bottom, max_x=None):
    gray = img.convert("L")
    arr = np.array(gray)
    h, w = arr.shape
    right_limit_px = max_x if max_x is not None else max(0, w - int(w * 0.08))
    top = max(0, int(search_top))
    bottom = min(h, int(search_bottom))
    for y in range(top, bottom):
        row = arr[y][:right_limit_px]
        if row.size == 0:
            continue
        dark_frac = float(np.mean(row < 150))
        if dark_frac > 0.4:
            y0 = max(0, y - 3)
            y1 = min(h, y + 4)
            draw.rectangle([0, y0, right_limit_px, y1], fill="white")


def replace_phone_text_based(page, block, new_phone):
    words = page.get_text("words")
    right_bound = block.x1 + 160
    matched = []
    for w in words:
        x0, y0, x1, y1, token = w[0], w[1], w[2], w[3], w[4]
        digit_count = sum(c.isdigit() for c in token)
        if digit_count >= 3 and x0 >= block.x1 - 5 and x0 < right_bound and y0 >= block.y0 - 5 and y1 <= block.y1 + 15:
            matched.append((x0, y0, x1, y1))
    if not matched:
        return False
    px0 = min(m[0] for m in matched)
    py0 = min(m[1] for m in matched)
    px1 = max(m[2] for m in matched)
    py1 = max(m[3] for m in matched)
    rect = fitz.Rect(px0 - 2, py0 - 2, px1 + 2, py1 + 2)
    page.add_redact_annot(rect, fill=(1, 1, 1))
    page.apply_redactions()
    page.insert_text((rect.x0, rect.y1 - 2), new_phone, fontsize=10, fontname="helv", color=(0, 0, 0))
    return True


def find_block_text_based(page):
    from_hits = page.search_for("FROM") or page.search_for("From")
    if not from_hits:
        return None
    from_rect = from_hits[0]
    to_candidates = [r for r in page.search_for("TO") if r.y0 > from_rect.y1 and r.x0 < from_rect.x1 + 50]
    if to_candidates:
        to_rect = min(to_candidates, key=lambda r: r.y0)
        bottom = to_rect.y0 - 2
    else:
        bottom = from_rect.y1 + 110
    right_limit = page.rect.width * 0.38
    return fitz.Rect(from_rect.x0 - 2, from_rect.y0 - 2, right_limit, bottom)


def replace_text_based(page, address_lines, phone):
    block = find_block_text_based(page)
    if block is None:
        return False
    page.add_redact_annot(block, fill=(1, 1, 1))
    page.apply_redactions()

    max_width = block.width - 4
    fontsize = 12
    while fontsize > 6:
        widest = max(fitz.get_text_length(line, fontname="helv", fontsize=fontsize) for line in address_lines)
        if widest <= max_width:
            break
        fontsize -= 0.5
    line_height = fontsize * 1.25
    max_lines_height = block.height - 8
    if line_height * len(address_lines) > max_lines_height:
        line_height = max_lines_height / len(address_lines)

    y = block.y0 + fontsize + 2
    for line in address_lines:
        page.insert_text((block.x0 + 2, y), line, fontsize=fontsize, fontname="helv", color=(0, 0, 0))
        y += line_height

    replace_phone_text_based(page, block, phone)
    return True


def replace_phone_image_based(draw, ocr_data, right_start_x, top_y, bottom_y, new_phone):
    n = len(ocr_data["text"])
    ship_x = None
    for i in range(n):
        if ocr_data["text"][i].strip().upper().startswith("SHIP"):
            same_row = ocr_data["top"][i] <= top_y + 90
            to_the_right = ocr_data["left"][i] > right_start_x
            if same_row and to_the_right:
                ship_x = ocr_data["left"][i]
                break
    right_bound = (ship_x - 15) if ship_x else (right_start_x + 220)

    matched = []
    for i in range(n):
        token = ocr_data["text"][i].strip()
        if not token:
            continue
        digit_count = sum(c.isdigit() for c in token)
        if digit_count < 3:
            continue
        x0, y0, w, h = ocr_data["left"][i], ocr_data["top"][i], ocr_data["width"][i], ocr_data["height"][i]
        if x0 >= right_start_x and x0 < right_bound and y0 >= top_y - 10 and y0 <= bottom_y:
            matched.append((x0, y0, x0 + w, y0 + h))
    if not matched:
        return False

    px0 = min(m[0] for m in matched)
    py0 = min(m[1] for m in matched)
    px1 = max(m[2] for m in matched)
    py1 = max(m[3] for m in matched)
    draw.rectangle([px0 - 3, py0 - 3, px1 + 3, py1 + 3], fill="white")

    box_w = px1 - px0
    box_h = py1 - py0
    font_size = max(10, int(box_h * 1.1))
    font = None
    while font_size > 8:
        candidate = get_font(font_size)
        if draw.textlength(new_phone, font=candidate) <= box_w * 1.3:
            font = candidate
            break
        font_size -= 1
    if font is None:
        font = candidate
    draw.text((px0, py0), new_phone, fill="black", font=font)
    return True


def replace_image_based(page, address_lines, phone, dpi=200):
    zoom = dpi / 72
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat)
    img = Image.open(io.BytesIO(pix.tobytes("png")))
    ocr_data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
    n = len(ocr_data["text"])

    from_box = None
    has_from_label = False
    anchor_block_num = None
    for i in range(n):
        word = ocr_data["text"][i].strip().upper().rstrip(":")
        if word == "FROM":
            from_box = (ocr_data["left"][i], ocr_data["top"][i], ocr_data["width"][i], ocr_data["height"][i])
            has_from_label = True
            break

    if from_box is None:
        candidates = [
            i for i in range(n)
            if len(ocr_data["text"][i].strip()) >= 2
            and ocr_data["left"][i] < img.width * 0.55
            and ocr_data["top"][i] < img.height * 0.25
        ]
        if not candidates:
            return False
        top_i = min(candidates, key=lambda i: (ocr_data["block_num"][i], ocr_data["line_num"][i], ocr_data["word_num"][i]))
        from_box = (ocr_data["left"][top_i], ocr_data["top"][top_i], ocr_data["width"][top_i], ocr_data["height"][top_i])
        anchor_block_num = ocr_data["block_num"][top_i]

    fx0, fy0, fw, fh = from_box
    fy1 = fy0 + fh

    anchor_idx = None
    for i in range(n):
        if (ocr_data["left"][i] == fx0 and ocr_data["top"][i] == fy0
                and ocr_data["width"][i] == fw and ocr_data["height"][i] == fh):
            anchor_idx = i
            break

    to_y = None
    for i in range(n):
        word = ocr_data["text"][i].strip().upper().rstrip(":")
        if word == "TO":
            ty, tx = ocr_data["top"][i], ocr_data["left"][i]
            if ty > fy1 and tx < fx0 + 200:
                to_y = ty
                break
    max_reasonable_gap = img.height * 0.22
    if to_y is not None and (to_y - fy0) > max_reasonable_gap:
        to_y = None

    block_bottom = None
    if anchor_block_num is not None:
        same_block_bottoms = [
            ocr_data["top"][i] + ocr_data["height"][i]
            for i in range(n)
            if ocr_data["text"][i].strip() and ocr_data["block_num"][i] == anchor_block_num
        ]
        if same_block_bottoms:
            block_bottom = max(same_block_bottoms) + 6

    if block_bottom is not None:
        bottom = block_bottom
    elif to_y is not None:
        bottom = to_y - 5
    else:
        bottom = fy1 + int(img.height * 0.15)

    if anchor_idx is not None:
        band_top = fy0 - 5
        band_bottom = fy0 + fh * 2.2
        top_row_words = sorted(
            [
                (ocr_data["left"][i], ocr_data["left"][i] + ocr_data["width"][i])
                for i in range(n)
                if ocr_data["text"][i].strip()
                and ocr_data["top"][i] < band_bottom
                and ocr_data["top"][i] + ocr_data["height"][i] > band_top
                and ocr_data["left"][i] + ocr_data["width"][i] > fx0
            ],
            key=lambda t: t[0],
        )
    else:
        top_row_words = sorted(
            [
                (ocr_data["left"][i], ocr_data["left"][i] + ocr_data["width"][i])
                for i in range(n)
                if ocr_data["text"][i].strip() and ocr_data["top"][i] <= fy0 + fh * 1.4
                and ocr_data["left"][i] + ocr_data["width"][i] > fx0
            ],
            key=lambda t: t[0],
        )
    adjacent_col_x = None
    if top_row_words:
        cur_end = top_row_words[0][1]
        gap_threshold = 45
        for (wx0, wx1) in top_row_words[1:]:
            if wx0 - cur_end > gap_threshold:
                adjacent_col_x = wx0
                break
            cur_end = max(cur_end, wx1)
    hard_cap = (adjacent_col_x - 15) if adjacent_col_x else int(img.width * 0.6)

    candidates_right = [
        ocr_data["left"][i] + ocr_data["width"][i]
        for i in range(n)
        if ocr_data["text"][i].strip()
        and ocr_data["top"][i] >= fy0 - 5
        and ocr_data["top"][i] <= bottom
        and ocr_data["left"][i] >= fx0 - 10
        and ocr_data["left"][i] < hard_cap
    ]
    dynamic_right = max(candidates_right) if candidates_right else (fx0 + 150)
    fallback_frac = int(img.width * (0.38 if has_from_label else 0.33))
    right_limit = max(dynamic_right + 8, fx0 + 150)
    right_limit = min(right_limit, max(fallback_frac, dynamic_right + 8))
    right_limit = min(right_limit, hard_cap)
    right_limit = max(right_limit, fx0 + 60)

    row_right_bound = max(hard_cap, right_limit + 20)

    draw = ImageDraw.Draw(img)
    draw.rectangle([2, max(0, fy0 - 6), row_right_bound, bottom], fill="white")

    box_width = right_limit - fx0
    box_height = bottom - fy0

    font_size = int(fh * 2.4)
    font = None
    wrapped_lines = address_lines
    while font_size > 8:
        candidate = get_font(font_size)
        wrapped = []
        for line in address_lines:
            wrapped.extend(wrap_line(line, candidate, draw, box_width))
        total_height = font_size * 1.25 * len(wrapped)
        widest = max(draw.textlength(l, font=candidate) for l in wrapped)
        if widest <= box_width and total_height <= box_height:
            font = candidate
            wrapped_lines = wrapped
            break
        font_size -= 1
    if font is None:
        font = candidate
        wrapped_lines = wrapped

    line_height = font_size * 1.25
    y = fy0
    for line in wrapped_lines:
        draw.text((fx0, y), line, fill="black", font=font)
        y += line_height

    if has_from_label:
        replace_phone_image_based(draw, ocr_data, right_limit, fy0, bottom, phone)

    erase_horizontal_dividers(img, draw, fy0 - 10, bottom + 25)

    img_bytes = io.BytesIO()
    img.convert("L").save(img_bytes, format="PNG", optimize=True)
    img_bytes.seek(0)
    rect = page.rect
    page.clean_contents()
    page.insert_image(rect, stream=img_bytes.getvalue())
    return True


def process_pdf_bytes(pdf_bytes, address_lines, phone):
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    for page in doc:
        page_text = page.get_text().strip()
        is_text_based = len(page_text) > 10
        if is_text_based:
            replace_text_based(page, address_lines, phone)
        else:
            replace_image_based(page, address_lines, phone)
    out_bytes = doc.tobytes(garbage=4, deflate=True, clean=True)
    doc.close()
    return out_bytes


# ============== STREAMLIT UI ==============

st.set_page_config(page_title="Shipping Label Address Replacer", page_icon="📦", layout="centered")

st.title("📦 Shipping Label Address Replacer")
st.write("PDF shipping labels mein FROM (sender) address ko naye address se badlein - FedEx, UPS, aur Amazon-style labels sab support hain.")

st.subheader("1. Naya Sender Address")
col1, col2 = st.columns(2)
with col1:
    company = st.text_input("Company Name", value="USA Wheels & Tire Outlet Wilmer")
    street = st.text_input("Street Address", value="301 S Milers Ferry Rd")
with col2:
    city_state_zip = st.text_input("City, State ZIP", value="Wilmer, TX 75172")
    phone = st.text_input("Phone Number", value="972-290-4900")

address_lines = [company, street, city_state_zip, phone]

st.subheader("2. PDF Files Upload Karein")
uploaded_files = st.file_uploader("Ek ya zyada PDF files chunain", type=["pdf"], accept_multiple_files=True)

if uploaded_files and st.button("🔄 Process Karein", type="primary"):
    results = []
    progress = st.progress(0, text="Processing...")
    for idx, uf in enumerate(uploaded_files):
        try:
            out_bytes = process_pdf_bytes(uf.read(), address_lines, phone)
            results.append((uf.name, out_bytes, "OK"))
        except Exception as e:
            results.append((uf.name, None, f"Error: {e}"))
        progress.progress((idx + 1) / len(uploaded_files), text=f"Processing... ({idx+1}/{len(uploaded_files)})")

    st.subheader("3. Nateeja")
    success_count = sum(1 for _, _, status in results if status == "OK")
    st.success(f"{success_count} / {len(results)} files successfully process hui.")

    for fname, out_bytes, status in results:
        if status != "OK":
            st.error(f"{fname}: {status}")

    # ZIP banayein sab processed files ka
    if success_count > 0:
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as zf:
            for fname, out_bytes, status in results:
                if status == "OK":
                    zf.writestr(fname, out_bytes)
        zip_buffer.seek(0)
        st.download_button(
            "⬇️ Sab Processed Files Download Karein (ZIP)",
            data=zip_buffer,
            file_name="processed_labels.zip",
            mime="application/zip",
        )

        with st.expander("Individual files download karein"):
            for fname, out_bytes, status in results:
                if status == "OK":
                    st.download_button(f"⬇️ {fname}", data=out_bytes, file_name=fname, mime="application/pdf", key=fname)

st.markdown("---")
st.caption("Note: Image-based (scanned) labels ke liye Tesseract OCR system par installed hona zaroori hai.")
