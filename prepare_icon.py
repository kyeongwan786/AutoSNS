"""Create a compact Windows app icon from the AutoSNS logo artwork."""
from pathlib import Path

from PIL import Image


source = Image.open(Path(__file__).with_name("autosns_icon.png")).convert("RGBA")
width, height = source.size

# The source image also contains the AutoSNS wordmark. Crop to the mascot and
# green mark so the recognizable part remains legible at Windows icon sizes.
art = source.crop((round(width * 0.07), round(height * 0.09),
                   round(width * 0.93), round(height * 0.78)))
art = art.resize((1024, 1024), Image.Resampling.LANCZOS)
art.save(Path(__file__).with_name("autosns_icon.ico"), format="ICO",
         sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                (64, 64), (128, 128), (256, 256)])
