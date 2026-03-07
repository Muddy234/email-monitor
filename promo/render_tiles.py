from playwright.sync_api import sync_playwright
from pathlib import Path

folder = Path(__file__).parent

tiles = [
    ("small_promo_440x280.html", "small_promo_440x280.png", 440, 280),
    ("marquee_promo_1400x560.html", "marquee_promo_1400x560.png", 1400, 560),
]

with sync_playwright() as p:
    browser = p.chromium.launch()
    for html_file, png_file, w, h in tiles:
        page = browser.new_page(viewport={"width": w, "height": h})
        page.goto((folder / html_file).as_uri())
        page.wait_for_timeout(1000)
        page.screenshot(path=str(folder / png_file), clip={"x": 0, "y": 0, "width": w, "height": h})
        print(f"Saved {png_file} at {w}x{h}")
        page.close()
    browser.close()
