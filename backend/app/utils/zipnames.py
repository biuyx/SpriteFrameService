"""zip 条目的真实文件名。

zip 里只有打了 UTF-8 标记（flag bit 11）的条目才明确是 UTF-8，没打标记的 Python 一律按
cp437 解。可实际上：
  · Windows 自带「发送到 → 压缩(zipped)文件夹」和不少国产压缩软件，在中文系统上按 GBK 存
  · macOS 的「压缩」按 UTF-8 存，却不打标记
按 cp437 解出来都是「╓╨╬─」这类乱码。模板库靠文件名解析动作和变体、批量导入拿文件名当
动作名，名字一乱就全错了。
"""
from __future__ import annotations

import zipfile

_UTF8_FLAG = 0x800


def member_name(info: zipfile.ZipInfo) -> str:
    """没有 UTF-8 标记时依次试 UTF-8、GBK，都解不开才保留原样。

    先试 UTF-8：GBK 的中文字节几乎凑不成合法 UTF-8，反过来却常能凑成 GBK，顺序不能换。
    """
    if info.flag_bits & _UTF8_FLAG:
        return info.filename
    try:
        # cp437 覆盖全部 256 个字节值，编回去就是原始字节。用 orig_filename：
        # Windows 上 filename 里的反斜杠已被换成 /，而 GBK 字的第二个字节可能正是 0x5C
        raw = info.orig_filename.encode("cp437")
    except UnicodeEncodeError:
        return info.filename
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return info.filename
