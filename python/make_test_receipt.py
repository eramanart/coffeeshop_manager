"""
make_test_receipt.py — generate a synthetic Lithuanian invoice image
for testing the OCR pipeline without needing a real supplier receipt.

Usage:
    python make_test_receipt.py
    # writes: data/workspace/test_invoice_LT.png

Requires: pillow (already installed)
"""

from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
import random, sys

def make_invoice(path: Path, scenario: str = "clean") -> None:
    """
    scenario:
      "clean"   — crisp text, confidence should hit Tier 1 (>=90%)
      "faded"   — reduced contrast, should hit Tier 2 (70-89%)
      "damaged" — noise + blur, should hit Tier 3 (<70%)
    """
    W, H = 794, 1123   # A4 at 96 dpi
    bg   = (255, 255, 255)
    ink  = (20,  20,  20)

    img  = Image.new("RGB", (W, H), bg)
    draw = ImageDraw.Draw(img)

    # Try to load a system font; fall back to default if unavailable
    def font(size: int):
        for name in ["arial.ttf", "Arial.ttf", "DejaVuSans.ttf",
                     "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                     "C:/Windows/Fonts/arial.ttf"]:
            try:
                return ImageFont.truetype(name, size)
            except Exception:
                pass
        return ImageFont.load_default()

    f_title  = font(22)
    f_normal = font(16)
    f_small  = font(13)

    lines = [
        ("UAB KAVOS TIEKĖJAS",              f_title,  80,  60),
        ("Įmonės kodas: 123456789",          f_normal, 80, 110),
        ("PVM kodas: LT123456789",           f_normal, 80, 132),
        ("Pylimo g. 14, Vilnius LT-01118",   f_small,  80, 155),
        ("",                                 f_normal, 80, 178),
        ("PVM SĄSKAITA FAKTŪRA",             f_title,  80, 200),
        ("Serija/Nr.: SF-2026-00421",        f_normal, 80, 240),
        ("Data: 2026-05-12",                 f_normal, 80, 265),
        ("",                                 f_normal, 80, 285),
        ("Pirkėjas: Kavos Baras UAB",        f_normal, 80, 310),
        ("Pirkėjo kodas: 987654321",         f_normal, 80, 333),
        ("",                                 f_normal, 80, 355),
        # Table header
        ("Prekė / Paslauga",                 f_normal, 80, 385),
        ("Kiekis",                           f_normal, 460, 385),
        ("Suma",                             f_normal, 580, 385),
        # Table rows
        ("Espresso pupelės (1 kg)",          f_small,  80, 415),
        ("20",                               f_small,  460, 415),
        ("248.76",                           f_small,  580, 415),
        ("Pienas (10 L)",                    f_small,  80, 438),
        ("5",                                f_small,  460, 438),
        ("41.50",                            f_small,  580, 438),
        ("",                                 f_normal, 80, 460),
        # Totals
        ("Suma be PVM:",                     f_normal, 400, 495),
        ("248.76 EUR",                       f_normal, 580, 495),
        ("PVM (21% PVM1):",                  f_normal, 400, 520),
        ("52.24 EUR",                        f_normal, 580, 520),
        ("Iš viso su PVM:",                  f_title,  400, 548),
        ("301.00 EUR",                       f_title,  560, 548),
        ("",                                 f_normal, 80, 580),
        ("Apmokėjimo terminas: 2026-05-26",  f_small,  80, 610),
        ("Banko sąskaita: LT12 3456 7890 1234 5678", f_small, 80, 630),
        ("Bankas: Swedbank AB",              f_small,  80, 650),
    ]

    # Draw horizontal rules
    draw.line([(80, 375), (714, 375)], fill=(180,180,180), width=1)
    draw.line([(80, 400), (714, 400)], fill=(200,200,200), width=1)
    draw.line([(80, 480), (714, 480)], fill=(180,180,180), width=1)
    draw.line([(80, 570), (714, 570)], fill=(100,100,100), width=2)

    for text, fnt, x, y in lines:
        if text:
            draw.text((x, y), text, font=fnt, fill=ink)

    # Apply degradation based on scenario
    if scenario == "faded":
        from PIL import ImageFilter
        import random as r
        # Blend to white — pale but still readable enough for Tier 2
        faded = Image.blend(img, Image.new("RGB", (W, H), (255, 255, 255)), alpha=0.60)
        draw2 = ImageDraw.Draw(faded)
        # Paper grain to break up character edges
        for _ in range(W * H // 6):
            px = r.randint(0, W - 1)
            py = r.randint(0, H - 1)
            v  = r.randint(200, 245)
            draw2.point((px, py), fill=(v, v - 3, v - 6))
        # Soft scanner blur
        img = faded.filter(ImageFilter.GaussianBlur(radius=1.1))

    elif scenario == "damaged":
        import random as r
        from PIL import ImageFilter
        # Heavy salt-and-pepper noise
        pixels = img.load()
        for _ in range(W * H // 4):
            px = r.randint(0, W - 1)
            py = r.randint(0, H - 1)
            v  = r.choice([0, 255])
            pixels[px, py] = (v, v, v)
        # Random dark ink blobs
        draw2 = ImageDraw.Draw(img)
        for _ in range(10):
            cx = r.randint(60, W - 60)
            cy = r.randint(60, H - 60)
            rr = r.randint(25, 65)
            draw2.ellipse([(cx - rr, cy - rr), (cx + rr, cy + rr)],
                          fill=(r.randint(0, 40), r.randint(0, 30), 0))
        # Skew
        img = img.rotate(r.uniform(-6, 6), expand=False, fillcolor=(245, 240, 230))
        # Heavy blur to smear characters
        img = img.filter(ImageFilter.GaussianBlur(radius=2.5))
        # Multiple white occlusion strips (torn / water-damaged areas)
        draw3 = ImageDraw.Draw(img)
        for _ in range(3):
            sy = r.randint(100, H - 100)
            draw3.rectangle([(0, sy), (W, sy + r.randint(20, 45))],
                            fill=(255, 255, 255))

    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(path), "PNG", dpi=(96, 96))
    print(f"Saved: {path}  ({scenario} scenario)")
    print(f"  Expected fields:")
    print(f"    supplier_vat:  LT123456789")
    print(f"    supplier_code: 123456789")
    print(f"    doc_date:      2026-05-12")
    print(f"    doc_num:       SF-2026-00421")
    print(f"    net_amount:    248.76")
    print(f"    pvm_amount:    52.24")
    print(f"    pvm_code:      PVM1")


if __name__ == "__main__":
    scenario = sys.argv[1] if len(sys.argv) > 1 else "clean"
    if scenario not in ("clean", "faded", "damaged"):
        print("Usage: python make_test_receipt.py [clean|faded|damaged]")
        sys.exit(1)

    out = Path(f"data/workspace/test_invoice_{scenario}.png")
    make_invoice(out, scenario)
    print(f"\nNow run:")
    print(f"  python agent/skills/scan_receipt.py {out}")
