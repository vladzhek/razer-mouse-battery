"""Рисует icon.ico (зелёная батарейка) в нескольких размерах."""
from PIL import Image, ImageDraw

S = 256
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
# корпус
d.rounded_rectangle((14, 64, 214, 192), radius=22, outline=(232, 232, 232, 255), width=16)
# «пипка»
d.rounded_rectangle((218, 100, 246, 156), radius=8, fill=(232, 232, 232, 255))
# заливка
d.rounded_rectangle((38, 88, 160, 168), radius=8, fill=(76, 175, 80, 255))
img.save("icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("icon.ico готова")
