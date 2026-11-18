"""Draw the integration's icon. Run with the dev venv: python scripts/make_icon.py"""

from pathlib import Path

from PIL import Image, ImageDraw

S = 2048  # drawn large, then scaled down for smooth edges
TILE = (16, 42, 67)
WHITE = (245, 248, 250)
BOLT = (255, 190, 40)
OUT = Path(__file__).parent.parent / "custom_components" / "ups_sync" / "brand"


def draw() -> Image.Image:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, S - 1, S - 1), radius=440, fill=TILE)
    # Battery, upright: terminal on top, body below.
    d.rounded_rectangle((864, 300, 1184, 500), radius=60, fill=WHITE)
    d.rounded_rectangle((584, 420, 1464, 1760), radius=150, fill=WHITE)
    d.rounded_rectangle((694, 530, 1354, 1650), radius=70, fill=TILE)
    # Lightning bolt.
    d.polygon(
        [(1130, 640), (800, 1150), (1010, 1150), (918, 1540), (1250, 1020), (1040, 1020)],
        fill=BOLT,
    )
    return img


def main() -> None:
    img = draw()
    OUT.mkdir(exist_ok=True)
    for name, size in (("icon.png", 256), ("icon@2x.png", 512)):
        img.resize((size, size), Image.LANCZOS).save(OUT / name, optimize=True)


if __name__ == "__main__":
    main()
