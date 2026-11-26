"""한글 글꼴: 맥이면 애플고딕, 리눅스(클라우드)면 Noto Sans CJK"""
import os
from PIL import ImageFont

CANDIDATES = [
    ("/System/Library/Fonts/AppleSDGothicNeo.ttc", 6, 0),
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", 1, None),
]
REG_LINUX = ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 1)


def font(size, bold=True):
    for path, bi, ri in CANDIDATES:
        if os.path.exists(path):
            if bold or ri is None:
                if not bold and os.path.exists(REG_LINUX[0]):
                    return ImageFont.truetype(REG_LINUX[0], size, index=REG_LINUX[1])
                return ImageFont.truetype(path, size, index=bi)
            return ImageFont.truetype(path, size, index=ri)
    raise RuntimeError("한글 글꼴을 찾을 수 없음")
